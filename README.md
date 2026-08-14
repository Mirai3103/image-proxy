# Image MITM Proxy

Native Python/mitmproxy image proxy for a trusted LAN. Android Chrome can use
the PC as its manual HTTP proxy, trust the generated mitmproxy CA, and receive
matching JPEG/WebP manga images with an `UPSCALED` watermark. Processed images
are cached on disk so repeat requests can be served without contacting the
origin.

This proxy listens on `0.0.0.0:8080` by default and has no authentication.
Run it only on a trusted LAN. Do not expose it to the public internet.

## PC setup

From this repository:

```bash
uv sync --all-extras
cp config.example.yaml config.yaml
uv run image-proxy --config config.yaml
hostname -I
```

Use one of the LAN addresses printed by `hostname -I` as the Android proxy
host. Keep the terminal running while Android Chrome is using the proxy.

To run the local end-to-end smoke test without Android:

```bash
uv run pytest -m smoke tests/smoke/test_live_proxy.py -q
```

The smoke test starts temporary loopback HTTP and HTTPS origins, launches
`mitmdump`, processes a JPEG once, stops the origin, and verifies the second
request is served from cache.

## Configure matching safely

Edit `config.yaml` before startup:

```yaml
matching:
  domains:
    - "*.example-cdn.com"
  url_regex:
    - "/manga/"
    - "\\.(jpe?g|webp)(\\?|$)"
```

`matching.domains` contains hostname globs only, not full URLs. Prefer the
narrowest CDN hostnames you can identify. `matching.url_regex` is searched
against the full URL, including the query string, so avoid broad expressions
that could intercept unrelated private or authenticated images. A hostname
glob or a URL regex match is enough to process a request.

Only static JPEG/JPG and WebP responses are processed. GIF, PNG, SVG, AVIF,
animated images, video, and non-image content pass through unchanged.

## Cache behavior and maintenance

Defaults store cache data under `./data/cache`:

```yaml
cache:
  directory: "./data/cache"
  ttl_hours: 168
  max_size_gb: 10
  low_watermark_ratio: 0.90
  cleanup_interval_minutes: 10
  eviction_batch_size: 25
```

TTL is absolute from creation time. A cache hit updates the SQLite
`last_accessed_at` field for LRU eviction, but it does not extend expiry.
When the cache exceeds `max_size_gb`, cleanup deletes oldest-accessed entries
in batches until size reaches `max_size_gb * low_watermark_ratio`.

To clear the cache, stop the proxy first, then remove the configured cache
directory:

```bash
rm -rf data/cache
```

Do not delete `data/cache` while the proxy is running.

The local SQLite metadata stores source URLs because the full URL is part of
cache identity. Treat the cache directory as local private data, especially if
matching URLs contain signed parameters.

## Firewall

Allow inbound TCP traffic to the configured proxy port only from trusted local
networks. The exact command depends on your Linux firewall. For UFW:

```bash
sudo ufw allow 8080/tcp
```

If you changed `proxy.port`, open that port instead. Close the rule when you no
longer need LAN devices to connect.

## Android Chrome setup

1. Connect the Android device to the same trusted Wi-Fi/LAN as the PC.
2. In Android Wi-Fi settings, edit the current network.
3. Set proxy mode to Manual.
4. Set proxy host name to the PC LAN address from `hostname -I`.
5. Set proxy port to `8080`, or your configured `proxy.port`.
6. Save the Wi-Fi settings.
7. Open Chrome on Android and visit:

   ```text
   http://mitm.it
   ```

8. Download the Android certificate from the mitmproxy page and install it as a
   CA certificate when Android prompts.
9. Android will show a security warning for user-installed CAs. That is
   expected for HTTPS interception. Remove the certificate and manual proxy
   setting when testing is complete.
10. In Chrome, visit a URL that matches your `matching.domains` or
    `matching.url_regex`. A matching JPEG/WebP should visibly show the centered
    red `UPSCALED` watermark. Reloading the same image should produce a
    `CACHE_HIT` log.

Certificate pinning and user-CA restrictions are outside this version's
support. Android Chrome can use the installed user CA; many apps and some
sites may reject user-installed CAs or pin their certificates, so they will
not be interceptable through this proxy.

## Logs

Watch the proxy terminal:

- `CACHE_MISS`: the request matched but no fresh cached artifact was served.
- `PROCESSED`: an upstream image was transformed and stored.
- `CACHE_HIT`: a fresh cached artifact was served without the upstream image.
- `FALLBACK`: proxy-specific processing/cache work failed and the original
  upstream response was allowed through when possible.
- `EVICTED`: cleanup removed expired, LRU, or orphaned cache files.

Logs use host and path only. They do not include cookies, authorization
headers, request bodies, response bodies, or URL query values.

## Replacing the processor later

The current processor is a Pillow watermark implementation for static JPEG and
WebP. Future Real-ESRGAN, Real-CUGAN, or ComfyUI support should replace the
`ImageProcessor` implementation while keeping URL matching, mitmproxy
lifecycle, response handling, and cache behavior unchanged.
