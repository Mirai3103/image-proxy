# Image MITM Proxy

Native Python/mitmproxy image proxy for a trusted LAN. Android Chrome can use
the PC as its manual HTTP proxy, trust the generated mitmproxy CA, and receive
matching JPEG/WebP manga images automatically upscaled using **ComfyUI** (or a
fallback watermark engine). Processed images are cached on disk so repeat
requests can be served without contacting the origin.

This proxy listens on `0.0.0.0:8080` by default and has no authentication.
Run it only on a trusted LAN. Do not expose it to the public internet.

## Features

- **AI Upscaling via ComfyUI**: Sends matching manga/webtoon pages to a local or
  remote ComfyUI server, applies model upscaling (e.g. `2x-AnimeSharpV3.pth`),
  and re-encodes to optimized WebP/JPEG for fast mobile delivery.
- **Selective TLS Interception**: Only intercepts domain hosts configured in the
  allowlist (`matching.domains`). All other HTTPS traffic passes through as an
  untouched tunnel.
- **Fail-Open Safe**: If ComfyUI is busy, offline, or times out, the proxy
  gracefully falls back to serving the original upstream image.
- **SQLite + Disk LRU Caching**: Fast disk-backed cache with configurable TTL
  and LRU size eviction to avoid repeated upscaling.

## PC setup

1. Ensure your ComfyUI server is running (e.g., at `http://127.0.0.1:8188`).
2. Clone this repository and sync dependencies:

```bash
uv sync --all-extras
cp config.example.yaml config.yaml
uv run image-proxy --config config.yaml
hostname -I  # Or 'ip addr' to check your LAN IP
```

Use one of the LAN addresses (e.g. `192.168.1.3`) as the Android proxy host.
Keep the terminal running while Android Chrome is using the proxy.

To run the local automated test suite:

```bash
uv run pytest
uv run pytest -m smoke tests/smoke/test_live_proxy.py -q
```

## Workflows

The repository includes pre-configured ComfyUI workflows in the `workflows/` directory:

- `workflows/upscale_workflow.json`: Full workflow graph for drag-and-drop into
  the ComfyUI Web UI.
- `workflows/upscale_workflow_api.json`: Node structure used by the proxy API
  (`LoadImage` -> `UpscaleModelLoader` -> `ImageUpscaleWithModel` -> `SaveImage`).

## Configuration

Edit `config.yaml` before startup:

```yaml
proxy:
  host: 0.0.0.0
  port: 8080

matching:
  domains:
    - "zs.wtcdn.xyz"
    - "img01.manga18fx.com"
  url_regex:
    - "chapter.*\\.webp$"
    - "^https://img01\\.manga18fx\\.com/(upload|online)/.*\\.(jpe?g|webp)(\\?|$)"

processing:
  engine: "comfyui"           # "comfyui" or "watermark"
  jpeg_quality: 90
  webp_quality: 90
  max_source_mb: 30
  max_pixels: 80000000
  workers: 1                  # Concurrent GPU workers
  comfyui:
    server_url: "http://127.0.0.1:8188"
    model_name: "2x-AnimeSharpV3.pth"
    timeout_seconds: 45

cache:
  directory: "./data/cache"
  ttl_hours: 168
  max_size_gb: 10
  low_watermark_ratio: 0.90
  cleanup_interval_minutes: 10
  eviction_batch_size: 25
```

### Matching Rules
`matching.domains` is a required TLS interception allowlist of hostname globs.
Within an allowed host, `matching.url_regex` is searched against the full URL.
Only static JPEG/JPG and WebP responses are processed. Non-image content and
unmatched domains pass through untouched.

## Cache behavior and maintenance

Defaults store cache data under `./data/cache`.
TTL is absolute from creation time. A cache hit updates the SQLite
`last_accessed_at` field for LRU eviction. When the cache exceeds `max_size_gb`,
cleanup deletes oldest-accessed entries in batches until size reaches
`max_size_gb * low_watermark_ratio`.

To clear the cache, stop the proxy first, then remove the cache directory:

```bash
rm -rf data/cache
```

## Android Chrome setup

1. Connect the Android device to the same Wi-Fi/LAN as the PC.
2. In Android Wi-Fi settings, edit the current network.
3. Set proxy mode to **Manual**.
4. Set proxy host name to your PC LAN address (e.g. `192.168.1.3`).
5. Set proxy port to `8080`.
6. Save the Wi-Fi settings.
7. Open Chrome on Android and visit:

   ```text
   http://mitm.it
   ```

8. Download the Android CA certificate from the mitmproxy page and install it.
9. Visit a supported manga chapter in Chrome. Images will be automatically
   upscaled by ComfyUI and cached.

## Logs

Watch the proxy terminal:

- `CACHE_MISS`: the request matched and upstream image was fetched.
- `PROCESSED`: an upstream image was transformed by ComfyUI and cached.
- `CACHE_HIT`: a fresh cached upscaled artifact was served directly.
- `FALLBACK`: proxy or ComfyUI processing failed and original upstream response
  was served safely.
- `EVICTED`: cleanup removed expired or LRU cache files.

