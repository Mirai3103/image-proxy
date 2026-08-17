"""mitmproxy response orchestration for matching image requests."""

from __future__ import annotations

import asyncio
import gzip
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from io import BytesIO
import logging
from pathlib import Path
import time
from typing import TypeVar
from urllib.parse import urlsplit
import zlib

from mitmproxy import ctx, http
from mitmproxy.exceptions import OptionsError
import zstandard as zstd

from image_proxy.cache import CacheError, CacheHit, CacheStore, CleanupReport
from image_proxy.config import AppConfig, ConfigError, load_config
from image_proxy.matcher import UrlMatcher, build_cache_key, is_eligible_request
from image_proxy.processor import (
    ComfyUIProcessor,
    ImageProcessor,
    KoharuProcessor,
    PipelineProcessor,
    ProcessedImage,
    WatermarkProcessor,
)



_CACHE_KEY_METADATA = "image_proxy.cache_key"
_ALLOWED_CONTENT_TYPES = {
    "",
    "application/octet-stream",
    "image/jpeg",
    "image/jpg",
    "image/webp",
}
_CACHE_RESPONSE_HEADERS = (
    "Cache-Control",
    "Expires",
    "Access-Control-Allow-Origin",
    "Access-Control-Allow-Credentials",
    "Cross-Origin-Resource-Policy",
    "Content-Disposition",
)
_STALE_REPRESENTATION_HEADERS = (
    "Content-Length",
    "Content-Encoding",
    "ETag",
    "Content-MD5",
    "Digest",
)

logger = logging.getLogger(__name__)

_Result = TypeVar("_Result")


@dataclass(eq=False)
class _Runtime:
    config: AppConfig
    matcher: UrlMatcher
    processor: ImageProcessor
    cache: CacheStore
    executor: ThreadPoolExecutor | None
    active_users: int = 0
    idle: asyncio.Event = field(default_factory=asyncio.Event)

    def __post_init__(self) -> None:
        self.idle.set()


def _decode_and_process(
    processor: ImageProcessor,
    raw_content: bytes,
    content_encoding: str | None,
    content_type: str | None,
    max_decoded_bytes: int,
) -> ProcessedImage:
    decoded_content = _decode_content(
        raw_content, content_encoding, max_decoded_bytes
    )
    return processor.process(decoded_content, content_type)


def _decode_content(
    raw_content: bytes, content_encoding: str | None, max_decoded_bytes: int
) -> bytes:
    if not content_encoding:
        return _bounded_identity(raw_content, max_decoded_bytes)

    normalized_encodings = [
        item.strip().lower() for item in content_encoding.split(",")
    ]
    supported_encodings = {
        "deflate",
        "deflateraw",
        "gzip",
        "identity",
        "none",
        "zstd",
    }
    if not all(
        item and item in supported_encodings for item in normalized_encodings
    ):
        raise ValueError("unsupported Content-Encoding")

    decoded_content = raw_content
    try:
        for normalized in reversed(normalized_encodings):
            if normalized in {"identity", "none"}:
                decoded_content = _bounded_identity(
                    decoded_content, max_decoded_bytes
                )
            elif normalized == "gzip":
                decoded_content = _decode_gzip_bounded(
                    decoded_content, max_decoded_bytes
                )
            elif normalized in {"deflate", "deflateraw"}:
                decoded_content = _decode_deflate_bounded(
                    decoded_content, max_decoded_bytes
                )
            else:
                decoded_content = _decode_zstd_bounded(
                    decoded_content, max_decoded_bytes
                )
        return decoded_content
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("could not decode response content") from exc


def _bounded_identity(content: bytes, max_decoded_bytes: int) -> bytes:
    if len(content) > max_decoded_bytes:
        raise ValueError("decoded response content exceeds source byte limit")
    return content


def _decode_gzip_bounded(content: bytes, max_decoded_bytes: int) -> bytes:
    if not content:
        return b""
    with gzip.GzipFile(fileobj=BytesIO(content)) as compressed:
        decoded = compressed.read(max_decoded_bytes + 1)
    return _bounded_identity(decoded, max_decoded_bytes)


def _decode_deflate_bounded(content: bytes, max_decoded_bytes: int) -> bytes:
    if not content:
        return b""
    try:
        return _decode_zlib_bounded(content, zlib.MAX_WBITS, max_decoded_bytes)
    except zlib.error:
        return _decode_zlib_bounded(content, -zlib.MAX_WBITS, max_decoded_bytes)


def _decode_zlib_bounded(
    content: bytes, window_bits: int, max_decoded_bytes: int
) -> bytes:
    decompressor = zlib.decompressobj(window_bits)
    decoded = decompressor.decompress(content, max_decoded_bytes + 1)
    if len(decoded) > max_decoded_bytes:
        raise ValueError("decoded response content exceeds source byte limit")
    if not decompressor.eof:
        raise zlib.error("incomplete compressed stream")
    return decoded


def _decode_zstd_bounded(content: bytes, max_decoded_bytes: int) -> bytes:
    if not content:
        return b""
    with zstd.ZstdDecompressor().stream_reader(
        BytesIO(content), read_across_frames=True
    ) as compressed:
        decoded = compressed.read(max_decoded_bytes + 1)
    return _bounded_identity(decoded, max_decoded_bytes)


def _create_processor(config: ProcessingConfig) -> ImageProcessor:
    pipeline = config.pipeline
    if pipeline == ("watermark",):
        return WatermarkProcessor(config)

    stages: list[object] = []
    for stage_name in pipeline:
        if stage_name == "translate":
            stages.append(KoharuProcessor(config))
        elif stage_name == "upscale":
            stages.append(ComfyUIProcessor(config))
        # "watermark" combined with ML stages is rejected by config validation.
    if len(stages) == 1:
        return stages[0]  # type: ignore[return-value]
    return PipelineProcessor(config, stages)  # type: ignore[arg-type]


class ImageProxyAddon:
    """Select, transform, and persist eligible upstream image responses."""

    def __init__(
        self,
        config: AppConfig | None = None,
        *,
        matcher: UrlMatcher | None = None,
        processor: ImageProcessor | None = None,
        cache: CacheStore | None = None,
    ) -> None:
        self.config = config
        self.matcher = matcher or (UrlMatcher(config.matching) if config else None)
        self.processor = processor or (
            _create_processor(config.processing) if config else None
        )
        self.cache = cache or (CacheStore(config.cache) if config else None)
        if config is not None and cache is None:
            self.cache.initialize()
        self._executor: ThreadPoolExecutor | None = None
        self._runtime: _Runtime | None = None
        if (
            config is not None
            and self.matcher is not None
            and self.processor is not None
            and self.cache is not None
        ):
            self._executor = ThreadPoolExecutor(
                max_workers=config.processing.workers,
                thread_name_prefix="image-proxy",
            )
            self._runtime = _Runtime(
                config,
                self.matcher,
                self.processor,
                self.cache,
                self._executor,
            )
        self._key_locks_guard = asyncio.Lock()
        self._key_locks: dict[str, tuple[asyncio.Lock, int]] = {}
        self._lifecycle_lock = asyncio.Lock()
        self._lifecycle_tasks: set[asyncio.Task[None]] = set()
        self._runtimes_pending_teardown: list[_Runtime] = []
        self._maintenance_task: asyncio.Task[None] | None = None
        self._closed = False
        self._running = False
        self._shutting_down = False

    @property
    def maintenance_task(self) -> asyncio.Task[None] | None:
        """Return the active periodic-maintenance task, if any."""
        return self._maintenance_task

    def load(self, loader) -> None:
        """Register the configuration-file path option with mitmproxy."""
        loader.add_option(
            "image_proxy_config",
            str,
            "",
            "Path to the image proxy YAML configuration file.",
        )

    def configure(self, updated: set[str]) -> None:
        """Load runtime dependencies when mitmproxy receives configuration."""
        if "image_proxy_config" not in updated:
            return

        config_value = getattr(ctx.options, "image_proxy_config", "")
        if not config_value:
            raise OptionsError("image_proxy_config is required")

        try:
            config = load_config(Path(config_value))
        except ConfigError as exc:
            raise OptionsError(str(exc)) from exc

        self._prefer_lazy_upstream_connection()
        self._configure_runtime(config)

    async def running(self) -> None:
        """Start background maintenance after mitmproxy starts running."""
        self._shutting_down = False
        self._running = True
        await self.start()

    async def done(self) -> None:
        """Release resources when mitmproxy shuts down."""
        try:
            await self.shutdown()
        finally:
            self._running = False

    async def start(self) -> None:
        """Run startup cleanup and start one periodic-maintenance task."""
        async with self._lifecycle_lock:
            if self._closed or self._maintenance_task is not None:
                return
            runtime = self._runtime
            if runtime is None or runtime.executor is None:
                return

            try:
                report = await self._run_runtime_blocking(
                    runtime, runtime.cache.cleanup
                )
            except Exception as exc:
                self._log_maintenance_fallback(exc)
            else:
                self._log_evicted(report)
            self._maintenance_task = asyncio.create_task(
                self._maintenance_loop(runtime),
                name="image-proxy-cache-maintenance",
            )

    async def shutdown(self) -> None:
        """Stop maintenance and release owned cache and worker resources."""
        self._shutting_down = True
        lifecycle_error: BaseException | None = None
        shutdown_error: BaseException | None = None
        caller_cancelled = False

        try:
            await self._wait_for_lifecycle_work()
        except asyncio.CancelledError:
            caller_cancelled = True
        except BaseException as exc:
            lifecycle_error = exc

        teardown_task = asyncio.create_task(
            self._shutdown_current_runtime(),
            name="image-proxy-shutdown-teardown",
        )
        while not teardown_task.done():
            try:
                await asyncio.shield(teardown_task)
            except asyncio.CancelledError:
                caller_cancelled = True
        try:
            teardown_task.result()
        except asyncio.CancelledError:
            caller_cancelled = True
        except BaseException as exc:
            shutdown_error = exc

        if shutdown_error is not None:
            raise shutdown_error
        if lifecycle_error is not None:
            raise lifecycle_error
        if caller_cancelled:
            raise asyncio.CancelledError

    async def _shutdown_current_runtime(self) -> None:
        async with self._lifecycle_lock:
            if self._closed and not self._runtimes_pending_teardown:
                return
            caller_cancelled = await self._cancel_maintenance_task()
            first_error: BaseException | None = None
            current_runtime = self._runtime
            current_attempted = False
            for pending_runtime in tuple(self._runtimes_pending_teardown):
                if pending_runtime is current_runtime:
                    current_attempted = True
                try:
                    await self._retire_runtime(pending_runtime)
                except BaseException as exc:
                    if first_error is None:
                        first_error = exc
                else:
                    if pending_runtime is current_runtime:
                        self._closed = True

            runtime = current_runtime
            if not self._closed and not current_attempted and runtime is not None:
                try:
                    await self._retire_runtime(runtime)
                except BaseException as exc:
                    if first_error is None:
                        first_error = exc
                else:
                    self._closed = True
            elif runtime is None:
                self._closed = True

            if first_error is not None:
                raise first_error
            if caller_cancelled:
                raise asyncio.CancelledError

    def _configure_runtime(self, config: AppConfig) -> None:
        if self._running:
            if self._shutting_down:
                raise OptionsError("image proxy is shutting down")
            task = asyncio.create_task(
                self._reconfigure_runtime(config),
                name="image-proxy-live-reconfigure",
            )
            self._lifecycle_tasks.add(task)
            return

        candidate = self._create_runtime(config)
        try:
            candidate.cache.initialize()
        except BaseException:
            self._close_runtime_sync(candidate)
            raise

        old_runtime = self._runtime
        self._install_runtime(candidate)
        self._shutting_down = False
        if old_runtime is not None:
            self._close_runtime_sync(old_runtime)

    async def _reconfigure_runtime(self, config: AppConfig) -> None:
        async with self._lifecycle_lock:
            candidate = self._create_runtime(config)
            installed = False
            try:
                await self._run_runtime_blocking(
                    candidate, candidate.cache.initialize
                )
                report = await self._run_runtime_blocking(
                    candidate, candidate.cache.cleanup
                )
                self._log_evicted(report)

                old_runtime = self._runtime
                caller_cancelled = await self._cancel_maintenance_task()
                self._install_runtime(candidate)
                installed = True
                self._maintenance_task = asyncio.create_task(
                    self._maintenance_loop(candidate),
                    name="image-proxy-cache-maintenance",
                )
                if old_runtime is not None:
                    await self._retire_runtime(old_runtime)
                if caller_cancelled:
                    raise asyncio.CancelledError
            except BaseException:
                if not installed:
                    await self._retire_runtime(candidate)
                raise

    async def _wait_for_lifecycle_work(self) -> None:
        first_error: BaseException | None = None
        caller_cancelled = False
        while self._lifecycle_tasks:
            tasks = tuple(self._lifecycle_tasks)
            self._lifecycle_tasks.difference_update(tasks)
            waiter = asyncio.gather(*tasks, return_exceptions=True)
            while not waiter.done():
                try:
                    await asyncio.shield(waiter)
                except asyncio.CancelledError:
                    caller_cancelled = True
            results = waiter.result()
            for result in results:
                if isinstance(result, BaseException) and first_error is None:
                    first_error = result

        if first_error is not None:
            raise first_error
        if caller_cancelled:
            raise asyncio.CancelledError

    async def _cancel_maintenance_task(self) -> bool:
        maintenance_task = self._maintenance_task
        self._maintenance_task = None
        if maintenance_task is None:
            return False

        caller_cancelled = False
        if not maintenance_task.done():
            maintenance_task.cancel()
            await asyncio.sleep(0)
            if not maintenance_task.done():
                maintenance_task.cancel()
        while not maintenance_task.done():
            try:
                await asyncio.shield(maintenance_task)
            except asyncio.CancelledError:
                if maintenance_task.done():
                    break
                caller_cancelled = True
                maintenance_task.cancel()
        try:
            maintenance_task.result()
        except asyncio.CancelledError:
            pass
        return caller_cancelled

    def _create_runtime(self, config: AppConfig) -> _Runtime:
        matcher = UrlMatcher(config.matching)
        processor = _create_processor(config.processing)
        cache = CacheStore(config.cache)
        executor = ThreadPoolExecutor(
            max_workers=config.processing.workers,
            thread_name_prefix="image-proxy",
        )
        return _Runtime(config, matcher, processor, cache, executor)

    def _install_runtime(self, runtime: _Runtime) -> None:
        self._runtime = runtime
        self.config = runtime.config
        self.matcher = runtime.matcher
        self.processor = runtime.processor
        self.cache = runtime.cache
        self._executor = runtime.executor
        self._closed = False

    async def _retire_runtime(self, runtime: _Runtime) -> None:
        if runtime not in self._runtimes_pending_teardown:
            self._runtimes_pending_teardown.append(runtime)
        await runtime.idle.wait()
        executor = runtime.executor
        if executor is not None:
            try:
                await asyncio.to_thread(
                    self._shutdown_executor_and_close_cache,
                    executor,
                    runtime.cache,
                )
            finally:
                runtime.executor = None
                if self._runtime is runtime:
                    self._executor = None
        else:
            await asyncio.to_thread(runtime.cache.close)
        self._runtimes_pending_teardown.remove(runtime)

    def _close_runtime_sync(self, runtime: _Runtime) -> None:
        executor = runtime.executor
        if executor is not None:
            try:
                self._shutdown_executor_and_close_cache(executor, runtime.cache)
            finally:
                runtime.executor = None
                if self._runtime is runtime:
                    self._executor = None
        else:
            runtime.cache.close()

    @staticmethod
    def _shutdown_executor_and_close_cache(
        executor: ThreadPoolExecutor, cache: CacheStore
    ) -> None:
        close_future = executor.submit(cache.close)
        executor.shutdown(wait=True)
        close_future.result()

    @staticmethod
    def _prefer_lazy_upstream_connection() -> None:
        update_options = getattr(ctx.options, "update", None)
        if update_options is not None:
            update_options(connection_strategy="lazy")

    async def request(self, flow: http.HTTPFlow) -> None:
        """Mark matching eligible requests for response-time processing."""
        flow.metadata.pop(_CACHE_KEY_METADATA, None)
        runtime = self._acquire_runtime()
        try:
            host, path = self._log_location(flow)
            if not is_eligible_request(flow.request.method, flow.request.headers):
                logger.info("BYPASS host=%s path=%s", host, path)
                return
            if runtime is None:
                logger.info("BYPASS host=%s path=%s", host, path)
                return
            if not runtime.matcher.matches(
                flow.request.host, flow.request.pretty_url
            ):
                logger.info("BYPASS host=%s path=%s", host, path)
                return

            flow.metadata[_CACHE_KEY_METADATA] = build_cache_key(
                flow.request.pretty_url,
                runtime.processor.fingerprint,
                flow.request.headers,
            )
            key = flow.metadata[_CACHE_KEY_METADATA]
            try:
                cached = await self._run_runtime_blocking(
                    runtime, runtime.cache.get, key
                )
            except CacheError as exc:
                self._log_fallback(flow, exc)
            else:
                if cached is not None:
                    flow.metadata.pop(_CACHE_KEY_METADATA, None)
                    flow.response = self._cached_response(cached)
                    logger.info("CACHE_HIT host=%s path=%s", host, path)
                    return
            logger.info("CACHE_MISS host=%s path=%s", host, path)
        except Exception as exc:
            flow.metadata.pop(_CACHE_KEY_METADATA, None)
            self._log_fallback(flow, exc)
        finally:
            self._release_runtime(runtime)

    async def response(self, flow: http.HTTPFlow) -> None:
        """Transform and cache an eligible successful upstream response."""
        response: http.Response | None = None
        original_content: bytes | None = None
        original_headers: http.Headers | None = None
        runtime = self._acquire_runtime()
        try:
            key = flow.metadata.get(_CACHE_KEY_METADATA)
            response = flow.response
            if not isinstance(key, str) or not key or response is None:
                return

            original_content = response.raw_content
            original_headers = response.headers.copy()
            host, path = self._log_location(flow)
            if response.status_code != 200:
                logger.info("BYPASS host=%s path=%s", host, path)
                return

            content_type = original_headers.get("Content-Type")
            if not self._allowed_content_type(content_type):
                logger.info("BYPASS host=%s path=%s", host, path)
                return
            if original_content is None or runtime is None:
                logger.info("BYPASS host=%s path=%s", host, path)
                return

            async with self._coordinate_key(key):
                cached = await self._run_runtime_blocking(
                    runtime, runtime.cache.get, key
                )
                if cached is not None:
                    self._apply_cached_artifact(response, cached)
                    logger.info("CACHE_HIT host=%s path=%s", host, path)
                    return

                cache_headers = {
                    name: original_headers[name]
                    for name in _CACHE_RESPONSE_HEADERS
                    if name in original_headers
                }
                logger.info(
                    "PROCESS.start host=%s path=%s bytes=%d encoding=%s content_type=%s",
                    host,
                    path,
                    len(original_content),
                    original_headers.get("Content-Encoding"),
                    content_type,
                )
                process_start = time.monotonic()
                processed = await self._run_runtime_blocking(
                    runtime,
                    _decode_and_process,
                    runtime.processor,
                    original_content,
                    original_headers.get("Content-Encoding"),
                    content_type,
                    runtime.config.processing.max_source_bytes,
                )
                logger.info(
                    "PROCESS.done host=%s path=%s bytes_out=%d elapsed=%.2fs",
                    host,
                    path,
                    len(processed.data),
                    time.monotonic() - process_start,
                )
                await self._run_runtime_blocking(
                    runtime,
                    runtime.cache.put,
                    key,
                    flow.request.pretty_url,
                    runtime.processor.fingerprint,
                    processed.mime_type,
                    cache_headers,
                    processed.data,
                )

                self._apply_processed_artifact(response, processed)
        except Exception as exc:
            if response is not None and original_headers is not None:
                response.raw_content = original_content
                response.headers = original_headers
            self._log_fallback(flow, exc)
            return
        finally:
            self._release_runtime(runtime)

        logger.info(
            "PROCESSED host=%s path=%s format=%s bytes=%d",
            host,
            path,
            processed.format_name,
            len(processed.data),
        )

    async def _run_runtime_blocking(
        self,
        runtime: _Runtime,
        function: Callable[..., _Result],
        /,
        *args: object,
    ) -> _Result:
        executor = runtime.executor
        if executor is None:
            raise RuntimeError("image proxy worker pool is shut down")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(executor, function, *args)

    def _acquire_runtime(self) -> _Runtime | None:
        runtime = self._runtime
        if runtime is None or runtime.executor is None or self._closed:
            return None
        runtime.active_users += 1
        if runtime.active_users == 1:
            runtime.idle.clear()
        return runtime

    @staticmethod
    def _release_runtime(runtime: _Runtime | None) -> None:
        if runtime is None:
            return
        runtime.active_users -= 1
        if runtime.active_users == 0:
            runtime.idle.set()

    @asynccontextmanager
    async def _coordinate_key(self, key: str) -> AsyncIterator[None]:
        async with self._key_locks_guard:
            entry = self._key_locks.get(key)
            if entry is None:
                lock = asyncio.Lock()
                users = 0
            else:
                lock, users = entry
            self._key_locks[key] = (lock, users + 1)

        acquired = False
        try:
            await lock.acquire()
            acquired = True
            yield
        finally:
            if acquired:
                lock.release()
            release_task = asyncio.create_task(self._release_key_user(key))
            cancelled_during_release = False
            while not release_task.done():
                try:
                    await asyncio.shield(release_task)
                except asyncio.CancelledError:
                    cancelled_during_release = True
            release_task.result()
            if cancelled_during_release:
                raise asyncio.CancelledError

    async def _release_key_user(self, key: str) -> None:
        async with self._key_locks_guard:
            current_lock, users = self._key_locks[key]
            if users == 1:
                del self._key_locks[key]
            else:
                self._key_locks[key] = (current_lock, users - 1)

    async def _maintenance_loop(self, runtime: _Runtime) -> None:
        interval = runtime.config.cache.cleanup_interval_seconds
        while True:
            await asyncio.sleep(interval)
            try:
                report = await self._run_runtime_blocking(
                    runtime, runtime.cache.cleanup
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log_maintenance_fallback(exc)
            else:
                self._log_evicted(report)

    @classmethod
    def _cached_response(cls, cached: CacheHit) -> http.Response:
        return http.Response.make(200, cached.data, cls._cached_headers(cached))

    @classmethod
    def _apply_cached_artifact(
        cls, response: http.Response, cached: CacheHit
    ) -> None:
        response.headers = http.Headers()
        response.headers.update(cls._cached_headers(cached))
        response.headers["Content-Length"] = str(len(cached.data))
        response.raw_content = cached.data

    @staticmethod
    def _apply_processed_artifact(
        response: http.Response, processed: ProcessedImage
    ) -> None:
        for name in _STALE_REPRESENTATION_HEADERS:
            response.headers.pop(name, None)
        response.headers["Content-Type"] = processed.mime_type
        response.headers["Content-Length"] = str(len(processed.data))
        response.raw_content = processed.data

    @staticmethod
    def _cached_headers(cached: CacheHit) -> dict[str, str]:
        stored_headers = http.Headers(
            (
                name.encode("utf-8", "surrogateescape"),
                value.encode("utf-8", "surrogateescape"),
            )
            for name, value in cached.headers.items()
        )
        headers = {"Content-Type": cached.mime_type}
        headers.update(
            {
                name: stored_headers[name]
                for name in _CACHE_RESPONSE_HEADERS
                if name in stored_headers
            }
        )
        return headers

    @staticmethod
    def _log_evicted(report: CleanupReport) -> None:
        if not (report.expired_count or report.lru_count or report.orphan_count):
            return
        logger.info(
            "EVICTED expired=%d lru=%d orphan=%d bytes=%d",
            report.expired_count,
            report.lru_count,
            report.orphan_count,
            report.bytes_freed,
        )

    @staticmethod
    def _log_maintenance_fallback(exc: Exception) -> None:
        logger.warning("FALLBACK operation=cleanup error=%s", type(exc).__name__)

    @staticmethod
    def _allowed_content_type(content_type: str | None) -> bool:
        normalized = (
            ""
            if content_type is None
            else content_type.split(";", 1)[0].strip().lower()
        )
        return normalized in _ALLOWED_CONTENT_TYPES

    @staticmethod
    def _log_location(flow: http.HTTPFlow) -> tuple[str, str]:
        try:
            parsed = urlsplit(flow.request.pretty_url)
        except Exception:
            return "", ""
        return (
            ImageProxyAddon._safe_log_value(parsed.hostname or ""),
            ImageProxyAddon._safe_log_value(parsed.path),
        )

    @staticmethod
    def _safe_log_value(value: str) -> str:
        return value.encode("utf-8", "backslashreplace").decode("utf-8")

    @classmethod
    def _log_fallback(cls, flow: http.HTTPFlow, exc: Exception) -> None:
        host, path = cls._log_location(flow)
        logger.warning(
            "FALLBACK host=%s path=%s error=%s",
            host,
            path,
            type(exc).__name__,
        )
