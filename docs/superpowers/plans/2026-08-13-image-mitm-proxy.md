# Image MITM Proxy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a native Python MITM proxy that intercepts selected JPEG/WebP manga images for Android Chrome, overlays `UPSCALED`, and serves processed images through a TTL/LRU disk cache.

**Architecture:** mitmdump owns HTTP(S) proxying and calls a small addon composed from independently tested configuration, URL matcher, Pillow processor, and SQLite/file cache units. The addon performs request-time cache hits and response-time transformation, delegates blocking work to threads, and always fails open to the original upstream response.

**Tech Stack:** Python 3.10, mitmproxy 11.0.2, Pillow 12.3.0, PyYAML 6.0.3, SQLite, pytest 9.1.1, pytest-asyncio 1.4.0, uv

## Global Constraints

- Run natively on Linux with Python 3.10; do not add Docker.
- Android Chrome is the primary client and trusts the mitmproxy user CA.
- Listen on `0.0.0.0:8080` by default with no proxy authentication; document trusted-LAN-only use.
- Match hostname globs or full-URL regexes with OR semantics; an empty rule set matches nothing.
- Process only status-200 static JPEG/JPG and WebP responses to eligible `GET` requests without `Range`.
- Pass GIF, PNG, SVG, AVIF, animated images, videos, and non-image content through unchanged.
- All interception, processing, and cache failures fail open to the original upstream response.
- Cache TTL is absolute from creation; hits update `last_accessed_at` without extending expiration.
- Size eviction deletes oldest-accessed entries in batches until reaching the configured 90% low watermark.
- Keep `ImageProcessor` free of mitmproxy and cache types so a future Real-ESRGAN or ComfyUI adapter can replace Pillow.
- Never log cookies, authorization values, request/response bodies, or raw cache-variant secrets.

---

## File Map

- `pyproject.toml`: package metadata, locked Python/dependency bounds, console script, and pytest settings.
- `uv.lock`: reproducible dependency resolution generated from `pyproject.toml`.
- `.gitignore`: local environment, Python build output, test caches, CA material, and image cache data.
- `config.example.yaml`: complete documented runtime defaults.
- `src/image_proxy/config.py`: immutable typed configuration and strict YAML validation.
- `src/image_proxy/matcher.py`: hostname/URL selection and privacy-safe cache-key construction.
- `src/image_proxy/processor.py`: processor protocol and Pillow watermark implementation.
- `src/image_proxy/cache.py`: atomic artifact storage, SQLite metadata, TTL lookup, and incremental LRU cleanup.
- `src/image_proxy/addon.py`: async mitmproxy flow orchestration and fail-open response behavior.
- `src/image_proxy/mitm_script.py`: the addon list loaded by mitmdump.
- `src/image_proxy/runner.py`: validates YAML and starts the venv's mitmdump with consistent options.
- `tests/`: focused unit and integration tests mirroring each module.
- `tests/smoke/test_live_proxy.py`: opt-in live HTTP/HTTPS proxy smoke test.
- `README.md`: PC installation, Android CA/proxy setup, configuration, operation, and troubleshooting.

---

### Task 1: Project Foundation and Strict Configuration

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `config.example.yaml`
- Create: `src/image_proxy/__init__.py`
- Create: `src/image_proxy/config.py`
- Create: `tests/test_config.py`
- Generate: `uv.lock`

**Interfaces:**
- Produces: `load_config(path: Path) -> AppConfig`
- Produces: `ProxyConfig(host: str, port: int)` and `MatchingConfig(domains: tuple[str, ...], url_regex: tuple[str, ...])`.
- Produces: `ProcessingConfig(text: str, jpeg_quality: int, webp_quality: int, max_source_bytes: int, max_pixels: int, workers: int)`.
- Produces: `CacheConfig(directory: Path, ttl_seconds: int, max_size_bytes: int, low_watermark_ratio: float, cleanup_interval_seconds: int, eviction_batch_size: int)`.
- Produces: `AppConfig(proxy: ProxyConfig, matching: MatchingConfig, processing: ProcessingConfig, cache: CacheConfig)`.
- Produces: `ConfigError(ValueError)` for all user-facing validation failures.

- [ ] **Step 1: Add package/test configuration and install dependencies**

Create `pyproject.toml` with exact runtime versions compatible with Python 3.10:

```toml
[build-system]
requires = ["setuptools>=69"]
build-backend = "setuptools.build_meta"

[project]
name = "image-mitm-proxy"
version = "0.1.0"
requires-python = ">=3.10,<3.11"
dependencies = [
  "mitmproxy==11.0.2",
  "Pillow==12.3.0",
  "PyYAML==6.0.3",
]

[project.optional-dependencies]
dev = [
  "pytest==9.1.1",
  "pytest-asyncio==1.4.0",
]

[project.scripts]
image-proxy = "image_proxy.runner:main"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
addopts = "-ra -m 'not smoke'"
asyncio_mode = "auto"
markers = ["smoke: starts a live mitmdump process"]
testpaths = ["tests"]
```

Create `.gitignore` with `.venv/`, `__pycache__/`, `*.py[cod]`, `.pytest_cache/`, `data/`, `.mitmproxy/`, `dist/`, and `*.egg-info/`. Add an empty `src/image_proxy/__init__.py`, then run:

```bash
uv sync --all-extras
```

Expected: `uv.lock` is generated and `uv run pytest --version` reports pytest 9.1.1.

- [ ] **Step 2: Write failing configuration tests**

Create `tests/test_config.py` with tests that load a valid temporary YAML file and assert exact converted units:

```python
from pathlib import Path

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


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("proxy: {host: 0.0.0.0, port: 8080}", "proxy: {port: 8080}", "proxy.host"),
        ("port: 8080", "port: 70000", "proxy.port"),
        ("workers: 2", "workers: 0", "processing.workers"),
        ("low_watermark_ratio: 0.90", "low_watermark_ratio: 1.1", "cache.low_watermark_ratio"),
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
```

- [ ] **Step 3: Run tests and verify RED**

Run:

```bash
uv run pytest tests/test_config.py -q
```

Expected: collection fails with `ModuleNotFoundError: No module named 'image_proxy.config'`.

- [ ] **Step 4: Implement immutable config loading and the example YAML**

In `src/image_proxy/config.py`, define frozen dataclasses using runtime-ready units (`*_bytes`, `*_seconds`) and parse with `yaml.safe_load`. Use small `_mapping`, `_string`, `_integer`, `_number`, and `_string_tuple` helpers that include the dotted field name in `ConfigError`. Validate:

```python
if not 1 <= port <= 65535:
    raise ConfigError("proxy.port must be between 1 and 65535")
if not 1 <= jpeg_quality <= 100 or not 1 <= webp_quality <= 100:
    raise ConfigError("processing quality must be between 1 and 100")
if max_source_mb <= 0 or max_pixels <= 0 or workers <= 0:
    raise ConfigError("processing limits and workers must be positive")
if ttl_hours <= 0 or max_size_gb <= 0 or cleanup_minutes <= 0:
    raise ConfigError("cache durations and size must be positive")
if not 0 < low_watermark_ratio < 1:
    raise ConfigError("cache.low_watermark_ratio must be between 0 and 1")
if eviction_batch_size <= 0:
    raise ConfigError("cache.eviction_batch_size must be positive")
for index, pattern in enumerate(url_regex):
    try:
        re.compile(pattern)
    except re.error as exc:
        raise ConfigError(f"matching.url_regex[{index}] is invalid: {exc}") from exc
```

Resolve a relative cache directory against `path.parent`, not the process working directory. Reject unknown top-level and section keys so misspelled security/configuration fields cannot be ignored. Wrap YAML syntax errors, a missing file, and a non-mapping root in `ConfigError`.

Create `config.example.yaml` with the exact defaults from the design spec and comments explaining OR matching and trusted-LAN exposure.

- [ ] **Step 5: Run configuration tests and the full suite**

Run:

```bash
uv run pytest tests/test_config.py -q
uv run pytest -q
```

Expected: all configuration tests pass and the suite exits 0.

- [ ] **Step 6: Commit the foundation**

```bash
git add pyproject.toml uv.lock .gitignore config.example.yaml src/image_proxy/__init__.py src/image_proxy/config.py tests/test_config.py
git commit -m "feat: add strict proxy configuration"
```

---

### Task 2: URL Matching, Eligibility, and Cache Identity

**Files:**
- Create: `src/image_proxy/matcher.py`
- Create: `tests/test_matcher.py`

**Interfaces:**
- Consumes: `MatchingConfig` from Task 1.
- Produces: `UrlMatcher.matches(host: str, url: str) -> bool`.
- Produces: `is_eligible_request(method: str, headers: Mapping[str, str]) -> bool`.
- Produces: `build_cache_key(url: str, processor_fingerprint: str, headers: Mapping[str, str]) -> str`.

- [ ] **Step 1: Write failing matching and identity tests**

Create `tests/test_matcher.py`:

```python
from image_proxy.config import MatchingConfig
from image_proxy.matcher import UrlMatcher, build_cache_key, is_eligible_request


def test_matcher_uses_domain_or_full_url_regex() -> None:
    matcher = UrlMatcher(MatchingConfig(("*.cdn.test",), (r"/manga/\d+",)))
    assert matcher.matches("img.cdn.test", "https://img.cdn.test/page.webp")
    assert matcher.matches("other.test", "https://other.test/manga/42/page")
    assert not matcher.matches("cdn.test", "https://cdn.test/not-selected")


def test_empty_rules_match_nothing() -> None:
    assert not UrlMatcher(MatchingConfig((), ())).matches(
        "cdn.test", "https://cdn.test/page.jpg"
    )


def test_request_eligibility_requires_get_without_range() -> None:
    assert is_eligible_request("GET", {})
    assert not is_eligible_request("POST", {})
    assert not is_eligible_request("GET", {"Range": "bytes=0-99"})


def test_cache_key_varies_by_private_headers_without_exposing_them() -> None:
    first = build_cache_key(
        "https://cdn.test/page.webp", "processor-v1", {"Cookie": "secret-a"}
    )
    second = build_cache_key(
        "https://cdn.test/page.webp", "processor-v1", {"Cookie": "secret-b"}
    )
    assert first != second
    assert "secret" not in first
    assert len(first) == 64
```

- [ ] **Step 2: Run tests and verify RED**

Run `uv run pytest tests/test_matcher.py -q`.

Expected: import fails because `image_proxy.matcher` does not exist.

- [ ] **Step 3: Implement matching and deterministic cache keys**

Use `fnmatch.fnmatchcase(host.lower(), pattern.lower())` for domain rules and precompiled regexes with `.search(url)`. Treat header names case-insensitively. Build the identity without storing raw secrets:

```python
def build_cache_key(url: str, processor_fingerprint: str, headers: Mapping[str, str]) -> str:
    normalized = {key.lower(): value for key, value in headers.items()}
    variants = []
    for name in ("accept", "authorization", "cookie"):
        digest = hashlib.sha256(normalized.get(name, "").encode()).hexdigest()
        variants.append(f"{name}:{digest}")
    payload = "\n".join((url, processor_fingerprint, *variants))
    return hashlib.sha256(payload.encode()).hexdigest()
```

`is_eligible_request` must use `method.upper() == "GET"` and case-insensitive detection of `Range`.

- [ ] **Step 4: Verify matcher behavior**

Run:

```bash
uv run pytest tests/test_matcher.py -q
uv run pytest -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit URL selection**

```bash
git add src/image_proxy/matcher.py tests/test_matcher.py
git commit -m "feat: add URL matching and cache identity"
```

---

### Task 3: Replaceable Pillow Watermark Processor

**Files:**
- Create: `src/image_proxy/processor.py`
- Create: `tests/test_processor.py`

**Interfaces:**
- Consumes: `ProcessingConfig` from Task 1.
- Produces: `ImageProcessor` protocol with `fingerprint: str` and `process(data: bytes, content_type: str | None) -> ProcessedImage`.
- Produces: `ProcessedImage(data: bytes, mime_type: str, format_name: str)`.
- Produces: `WatermarkProcessor` and `ProcessingError`.

- [ ] **Step 1: Write failing JPEG/WebP behavior tests**

In `tests/test_processor.py`, create in-memory fixtures with Pillow and verify real decoded pixels rather than mocks:

```python
from io import BytesIO

import pytest
from PIL import Image

from image_proxy.config import ProcessingConfig
from image_proxy.processor import ProcessingError, WatermarkProcessor


def image_bytes(format_name: str, size: tuple[int, int] = (400, 600)) -> bytes:
    output = BytesIO()
    Image.new("RGB", size, "white").save(output, format=format_name)
    return output.getvalue()


def processor(max_source_bytes: int = 1024**2, max_pixels: int = 1_000_000):
    return WatermarkProcessor(
        ProcessingConfig("UPSCALED", 90, 90, max_source_bytes, max_pixels, 2)
    )


@pytest.mark.parametrize(
    ("format_name", "content_type", "expected_mime"),
    [("JPEG", "image/jpeg", "image/jpeg"), ("WEBP", "image/webp", "image/webp")],
)
def test_process_preserves_supported_format_and_changes_visible_pixels(
    format_name: str, content_type: str, expected_mime: str
) -> None:
    source = image_bytes(format_name)
    result = processor().process(source, content_type)
    with Image.open(BytesIO(source)) as before, Image.open(BytesIO(result.data)) as after:
        assert after.format == format_name
        assert after.size == before.size
        red_pixels = sum(
            1
            for red, green, blue in after.convert("RGB").getdata()
            if red > 180 and green < 120 and blue < 120
        )
        assert red_pixels > 50
    assert result.mime_type == expected_mime


def test_process_rejects_explicit_unsupported_content_type() -> None:
    with pytest.raises(ProcessingError, match="content type"):
        processor().process(image_bytes("JPEG"), "image/png")


def test_process_rejects_source_and_pixel_limits() -> None:
    source = image_bytes("JPEG", (100, 100))
    with pytest.raises(ProcessingError, match="source byte limit"):
        processor(max_source_bytes=len(source) - 1).process(source, "image/jpeg")
    with pytest.raises(ProcessingError, match="pixel limit"):
        processor(max_pixels=9_999).process(source, "image/jpeg")


def test_process_rejects_animated_webp() -> None:
    output = BytesIO()
    frames = [Image.new("RGB", (20, 20), color) for color in ("red", "blue")]
    frames[0].save(output, "WEBP", save_all=True, append_images=frames[1:], duration=50)
    with pytest.raises(ProcessingError, match="animated"):
        processor().process(output.getvalue(), "image/webp")


def test_fingerprint_changes_when_output_configuration_changes() -> None:
    base = processor().fingerprint
    changed = WatermarkProcessor(
        ProcessingConfig("DIFFERENT", 90, 90, 1024**2, 1_000_000, 2)
    ).fingerprint
    assert changed != base
```

- [ ] **Step 2: Run processor tests and verify RED**

Run `uv run pytest tests/test_processor.py -q`.

Expected: import fails because `image_proxy.processor` does not exist.

- [ ] **Step 3: Implement the protocol, limits, format validation, and fingerprint**

Define frozen `ProcessedImage`, `ProcessingError`, and a runtime-checkable protocol. `WatermarkProcessor.fingerprint` is SHA-256 of stable JSON containing `name="pillow-watermark"`, `version=1`, watermark text, and both quality values. Worker count and safety limits do not affect output and must not enter the fingerprint.

Before `image.load()`, reject byte length, inspect `image.format`, `image.n_frames`, and `width * height`. Accept explicit content types `image/jpeg`, `image/jpg`, `image/webp`, empty, or `application/octet-stream`; decoded format remains authoritative.

- [ ] **Step 4: Draw centered scalable text and encode the original format**

Use a bold DejaVu font when available and Pillow's default font as fallback. Compute font size from image width, clamp it from 18 to 160, then reduce until `textbbox` fits within 90% of image width. Draw fill `(255, 32, 32)` with black stroke at the centered coordinate.

For JPEG, convert to `RGB` and save with `quality=config.jpeg_quality`. For WebP, preserve `RGBA` when alpha exists, otherwise use `RGB`, and save with `quality=config.webp_quality`. Convert Pillow decoding/encoding exceptions into `ProcessingError` with no source bytes in the message.

- [ ] **Step 5: Verify RED–GREEN and full regression**

Run:

```bash
uv run pytest tests/test_processor.py -q
uv run pytest -q
```

Expected: all tests pass without decompression warnings.

- [ ] **Step 6: Commit the processor**

```bash
git add src/image_proxy/processor.py tests/test_processor.py
git commit -m "feat: add Pillow watermark processor"
```

---

### Task 4: Atomic TTL Disk Cache and Last-Access Metadata

**Files:**
- Create: `src/image_proxy/cache.py`
- Create: `tests/test_cache.py`

**Interfaces:**
- Consumes: `CacheConfig` from Task 1 and 64-character cache keys from Task 2.
- Produces: `CacheStore.initialize() -> None`, `get(key: str) -> CacheHit | None`, `put(key: str, source_url: str, processor_fingerprint: str, mime_type: str, headers: Mapping[str, str], data: bytes) -> None`, and `close() -> None`.
- Produces: `CacheHit(data: bytes, mime_type: str, headers: dict[str, str])`.
- Produces: `CacheError(RuntimeError)`.

- [ ] **Step 1: Write failing cache miss/hit/TTL tests**

Create a mutable test clock and a `cache_store` helper in `tests/test_cache.py`, then specify behavior:

```python
import sqlite3
from pathlib import Path

from image_proxy.cache import CacheStore
from image_proxy.config import CacheConfig


class Clock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def store(tmp_path: Path, clock: Clock, ttl: int = 60) -> CacheStore:
    cache = CacheStore(
        CacheConfig(tmp_path, ttl, 10_000, 0.9, 600, 2), clock=clock
    )
    cache.initialize()
    return cache


def test_put_get_updates_access_without_extending_absolute_ttl(tmp_path: Path) -> None:
    clock = Clock()
    cache = store(tmp_path, clock)
    key = "a" * 64
    cache.put(key, "https://cdn.test/a.jpg", "fp", "image/jpeg", {"Cache-Control": "max-age=60"}, b"processed")

    clock.now = 1_030.0
    assert cache.get(key).data == b"processed"
    with sqlite3.connect(tmp_path / "cache.sqlite3") as database:
        row = database.execute(
            "SELECT created_at, expires_at, last_accessed_at FROM entries WHERE cache_key = ?",
            (key,),
        ).fetchone()
    assert tuple(row) == (1_000.0, 1_060.0, 1_030.0)

    clock.now = 1_061.0
    assert cache.get(key) is None
    assert not list((tmp_path / "artifacts").rglob("*.img"))


def test_missing_artifact_removes_broken_metadata(tmp_path: Path) -> None:
    clock = Clock()
    cache = store(tmp_path, clock)
    key = "b" * 64
    cache.put(key, "https://cdn.test/b.webp", "fp", "image/webp", {}, b"processed")
    next((tmp_path / "artifacts").rglob("*.img")).unlink()
    assert cache.get(key) is None
    with sqlite3.connect(tmp_path / "cache.sqlite3") as database:
        count = database.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    assert count == 0
```

- [ ] **Step 2: Run cache tests and verify RED**

Run `uv run pytest tests/test_cache.py -q`.

Expected: import fails because `image_proxy.cache` does not exist.

- [ ] **Step 3: Implement schema, WAL initialization, atomic put, and TTL get**

Store metadata at `<cache.directory>/cache.sqlite3`. Use a single SQLite connection with `check_same_thread=False`, `sqlite3.Row`, `PRAGMA journal_mode=WAL`, and a `threading.RLock`. Create this schema and access index:

```sql
CREATE TABLE IF NOT EXISTS entries (
  cache_key TEXT PRIMARY KEY,
  source_url TEXT NOT NULL,
  processor_fingerprint TEXT NOT NULL,
  mime_type TEXT NOT NULL,
  artifact_path TEXT NOT NULL UNIQUE,
  response_headers_json TEXT NOT NULL,
  size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL,
  last_accessed_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS entries_lru ON entries(last_accessed_at);
CREATE INDEX IF NOT EXISTS entries_expiry ON entries(expires_at);
```

Validate keys as lowercase 64-character hex. Store artifacts under `artifacts/<first-two-chars>/<key>.img`. Write with `tempfile.NamedTemporaryFile` in the destination directory, flush and `os.fsync`, then `os.replace`. Commit metadata only after replacement. JSON-encode only `Cache-Control`, `Expires`, `Access-Control-Allow-Origin`, `Access-Control-Allow-Credentials`, `Cross-Origin-Resource-Policy`, and `Content-Disposition`; return a fresh dictionary from `get`.

Expired/missing artifacts are deleted from both stores before returning `None`. Convert operational SQLite/filesystem failures to `CacheError`; never include response body bytes in the message.

- [ ] **Step 4: Verify basic cache behavior**

Run:

```bash
uv run pytest tests/test_cache.py -q
uv run pytest -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit the base cache**

```bash
git add src/image_proxy/cache.py tests/test_cache.py
git commit -m "feat: add atomic TTL image cache"
```

---

### Task 5: Incremental Expiry, LRU Eviction, and Orphan Cleanup

**Files:**
- Modify: `src/image_proxy/cache.py`
- Modify: `tests/test_cache.py`

**Interfaces:**
- Extends: `CacheStore.cleanup() -> CleanupReport`.
- Produces: `CleanupReport(expired_count: int, lru_count: int, orphan_count: int, bytes_freed: int)`.

- [ ] **Step 1: Add failing expiration and low-watermark tests**

Append tests that insert deterministic sizes at different access times:

```python
def test_cleanup_evicts_oldest_in_batches_to_low_watermark(tmp_path: Path) -> None:
    clock = Clock()
    writer = CacheStore(CacheConfig(tmp_path, 600, 100, 0.6, 600, 1), clock=clock)
    writer.initialize()
    for index, key_char in enumerate(("a", "b", "c", "d")):
        clock.now = 1_000 + index
        writer.put(key_char * 64, f"https://cdn/{key_char}", "fp", "image/jpeg", {}, b"xxxx")
    writer.close()

    cache = CacheStore(CacheConfig(tmp_path, 600, 10, 0.6, 600, 1), clock=clock)
    cache.initialize()

    report = cache.cleanup()

    assert report.lru_count == 3
    assert cache.total_size_bytes() == 4
    assert cache.get("d" * 64) is not None
    assert cache.get("a" * 64) is None


def test_put_triggers_cleanup_after_crossing_maximum(tmp_path: Path) -> None:
    clock = Clock()
    cache = CacheStore(CacheConfig(tmp_path, 600, 10, 0.6, 600, 1), clock=clock)
    cache.initialize()
    for key_char in ("a", "b", "c"):
        clock.now += 1
        cache.put(key_char * 64, f"https://cdn/{key_char}", "fp", "image/jpeg", {}, b"xxxx")
    assert cache.total_size_bytes() == 4
    assert cache.get("c" * 64) is not None
    assert cache.get("a" * 64) is None


def test_cleanup_removes_expired_rows_and_orphan_files(tmp_path: Path) -> None:
    clock = Clock()
    cache = store(tmp_path, clock, ttl=10)
    cache.put("a" * 64, "https://cdn/a", "fp", "image/jpeg", {}, b"old")
    orphan = tmp_path / "artifacts" / "ff" / ("f" * 64 + ".img")
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b"orphan")
    clock.now += 11

    report = cache.cleanup()

    assert report.expired_count == 1
    assert report.orphan_count == 1
    assert cache.total_size_bytes() == 0
    assert not orphan.exists()
```

- [ ] **Step 2: Run focused tests and verify RED**

Run `uv run pytest tests/test_cache.py -q`.

Expected: fails because `cleanup`, `CleanupReport`, or `total_size_bytes` is missing.

- [ ] **Step 3: Implement deterministic incremental cleanup**

Within the cache lock and short transactions:

1. Select and delete expired rows, unlinking their artifacts.
2. Compare artifact files against database-relative paths and unlink orphans.
3. Sum `size_bytes` from metadata.
4. If the total exceeds `max_size_bytes`, repeatedly select at most `eviction_batch_size` rows ordered by `last_accessed_at ASC, cache_key ASC`, unlink them, delete rows, and commit the batch.
5. Stop only when the total is at or below `int(max_size_bytes * low_watermark_ratio)`.

After a successful `put`, compare total tracked size with `max_size_bytes` and invoke cleanup immediately when it has crossed the maximum. Startup and the periodic addon loop remain additional cleanup triggers.

Count bytes only for tracked entry eviction; orphan byte removal also contributes to `bytes_freed`. Missing files are not fatal. A permission or database failure raises `CacheError` after leaving committed batches internally consistent.

- [ ] **Step 4: Verify cache cleanup and regressions**

Run:

```bash
uv run pytest tests/test_cache.py -q
uv run pytest -q
```

Expected: all tests pass and the LRU test leaves only the newest artifact.

- [ ] **Step 5: Commit maintenance behavior**

```bash
git add src/image_proxy/cache.py tests/test_cache.py
git commit -m "feat: add incremental LRU cache cleanup"
```

---

### Task 6: Proxy Response Transformation and Fail-Open Behavior

**Files:**
- Create: `src/image_proxy/addon.py`
- Create: `tests/test_addon.py`

**Interfaces:**
- Consumes: `AppConfig`, `UrlMatcher`, `WatermarkProcessor`, `CacheStore`, `build_cache_key`, and mitmproxy `http.HTTPFlow`.
- Produces: `ImageProxyAddon(config: AppConfig | None = None, *, matcher: UrlMatcher | None = None, processor: ImageProcessor | None = None, cache: CacheStore | None = None)`; omitted dependencies are constructed from config, while explicit dependencies support real isolated integration tests.
- Produces: async `ImageProxyAddon.request(flow) -> None` and `response(flow) -> None`.
- Produces: log events `BYPASS`, `CACHE_MISS`, `PROCESSED`, and `FALLBACK`.

- [ ] **Step 1: Write failing flow integration tests**

Use real mitmproxy test flows from `mitmproxy.test.tflow` and small real images. Inject real matcher/processor/cache objects through the addon constructor. Cover:

```python
import logging
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
def addon(tmp_path: Path):
    matching = MatchingConfig(("*.cdn.test",), (r"/manga/",))
    processing = ProcessingConfig("UPSCALED", 90, 90, 10 * 1024**2, 10_000_000, 2)
    cache_config = CacheConfig(tmp_path / "cache", 3600, 100 * 1024**2, 0.9, 600, 25)
    config = AppConfig(ProxyConfig("127.0.0.1", 8080), matching, processing, cache_config)
    cache = CacheStore(cache_config)
    cache.initialize()
    instance = ImageProxyAddon(
        config,
        matcher=UrlMatcher(matching),
        processor=WatermarkProcessor(processing),
        cache=cache,
    )
    yield instance
    cache.close()


def matching_flow(jpeg_bytes: bytes):
    flow = tflow.tflow(resp=True)
    flow.request.url = "https://img.cdn.test/manga/page.jpg"
    flow.response = http.Response.make(
        200,
        jpeg_bytes,
        {"Content-Type": "image/jpeg", "Access-Control-Allow-Origin": "*"},
    )
    return flow


@pytest.mark.asyncio
async def test_non_matching_response_passes_through_byte_for_byte(addon) -> None:
    flow = tflow.tflow(resp=True)
    flow.request.url = "https://other.test/page.jpg"
    original = flow.response.raw_content
    await addon.request(flow)
    await addon.response(flow)
    assert flow.response.raw_content == original


@pytest.mark.asyncio
async def test_matching_jpeg_is_processed_and_representation_headers_repaired(
    addon, jpeg_bytes: bytes
) -> None:
    flow = tflow.tflow(resp=True)
    flow.request.url = "https://img.cdn.test/manga/page.jpg"
    flow.response = http.Response.make(
        200,
        jpeg_bytes,
        {
            "Content-Type": "image/jpeg",
            "Content-Encoding": "identity",
            "ETag": '"old"',
            "Access-Control-Allow-Origin": "*",
        },
    )
    await addon.request(flow)
    await addon.response(flow)
    assert flow.response.raw_content != jpeg_bytes
    assert flow.response.headers["Content-Type"] == "image/jpeg"
    assert "ETag" not in flow.response.headers
    assert "Content-Encoding" not in flow.response.headers
    assert flow.response.headers["Access-Control-Allow-Origin"] == "*"


@pytest.mark.asyncio
async def test_range_request_and_unsupported_response_pass_through(addon, png_bytes) -> None:
    range_flow = tflow.tflow(resp=True)
    range_flow.request.url = "https://img.cdn.test/manga/page.jpg"
    range_flow.request.headers["Range"] = "bytes=0-10"
    range_original = range_flow.response.raw_content
    await addon.request(range_flow)
    await addon.response(range_flow)
    assert range_flow.response.raw_content == range_original

    png_flow = tflow.tflow(resp=True)
    png_flow.request.url = "https://img.cdn.test/manga/page.png"
    png_flow.response = http.Response.make(200, png_bytes, {"Content-Type": "image/png"})
    await addon.request(png_flow)
    await addon.response(png_flow)
    assert png_flow.response.raw_content == png_bytes


@pytest.mark.asyncio
async def test_processor_failure_restores_original_response(addon, caplog) -> None:
    flow = tflow.tflow(resp=True)
    flow.request.url = "https://img.cdn.test/manga/broken.jpg"
    flow.response = http.Response.make(200, b"not-an-image", {"Content-Type": "image/jpeg", "ETag": "keep"})
    original_headers = flow.response.headers.copy()
    with caplog.at_level(logging.WARNING):
        await addon.request(flow)
        await addon.response(flow)
    assert flow.response.raw_content == b"not-an-image"
    assert flow.response.headers == original_headers
    assert "FALLBACK" in caplog.text
```

Fixtures must build an isolated config/cache per test and close the cache after use. Avoid mocking Pillow, SQLite, or mitmproxy flow types.

- [ ] **Step 2: Run addon tests and verify RED**

Run `uv run pytest tests/test_addon.py -q`.

Expected: import fails because `image_proxy.addon` does not exist.

- [ ] **Step 3: Implement response orchestration with exact restoration**

In `request`, return early unless method/range and matcher checks pass. Store the cache key in `flow.metadata["image_proxy.cache_key"]`; cache lookup arrives in Task 7, so this task logs `CACHE_MISS` and lets the request continue.

In `response`, require the metadata key, a non-null response, status 200, and an allowed/generic content type. Before touching decoded content, copy `raw_content` and `headers`. Run `processor.process` and `cache.put` with `asyncio.to_thread`. Apply output only after both succeed:

```python
for name in ("Content-Length", "Content-Encoding", "ETag", "Content-MD5", "Digest"):
    response.headers.pop(name, None)
response.headers["Content-Type"] = processed.mime_type
response.raw_content = processed.data
```

Extract only the response-header allowlist defined in the spec for cache metadata. On any `ProcessingError`, `CacheError`, Pillow error escaping unexpectedly, or filesystem/SQLite error, restore the copied raw body and headers and log `FALLBACK` with exception class plus hostname and the parsed URL path. Derive the path with `urllib.parse.urlsplit(flow.request.pretty_url).path` so query parameter values never enter logs. Never put cookie, authorization, request body, response body, or query parameter values in logs.

- [ ] **Step 4: Verify transformation and fail-open tests**

Run:

```bash
uv run pytest tests/test_addon.py -q
uv run pytest -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit response interception**

```bash
git add src/image_proxy/addon.py tests/test_addon.py
git commit -m "feat: transform matching proxy responses"
```

---

### Task 7: Request-Time Cache Hits, Duplicate Suppression, and Maintenance Lifecycle

**Files:**
- Modify: `src/image_proxy/addon.py`
- Modify: `tests/test_addon.py`

**Interfaces:**
- Extends: `ImageProxyAddon.request` to synthesize `http.Response` on cache hit.
- Produces: per-key async coordination with reference-counted lock entries.
- Produces: `start()`, periodic cleanup task, and `shutdown()` lifecycle methods usable from mitmproxy hooks and tests.
- Produces: read-only `maintenance_task: asyncio.Task[None] | None` state for deterministic lifecycle verification.

- [ ] **Step 1: Add failing cache-hit and concurrency tests**

Append tests that first populate the cache through a response, then send the same request with no upstream response:

```python
@pytest.mark.asyncio
async def test_cache_hit_returns_response_before_upstream_and_preserves_cors(addon, jpeg_bytes) -> None:
    first = matching_flow(jpeg_bytes)
    await addon.request(first)
    await addon.response(first)

    second = tflow.tflow(resp=False)
    second.request.url = first.request.url
    await addon.request(second)

    assert second.response is not None
    assert second.response.status_code == 200
    assert second.response.raw_content == first.response.raw_content
    assert second.response.headers["Content-Type"] == "image/jpeg"
    assert second.response.headers["Access-Control-Allow-Origin"] == "*"


@pytest.mark.asyncio
async def test_identical_concurrent_misses_commit_one_processed_artifact(
    addon, jpeg_bytes, monkeypatch
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
```

Add a lifecycle test with a short cleanup interval and fake/spy cache cleanup method to prove startup cleanup, periodic invocation, task cancellation, and cache close. Mocking is acceptable here because the assertion concerns scheduling calls, while cache behavior is already covered with real SQLite tests.

Use a dedicated addon configured with `cleanup_interval_seconds=1` and this test shape:

```python
@pytest.fixture
async def lifecycle_addon(tmp_path: Path):
    matching = MatchingConfig(("*.cdn.test",), ())
    processing = ProcessingConfig("UPSCALED", 90, 90, 10 * 1024**2, 10_000_000, 1)
    cache_config = CacheConfig(tmp_path / "cache", 3600, 100 * 1024**2, 0.9, 1, 25)
    config = AppConfig(ProxyConfig("127.0.0.1", 8080), matching, processing, cache_config)
    cache = CacheStore(cache_config)
    cache.initialize()
    instance = ImageProxyAddon(
        config,
        matcher=UrlMatcher(matching),
        processor=WatermarkProcessor(processing),
        cache=cache,
    )
    yield instance
    if instance.maintenance_task is not None:
        await instance.shutdown()
    else:
        cache.close()


@pytest.mark.asyncio
async def test_lifecycle_runs_startup_and_periodic_cleanup_without_leaking_task(
    lifecycle_addon, monkeypatch
) -> None:
    calls = 0
    original_cleanup = lifecycle_addon.cache.cleanup

    def counted_cleanup():
        nonlocal calls
        calls += 1
        return original_cleanup()

    monkeypatch.setattr(lifecycle_addon.cache, "cleanup", counted_cleanup)
    await lifecycle_addon.start()
    await asyncio.sleep(1.1)
    await lifecycle_addon.shutdown()
    assert calls >= 2
    assert lifecycle_addon.maintenance_task is None
```

Make `CacheStore.close()` idempotent so normal mitmproxy teardown is safe after partial startup.

- [ ] **Step 2: Run focused tests and verify RED**

Run `uv run pytest tests/test_addon.py -q`.

Expected: cache-hit response is absent and concurrent processing count is 2.

- [ ] **Step 3: Implement request-time hits and per-key coordination**

On a matching eligible request, call `cache.get` in a thread. On `CacheHit`, synthesize status 200 with stored allowlisted headers plus authoritative `Content-Type`; log `CACHE_HIT`. A `CacheError` logs `FALLBACK` and continues upstream as a miss.

Implement a keyed lock pool with an `asyncio.Lock` protecting a dictionary of `{key: (lock, user_count)}`. Increment users before waiting; decrement in `finally`; remove only when the count reaches zero. In the response hook, acquire the keyed lock, recheck cache, and use the just-created artifact if another flow won the race. Only the winner invokes the processor and `put`.

- [ ] **Step 4: Implement startup/periodic/shutdown maintenance**

`start()` runs `cache.cleanup` once in a thread and creates one cleanup loop task. The loop waits `cleanup_interval_seconds`, runs cleanup, and logs one `EVICTED` record when counts are nonzero. `shutdown()` cancels/awaits the loop, shuts down the bounded `ThreadPoolExecutor(wait=True)`, and closes the cache. Cancellation is normal and must not log `FALLBACK`.

Pass the executor explicitly to `loop.run_in_executor` so `processing.workers` is the actual bound; do not rely on asyncio's global default pool.

- [ ] **Step 5: Verify cache/concurrency/lifecycle behavior**

Run:

```bash
uv run pytest tests/test_addon.py -q
uv run pytest -q
```

Expected: one processor call for duplicate concurrent misses, cache hits have complete browser headers, and all tasks shut down cleanly without pending-task warnings.

- [ ] **Step 6: Commit cache-aware orchestration**

```bash
git add src/image_proxy/addon.py tests/test_addon.py
git commit -m "feat: serve and maintain processed image cache"
```

---

### Task 8: mitmdump Entrypoint and Validated Native Runner

**Files:**
- Create: `src/image_proxy/mitm_script.py`
- Create: `src/image_proxy/runner.py`
- Create: `tests/test_runner.py`

**Interfaces:**
- Consumes: `load_config` and `ImageProxyAddon`.
- Produces: `build_mitmdump_command(config_path: Path, config: AppConfig, executable: str, script_path: Path) -> list[str]`.
- Produces: console command `image-proxy --config PATH`.

- [ ] **Step 1: Write failing command-construction tests**

Create `tests/test_runner.py`:

```python
from pathlib import Path

from image_proxy.config import load_config
from image_proxy import runner
from image_proxy.runner import build_mitmdump_command


def test_build_command_uses_validated_listener_and_absolute_paths(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(Path("config.example.yaml").read_text())
    config = load_config(config_path)
    command = build_mitmdump_command(
        config_path, config, "/venv/bin/mitmdump", Path("/package/mitm_script.py")
    )
    assert command == [
        "/venv/bin/mitmdump",
        "--listen-host", "0.0.0.0",
        "--listen-port", "8080",
        "--set", f"image_proxy_config={config_path.resolve()}",
        "-s", "/package/mitm_script.py",
    ]


def test_main_reports_invalid_config(capsys) -> None:
    assert runner.main(["--config", "missing.yaml"]) == 2
    assert "missing.yaml" in capsys.readouterr().err


def test_main_reports_missing_mitmdump(tmp_path: Path, monkeypatch, capsys) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(Path("config.example.yaml").read_text())
    monkeypatch.setattr(runner.shutil, "which", lambda _: None)
    assert runner.main(["--config", str(config_path)]) == 2
    assert "uv sync --all-extras" in capsys.readouterr().err
```

- [ ] **Step 2: Run runner tests and verify RED**

Run `uv run pytest tests/test_runner.py -q`.

Expected: import fails because `image_proxy.runner` does not exist.

- [ ] **Step 3: Implement the runner and mitmproxy lifecycle hooks**

`runner.main` parses only `--config`, resolves it, validates before subprocess launch, locates `mitmdump` with `shutil.which`, builds the command exactly as tested, and returns `subprocess.run(command, check=False).returncode`. It catches `ConfigError` and `FileNotFoundError`, prints to stderr, and returns 2; `KeyboardInterrupt` returns 130.

In `mitm_script.py`, instantiate exactly one addon:

```python
from image_proxy.addon import ImageProxyAddon

addons = [ImageProxyAddon()]
```

The addon's mitmproxy hooks must:

- register `image_proxy_config` in `load(loader)`;
- load/configure dependencies when that option changes;
- reject an empty/missing path with `OptionsError`;
- call `start()` from `running()`;
- call `shutdown()` from `done()` without swallowing unexpected errors.

- [ ] **Step 4: Verify runner and importability**

Run:

```bash
uv run pytest tests/test_runner.py tests/test_addon.py -q
uv run image-proxy --help
uv run python -c "from image_proxy.mitm_script import addons; assert len(addons) == 1"
uv run pytest -q
```

Expected: tests pass, help exits 0, and the addon module imports without starting the proxy.

- [ ] **Step 5: Commit the native executable path**

```bash
git add src/image_proxy/mitm_script.py src/image_proxy/runner.py tests/test_runner.py src/image_proxy/addon.py
git commit -m "feat: add validated mitmdump runner"
```

---

### Task 9: Live HTTP/HTTPS Smoke Test and Android Operations Guide

**Files:**
- Create: `tests/smoke/test_live_proxy.py`
- Create: `README.md`
- Modify: `config.example.yaml` only if the final test reveals a documented-default mismatch.

**Interfaces:**
- Consumes: the installed package/addon, mitmdump CA generation, and the example configuration.
- Produces: `uv run pytest -m smoke tests/smoke/test_live_proxy.py -q` as the PC end-to-end check.
- Produces: complete manual Android Chrome acceptance procedure.

- [ ] **Step 1: Write an opt-in failing live smoke test**

The test must use only loopback interfaces and temporary directories. Generate a temporary self-signed localhost origin certificate with the `cryptography` package already brought by mitmproxy, start a threaded HTTPS origin serving a known JPEG, write a temporary matching config, and launch mitmdump with:

```text
--listen-host 127.0.0.1
--listen-port <free-port>
--set confdir=<temporary-ca-directory>
--set ssl_insecure=true
--set image_proxy_config=<temporary-config>
-s <absolute-mitm-script-path>
```

Use this concrete structure in `tests/smoke/test_live_proxy.py`; small helper extraction is allowed as long as the assertions and bounded cleanup remain identical:

```python
import datetime as dt
import ipaddress
import shutil
import socket
import ssl
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path

import pytest
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from PIL import Image

import image_proxy.mitm_script


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for_port(port: int, ca_path: Path | None = None) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                if ca_path is None or ca_path.exists():
                    return
        except OSError:
            pass
        time.sleep(0.05)
    raise AssertionError(f"proxy port {port} did not become ready")


def origin_certificate(directory: Path) -> tuple[Path, Path]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = dt.datetime.now(dt.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=1))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = directory / "origin.crt", directory / "origin.key"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


def start_origin(directory: Path, tls: bool, body: bytes):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path != "/manga/page.jpg":
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    if tls:
        cert_path, key_path = origin_certificate(directory)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert_path, key_path)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def stop_origin(server: ThreadingHTTPServer, thread: threading.Thread) -> None:
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
    assert not thread.is_alive()


@pytest.mark.smoke
@pytest.mark.parametrize("tls", [False, True], ids=["http", "https"])
def test_live_proxy_processes_then_serves_cache_without_origin(tmp_path: Path, tls: bool) -> None:
    source_buffer = BytesIO()
    Image.new("RGB", (320, 480), "white").save(source_buffer, "JPEG")
    source = source_buffer.getvalue()
    origin, origin_thread = start_origin(tmp_path, tls, source)
    origin_stopped = False

    proxy_port = free_port()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "proxy": {"host": "127.0.0.1", "port": proxy_port},
                "matching": {"domains": ["127.0.0.1"], "url_regex": []},
                "processing": {
                    "text": "UPSCALED", "jpeg_quality": 90, "webp_quality": 90,
                    "max_source_mb": 30, "max_pixels": 80_000_000, "workers": 2,
                },
                "cache": {
                    "directory": str(tmp_path / "cache"), "ttl_hours": 1,
                    "max_size_gb": 1, "low_watermark_ratio": 0.9,
                    "cleanup_interval_minutes": 10, "eviction_batch_size": 25,
                },
            }
        )
    )
    confdir = tmp_path / "mitmproxy"
    script_path = Path(image_proxy.mitm_script.__file__).resolve()
    executable = shutil.which("mitmdump")
    assert executable is not None
    log_file = (tmp_path / "mitmdump.log").open("wb")
    process = subprocess.Popen(
        [
            executable,
            "--listen-host", "127.0.0.1",
            "--listen-port", str(proxy_port),
            "--set", f"confdir={confdir}",
            "--set", "ssl_insecure=true",
            "--set", f"image_proxy_config={config_path}",
            "-s", str(script_path),
        ],
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    try:
        ca_path = confdir / "mitmproxy-ca-cert.pem" if tls else None
        wait_for_port(proxy_port, ca_path)
        scheme = "https" if tls else "http"
        url = f"{scheme}://127.0.0.1:{origin.server_port}/manga/page.jpg"
        first, second = tmp_path / "first.jpg", tmp_path / "second.jpg"
        curl = [
            "curl", "--fail", "--silent", "--show-error", "--noproxy", "",
            "--proxy", f"http://127.0.0.1:{proxy_port}",
        ]
        if tls:
            curl.extend(["--cacert", str(ca_path)])
        subprocess.run([*curl, url, "--output", str(first)], check=True)
        stop_origin(origin, origin_thread)
        origin_stopped = True
        subprocess.run([*curl, url, "--output", str(second)], check=True)

        assert first.read_bytes() == second.read_bytes()
        assert first.read_bytes() != source
        with Image.open(first) as processed:
            assert processed.format == "JPEG"
            red_pixels = sum(
                1
                for red, green, blue in processed.convert("RGB").getdata()
                if red > 180 and green < 120 and blue < 120
            )
            assert red_pixels > 50
    finally:
        if not origin_stopped:
            stop_origin(origin, origin_thread)
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        log_file.close()
```

- [ ] **Step 2: Run smoke test and verify RED**

Run:

```bash
uv run pytest -m smoke tests/smoke/test_live_proxy.py -q
```

Expected: fails at the first missing lifecycle/command assumption or image assertion; record the exact failure before changing production code.

- [ ] **Step 3: Make only the minimal integration corrections needed for GREEN**

Correct package paths, hook signatures, option parsing, or response handling exposed by the live process. For every production correction, first narrow the smoke failure into a focused regression test in the corresponding unit test file, verify that focused test fails, then apply the correction and rerun it.

- [ ] **Step 4: Write the operations and Android guide**

`README.md` must contain copy-paste commands for:

```bash
uv sync --all-extras
cp config.example.yaml config.yaml
uv run image-proxy --config config.yaml
hostname -I
```

Document:

- editing `matching.domains` and `matching.url_regex` safely;
- TTL/LRU settings and the SQLite `last_accessed_at` field;
- firewall port 8080 with distro-neutral wording plus UFW example;
- Android Wi-Fi manual proxy host/port setup;
- visiting `http://mitm.it` in Chrome and installing the Android CA certificate;
- the Android security warning and trusted-LAN-only nature of `0.0.0.0` with no authentication;
- how to recognize `CACHE_HIT`, `CACHE_MISS`, `PROCESSED`, `FALLBACK`, and `EVICTED` logs;
- clearing `data/cache` only while the proxy is stopped;
- certificate pinning/user-CA limitations outside Chrome;
- current JPEG/WebP-only scope and future `ImageProcessor` replacement point.

- [ ] **Step 5: Run fresh final verification**

Run all commands from a clean stopped-proxy state:

```bash
uv sync --all-extras
uv run pytest -q
uv run pytest -m smoke tests/smoke/test_live_proxy.py -q
uv run image-proxy --help
git diff --check
git status --short
```

Expected: dependency sync exits 0; default and smoke suites report zero failures; CLI help exits 0; diff check is empty; status contains only the intended Task 9 changes.

- [ ] **Step 6: Commit documentation and end-to-end proof**

```bash
git add README.md tests/smoke/test_live_proxy.py config.example.yaml src tests pyproject.toml uv.lock
git commit -m "docs: add Android setup and live proxy verification"
```

---

## Completion Checklist

- [ ] Re-read `docs/superpowers/specs/2026-08-13-image-mitm-proxy-design.md` and map every acceptance criterion to a passing automated or documented manual test.
- [ ] Confirm every new production function was introduced after a focused test failed for the expected missing behavior.
- [ ] Confirm no log assertion or emitted log includes authorization, cookies, response bytes, request bodies, or query values.
- [ ] Confirm cache metadata records UTC creation, expiration, and last-access timestamps and that only last access changes on hits.
- [ ] Confirm cached CORS headers are allowlisted and stale representation headers are absent.
- [ ] Confirm live HTTPS interception uses a generated mitmproxy CA and the second request succeeds with the origin stopped.
- [ ] Perform the Android Chrome manual test on the trusted LAN and record the tested Android version, Chrome version, PC LAN address, and matching test URL in the final handoff without committing private URLs or IPs.
