import asyncio
import base64
import gzip
import logging
import threading
import time
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
import zlib

import brotli
import pytest
from mitmproxy.exceptions import OptionsError
from mitmproxy import http
from mitmproxy.test import tflow
from PIL import Image
import zstandard as zstd

from image_proxy.addon import ImageProxyAddon
from image_proxy.cache import CacheError, CacheStore, CleanupReport
from image_proxy.config import (
    AppConfig,
    CacheConfig,
    MatchingConfig,
    ProcessingConfig,
    ProxyConfig,
)
from image_proxy.matcher import UrlMatcher
from image_proxy.processor import WatermarkProcessor


def encoded_image(format_name: str) -> bytes:
    output = BytesIO()
    Image.new("RGB", (240, 320), "white").save(output, format_name)
    return output.getvalue()


def raw_deflate(data: bytes) -> bytes:
    compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    return compressor.compress(data) + compressor.flush()


@pytest.fixture
def jpeg_bytes() -> bytes:
    return encoded_image("JPEG")


@pytest.fixture
def png_bytes() -> bytes:
    return encoded_image("PNG")


@pytest.fixture
def webp_bytes() -> bytes:
    return encoded_image("WEBP")


def addon_config(tmp_path: Path) -> AppConfig:
    matching = MatchingConfig(("*.cdn.test",), (r"/manga/",))
    processing = ProcessingConfig(
        "UPSCALED", 90, 90, 10 * 1024**2, 10_000_000, 2
    )
    cache_config = CacheConfig(
        tmp_path / "cache", 3600, 100 * 1024**2, 0.9, 600, 25
    )
    return AppConfig(
        ProxyConfig("127.0.0.1", 8080), matching, processing, cache_config
    )


@pytest.fixture
async def addon(tmp_path: Path):
    config = addon_config(tmp_path)
    cache = CacheStore(config.cache)
    cache.initialize()
    instance = ImageProxyAddon(
        config,
        matcher=UrlMatcher(config.matching),
        processor=WatermarkProcessor(config.processing),
        cache=cache,
    )
    yield instance
    await instance.shutdown()


def matching_flow(image_bytes: bytes, content_type: str = "image/jpeg"):
    flow = tflow.tflow(resp=True)
    flow.request.url = "https://img.cdn.test/manga/page.jpg"
    flow.response = http.Response.make(
        200,
        image_bytes,
        {"Content-Type": content_type, "Access-Control-Allow-Origin": "*"},
    )
    return flow


class RecordingLoader:
    def __init__(self) -> None:
        self.options: list[tuple[str, type, object]] = []

    def add_option(
        self, name: str, typespec: type, default: object, help: str
    ) -> None:
        self.options.append((name, typespec, default))


class RecordingCacheStore(CacheStore):
    instances: list["RecordingCacheStore"] = []

    def __init__(self, config: CacheConfig, *, clock=time.time) -> None:
        super().__init__(config, clock=clock)
        self.cleanup_count = 0
        self.close_count = 0
        RecordingCacheStore.instances.append(self)

    def cleanup(self) -> CleanupReport:
        self.cleanup_count += 1
        return super().cleanup()

    def close(self) -> None:
        self.close_count += 1
        super().close()


class RecordingOptions(SimpleNamespace):
    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.updates: list[dict[str, object]] = []

    def update(self, **kwargs: object) -> None:
        self.updates.append(kwargs)
        for name, value in kwargs.items():
            setattr(self, name, value)


async def wait_until(predicate, *, timeout: float = 2.0) -> None:
    async def wait() -> None:
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(wait(), timeout)


@pytest.mark.asyncio
async def test_non_matching_response_passes_through_byte_for_byte(addon) -> None:
    flow = tflow.tflow(resp=True)
    flow.request.url = "https://other.test/page.jpg"
    original_body = flow.response.raw_content
    original_headers = tuple(flow.response.headers.fields)

    await addon.request(flow)
    await addon.response(flow)

    assert "image_proxy.cache_key" not in flow.metadata
    assert flow.response.raw_content == original_body
    assert tuple(flow.response.headers.fields) == original_headers


@pytest.mark.asyncio
@pytest.mark.parametrize("method, range_value", [("POST", None), ("GET", "bytes=0-10")])
async def test_ineligible_request_never_gets_interception_metadata(
    addon, jpeg_bytes: bytes, method: str, range_value: str | None
) -> None:
    flow = matching_flow(jpeg_bytes)
    flow.request.method = method
    if range_value is not None:
        flow.request.headers["rAnGe"] = range_value
    original_body = flow.response.raw_content
    original_headers = tuple(flow.response.headers.fields)

    await addon.request(flow)
    await addon.response(flow)

    assert "image_proxy.cache_key" not in flow.metadata
    assert flow.response.raw_content == original_body
    assert tuple(flow.response.headers.fields) == original_headers


@pytest.mark.asyncio
async def test_matching_request_records_cache_miss_without_logging_secrets(
    addon, jpeg_bytes: bytes, caplog
) -> None:
    flow = matching_flow(jpeg_bytes)
    flow.request.url = "https://img.cdn.test/manga/page.jpg?token=query-secret"
    flow.request.headers["Authorization"] = "Bearer header-secret"
    flow.request.headers["Cookie"] = "session=cookie-secret"

    with caplog.at_level(logging.INFO):
        await addon.request(flow)

    assert "image_proxy.cache_key" in flow.metadata
    assert "CACHE_MISS" in caplog.text
    assert "img.cdn.test" in caplog.text
    assert "/manga/page.jpg" in caplog.text
    assert "query-secret" not in caplog.text
    assert "header-secret" not in caplog.text
    assert "cookie-secret" not in caplog.text


@pytest.mark.asyncio
async def test_non_utf8_private_header_fails_open_without_leaking_secrets(
    addon, caplog
) -> None:
    flow = tflow.tflow(resp=False)
    flow.request.url = "https://img.cdn.test/manga/page.jpg?token=query-secret"
    flow.request.headers.fields += (
        (b"Authorization", b"Bearer \xffheader-secret"),
        (b"Cookie", b"session=cookie-secret"),
    )

    with caplog.at_level(logging.WARNING):
        await addon.request(flow)

    assert "image_proxy.cache_key" not in flow.metadata
    assert "FALLBACK" in caplog.text
    assert "UnicodeEncodeError" in caplog.text
    assert "img.cdn.test" in caplog.text
    assert "/manga/page.jpg" in caplog.text
    for secret in ("query-secret", "header-secret", "cookie-secret"):
        assert secret not in caplog.text


@pytest.mark.asyncio
async def test_matching_jpeg_is_processed_and_representation_headers_repaired(
    addon, jpeg_bytes: bytes
) -> None:
    flow = matching_flow(jpeg_bytes)
    flow.response.headers.update(
        {
            "content-length": str(len(jpeg_bytes)),
            "CONTENT-ENCODING": "identity",
            "eTaG": '"old"',
            "Content-MD5": "old-md5",
            "digest": "sha-256=old",
            "Cache-Control": "public, max-age=60",
        }
    )

    await addon.request(flow)
    cache_key = flow.metadata["image_proxy.cache_key"]
    await addon.response(flow)

    assert flow.response.raw_content != jpeg_bytes
    assert flow.response.headers["Content-Type"] == "image/jpeg"
    assert flow.response.headers["Content-Length"] == str(len(flow.response.raw_content))
    for name in ("Content-Encoding", "ETag", "Content-MD5", "Digest"):
        assert name not in flow.response.headers
    assert flow.response.headers["Access-Control-Allow-Origin"] == "*"
    assert flow.response.headers["Cache-Control"] == "public, max-age=60"

    cached = addon.cache.get(cache_key)
    assert cached is not None
    assert cached.data == flow.response.raw_content
    assert cached.mime_type == "image/jpeg"
    assert cached.headers == {
        "Cache-Control": "public, max-age=60",
        "Access-Control-Allow-Origin": "*",
    }


@pytest.mark.asyncio
async def test_processed_response_replaces_stale_content_length(
    addon, jpeg_bytes: bytes
) -> None:
    flow = matching_flow(jpeg_bytes)
    flow.response.headers["Content-Length"] = str(len(jpeg_bytes))

    await addon.request(flow)
    await addon.response(flow)

    assert flow.response.headers["Content-Length"] == str(
        len(flow.response.raw_content)
    )


@pytest.mark.asyncio
async def test_response_time_cache_replay_sets_content_length(
    addon, jpeg_bytes: bytes
) -> None:
    first = matching_flow(jpeg_bytes)
    second = matching_flow(jpeg_bytes)
    await addon.request(first)
    await addon.request(second)

    await addon.response(first)
    await addon.response(second)

    assert second.response.raw_content == first.response.raw_content
    assert second.response.headers["Content-Length"] == str(
        len(second.response.raw_content)
    )


@pytest.mark.asyncio
async def test_content_encoded_jpeg_is_processed_from_decoded_body(
    addon, jpeg_bytes: bytes
) -> None:
    flow = matching_flow(jpeg_bytes)
    encoded_body = gzip.compress(jpeg_bytes)
    flow.response.headers["Content-Encoding"] = "gzip"
    flow.response.raw_content = encoded_body

    await addon.request(flow)
    await addon.response(flow)

    assert flow.response.raw_content != encoded_body
    assert "Content-Encoding" not in flow.response.headers
    with Image.open(BytesIO(flow.response.raw_content)) as image:
        assert image.format == "JPEG"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content_encoding", "encode"),
    [
        ("deflate", zlib.compress),
        ("deflateraw", raw_deflate),
        ("zstd", zstd.ZstdCompressor().compress),
    ],
)
async def test_other_mitmproxy_supported_encodings_are_processed(
    addon, jpeg_bytes: bytes, content_encoding: str, encode
) -> None:
    encoded_body = encode(jpeg_bytes)
    flow = matching_flow(encoded_body)
    flow.response.headers["Content-Encoding"] = content_encoding
    flow.response.raw_content = encoded_body

    await addon.request(flow)
    await addon.response(flow)

    assert flow.response.raw_content != encoded_body
    assert "Content-Encoding" not in flow.response.headers
    with Image.open(BytesIO(flow.response.raw_content)) as image:
        assert image.format == "JPEG"


@pytest.mark.asyncio
async def test_brotli_fails_open_without_calling_unbounded_decoder(
    addon, monkeypatch, caplog
) -> None:
    encoded_body = brotli.compress(b"x" * (16 * 1024**2))
    assert len(encoded_body) == 27
    flow = matching_flow(encoded_body)
    flow.response.headers["Content-Encoding"] = "br"
    flow.response.headers["ETag"] = '"upstream"'
    flow.response.raw_content = encoded_body
    original_headers = tuple(flow.response.headers.fields)
    decoder_called = False

    def forbidden_decoder():
        nonlocal decoder_called
        decoder_called = True
        raise AssertionError("Brotli decoding must not be attempted")

    monkeypatch.setattr(brotli, "Decompressor", forbidden_decoder)

    with caplog.at_level(logging.WARNING):
        await addon.request(flow)
        await addon.response(flow)

    assert not decoder_called
    assert flow.response.raw_content == encoded_body
    assert tuple(flow.response.headers.fields) == original_headers
    assert "FALLBACK" in caplog.text


@pytest.mark.asyncio
async def test_content_decoding_and_processing_run_off_event_loop_thread(
    addon, jpeg_bytes: bytes, monkeypatch
) -> None:
    flow = matching_flow(jpeg_bytes)
    flow.response.headers["Content-Encoding"] = "gzip"
    flow.response.raw_content = gzip.compress(jpeg_bytes, mtime=1)
    event_loop_thread = threading.get_ident()
    decode_threads: list[int] = []
    real_read = gzip.GzipFile.read

    def recording_read(file: gzip.GzipFile, size: int = -1) -> bytes:
        decode_threads.append(threading.get_ident())
        return real_read(file, size)

    monkeypatch.setattr(gzip.GzipFile, "read", recording_read)

    await addon.request(flow)
    await addon.response(flow)

    assert decode_threads
    assert all(thread_id != event_loop_thread for thread_id in decode_threads)
    assert "Content-Encoding" not in flow.response.headers
    with Image.open(BytesIO(flow.response.raw_content)) as image:
        assert image.format == "JPEG"


@pytest.mark.asyncio
async def test_gzip_expansion_stops_at_source_limit_and_restores_response(
    addon, monkeypatch, caplog
) -> None:
    limit = addon.config.processing.max_source_bytes
    encoded_body = gzip.compress(b"x" * (limit + 1), compresslevel=9, mtime=1)
    flow = matching_flow(encoded_body)
    flow.response.headers["Content-Encoding"] = "gzip"
    flow.response.headers["ETag"] = '"upstream"'
    flow.response.raw_content = encoded_body
    original_headers = tuple(flow.response.headers.fields)
    read_sizes: list[int] = []
    real_read = gzip.GzipFile.read

    def recording_read(file: gzip.GzipFile, size: int = -1) -> bytes:
        read_sizes.append(size)
        return real_read(file, size)

    monkeypatch.setattr(gzip.GzipFile, "read", recording_read)

    with caplog.at_level(logging.WARNING):
        await addon.request(flow)
        await addon.response(flow)

    assert read_sizes
    assert all(0 <= size <= limit + 1 for size in read_sizes)
    assert flow.response.raw_content == encoded_body
    assert tuple(flow.response.headers.fields) == original_headers
    assert "FALLBACK" in caplog.text


@pytest.mark.asyncio
async def test_unknown_python_codec_content_encoding_restores_response(
    addon, jpeg_bytes: bytes, caplog
) -> None:
    encoded_body = base64.b64encode(jpeg_bytes)
    flow = matching_flow(encoded_body)
    flow.response.headers["Content-Encoding"] = "base64"
    flow.response.headers["ETag"] = '"upstream"'
    flow.response.raw_content = encoded_body
    original_headers = tuple(flow.response.headers.fields)

    with caplog.at_level(logging.WARNING):
        await addon.request(flow)
        await addon.response(flow)

    assert flow.response.raw_content == encoded_body
    assert tuple(flow.response.headers.fields) == original_headers
    assert "FALLBACK" in caplog.text


@pytest.mark.asyncio
async def test_concurrent_gzip_flows_keep_decoded_bodies_with_their_cache_keys(
    addon, monkeypatch
) -> None:
    first_source = BytesIO()
    Image.new("RGB", (240, 320), "white").save(first_source, "JPEG")
    second_source = BytesIO()
    Image.new("RGB", (240, 320), "black").save(second_source, "JPEG")
    first_encoded = gzip.compress(first_source.getvalue(), mtime=1)
    second_encoded = gzip.compress(second_source.getvalue(), mtime=2)
    decoders_met = threading.Barrier(2)
    seen_decoders: set[int] = set()
    seen_guard = threading.Lock()
    real_read = gzip.GzipFile.read

    def coordinated_read(file: gzip.GzipFile, size: int = -1) -> bytes:
        with seen_guard:
            first_read = id(file) not in seen_decoders
            seen_decoders.add(id(file))
        if first_read:
            decoders_met.wait(timeout=2)
        return real_read(file, size)

    monkeypatch.setattr(gzip.GzipFile, "read", coordinated_read)

    first = matching_flow(first_encoded)
    first.request.url = "https://img.cdn.test/manga/first.jpg"
    first.response.headers["Content-Encoding"] = "gzip"
    first.response.raw_content = first_encoded
    second = matching_flow(second_encoded)
    second.request.url = "https://img.cdn.test/manga/second.jpg"
    second.response.headers["Content-Encoding"] = "gzip"
    second.response.raw_content = second_encoded
    await addon.request(first)
    await addon.request(second)
    first_key = first.metadata["image_proxy.cache_key"]
    second_key = second.metadata["image_proxy.cache_key"]

    await asyncio.gather(addon.response(first), addon.response(second))

    first_cached = addon.cache.get(first_key)
    second_cached = addon.cache.get(second_key)
    assert first_cached is not None
    assert second_cached is not None
    with Image.open(BytesIO(first_cached.data)) as image:
        assert min(image.getpixel((0, 0))) > 240
    with Image.open(BytesIO(second_cached.data)) as image:
        assert max(image.getpixel((0, 0))) < 15
    assert first.response.raw_content == first_cached.data
    assert second.response.raw_content == second_cached.data


@pytest.mark.asyncio
async def test_content_decode_failure_restores_raw_body_and_headers(
    addon, caplog
) -> None:
    flow = matching_flow(b"not-gzip-data")
    flow.request.url = "https://img.cdn.test/manga/page.jpg?token=query-secret"
    flow.response.headers["Content-Encoding"] = "gzip"
    flow.response.raw_content = b"not-gzip-data"
    original_body = flow.response.raw_content
    original_headers = tuple(flow.response.headers.fields)

    with caplog.at_level(logging.WARNING):
        await addon.request(flow)
        await addon.response(flow)

    assert flow.response.raw_content == original_body
    assert tuple(flow.response.headers.fields) == original_headers
    assert "FALLBACK" in caplog.text
    assert "ValueError" in caplog.text
    assert "query-secret" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("content_type", [None, "", "application/octet-stream"])
async def test_generic_content_type_accepts_decoded_jpeg(
    addon, jpeg_bytes: bytes, content_type: str | None
) -> None:
    flow = matching_flow(jpeg_bytes)
    if content_type is None:
        del flow.response.headers["Content-Type"]
    else:
        flow.response.headers["Content-Type"] = content_type

    await addon.request(flow)
    await addon.response(flow)

    assert flow.response.raw_content != jpeg_bytes
    assert flow.response.headers["Content-Type"] == "image/jpeg"


@pytest.mark.asyncio
async def test_matching_webp_is_processed_in_original_format(addon, webp_bytes) -> None:
    flow = matching_flow(webp_bytes, "IMAGE/WEBP; charset=binary")

    await addon.request(flow)
    await addon.response(flow)

    assert flow.response.raw_content != webp_bytes
    assert flow.response.headers["Content-Type"] == "image/webp"
    with Image.open(BytesIO(flow.response.raw_content)) as image:
        assert image.format == "WEBP"


@pytest.mark.asyncio
async def test_unsupported_response_passes_through_exactly(addon, png_bytes) -> None:
    flow = matching_flow(png_bytes, "image/png")
    original_body = flow.response.raw_content
    original_headers = tuple(flow.response.headers.fields)

    await addon.request(flow)
    await addon.response(flow)

    assert flow.response.raw_content == original_body
    assert tuple(flow.response.headers.fields) == original_headers


@pytest.mark.asyncio
async def test_non_200_and_missing_responses_pass_through(addon, jpeg_bytes) -> None:
    non_200 = matching_flow(jpeg_bytes)
    non_200.response.status_code = 304
    original_body = non_200.response.raw_content
    original_headers = tuple(non_200.response.headers.fields)
    await addon.request(non_200)
    await addon.response(non_200)
    assert non_200.response.raw_content == original_body
    assert tuple(non_200.response.headers.fields) == original_headers

    missing = tflow.tflow(resp=False)
    missing.request.url = "https://img.cdn.test/manga/page.jpg"
    await addon.request(missing)
    await addon.response(missing)
    assert missing.response is None


@pytest.mark.asyncio
async def test_processor_failure_restores_original_response_and_logs_safely(
    addon, caplog
) -> None:
    flow = tflow.tflow(resp=True)
    flow.request.url = "https://img.cdn.test/manga/broken.jpg?token=query-secret"
    flow.request.headers["Authorization"] = "Bearer header-secret"
    flow.request.headers["Cookie"] = "session=cookie-secret"
    flow.response = http.Response.make(
        200,
        b"response-body-secret",
        {"Content-Type": "image/jpeg", "ETag": "keep", "X-Secret": "response-secret"},
    )
    original_body = flow.response.raw_content
    original_headers = tuple(flow.response.headers.fields)

    with caplog.at_level(logging.WARNING):
        await addon.request(flow)
        await addon.response(flow)

    assert flow.response.raw_content == original_body
    assert tuple(flow.response.headers.fields) == original_headers
    assert "FALLBACK" in caplog.text
    assert "ProcessingError" in caplog.text
    assert "img.cdn.test" in caplog.text
    assert "/manga/broken.jpg" in caplog.text
    for secret in (
        "query-secret",
        "header-secret",
        "cookie-secret",
        "response-body-secret",
        "response-secret",
    ):
        assert secret not in caplog.text


@pytest.mark.asyncio
async def test_cache_failure_restores_original_response(tmp_path, jpeg_bytes, caplog) -> None:
    config = addon_config(tmp_path)
    cache = CacheStore(config.cache)
    cache.initialize()
    instance = ImageProxyAddon(
        config,
        matcher=UrlMatcher(config.matching),
        processor=WatermarkProcessor(config.processing),
        cache=cache,
    )
    cache.close()
    flow = matching_flow(jpeg_bytes)
    original_body = flow.response.raw_content
    original_headers = tuple(flow.response.headers.fields)

    with caplog.at_level(logging.WARNING):
        await instance.request(flow)
        await instance.response(flow)

    assert flow.response.raw_content == original_body
    assert tuple(flow.response.headers.fields) == original_headers
    assert "FALLBACK" in caplog.text
    assert "CacheError" in caplog.text
    await instance.shutdown()


@pytest.fixture
async def lifecycle_addon(tmp_path: Path):
    matching = MatchingConfig(("*.cdn.test",), ())
    processing = ProcessingConfig(
        "UPSCALED", 90, 90, 10 * 1024**2, 10_000_000, 1
    )
    cache_config = CacheConfig(
        tmp_path / "cache", 3600, 100 * 1024**2, 0.9, 1, 25
    )
    config = AppConfig(
        ProxyConfig("127.0.0.1", 8080), matching, processing, cache_config
    )
    cache = CacheStore(cache_config)
    cache.initialize()
    instance = ImageProxyAddon(
        config,
        matcher=UrlMatcher(matching),
        processor=WatermarkProcessor(processing),
        cache=cache,
    )
    yield instance
    await instance.shutdown()


@pytest.mark.asyncio
async def test_cache_hit_returns_response_before_upstream_and_preserves_cors(
    addon, jpeg_bytes: bytes, caplog
) -> None:
    first = matching_flow(jpeg_bytes)
    first.response.headers["Cache-Control"] = "public, max-age=60"
    first.response.headers["X-Upstream-Only"] = "not-cacheable"
    await addon.request(first)
    await addon.response(first)

    second = tflow.tflow(resp=False)
    second.request.url = first.request.url
    with caplog.at_level(logging.INFO):
        await addon.request(second)

    assert second.response is not None
    assert second.response.status_code == 200
    assert second.response.raw_content == first.response.raw_content
    assert second.response.headers["Content-Type"] == "image/jpeg"
    assert second.response.headers["Access-Control-Allow-Origin"] == "*"
    assert second.response.headers["Cache-Control"] == "public, max-age=60"
    assert "X-Upstream-Only" not in second.response.headers
    assert "image_proxy.cache_key" not in second.metadata
    assert "CACHE_HIT" in caplog.text


@pytest.mark.asyncio
async def test_cache_hit_lookup_runs_off_event_loop_thread(
    addon, jpeg_bytes: bytes, monkeypatch
) -> None:
    first = matching_flow(jpeg_bytes)
    await addon.request(first)
    await addon.response(first)
    event_loop_thread = threading.get_ident()
    lookup_threads: list[int] = []
    original_get = addon.cache.get

    def recording_get(key: str):
        lookup_threads.append(threading.get_ident())
        return original_get(key)

    monkeypatch.setattr(addon.cache, "get", recording_get)
    second = tflow.tflow(resp=False)
    second.request.url = first.request.url

    await addon.request(second)

    assert second.response is not None
    assert lookup_threads
    assert all(thread_id != event_loop_thread for thread_id in lookup_threads)


@pytest.mark.asyncio
async def test_cache_read_failure_fails_open_as_upstream_miss(
    tmp_path: Path, jpeg_bytes: bytes, caplog
) -> None:
    config = addon_config(tmp_path)
    cache = CacheStore(config.cache)
    cache.initialize()
    instance = ImageProxyAddon(
        config,
        matcher=UrlMatcher(config.matching),
        processor=WatermarkProcessor(config.processing),
        cache=cache,
    )
    cache.close()
    flow = matching_flow(jpeg_bytes)

    with caplog.at_level(logging.INFO):
        await instance.request(flow)

    assert "image_proxy.cache_key" in flow.metadata
    assert "FALLBACK" in caplog.text
    assert "CacheError" in caplog.text
    assert "CACHE_MISS" in caplog.text
    await instance.shutdown()


@pytest.mark.asyncio
async def test_identical_concurrent_misses_commit_one_processed_artifact(
    addon, jpeg_bytes: bytes, monkeypatch
) -> None:
    calls = 0
    original = addon.processor.process

    def counted_process(data: bytes, content_type: str | None):
        nonlocal calls
        calls += 1
        return original(data, content_type)

    monkeypatch.setattr(addon.processor, "process", counted_process)
    flows = [matching_flow(jpeg_bytes), matching_flow(jpeg_bytes)]
    for flow in flows:
        await addon.request(flow)

    await asyncio.gather(*(addon.response(flow) for flow in flows))

    assert calls == 1
    assert flows[0].response.raw_content == flows[1].response.raw_content
    assert tuple(flows[0].response.headers.fields) == tuple(
        flows[1].response.headers.fields
    )


@pytest.mark.asyncio
async def test_duplicate_miss_loser_replays_only_cached_headers(
    addon, jpeg_bytes: bytes, monkeypatch
) -> None:
    process_started = threading.Event()
    release_process = threading.Event()
    original = addon.processor.process

    def delayed_process(data: bytes, content_type: str | None):
        process_started.set()
        assert release_process.wait(timeout=2)
        return original(data, content_type)

    monkeypatch.setattr(addon.processor, "process", delayed_process)
    winner = matching_flow(jpeg_bytes)
    loser = matching_flow(jpeg_bytes)
    winner.response.headers["Cache-Control"] = "public, max-age=60"
    loser.response.headers["Cache-Control"] = "private, no-store"
    loser.response.headers["Set-Cookie"] = "session=secret"
    loser.response.headers["X-Upstream-Only"] = "loser-specific"

    await addon.request(winner)
    await addon.request(loser)
    winner_task = asyncio.create_task(addon.response(winner))
    assert await asyncio.to_thread(process_started.wait, 2)
    loser_task = asyncio.create_task(addon.response(loser))
    release_process.set()

    await asyncio.gather(winner_task, loser_task)

    assert loser.response.raw_content == winner.response.raw_content
    assert loser.response.headers["Content-Type"] == "image/jpeg"
    assert loser.response.headers["Access-Control-Allow-Origin"] == "*"
    assert loser.response.headers["Cache-Control"] == "public, max-age=60"
    assert "Set-Cookie" not in loser.response.headers
    assert "X-Upstream-Only" not in loser.response.headers
    assert {
        name.decode("ascii")
        for name, _ in loser.response.headers.fields
    } == {
        "Access-Control-Allow-Origin",
        "Cache-Control",
        "Content-Length",
        "Content-Type",
    }


@pytest.mark.asyncio
async def test_different_cache_keys_process_in_parallel(
    addon, jpeg_bytes: bytes, monkeypatch
) -> None:
    rendezvous = threading.Barrier(2)
    original = addon.processor.process

    def synchronized_process(data: bytes, content_type: str | None):
        rendezvous.wait(timeout=2)
        return original(data, content_type)

    monkeypatch.setattr(addon.processor, "process", synchronized_process)
    flows = [matching_flow(jpeg_bytes), matching_flow(jpeg_bytes)]
    flows[0].request.url = "https://img.cdn.test/manga/first.jpg"
    flows[1].request.url = "https://img.cdn.test/manga/second.jpg"
    for flow in flows:
        await addon.request(flow)

    await asyncio.gather(*(addon.response(flow) for flow in flows))

    assert all(flow.response.raw_content != jpeg_bytes for flow in flows)


@pytest.mark.asyncio
async def test_lifecycle_runs_startup_and_periodic_cleanup_without_leaking_task(
    lifecycle_addon, monkeypatch, caplog
) -> None:
    calls = 0
    cleanup_threads: list[int] = []
    event_loop_thread = threading.get_ident()
    original_cleanup = lifecycle_addon.cache.cleanup

    def counted_cleanup():
        nonlocal calls
        calls += 1
        cleanup_threads.append(threading.get_ident())
        return original_cleanup()

    monkeypatch.setattr(lifecycle_addon.cache, "cleanup", counted_cleanup)
    with caplog.at_level(logging.INFO):
        await lifecycle_addon.start()
        await lifecycle_addon.start()
        task = lifecycle_addon.maintenance_task
        await asyncio.sleep(1.1)
        await lifecycle_addon.shutdown()
        await lifecycle_addon.shutdown()

    assert calls >= 2
    assert cleanup_threads
    assert all(thread_id != event_loop_thread for thread_id in cleanup_threads)
    assert task is not None
    assert task.cancelled()
    assert lifecycle_addon.maintenance_task is None
    assert "EVICTED" not in caplog.text
    assert "FALLBACK" not in caplog.text


@pytest.mark.asyncio
async def test_cache_hit_replays_non_utf8_allowlisted_header_bytes(
    addon, jpeg_bytes: bytes
) -> None:
    content_disposition = b'inline; filename="page-\xff.jpg"'
    first = matching_flow(jpeg_bytes)
    first.response.headers.fields += (
        (b"Content-Disposition", content_disposition),
    )
    await addon.request(first)
    await addon.response(first)
    second = tflow.tflow(resp=False)
    second.request.url = first.request.url

    await addon.request(second)

    assert second.response is not None
    assert next(
        value
        for name, value in second.response.headers.fields
        if name.lower() == b"content-disposition"
    ) == content_disposition


@pytest.mark.asyncio
async def test_repeated_waiter_cancellation_does_not_leak_key_lock(addon) -> None:
    holder = addon._coordinate_key("same-key")
    await holder.__aenter__()
    waiter_context = addon._coordinate_key("same-key")
    waiter = asyncio.create_task(waiter_context.__aenter__())
    while addon._key_locks["same-key"][1] != 2:
        await asyncio.sleep(0)

    async with addon._key_locks_guard:
        waiter.cancel()
        await asyncio.sleep(0)
        waiter.cancel()

    with pytest.raises(asyncio.CancelledError):
        await waiter
    await holder.__aexit__(None, None, None)

    assert "same-key" not in addon._key_locks


@pytest.mark.asyncio
async def test_shutdown_preserves_caller_cancellation_after_teardown(
    lifecycle_addon,
) -> None:
    maintenance_cancelled = asyncio.Event()

    async def delayed_cancellation() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            maintenance_cancelled.set()
            await asyncio.Event().wait()

    maintenance_task = asyncio.create_task(delayed_cancellation())
    lifecycle_addon._maintenance_task = maintenance_task
    shutdown_task = asyncio.create_task(lifecycle_addon.shutdown())
    await maintenance_cancelled.wait()

    shutdown_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await shutdown_task
    assert maintenance_task.cancelled()
    assert lifecycle_addon.maintenance_task is None
    with pytest.raises(CacheError, match="not initialized"):
        lifecycle_addon.cache.total_size_bytes()


@pytest.mark.asyncio
async def test_repeated_shutdown_cancellation_waits_for_runtime_teardown(
    lifecycle_addon, monkeypatch
) -> None:
    close_started = threading.Event()
    release_close = threading.Event()
    original_close = lifecycle_addon.cache.close

    def slow_close() -> None:
        close_started.set()
        assert release_close.wait(timeout=2)
        original_close()

    monkeypatch.setattr(lifecycle_addon.cache, "close", slow_close)
    shutdown_task = asyncio.create_task(lifecycle_addon.shutdown())
    assert await asyncio.to_thread(close_started.wait, 2)

    shutdown_task.cancel()
    await asyncio.sleep(0)
    shutdown_task.cancel()
    await asyncio.sleep(0)

    assert not shutdown_task.done()
    release_close.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(shutdown_task, timeout=2)
    with pytest.raises(CacheError, match="not initialized"):
        lifecycle_addon.cache.total_size_bytes()


@pytest.mark.asyncio
async def test_shutdown_retries_cache_close_after_transient_failure(
    lifecycle_addon, monkeypatch
) -> None:
    calls = 0
    original_close = lifecycle_addon.cache.close

    def flaky_close() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise CacheError("simulated close failure")
        original_close()

    monkeypatch.setattr(lifecycle_addon.cache, "close", flaky_close)

    with pytest.raises(CacheError, match="simulated close failure"):
        await lifecycle_addon.shutdown()
    await lifecycle_addon.shutdown()

    assert calls == 2
    assert lifecycle_addon.maintenance_task is None
    with pytest.raises(CacheError, match="not initialized"):
        lifecycle_addon.cache.total_size_bytes()


def test_load_registers_image_proxy_config_option() -> None:
    instance = ImageProxyAddon()
    loader = RecordingLoader()

    instance.load(loader)

    assert loader.options == [("image_proxy_config", str, "")]


def test_configure_rejects_missing_image_proxy_config(monkeypatch) -> None:
    instance = ImageProxyAddon()
    monkeypatch.setattr(
        "image_proxy.addon.ctx",
        SimpleNamespace(options=SimpleNamespace(image_proxy_config="")),
    )

    with pytest.raises(OptionsError, match="image_proxy_config"):
        instance.configure({"image_proxy_config"})


@pytest.mark.asyncio
async def test_configure_loads_config_and_initializes_dependencies(
    tmp_path: Path, monkeypatch
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(Path("config.example.yaml").read_text())
    instance = ImageProxyAddon()
    options = RecordingOptions(image_proxy_config=str(config_path))
    monkeypatch.setattr(
        "image_proxy.addon.ctx",
        SimpleNamespace(options=options),
    )

    instance.configure({"image_proxy_config"})

    assert options.updates == [{"connection_strategy": "lazy"}]
    assert instance.config is not None
    assert instance.config.proxy.port == 8080
    assert instance.matcher is not None
    assert instance.processor is not None
    assert instance.cache is not None
    assert instance.cache.total_size_bytes() == 0
    await instance.shutdown()


@pytest.mark.asyncio
async def test_running_starts_and_done_stops_configured_addon(
    tmp_path: Path, monkeypatch
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(Path("config.example.yaml").read_text())
    instance = ImageProxyAddon()
    monkeypatch.setattr(
        "image_proxy.addon.ctx",
        SimpleNamespace(options=SimpleNamespace(image_proxy_config=str(config_path))),
    )
    instance.configure({"image_proxy_config"})

    await instance.running()
    maintenance_task = instance.maintenance_task
    await instance.done()

    assert maintenance_task is not None
    assert maintenance_task.cancelled()
    assert instance.maintenance_task is None
    assert instance.cache is not None
    with pytest.raises(CacheError, match="not initialized"):
        instance.cache.total_size_bytes()


@pytest.mark.asyncio
async def test_done_propagates_unexpected_shutdown_errors(monkeypatch) -> None:
    instance = ImageProxyAddon()

    async def fail_shutdown() -> None:
        raise RuntimeError("shutdown exploded")

    monkeypatch.setattr(instance, "shutdown", fail_shutdown)

    with pytest.raises(RuntimeError, match="shutdown exploded"):
        await instance.done()


@pytest.mark.asyncio
async def test_live_reconfigure_is_transactional_and_nonblocking_during_setup(
    tmp_path: Path, monkeypatch
) -> None:
    old_config = AppConfig(
        ProxyConfig("127.0.0.1", 8080),
        MatchingConfig(("*.old.test",), ()),
        ProcessingConfig("OLD", 90, 90, 10 * 1024**2, 10_000_000, 1),
        CacheConfig(tmp_path / "old-cache", 3600, 100 * 1024**2, 0.9, 9999, 25),
    )
    new_config = AppConfig(
        ProxyConfig("127.0.0.1", 8080),
        MatchingConfig(("*.new.test",), ()),
        ProcessingConfig("NEW", 90, 90, 10 * 1024**2, 10_000_000, 1),
        CacheConfig(tmp_path / "new-cache", 3600, 100 * 1024**2, 0.9, 1, 25),
    )

    def fake_load_config(path: Path) -> AppConfig:
        if path.name == "old.yaml":
            return old_config
        if path.name == "new.yaml":
            return new_config
        raise AssertionError(f"unexpected config path: {path}")

    initialize_started = threading.Event()
    release_initialize = threading.Event()
    cleanup_started = threading.Event()
    release_cleanup = threading.Event()
    event_loop_thread = threading.get_ident()

    class SlowCandidateCacheStore(RecordingCacheStore):
        def initialize(self) -> None:
            if self._config.directory.name == "new-cache":
                assert threading.get_ident() != event_loop_thread
                initialize_started.set()
                assert release_initialize.wait(timeout=2)
            super().initialize()

        def cleanup(self) -> CleanupReport:
            if (
                self._config.directory.name == "new-cache"
                and self.cleanup_count == 0
            ):
                cleanup_started.set()
                assert release_cleanup.wait(timeout=2)
            return super().cleanup()

    RecordingCacheStore.instances = []
    instance = ImageProxyAddon()
    monkeypatch.setattr("image_proxy.addon.CacheStore", SlowCandidateCacheStore)
    monkeypatch.setattr("image_proxy.addon.load_config", fake_load_config)
    options = SimpleNamespace(image_proxy_config=str(tmp_path / "old.yaml"))
    monkeypatch.setattr("image_proxy.addon.ctx", SimpleNamespace(options=options))

    instance.configure({"image_proxy_config"})
    first_cache = RecordingCacheStore.instances[-1]
    first_executor = instance._executor
    assert instance.maintenance_task is None

    await instance.running()
    first_task = instance.maintenance_task
    assert first_task is not None
    assert first_cache.cleanup_count == 1

    options.image_proxy_config = str(tmp_path / "new.yaml")
    instance.configure({"image_proxy_config"})
    assert await asyncio.to_thread(initialize_started.wait, 2)
    second_cache = RecordingCacheStore.instances[-1]

    try:
        assert second_cache is not first_cache
        assert instance.cache is first_cache
        assert instance._executor is first_executor
        assert first_cache.close_count == 0
        assert instance.maintenance_task is first_task
        assert not first_task.done()

        release_initialize.set()
        assert await asyncio.to_thread(cleanup_started.wait, 2)

        assert instance.cache is first_cache
        assert first_cache.close_count == 0
        assert instance.maintenance_task is first_task

        release_cleanup.set()
        await wait_until(
            lambda: instance.cache is second_cache and first_cache.close_count == 1
        )
        second_task = instance.maintenance_task

        assert instance._executor is not first_executor
        assert getattr(first_executor, "_shutdown", False)
        assert first_task is not second_task
        assert first_task.cancelled()
        assert second_task is not None
        assert not second_task.done()
        assert second_cache.cleanup_count == 1
        assert first_cache.cleanup_count == 1
        assert instance.maintenance_task is second_task
    finally:
        release_initialize.set()
        release_cleanup.set()
        await instance.done()

    assert second_cache.close_count == 1
    assert instance.maintenance_task is None


@pytest.mark.asyncio
async def test_failed_live_reconfigure_preserves_working_runtime_until_done(
    tmp_path: Path, monkeypatch
) -> None:
    old_config = AppConfig(
        ProxyConfig("127.0.0.1", 8080),
        MatchingConfig(("*.old.test",), ()),
        ProcessingConfig("OLD", 90, 90, 10 * 1024**2, 10_000_000, 1),
        CacheConfig(tmp_path / "old-cache", 3600, 100 * 1024**2, 0.9, 9999, 25),
    )
    new_config = AppConfig(
        ProxyConfig("127.0.0.1", 8080),
        MatchingConfig(("*.new.test",), ()),
        ProcessingConfig("NEW", 90, 90, 10 * 1024**2, 10_000_000, 1),
        CacheConfig(tmp_path / "new-cache", 3600, 100 * 1024**2, 0.9, 9999, 25),
    )
    initialize_attempted = threading.Event()

    class FailingCandidateCacheStore(RecordingCacheStore):
        def initialize(self) -> None:
            if self._config.directory.name == "new-cache":
                initialize_attempted.set()
                raise CacheError("candidate initialize failed")
            super().initialize()

    def fake_load_config(path: Path) -> AppConfig:
        return old_config if path.name == "old.yaml" else new_config

    RecordingCacheStore.instances = []
    instance = ImageProxyAddon()
    monkeypatch.setattr("image_proxy.addon.CacheStore", FailingCandidateCacheStore)
    monkeypatch.setattr("image_proxy.addon.load_config", fake_load_config)
    options = SimpleNamespace(image_proxy_config=str(tmp_path / "old.yaml"))
    monkeypatch.setattr("image_proxy.addon.ctx", SimpleNamespace(options=options))

    instance.configure({"image_proxy_config"})
    old_cache = RecordingCacheStore.instances[-1]
    old_executor = instance._executor
    await instance.running()
    old_task = instance.maintenance_task
    options.image_proxy_config = str(tmp_path / "new.yaml")

    instance.configure({"image_proxy_config"})
    assert await asyncio.to_thread(initialize_attempted.wait, 2)
    candidate_cache = RecordingCacheStore.instances[-1]
    await wait_until(lambda: candidate_cache.close_count == 1)

    assert instance.cache is old_cache
    assert instance._executor is old_executor
    assert not getattr(old_executor, "_shutdown", False)
    assert instance.maintenance_task is old_task
    assert old_task is not None and not old_task.done()
    assert old_cache.close_count == 0

    with pytest.raises(CacheError, match="candidate initialize failed"):
        await instance.done()

    assert old_cache.close_count == 1
    assert old_task.cancelled()
    assert instance.maintenance_task is None


@pytest.mark.asyncio
async def test_candidate_cleanup_and_teardown_failures_preserve_old_runtime_and_retry(
    tmp_path: Path, monkeypatch
) -> None:
    old_config = AppConfig(
        ProxyConfig("127.0.0.1", 8080),
        MatchingConfig(("*.old.test",), ()),
        ProcessingConfig("OLD", 90, 90, 10 * 1024**2, 10_000_000, 1),
        CacheConfig(tmp_path / "old-cache", 3600, 100 * 1024**2, 0.9, 9999, 25),
    )
    new_config = AppConfig(
        ProxyConfig("127.0.0.1", 8080),
        MatchingConfig(("*.new.test",), ()),
        ProcessingConfig("NEW", 90, 90, 10 * 1024**2, 10_000_000, 1),
        CacheConfig(tmp_path / "new-cache", 3600, 100 * 1024**2, 0.9, 9999, 25),
    )

    class FailingCandidateCacheStore(RecordingCacheStore):
        def cleanup(self) -> CleanupReport:
            if self._config.directory.name == "new-cache":
                self.cleanup_count += 1
                raise CacheError("candidate cleanup failed")
            return super().cleanup()

        def close(self) -> None:
            if self._config.directory.name == "new-cache":
                self.close_count += 1
                if self.close_count == 1:
                    raise CacheError("candidate close failed")
                CacheStore.close(self)
                return
            super().close()

    def fake_load_config(path: Path) -> AppConfig:
        return old_config if path.name == "old.yaml" else new_config

    RecordingCacheStore.instances = []
    instance = ImageProxyAddon()
    monkeypatch.setattr("image_proxy.addon.CacheStore", FailingCandidateCacheStore)
    monkeypatch.setattr("image_proxy.addon.load_config", fake_load_config)
    options = SimpleNamespace(image_proxy_config=str(tmp_path / "old.yaml"))
    monkeypatch.setattr("image_proxy.addon.ctx", SimpleNamespace(options=options))

    instance.configure({"image_proxy_config"})
    old_runtime = instance._runtime
    assert old_runtime is not None
    await instance.running()
    old_task = instance.maintenance_task
    old_state = (
        instance._runtime,
        instance.config,
        instance.matcher,
        instance.processor,
        instance.cache,
        instance._executor,
        instance.maintenance_task,
    )
    options.image_proxy_config = str(tmp_path / "new.yaml")

    instance.configure({"image_proxy_config"})
    await wait_until(
        lambda: bool(instance._lifecycle_tasks)
        and all(task.done() for task in instance._lifecycle_tasks)
    )
    candidate_cache = RecordingCacheStore.instances[-1]

    assert (
        instance._runtime,
        instance.config,
        instance.matcher,
        instance.processor,
        instance.cache,
        instance._executor,
        instance.maintenance_task,
    ) == old_state
    assert old_task is not None and not old_task.done()
    assert old_runtime.cache.close_count == 0
    assert candidate_cache.cleanup_count == 1
    assert candidate_cache.close_count == 1

    with pytest.raises(CacheError, match="candidate close failed"):
        await instance.done()

    assert candidate_cache.close_count == 2
    assert old_runtime.cache.close_count == 1
    assert old_task.cancelled()
    with pytest.raises(CacheError, match="not initialized"):
        candidate_cache.total_size_bytes()


@pytest.mark.asyncio
async def test_live_reconfigure_retires_slow_old_cache_without_blocking_loop(
    tmp_path: Path, monkeypatch
) -> None:
    old_config = AppConfig(
        ProxyConfig("127.0.0.1", 8080),
        MatchingConfig(("*.old.test",), ()),
        ProcessingConfig("OLD", 90, 90, 10 * 1024**2, 10_000_000, 1),
        CacheConfig(tmp_path / "old-cache", 3600, 100 * 1024**2, 0.9, 9999, 25),
    )
    new_config = AppConfig(
        ProxyConfig("127.0.0.1", 8080),
        MatchingConfig(("*.new.test",), ()),
        ProcessingConfig("NEW", 90, 90, 10 * 1024**2, 10_000_000, 1),
        CacheConfig(tmp_path / "new-cache", 3600, 100 * 1024**2, 0.9, 9999, 25),
    )
    block_old_close = threading.Event()
    close_started = threading.Event()
    release_close = threading.Event()

    class SlowCloseCacheStore(RecordingCacheStore):
        def close(self) -> None:
            if (
                self._config.directory.name == "old-cache"
                and block_old_close.is_set()
            ):
                close_started.set()
                assert release_close.wait(timeout=2)
            super().close()

    def fake_load_config(path: Path) -> AppConfig:
        return old_config if path.name == "old.yaml" else new_config

    RecordingCacheStore.instances = []
    instance = ImageProxyAddon()
    monkeypatch.setattr("image_proxy.addon.CacheStore", SlowCloseCacheStore)
    monkeypatch.setattr("image_proxy.addon.load_config", fake_load_config)
    options = SimpleNamespace(image_proxy_config=str(tmp_path / "old.yaml"))
    monkeypatch.setattr("image_proxy.addon.ctx", SimpleNamespace(options=options))

    instance.configure({"image_proxy_config"})
    old_cache = RecordingCacheStore.instances[-1]
    await instance.running()
    block_old_close.set()
    options.image_proxy_config = str(tmp_path / "new.yaml")
    instance.configure({"image_proxy_config"})
    assert await asyncio.to_thread(close_started.wait, 2)
    new_cache = RecordingCacheStore.instances[-1]

    done_task = asyncio.create_task(instance.done())
    await asyncio.sleep(0)

    assert instance.cache is new_cache
    assert instance.maintenance_task is not None
    assert not done_task.done()
    assert old_cache.close_count == 0

    release_close.set()
    await done_task

    assert old_cache.close_count == 1
    assert new_cache.close_count == 1
    assert instance.maintenance_task is None


@pytest.mark.asyncio
async def test_failed_retirement_is_retried_before_done_propagates_error(
    tmp_path: Path, monkeypatch
) -> None:
    old_config = AppConfig(
        ProxyConfig("127.0.0.1", 8080),
        MatchingConfig(("*.old.test",), ()),
        ProcessingConfig("OLD", 90, 90, 10 * 1024**2, 10_000_000, 1),
        CacheConfig(tmp_path / "old-cache", 3600, 100 * 1024**2, 0.9, 9999, 25),
    )
    new_config = AppConfig(
        ProxyConfig("127.0.0.1", 8080),
        MatchingConfig(("*.new.test",), ()),
        ProcessingConfig("NEW", 90, 90, 10 * 1024**2, 10_000_000, 1),
        CacheConfig(tmp_path / "new-cache", 3600, 100 * 1024**2, 0.9, 9999, 25),
    )

    class FailFirstRetirementCacheStore(RecordingCacheStore):
        def close(self) -> None:
            if self._config.directory.name == "old-cache":
                self.close_count += 1
                if self.close_count == 1:
                    raise CacheError("old retirement failed")
                CacheStore.close(self)
                return
            super().close()

    def fake_load_config(path: Path) -> AppConfig:
        return old_config if path.name == "old.yaml" else new_config

    RecordingCacheStore.instances = []
    instance = ImageProxyAddon()
    monkeypatch.setattr("image_proxy.addon.CacheStore", FailFirstRetirementCacheStore)
    monkeypatch.setattr("image_proxy.addon.load_config", fake_load_config)
    options = SimpleNamespace(image_proxy_config=str(tmp_path / "old.yaml"))
    monkeypatch.setattr("image_proxy.addon.ctx", SimpleNamespace(options=options))

    instance.configure({"image_proxy_config"})
    old_cache = RecordingCacheStore.instances[-1]
    await instance.running()
    options.image_proxy_config = str(tmp_path / "new.yaml")
    instance.configure({"image_proxy_config"})
    await wait_until(
        lambda: bool(instance._lifecycle_tasks)
        and all(task.done() for task in instance._lifecycle_tasks)
    )
    new_cache = RecordingCacheStore.instances[-1]

    assert instance.cache is new_cache
    assert old_cache.close_count == 1
    assert new_cache.close_count == 0

    with pytest.raises(CacheError, match="old retirement failed"):
        await instance.done()

    assert old_cache.close_count == 2
    assert new_cache.close_count == 1
    with pytest.raises(CacheError, match="not initialized"):
        old_cache.total_size_bytes()
    with pytest.raises(CacheError, match="not initialized"):
        new_cache.total_size_bytes()


@pytest.mark.asyncio
async def test_done_waits_for_live_reconfigure_then_closes_each_runtime_once(
    tmp_path: Path, monkeypatch
) -> None:
    old_config = AppConfig(
        ProxyConfig("127.0.0.1", 8080),
        MatchingConfig(("*.old.test",), ()),
        ProcessingConfig("OLD", 90, 90, 10 * 1024**2, 10_000_000, 1),
        CacheConfig(tmp_path / "old-cache", 3600, 100 * 1024**2, 0.9, 9999, 25),
    )
    new_config = AppConfig(
        ProxyConfig("127.0.0.1", 8080),
        MatchingConfig(("*.new.test",), ()),
        ProcessingConfig("NEW", 90, 90, 10 * 1024**2, 10_000_000, 1),
        CacheConfig(tmp_path / "new-cache", 3600, 100 * 1024**2, 0.9, 9999, 25),
    )
    initialize_started = threading.Event()
    release_initialize = threading.Event()

    class SlowInitializeCacheStore(RecordingCacheStore):
        def initialize(self) -> None:
            if self._config.directory.name == "new-cache":
                initialize_started.set()
                assert release_initialize.wait(timeout=2)
            super().initialize()

    def fake_load_config(path: Path) -> AppConfig:
        return old_config if path.name == "old.yaml" else new_config

    RecordingCacheStore.instances = []
    instance = ImageProxyAddon()
    monkeypatch.setattr("image_proxy.addon.CacheStore", SlowInitializeCacheStore)
    monkeypatch.setattr("image_proxy.addon.load_config", fake_load_config)
    options = SimpleNamespace(image_proxy_config=str(tmp_path / "old.yaml"))
    monkeypatch.setattr("image_proxy.addon.ctx", SimpleNamespace(options=options))

    instance.configure({"image_proxy_config"})
    old_cache = RecordingCacheStore.instances[-1]
    await instance.running()
    options.image_proxy_config = str(tmp_path / "new.yaml")
    instance.configure({"image_proxy_config"})
    assert await asyncio.to_thread(initialize_started.wait, 2)
    candidate_cache = RecordingCacheStore.instances[-1]

    done_task = asyncio.create_task(instance.done())
    await asyncio.sleep(0)

    assert not done_task.done()
    assert old_cache.close_count == 0
    assert candidate_cache.close_count == 0

    release_initialize.set()
    await done_task

    assert old_cache.close_count == 1
    assert candidate_cache.close_count == 1
    assert instance.maintenance_task is None
