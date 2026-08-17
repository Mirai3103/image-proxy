# Image MITM Proxy (Manga Translate & Upscale)

A high-performance Python/mitmproxy image proxy designed for a trusted LAN. It intercepts manga and webtoon image requests from mobile devices (Android Chrome, tablets) and runs an **ordered ML pipeline** on each page — e.g. **translate then upscale** — using [Koharu](https://koharu.rs) for translation/redrawing and [ComfyUI](https://github.com/comfyanonymous/ComfyUI) for AI super-resolution. Processed images are cached on disk to serve subsequent requests instantly without contacting the origin CDN or re-running the GPU.

```mermaid
flowchart LR
    Client["📱 Android Chrome<br/>(Wi-Fi Proxy)"]
    Proxy["⚡ Image MITM Proxy<br/>(mitmproxy + asyncio)"]
    Cache[("💾 SQLite + Disk Cache<br/>(Instant CACHE_HIT)")]
    Koharu["🗨️ Koharu Server<br/>(translate + redraw)"]
    ComfyUI["🎨 ComfyUI Server<br/>(AI super-resolution)"]
    CDN["🌐 Manga CDN<br/>(asurascans, truyenvua, etc.)"]

    Client -->|1. GET Image| Proxy
    Proxy -->|2. Check Cache| Cache
    Cache -.->|CACHE_HIT: Return Image| Client
    Proxy -->|3. CACHE_MISS: Fetch Original| CDN
    CDN -->|4. Raw Image Bytes| Proxy
    Proxy -->|5. Translate<br/>(detect+OCR+inpaint+render)| Koharu
    Koharu -->|6. Translated PNG| Proxy
    Proxy -->|7. Upscale<br/>(ESRGAN/RCAN model)| ComfyUI
    ComfyUI -->|8. Upscaled PNG| Proxy
    Proxy -->|9. Re-encode WebP/JPEG| Cache
    Proxy -->|10. Deliver Final Image| Client
```

> [!NOTE]
> This proxy listens on `0.0.0.0:8080` by default with no authentication. Run it only on a trusted LAN. Do not expose it directly to the public internet.

---

## Key Features

- **Ordered ML Pipeline**: Configure any combination of stages — `["translate"]`, `["upscale"]`, or `["translate", "upscale"]` (translate must come before upscale). The proxy validates the source image once, runs stages sequentially, and re-encodes the final output once.
- **Koharu Translation**: Direct integration with koharu-server's `/translate` endpoint, which performs the full Operation::Full pipeline — text detection, OCR, inpainting (source lettering removal), LLM translation, and rendered text re-layout (vertical CJK, RTL support).
- **ComfyUI AI Super-Resolution**: Direct integration with ComfyUI's REST & WebSocket API to upscale images on your local GPU (e.g. `2x-AnimeSharpV3.pth`, `RealESRGAN_x2plus`).
- **Real-Time WebSocket Communication**: Connects to `ws://.../ws` to receive instant notification the millisecond GPU rendering finishes, eliminating polling delay.
- **Best-Effort Translate, Required Upscale**: When the pipeline is `[translate, upscale]` and koharu is unreachable or errors, the proxy logs a warning, skips translation, and feeds the original bytes into the upscale stage — so you still get a high-res page. Upscale (the final stage) failures fall back to the original CDN image.
- **Mobile-Optimized Re-encoding**: Compresses raw engine output (often ~30MB PNG) down to **~1.5 - 2.8MB WebP/JPEG**, saving phone RAM and network bandwidth.
- **Webtoon Long-Strip Handling**: Automatically adapts when processed webtoons exceed the WebP format limit (`16,383 px`) by encoding in JPEG, preserving full 17,000+ px resolution without errors.
- **Selective TLS Interception**: Intercepts only specified manga CDN domains in `matching.domains`. All other HTTPS traffic (social media, private browsing) passes through untouched.
- **Fail-Open Safety**: If a required stage fails or times out, the proxy automatically serves the original image so your reading experience is never interrupted.
- **SQLite + Disk LRU Cache**: Disk-backed cache with configurable TTL and LRU size eviction for instant loading of previously viewed pages. Cache keys are fingerprinted by pipeline configuration, so changing models invalidates correctly.

---

## Pipeline Stages

The `processing.pipeline` field is an ordered list of stage names. Allowed values:

| Stage | Engine | Endpoint | Requires config section |
|-------|--------|----------|-------------------------|
| `watermark` | Pillow (built-in) | — | `text` |
| `translate` | Koharu server | `POST /translate` (multipart `image`) | `koharu` |
| `upscale` | ComfyUI server | `/upload/image` + `/prompt` + `/view` | `comfyui` |

**Validation rules:**
- `watermark` cannot combine with other stages (it's a standalone test/fallback mode).
- When both `translate` and `upscale` are present, `translate` must come before `upscale` (redraw before super-resolution).
- Each ML stage requires its corresponding config section (`koharu` and/or `comfyui`).

---

## ComfyUI Workflows

The repository includes pre-built workflows under the `workflows/` directory:

- [workflows/upscale_workflow.json](workflows/upscale_workflow.json): Complete UI workflow graph that can be dragged and dropped directly into the ComfyUI Web UI.
- [workflows/upscale_workflow_api.json](workflows/upscale_workflow_api.json): The API prompt node structure (`LoadImage` ➔ `UpscaleModelLoader` ➔ `ImageUpscaleWithModel` ➔ `SaveImage`) executed by the proxy adapter.

Supported models placed in `ComfyUI/models/upscale_models/`:
- `2x-AnimeSharpV3.pth` *(Default)*
- `2x-AnimeSharpV4_Fast_RCAN_PU.safetensors`
- `RealESRGAN_x2plus.pth`
- `4x-UltraSharp.pth` (or any compatible ESRGAN/RCAN/SwinIR model)

---

## Quick Start Guide

### 1. Start the ML servers

**Koharu** (translate + redraw) — see [koharu/docs](https://koharu.rs) for build instructions:
```bash
cd ../koharu
cargo run -p koharu-server -- --bind 0.0.0.0 --port 8383
```

**ComfyUI** (upscale) — start on your PC or local GPU server:
```bash
python main.py --port 8188 --listen 0.0.0.0
```

### 2. Setup and Run Proxy
In this repository:
```bash
# Install dependencies (including mitmproxy and websockets)
uv sync --all-extras

# Create local configuration
cp config.example.yaml config.yaml

# Run the proxy
uv run image-proxy --config config.yaml

# Find your LAN IP address
ip addr  # or 'hostname -I'
```

### 3. Setup Android Device
1. Connect Android to the **same Wi-Fi network** as the PC.
2. In Android Wi-Fi settings, edit network ➔ Set **Proxy** to **Manual**.
3. **Proxy Host**: Enter your PC LAN IP (e.g. `192.168.1.3`).
4. **Proxy Port**: `8080`.
5. Open Chrome on Android and navigate to:
   ```text
   http://mitm.it
   ```
6. Download and install the **Android CA Certificate** (under Settings ➔ Security ➔ CA Certificate).
7. Open your favorite manga reader site in Chrome. Images will be automatically translated and upscaled!

---

## Configuration Reference (`config.yaml`)

```yaml
proxy:
  host: 0.0.0.0
  port: 8080

matching:
  # Intercept only these manga CDN domains for TLS decryption
  domains:
    - "zs.wtcdn.xyz"
    - "img01.manga18fx.com"
    - "*.truyenvua.com"
    - "cdn.asurascans.com"
  # Target matching image URLs within allowed domains
  url_regex:
    - "/chapter/.*\\.webp(\\?|$)"
    - "^https://img01\\.manga18fx\\.com/(upload|online)/.*\\.(jpe?g|webp)(\\?|$)"
    - "^https?://.*\\.truyenvua\\.com/.*\\.(jpe?g|webp|png)(\\?|$)"
    - "^https?://cdn\\.asurascans\\.com/asura-images/chapters/[A-Za-z0-9_\\-]+/[A-Za-z0-9_\\-]+/.*\\.(jpe?g|webp|png)(\\?|$)"

processing:
  # Ordered pipeline of image stages. Allowed: "watermark", "translate", "upscale".
  # "watermark" cannot combine with others. "translate" must come before "upscale".
  # Examples:
  #   pipeline: ["watermark"]              # standalone test mode
  #   pipeline: ["translate"]              # translate only
  #   pipeline: ["upscale"]                # upscale only
  #   pipeline: ["translate", "upscale"]   # translate then upscale
  pipeline:
    - "translate"
    - "upscale"
  text: "UPSCALED"             # only used when pipeline includes "watermark"
  jpeg_quality: 90             # Quality for JPEG responses (1-100)
  webp_quality: 90             # Quality for WebP responses (1-100)
  max_source_mb: 30            # Source image safety byte limit
  max_pixels: 80000000         # Pixel limit safety guard
  workers: 1                   # GPU concurrency worker pool
  # Required when pipeline includes "translate" (koharu-server)
  koharu:
    server_url: "http://127.0.0.1:8383"
    timeout_seconds: 120
  # Required when pipeline includes "upscale" (ComfyUI)
  comfyui:
    server_url: "http://127.0.0.1:8188"
    model_name: "2x-AnimeSharpV3.pth"
    timeout_seconds: 45

cache:
  directory: "./data/cache"
  ttl_hours: 168               # 7 days
  max_size_gb: 10              # Maximum cache size
  low_watermark_ratio: 0.90    # Evict down to 90% when full
  cleanup_interval_minutes: 10
  eviction_batch_size: 25
```

---

## Terminal Logs

Watch the proxy terminal to observe real-time behavior:

**Request flow:**
- `CACHE_MISS`: Request matched; original image fetched from CDN.
- `PROCESS.start host=... path=... bytes=N encoding=... content_type=...`: Begin processing.
- `source.inspect bytes=N format=WEBP size=900x16000 pixels=...`: Source image validated.
- `pipeline.stage.start 1/2=KoharuProcessor bytes_in=N`: Entering a pipeline stage.
- `pipeline.stage.done 1/2=KoharuProcessor bytes_out=N elapsed=35.7s`: Stage completed.
- `pipeline.stage.skipped 1/2=KoharuProcessor error=...`: Non-final stage failed, skipped.
- `pipeline.stage.failed 2/2=ComfyUIProcessor error=...`: Final stage failed, propagating.
- `koharu.translate.start url=... bytes_in=N timeout=120s`: Koharu HTTP call begins.
- `koharu.translate.done status=200 bytes_out=N elapsed=35.7s`: Koharu HTTP call returns.
- `comfyui.upscale.start bytes_in=N` / `comfyui.upscale.done bytes_out=N elapsed=...`: ComfyUI workflow.
- `output.encode bytes_in=N bytes_out=N format=WEBP size=... elapsed=...`: Final re-encode.
- `PROCESS.done bytes_out=N elapsed=...`: Full pipeline finished.
- `PROCESSED host=... path=... format=WEBP bytes=N`: Image delivered to client.
- `CACHE_HIT`: Image served immediately from local SSD cache (0ms GPU load).
- `BYPASS`: URL did not match or response was not eligible; original image delivered.
- `FALLBACK`: Pipeline failed; original CDN image delivered seamlessly.
- `EVICTED`: LRU cache cleanup freed expired or surplus disk space.

---

## Running Tests

Run the complete test suite:

```bash
uv run pytest
uv run pytest -m smoke tests/smoke/test_live_proxy.py -q
```

Test coverage:
- `tests/test_config.py` — pipeline validation, stage ordering, required sections.
- `tests/test_processor.py` — KoharuProcessor, ComfyUIProcessor, PipelineProcessor, encoding.
- `tests/test_addon.py` — end-to-end flow including pipeline `translate → upscale` and skip-on-translate-failure.
