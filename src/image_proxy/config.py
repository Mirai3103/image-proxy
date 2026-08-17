"""Strict, immutable runtime configuration for the image proxy."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Mapping

import yaml


class ConfigError(ValueError):
    """Raised when a user-supplied configuration file is invalid."""


@dataclass(frozen=True)
class ProxyConfig:
    host: str
    port: int


@dataclass(frozen=True)
class MatchingConfig:
    domains: tuple[str, ...]
    url_regex: tuple[str, ...]


@dataclass(frozen=True)
class ComfyUIConfig:
    server_url: str
    model_name: str
    timeout_seconds: float = 45.0


@dataclass(frozen=True)
class KoharuConfig:
    server_url: str
    timeout_seconds: float = 120.0


# Allowed stage names in the ordered processing pipeline.
_ALLOWED_STAGES = ("watermark", "translate", "upscale")


@dataclass(frozen=True)
class ProcessingConfig:
    text: str
    jpeg_quality: int
    webp_quality: int
    max_source_bytes: int
    max_pixels: int
    workers: int
    pipeline: tuple[str, ...] = ("watermark",)
    comfyui: ComfyUIConfig | None = None
    koharu: KoharuConfig | None = None


@dataclass(frozen=True)
class CacheConfig:
    directory: Path
    ttl_seconds: int
    max_size_bytes: int
    low_watermark_ratio: float
    cleanup_interval_seconds: int
    eviction_batch_size: int


@dataclass(frozen=True)
class AppConfig:
    proxy: ProxyConfig
    matching: MatchingConfig
    processing: ProcessingConfig
    cache: CacheConfig


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{field} must be a mapping")
    if any(not isinstance(key, str) for key in value):
        raise ConfigError(f"{field} keys must be strings")
    return value


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{field} must be a non-empty string")
    return value


def _integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{field} must be an integer")
    return value


def _number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{field} must be a number")
    return float(value)


def _string_tuple(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ConfigError(f"{field} must be a list of strings")
    return tuple(_string(item, f"{field}[{index}]") for index, item in enumerate(value))


def _section(
    root: Mapping[str, Any],
    name: str,
    required_keys: set[str],
    optional_keys: set[str] | None = None,
) -> Mapping[str, Any]:
    if name not in root:
        raise ConfigError(f"{name} is required")
    section = _mapping(root[name], name)
    allowed_keys = required_keys | (optional_keys or set())
    unknown_keys = set(section) - allowed_keys
    if unknown_keys:
        names = ", ".join(sorted(unknown_keys))
        raise ConfigError(f"{name} contains unknown keys: {names}")
    missing_keys = required_keys - set(section)
    if missing_keys:
        field = sorted(missing_keys)[0]
        raise ConfigError(f"{name}.{field} is required")
    return section


def load_config(path: Path) -> AppConfig:
    """Load, validate, and convert the YAML configuration at *path*."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"configuration file not found: {path}") from exc
    except OSError as exc:
        raise ConfigError(f"could not read configuration file: {path}") from exc
    except UnicodeError as exc:
        raise ConfigError(f"could not read configuration file: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML: {exc}") from exc

    root = _mapping(raw, "root")
    allowed_sections = {"proxy", "matching", "processing", "cache"}
    unknown_sections = set(root) - allowed_sections
    if unknown_sections:
        names = ", ".join(sorted(unknown_sections))
        raise ConfigError(f"root contains unknown keys: {names}")
    missing_sections = allowed_sections - set(root)
    if missing_sections:
        names = ", ".join(sorted(missing_sections))
        raise ConfigError(f"root is missing required sections: {names}")

    proxy_data = _section(root, "proxy", {"host", "port"})
    host = _string(proxy_data["host"], "proxy.host")
    port = _integer(proxy_data["port"], "proxy.port")
    if not 1 <= port <= 65535:
        raise ConfigError("proxy.port must be between 1 and 65535")

    matching_data = _section(root, "matching", {"domains", "url_regex"})
    domains = _string_tuple(matching_data["domains"], "matching.domains")
    if not domains:
        raise ConfigError("matching.domains must contain at least one hostname glob")
    url_regex = _string_tuple(matching_data["url_regex"], "matching.url_regex")
    for index, pattern in enumerate(url_regex):
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ConfigError(f"matching.url_regex[{index}] is invalid: {exc}") from exc

    processing_data = _section(
        root,
        "processing",
        {"jpeg_quality", "webp_quality", "max_source_mb", "max_pixels", "workers"},
        {"pipeline", "text", "comfyui", "koharu"},
    )

    if "pipeline" in processing_data:
        pipeline_raw = processing_data["pipeline"]
        if isinstance(pipeline_raw, str):
            pipeline = (pipeline_raw,)
        elif isinstance(pipeline_raw, list):
            pipeline = tuple(
                _string(item, f"processing.pipeline[{index}]")
                for index, item in enumerate(pipeline_raw)
            )
        else:
            raise ConfigError("processing.pipeline must be a string or list of strings")
    else:
        pipeline = ("watermark",)

    if not pipeline:
        raise ConfigError("processing.pipeline must contain at least one stage")
    normalized = tuple(stage.lower() for stage in pipeline)
    for index, stage in enumerate(normalized):
        if stage not in _ALLOWED_STAGES:
            raise ConfigError(
                f"processing.pipeline[{index}] must be one of: "
                f"{', '.join(_ALLOWED_STAGES)}"
            )

    if "watermark" in normalized and len(normalized) > 1:
        raise ConfigError("processing.pipeline 'watermark' cannot combine with other stages")
    if (
        "translate" in normalized
        and "upscale" in normalized
        and normalized.index("translate") > normalized.index("upscale")
    ):
        raise ConfigError(
            "processing.pipeline 'translate' must come before 'upscale'"
        )

    needs_text = "watermark" in normalized
    needs_koharu = "translate" in normalized
    needs_comfyui = "upscale" in normalized

    if needs_text:
        if "text" not in processing_data:
            raise ConfigError("processing.text is required when pipeline includes 'watermark'")
        text = _string(processing_data["text"], "processing.text")
    else:
        text = _string(processing_data.get("text", "PROCESSED"), "processing.text")

    comfyui = None
    if needs_comfyui:
        if "comfyui" not in processing_data:
            raise ConfigError("processing.comfyui is required when pipeline includes 'upscale'")
        comfyui_data = _section(
            processing_data,
            "comfyui",
            {"server_url", "model_name"},
            {"timeout_seconds"},
        )
        server_url = _string(comfyui_data["server_url"], "comfyui.server_url")
        model_name = _string(comfyui_data["model_name"], "comfyui.model_name")
        timeout_seconds = _number(
            comfyui_data.get("timeout_seconds", 45.0), "comfyui.timeout_seconds"
        )
        if timeout_seconds <= 0:
            raise ConfigError("comfyui.timeout_seconds must be positive")
        comfyui = ComfyUIConfig(
            server_url=server_url,
            model_name=model_name,
            timeout_seconds=timeout_seconds,
        )

    koharu = None
    if needs_koharu:
        if "koharu" not in processing_data:
            raise ConfigError("processing.koharu is required when pipeline includes 'translate'")
        koharu_data = _section(
            processing_data,
            "koharu",
            {"server_url"},
            {"timeout_seconds"},
        )
        koharu_server_url = _string(koharu_data["server_url"], "koharu.server_url")
        koharu_timeout = _number(
            koharu_data.get("timeout_seconds", 120.0), "koharu.timeout_seconds"
        )
        if koharu_timeout <= 0:
            raise ConfigError("koharu.timeout_seconds must be positive")
        koharu = KoharuConfig(
            server_url=koharu_server_url,
            timeout_seconds=koharu_timeout,
        )

    jpeg_quality = _integer(processing_data["jpeg_quality"], "processing.jpeg_quality")
    webp_quality = _integer(processing_data["webp_quality"], "processing.webp_quality")
    max_source_mb = _integer(processing_data["max_source_mb"], "processing.max_source_mb")
    max_pixels = _integer(processing_data["max_pixels"], "processing.max_pixels")
    workers = _integer(processing_data["workers"], "processing.workers")
    if not 1 <= jpeg_quality <= 100 or not 1 <= webp_quality <= 100:
        raise ConfigError("processing quality must be between 1 and 100")
    if max_source_mb <= 0:
        raise ConfigError("processing.max_source_mb must be positive")
    if max_pixels <= 0:
        raise ConfigError("processing.max_pixels must be positive")
    if workers <= 0:
        raise ConfigError("processing.workers must be positive")


    cache_data = _section(
        root,
        "cache",
        {
            "directory",
            "ttl_hours",
            "max_size_gb",
            "low_watermark_ratio",
            "cleanup_interval_minutes",
            "eviction_batch_size",
        },
    )
    directory = Path(_string(cache_data["directory"], "cache.directory"))
    ttl_hours = _integer(cache_data["ttl_hours"], "cache.ttl_hours")
    max_size_gb = _integer(cache_data["max_size_gb"], "cache.max_size_gb")
    low_watermark_ratio = _number(
        cache_data["low_watermark_ratio"], "cache.low_watermark_ratio"
    )
    cleanup_minutes = _integer(
        cache_data["cleanup_interval_minutes"], "cache.cleanup_interval_minutes"
    )
    eviction_batch_size = _integer(
        cache_data["eviction_batch_size"], "cache.eviction_batch_size"
    )
    if ttl_hours <= 0 or max_size_gb <= 0 or cleanup_minutes <= 0:
        raise ConfigError("cache durations and size must be positive")
    if not 0 < low_watermark_ratio < 1:
        raise ConfigError("cache.low_watermark_ratio must be between 0 and 1")
    if eviction_batch_size <= 0:
        raise ConfigError("cache.eviction_batch_size must be positive")

    if not directory.is_absolute():
        directory = (path.parent / directory).resolve()

    return AppConfig(
        proxy=ProxyConfig(host=host, port=port),
        matching=MatchingConfig(domains=domains, url_regex=url_regex),
        processing=ProcessingConfig(
            text=text,
            jpeg_quality=jpeg_quality,
            webp_quality=webp_quality,
            max_source_bytes=max_source_mb * 1024**2,
            max_pixels=max_pixels,
            workers=workers,
            pipeline=normalized,
            comfyui=comfyui,
            koharu=koharu,
        ),
        cache=CacheConfig(
            directory=directory,
            ttl_seconds=ttl_hours * 3600,
            max_size_bytes=max_size_gb * 1024**3,
            low_watermark_ratio=low_watermark_ratio,
            cleanup_interval_seconds=cleanup_minutes * 60,
            eviction_batch_size=eviction_batch_size,
        ),
    )
