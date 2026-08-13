"""mitmproxy response orchestration for matching image requests."""

from __future__ import annotations

import asyncio
import codecs
import logging
from urllib.parse import urlsplit

from mitmproxy import http
from mitmproxy.net import encoding

from image_proxy.cache import CacheStore
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

            cache_headers = {
                name: original_headers[name]
                for name in _CACHE_RESPONSE_HEADERS
                if name in original_headers
            }
            processed = await asyncio.to_thread(
                _decode_and_process,
                self.processor,
                original_content,
                original_headers.get("Content-Encoding"),
                content_type,
            )
            await asyncio.to_thread(
                self.cache.put,
                key,
                flow.request.pretty_url,
                self.processor.fingerprint,
                processed.mime_type,
                cache_headers,
                processed.data,
            )

            for name in _STALE_REPRESENTATION_HEADERS:
                response.headers.pop(name, None)
            response.headers["Content-Type"] = processed.mime_type
            response.raw_content = processed.data
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
