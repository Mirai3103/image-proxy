"""mitmproxy response orchestration for matching image requests."""

from __future__ import annotations

import asyncio
import codecs
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
import logging
from typing import TypeVar
from urllib.parse import urlsplit

from mitmproxy import http
from mitmproxy.net import encoding

from image_proxy.cache import CacheError, CacheHit, CacheStore, CleanupReport
from image_proxy.config import AppConfig
from image_proxy.matcher import UrlMatcher, build_cache_key, is_eligible_request
from image_proxy.processor import ImageProcessor, ProcessedImage, WatermarkProcessor


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


def _decode_and_process(
    processor: ImageProcessor,
    raw_content: bytes,
    content_encoding: str | None,
    content_type: str | None,
) -> ProcessedImage:
    decoded_content = _decode_content(raw_content, content_encoding)
    if not isinstance(decoded_content, bytes):
        raise ValueError("decoded response content is not bytes")
    return processor.process(decoded_content, content_type)


def _decode_content(raw_content: bytes, content_encoding: str | None) -> str | bytes:
    if not content_encoding:
        return raw_content
    normalized = content_encoding.lower()
    try:
        decoder = encoding.custom_decode.get(normalized)
        if decoder is not None:
            return decoder(raw_content)
        return codecs.decode(raw_content, normalized, "strict")
    except TypeError:
        raise
    except Exception as exc:
        raise ValueError("could not decode response content") from exc


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
            WatermarkProcessor(config.processing) if config else None
        )
        self.cache = cache or (CacheStore(config.cache) if config else None)
        if config is not None and cache is None:
            self.cache.initialize()
        workers = config.processing.workers if config is not None else 1
        self._executor: ThreadPoolExecutor | None = ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="image-proxy",
        )
        self._key_locks_guard = asyncio.Lock()
        self._key_locks: dict[str, tuple[asyncio.Lock, int]] = {}
        self._lifecycle_lock = asyncio.Lock()
        self._maintenance_task: asyncio.Task[None] | None = None
        self._closed = False

    @property
    def maintenance_task(self) -> asyncio.Task[None] | None:
        """Return the active periodic-maintenance task, if any."""
        return self._maintenance_task

    async def start(self) -> None:
        """Run startup cleanup and start one periodic-maintenance task."""
        async with self._lifecycle_lock:
            if self._closed or self._maintenance_task is not None:
                return
            if self.cache is None or self.config is None:
                return

            try:
                report = await self._run_blocking(self.cache.cleanup)
            except Exception as exc:
                self._log_maintenance_fallback(exc)
            else:
                self._log_evicted(report)
            self._maintenance_task = asyncio.create_task(
                self._maintenance_loop(), name="image-proxy-cache-maintenance"
            )

    async def shutdown(self) -> None:
        """Stop maintenance and release owned cache and worker resources."""
        async with self._lifecycle_lock:
            if self._closed:
                return
            caller_cancelled = False

            maintenance_task = self._maintenance_task
            self._maintenance_task = None
            if maintenance_task is not None:
                maintenance_task.cancel()
                try:
                    await asyncio.shield(maintenance_task)
                except asyncio.CancelledError:
                    if not maintenance_task.done():
                        caller_cancelled = True
                        maintenance_task.cancel()
                        try:
                            await maintenance_task
                        except asyncio.CancelledError:
                            pass

            executor = self._executor
            self._executor = None
            if executor is not None:
                close_future = (
                    executor.submit(self.cache.close) if self.cache is not None else None
                )
                executor.shutdown(wait=True)
                if close_future is not None:
                    close_future.result()
            elif self.cache is not None:
                self.cache.close()
            self._closed = True
            if caller_cancelled:
                raise asyncio.CancelledError

    async def request(self, flow: http.HTTPFlow) -> None:
        """Mark matching eligible requests for response-time processing."""
        flow.metadata.pop(_CACHE_KEY_METADATA, None)
        try:
            host, path = self._log_location(flow)
            if not is_eligible_request(flow.request.method, flow.request.headers):
                logger.info("BYPASS host=%s path=%s", host, path)
                return
            if self.matcher is None or self.processor is None or self.cache is None:
                logger.info("BYPASS host=%s path=%s", host, path)
                return
            if not self.matcher.matches(flow.request.host, flow.request.pretty_url):
                logger.info("BYPASS host=%s path=%s", host, path)
                return

            flow.metadata[_CACHE_KEY_METADATA] = build_cache_key(
                flow.request.pretty_url,
                self.processor.fingerprint,
                flow.request.headers,
            )
            key = flow.metadata[_CACHE_KEY_METADATA]
            try:
                cached = await self._run_blocking(self.cache.get, key)
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

    async def response(self, flow: http.HTTPFlow) -> None:
        """Transform and cache an eligible successful upstream response."""
        response: http.Response | None = None
        original_content: bytes | None = None
        original_headers: http.Headers | None = None
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
            if original_content is None or self.processor is None or self.cache is None:
                logger.info("BYPASS host=%s path=%s", host, path)
                return

            async with self._coordinate_key(key):
                cached = await self._run_blocking(self.cache.get, key)
                if cached is not None:
                    self._apply_cached_artifact(response, cached)
                    logger.info("CACHE_HIT host=%s path=%s", host, path)
                    return

                cache_headers = {
                    name: original_headers[name]
                    for name in _CACHE_RESPONSE_HEADERS
                    if name in original_headers
                }
                processed = await self._run_blocking(
                    _decode_and_process,
                    self.processor,
                    original_content,
                    original_headers.get("Content-Encoding"),
                    content_type,
                )
                await self._run_blocking(
                    self.cache.put,
                    key,
                    flow.request.pretty_url,
                    self.processor.fingerprint,
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

        logger.info(
            "PROCESSED host=%s path=%s format=%s bytes=%d",
            host,
            path,
            processed.format_name,
            len(processed.data),
        )

    async def _run_blocking(
        self, function: Callable[..., _Result], /, *args: object
    ) -> _Result:
        executor = self._executor
        if executor is None:
            raise RuntimeError("image proxy worker pool is shut down")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(executor, function, *args)

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

    async def _maintenance_loop(self) -> None:
        if self.config is None or self.cache is None:
            return
        interval = self.config.cache.cleanup_interval_seconds
        while True:
            await asyncio.sleep(interval)
            try:
                report = await self._run_blocking(self.cache.cleanup)
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
        response.raw_content = cached.data

    @staticmethod
    def _apply_processed_artifact(
        response: http.Response, processed: ProcessedImage
    ) -> None:
        for name in _STALE_REPRESENTATION_HEADERS:
            response.headers.pop(name, None)
        response.headers["Content-Type"] = processed.mime_type
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
