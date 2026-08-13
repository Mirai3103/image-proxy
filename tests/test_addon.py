import logging
import gzip
from io import BytesIO
from pathlib import Path

import pytest
from mitmproxy import http
from mitmproxy.test import tflow
from PIL import Image

from image_proxy.addon import ImageProxyAddon
from image_proxy.cache import CacheStore
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
def addon(tmp_path: Path):
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
    cache.close()


def matching_flow(image_bytes: bytes, content_type: str = "image/jpeg"):
    flow = tflow.tflow(resp=True)
    flow.request.url = "https://img.cdn.test/manga/page.jpg"
    flow.response = http.Response.make(
        200,
        image_bytes,
        {"Content-Type": content_type, "Access-Control-Allow-Origin": "*"},
    )
    return flow


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
    for name in ("Content-Length", "Content-Encoding", "ETag", "Content-MD5", "Digest"):
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
