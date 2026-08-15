import os
from pathlib import Path
import subprocess
import sys

import pytest

from image_proxy.config import ConfigError, load_config


VALID = """
proxy: {host: 0.0.0.0, port: 8080}
matching:
  domains: ["*.cdn.test"]
  url_regex: ["/manga/"]
processing:
  text: UPSCALED
  jpeg_quality: 90
  webp_quality: 88
  max_source_mb: 30
  max_pixels: 80000000
  workers: 2
cache:
  directory: ./data/cache
  ttl_hours: 168
  max_size_gb: 10
  low_watermark_ratio: 0.90
  cleanup_interval_minutes: 10
  eviction_batch_size: 25
"""


def test_load_config_converts_units_and_resolves_cache_path(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(VALID)

    config = load_config(path)

    assert config.proxy.port == 8080
    assert config.matching.domains == ("*.cdn.test",)
    assert config.processing.max_source_bytes == 30 * 1024**2
    assert config.cache.ttl_seconds == 168 * 3600
    assert config.cache.max_size_bytes == 10 * 1024**3
    assert config.cache.directory == tmp_path / "data/cache"


def test_load_config_rejects_empty_matching_domains(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(VALID.replace('domains: ["*.cdn.test"]', "domains: []"))

    with pytest.raises(ConfigError, match="matching.domains must contain"):
        load_config(path)


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("proxy: {host: 0.0.0.0, port: 8080}", "proxy: {port: 8080}", "proxy.host"),
        ("port: 8080", "port: 70000", "proxy.port"),
        ("workers: 2", "workers: 0", "processing.workers"),
        (
            "low_watermark_ratio: 0.90",
            "low_watermark_ratio: 1.1",
            "cache.low_watermark_ratio",
        ),
        ('url_regex: ["/manga/"]', "url_regex: ['[']", "matching.url_regex"),
    ],
)
def test_load_config_rejects_invalid_values(
    tmp_path: Path, old: str, new: str, message: str
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(VALID.replace(old, new))

    with pytest.raises(ConfigError, match=message):
        load_config(path)


@pytest.mark.parametrize(
    ("contents", "message"),
    [
        (VALID + "unknown: true\n", "unknown keys"),
        (
            VALID.replace(
                "proxy: {host: 0.0.0.0, port: 8080}",
                "proxy: {host: 0.0.0.0, port: 8080, extra: true}",
            ),
            "proxy contains unknown keys",
        ),
        ("1: bad\n", "root"),
        (VALID.replace("proxy: {host: 0.0.0.0, port: 8080}", "proxy: [not, a, mapping]"), "proxy"),
        ("- not\n- a\n- mapping\n", "root"),
        ("proxy: [\n", "invalid YAML"),
    ],
)
def test_load_config_rejects_invalid_yaml_structure(
    tmp_path: Path, contents: str, message: str
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(contents)

    with pytest.raises(ConfigError, match=message):
        load_config(path)


def test_load_config_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "missing.yaml")


def test_load_config_rejects_non_utf8_file(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_bytes(b"\xff")

    with pytest.raises(ConfigError, match="could not read"):
        load_config(path)


def test_load_config_reads_utf8_config_under_c_locale(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(VALID.replace("UPSCALED", "CAFÉ"), encoding="utf-8")
    environment = os.environ | {
        "LC_ALL": "C",
        "PYTHONCOERCECLOCALE": "0",
        "PYTHONUTF8": "0",
    }

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from pathlib import Path; "
                "from image_proxy.config import load_config; "
                "load_config(Path(__import__('sys').argv[1]))"
            ),
            str(path),
        ],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
    )

    assert result.returncode == 0, result.stderr


VALID_COMFYUI = """
proxy: {host: 0.0.0.0, port: 8080}
matching:
  domains: ["*.cdn.test"]
  url_regex: ["/manga/"]
processing:
  engine: comfyui
  jpeg_quality: 90
  webp_quality: 88
  max_source_mb: 30
  max_pixels: 80000000
  workers: 1
  comfyui:
    server_url: "http://127.0.0.1:8188"
    model_name: "2x-AnimeSharpV3.pth"
    timeout_seconds: 45
cache:
  directory: ./data/cache
  ttl_hours: 168
  max_size_gb: 10
  low_watermark_ratio: 0.90
  cleanup_interval_minutes: 10
  eviction_batch_size: 25
"""


def test_load_config_parses_valid_comfyui_configuration(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(VALID_COMFYUI)

    config = load_config(path)

    assert config.processing.engine == "comfyui"
    assert config.processing.comfyui is not None
    assert config.processing.comfyui.server_url == "http://127.0.0.1:8188"
    assert config.processing.comfyui.model_name == "2x-AnimeSharpV3.pth"
    assert config.processing.comfyui.timeout_seconds == 45.0


def test_load_config_rejects_invalid_engine(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(VALID.replace("text: UPSCALED", "engine: invalid\n  text: UPSCALED"))

    with pytest.raises(ConfigError, match="processing.engine"):
        load_config(path)


def test_load_config_rejects_missing_comfyui_section(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(VALID.replace("text: UPSCALED", "engine: comfyui"))

    with pytest.raises(ConfigError, match="processing.comfyui is required"):
        load_config(path)


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("server_url: \"http://127.0.0.1:8188\"", "server_url: \"\"", "comfyui.server_url"),
        ("model_name: \"2x-AnimeSharpV3.pth\"", "model_name: \"\"", "comfyui.model_name"),
        ("timeout_seconds: 45", "timeout_seconds: 0", "comfyui.timeout_seconds"),
        ("timeout_seconds: 45", "timeout_seconds: -5", "comfyui.timeout_seconds"),
    ],
)
def test_load_config_rejects_invalid_comfyui_values(
    tmp_path: Path, old: str, new: str, message: str
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(VALID_COMFYUI.replace(old, new))

    with pytest.raises(ConfigError, match=message):
        load_config(path)

