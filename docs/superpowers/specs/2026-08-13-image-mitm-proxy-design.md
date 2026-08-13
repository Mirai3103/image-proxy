# Image MITM Proxy Design

## Goal

Build a native Python MITM proxy that runs on a Linux PC and lets Android
devices on the same trusted LAN use it as their HTTP proxy. The proxy
intercepts matching manga image responses, writes `UPSCALED` over JPEG and
WebP images, caches the processed result, and returns it to Chrome. Android
does not need a custom reader application, but it must trust mitmproxy's CA
certificate for HTTPS interception.

The first version proves the complete interception and image-processing flow.
The watermark processor must be replaceable later by either Real-ESRGAN or a
ComfyUI workflow without changing URL matching, proxy, or cache behavior.

## Scope

### Included

- Native Python 3.10 runtime in the repository's virtual environment.
- mitmproxy/mitmdump as the HTTP and HTTPS interception layer.
- Android Chrome as the primary client.
- LAN listener with no application-level authentication.
- Hostname glob and full-URL regular-expression matching.
- Static JPEG/JPG and WebP response processing with Pillow.
- Configurable filesystem cache with SQLite metadata, absolute TTL, and LRU
  size eviction.
- Fail-open behavior when downloading, decoding, processing, encoding, or
  caching fails.
- Automated unit and integration tests plus documented manual Android setup.

### Excluded

- GIF, PNG, SVG, AVIF, animated images, video, and non-image content.
- Android applications that reject user-installed CAs or implement
  certificate pinning.
- Real-ESRGAN, Real-CUGAN, ComfyUI, and GPU setup in this version.
- Proxy authentication, remote internet exposure, and multi-user isolation.
- A graphical user interface.

## Architecture

The service runs as a mitmproxy addon loaded by `mitmdump`. mitmproxy owns
HTTP, CONNECT, TLS certificate generation, upstream communication, and
response delivery. The addon owns configuration, URL selection, cache
lookups, image transformation, and application logging.

The main boundaries are:

- `Config`: loads and validates YAML before the proxy begins serving.
- `UrlMatcher`: matches the request hostname against configured globs and
  applies configured regular expressions with search semantics to the full
  URL. A URL is selected when any rule matches.
- `ImageProcessor`: accepts source bytes and MIME type and returns processed
  bytes. The v1 implementation uses Pillow; future GPU or ComfyUI adapters
  implement the same boundary.
- `CacheStore`: stores processed files by a hashed key and stores metadata in
  SQLite.
- `ProxyAddon`: coordinates request-time cache hits and response-time
  validation, processing, storage, header repair, and fail-open behavior.

The proxy listens on `0.0.0.0:8080` by default. This intentionally permits any
device on the local network to connect. Documentation must warn that it is
appropriate only for a trusted LAN and must not be exposed to the public
internet.

## Configuration

The repository includes a documented example with these defaults:

```yaml
proxy:
  host: 0.0.0.0
  port: 8080

matching:
  domains:
    - "*.example-cdn.com"
  url_regex:
    - "/manga/"
    - "\\.(jpe?g|webp)(\\?|$)"

processing:
  text: "UPSCALED"
  jpeg_quality: 90
  webp_quality: 90
  max_source_mb: 30
  max_pixels: 80000000
  workers: 2

cache:
  directory: "./data/cache"
  ttl_hours: 168
  max_size_gb: 10
  low_watermark_ratio: 0.90
  cleanup_interval_minutes: 10
  eviction_batch_size: 25
```

Domain globs apply only to the parsed hostname. Regex rules are compiled at
startup and searched against the full URL, including its query string. An
empty rule set matches nothing. Invalid types, values, paths, or regular
expressions stop startup with a concise validation error rather than silently
running with unintended interception behavior.

The processor fingerprint includes every setting that can change output,
including processor identity and version, watermark text, and encoder quality.
Changing one of those values yields a new cache namespace without requiring
the old cache to be manually deleted.

Only `GET` requests without a `Range` header are eligible for cache lookup or
processing. Other methods and partial-content requests pass through unchanged.

## Request and Response Flow

1. `ProxyAddon` evaluates the URL when a request arrives. Non-matching URLs
   continue through mitmproxy without modification.
2. For a matching URL, it derives a cache key from the full URL, processor
   fingerprint, and hashes of the request's `Accept`, `Authorization`, and
   `Cookie` header values. Raw authorization and cookie values are never
   stored. Including these variants prevents a cached private or
   content-negotiated response from being served to a different client.
3. A fresh cache entry returns immediately from the request hook, bypassing
   the upstream CDN. Its `last_accessed_at` value is updated.
4. A cache miss continues upstream normally.
5. Only status 200 responses are candidates. Pillow's decoded format is
   authoritative: only static JPEG and WebP are accepted. An explicit
   `Content-Type` for another format causes pass-through, while a missing or
   generic `application/octet-stream` type may still be accepted when decoded
   content is JPEG or WebP. File extensions alone are not trusted.
6. The image work runs in a bounded worker pool so Pillow does not block other
   proxy flows. Concurrent misses for the same key share per-key coordination
   so only one processed cache artifact is committed.
7. The processor checks compressed byte and decoded pixel limits, decodes the
   image, draws the watermark, and encodes it in the original JPEG or WebP
   format.
8. The cache writes the artifact to a temporary file, atomically renames it,
   and then commits its metadata. Partially written images are never served.
9. The addon replaces the response body and removes representation metadata
   that no longer describes it, including `Content-Length`, `Content-Encoding`,
   `ETag`, `Content-MD5`, and `Digest`. It preserves the correct JPEG or WebP
   content type; mitmproxy computes the delivered length. A small allowlist of
   response headers needed by browser image delivery and CORS is stored with
   the artifact for request-time cache hits.
10. If any interception-specific operation fails, the addon logs `FALLBACK`
    and returns the unmodified upstream response. A cache-hit read failure is
    treated as a miss and its broken entry is removed.

Because a request-time cache hit bypasses upstream validation, cached content
can remain different from the current CDN object until its absolute TTL
expires. This is an intentional performance trade-off for manga images.

## Watermark Processor

The v1 processor draws bold red `UPSCALED` text with a black stroke at the
center of the image. Font size scales with image width and is clamped so the
text stays legible and inside small or large images. JPEG output is encoded as
JPEG and WebP output as WebP, using their separately configured quality values.

The processor rejects source bodies larger than `max_source_mb` and decoded
images larger than `max_pixels`. Unsupported, animated, malformed, or unsafe
images raise a processing error that triggers fail-open response behavior.

The processor interface is byte-oriented and contains no mitmproxy or cache
types. This lets a future Real-ESRGAN or ComfyUI implementation be selected by
configuration while the rest of the system remains unchanged.

## Cache Design

Processed image files are named from a cryptographic hash of the cache key.
SQLite stores:

- cache key and source URL;
- processor fingerprint;
- MIME type and relative artifact path;
- an allowlisted response-header JSON object containing only `Cache-Control`,
  `Expires`, `Access-Control-Allow-Origin`,
  `Access-Control-Allow-Credentials`, `Cross-Origin-Resource-Policy`, and
  `Content-Disposition` when present;
- `size_bytes`;
- `created_at`, `expires_at`, and `last_accessed_at` as UTC timestamps.

TTL is absolute from `created_at`; cache hits update `last_accessed_at` but do
not extend `expires_at`. SQLite uses WAL mode and short transactions. Cache
file and database operations are kept outside mitmproxy's main event path
where they could block unrelated traffic.

Cleanup runs at startup, at the configured interval, and after writes that may
cross the maximum size:

1. Remove expired entries and their files.
2. Recalculate tracked total size.
3. If total size exceeds `max_size_gb`, select entries ordered by oldest
   `last_accessed_at` and delete at most `eviction_batch_size` per transaction.
4. Continue in incremental batches until total size is at or below
   `max_size_gb * low_watermark_ratio`.

Orphan files, missing artifacts, and corrupt metadata are removed during
maintenance or lazily when encountered. Cache maintenance errors are logged
but never prevent proxying the current response.

## Concurrency

Pillow processing uses a bounded worker pool configured by `workers`.
Coordination is scoped to the cache key: unrelated images can process in
parallel, while identical concurrent misses do not commit duplicate artifacts.
SQLite access is serialized where required by its connection model, and no
database transaction is held while an image is decoded or encoded.

## Logging and Privacy

Console logs use the event names `BYPASS`, `CACHE_HIT`, `CACHE_MISS`,
`PROCESSED`, `FALLBACK`, and `EVICTED`, with concise host/path, format, byte
size, timing, and error information where applicable. Cookies, authorization
headers, request bodies, and response bodies are never logged. Full URLs with
query strings are retained in cache metadata because they are part of cache
identity; documentation notes that the local cache database can therefore
contain signed URL parameters.

## Testing

Unit tests cover:

- hostname glob and URL regex matching, including an empty rule set;
- configuration validation and invalid regular expressions;
- JPEG and WebP watermarking, output format, source byte limits, decoded pixel
  limits, and rejection of unsupported or animated input;
- cache hit, absolute TTL, access-time updates, atomic artifact behavior,
  expired cleanup, missing files, and LRU eviction to the low watermark;
- processor fingerprint changes invalidating old output.

Integration tests construct representative mitmproxy flows and cover:

- byte-identical pass-through for non-matches and unsupported formats;
- processing for matching JPEG and WebP responses;
- repaired response headers;
- `GET` eligibility and pass-through for range or non-GET requests;
- request-time cache hits;
- fail-open behavior for processor and cache errors;
- duplicate concurrent requests producing one committed artifact.

A local smoke test runs an HTTP and HTTPS image origin through the live proxy.
The final manual acceptance test uses Android Chrome configured with the PC's
LAN address and mitmproxy CA.

## Operations

The README documents virtual-environment setup, dependency installation,
configuration, proxy startup, firewall port 8080, locating the PC LAN address,
Android Wi-Fi proxy settings, and installation of the mitmproxy CA through
`http://mitm.it`. It also explains Android's certificate warning, Chrome test
steps, cache inspection, and the fact that pinned apps or apps that reject
user-installed CAs are outside this version's support.

## Acceptance Criteria

- A matching JPEG or WebP loaded in Android Chrome visibly contains the
  centered `UPSCALED` watermark.
- A non-matching URL and any unsupported format pass through byte-for-byte.
- Repeating a matching request produces `CACHE_HIT`, avoids the upstream
  request, and updates `last_accessed_at`.
- An expired entry is not served and is replaced after successful processing.
- When cache size exceeds the configured maximum, incremental LRU cleanup
  reduces it to the configured low watermark.
- Processing, encoding, cache, or maintenance failures do not break page image
  loading when the upstream response is otherwise usable.
- Automated tests pass, and the documented live proxy smoke test succeeds
  before the Android acceptance test.
