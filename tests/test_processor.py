from io import BytesIO

import pytest
from PIL import Image, ImageDraw

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
            for red, green, blue in after.convert("RGB").get_flattened_data()
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
    frames[0].save(
        output, "WEBP", save_all=True, append_images=frames[1:], duration=50
    )

    with pytest.raises(ProcessingError, match="animated"):
        processor().process(output.getvalue(), "image/webp")


def test_fitting_font_keeps_stroked_long_watermark_within_narrow_image() -> None:
    instance = processor()
    image_width = 120
    draw = ImageDraw.Draw(Image.new("RGB", (image_width, 80), "white"))

    font = instance._fitting_font(draw, image_width)
    stroke_width = max(1, font.size // 16)
    left, _, right, _ = draw.textbbox(
        (0, 0), "UPSCALED", font=font, stroke_width=stroke_width
    )

    assert right - left <= image_width * 0.9


def test_fingerprint_changes_when_output_configuration_changes() -> None:
    base = processor().fingerprint
    changed = WatermarkProcessor(
        ProcessingConfig("DIFFERENT", 90, 90, 1024**2, 1_000_000, 2)
    ).fingerprint

    assert changed != base


@pytest.mark.parametrize(
    "config",
    [
        ProcessingConfig("UPSCALED", 90, 90, 2 * 1024**2, 1_000_000, 2),
        ProcessingConfig("UPSCALED", 90, 90, 1024**2, 2_000_000, 2),
        ProcessingConfig("UPSCALED", 90, 90, 1024**2, 1_000_000, 8),
    ],
)
def test_fingerprint_ignores_safety_and_worker_configuration(
    config: ProcessingConfig,
) -> None:
    assert WatermarkProcessor(config).fingerprint == processor().fingerprint


from image_proxy.config import ComfyUIConfig
from image_proxy.processor import ComfyUIProcessor
import json
import urllib.request
import urllib.error


def comfy_processor(
    server_url: str = "http://127.0.0.1:8188",
    model_name: str = "2x-AnimeSharpV3.pth",
    timeout_seconds: float = 45.0,
    jpeg_quality: int = 90,
    webp_quality: int = 88,
    max_source_bytes: int = 1024**2,
    max_pixels: int = 1_000_000,
):
    return ComfyUIProcessor(
        ProcessingConfig(
            text="UPSCALED",
            jpeg_quality=jpeg_quality,
            webp_quality=webp_quality,
            max_source_bytes=max_source_bytes,
            max_pixels=max_pixels,
            workers=1,
            engine="comfyui",
            comfyui=ComfyUIConfig(
                server_url=server_url,
                model_name=model_name,
                timeout_seconds=timeout_seconds,
            ),
        )
    )


def test_comfyui_fingerprint_changes_with_configuration() -> None:
    base = comfy_processor().fingerprint
    changed_model = comfy_processor(model_name="4x-UltraSharp.pth").fingerprint
    changed_server = comfy_processor(server_url="http://192.168.1.10:8188").fingerprint
    changed_quality = comfy_processor(webp_quality=95).fingerprint

    assert changed_model != base
    assert changed_server != base
    assert changed_quality != base


def test_comfyui_process_success_and_reencodes_to_requested_format(monkeypatch) -> None:
    # Prepare dummy responses for ComfyUI HTTP calls
    upscaled_png = image_bytes("PNG", (800, 1200))
    prompt_id = "test-prompt-id"

    class FakeHTTPResponse:
        def __init__(self, data: bytes, status: int = 200):
            self._data = data
            self.status = status

        def read(self) -> bytes:
            return self._data

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            pass

    def fake_urlopen(req, *args, **kwargs):
        url = req.full_url if isinstance(req, urllib.request.Request) else req
        if "/upload/image" in url:
            return FakeHTTPResponse(json.dumps({"name": "uploaded.webp"}).encode("utf-8"))
        elif "/prompt" in url:
            return FakeHTTPResponse(json.dumps({"prompt_id": prompt_id}).encode("utf-8"))
        elif f"/history/{prompt_id}" in url:
            history = {
                prompt_id: {
                    "outputs": {
                        "4": {
                            "images": [
                                {
                                    "filename": "out_0001.png",
                                    "subfolder": "",
                                    "type": "output",
                                }
                            ]
                        }
                    }
                }
            }
            return FakeHTTPResponse(json.dumps(history).encode("utf-8"))
        elif "/view" in url:
            return FakeHTTPResponse(upscaled_png)
        raise ValueError(f"Unexpected URL: {url}")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    source = image_bytes("WEBP", (400, 600))
    result = comfy_processor().process(source, "image/webp")

    assert result.mime_type == "image/webp"
    assert result.format_name == "WEBP"
    with Image.open(BytesIO(result.data)) as img:
        assert img.format == "WEBP"
        assert img.size == (800, 1200)


def test_comfyui_process_fails_on_timeout(monkeypatch) -> None:
    class FakeHTTPResponse:
        def __init__(self, data: bytes):
            self._data = data

        def read(self) -> bytes:
            return self._data

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            pass

    def fake_urlopen(req, *args, **kwargs):
        url = req.full_url if isinstance(req, urllib.request.Request) else req
        if "/upload/image" in url:
            return FakeHTTPResponse(json.dumps({"name": "uploaded.webp"}).encode("utf-8"))
        elif "/prompt" in url:
            return FakeHTTPResponse(json.dumps({"prompt_id": "p-1"}).encode("utf-8"))
        elif "/history/p-1" in url:
            return FakeHTTPResponse(json.dumps({}).encode("utf-8"))  # never finishes
        raise ValueError(url)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    source = image_bytes("JPEG", (100, 100))
    # Using very short timeout
    proc = comfy_processor(timeout_seconds=0.1)
    with pytest.raises(ProcessingError, match="timeout|timed out"):
        proc.process(source, "image/jpeg")


def test_comfyui_process_fails_on_server_error(monkeypatch) -> None:
    def fake_urlopen(req, *args, **kwargs):
        raise urllib.error.URLError("Connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    source = image_bytes("JPEG", (100, 100))
    with pytest.raises(ProcessingError, match="ComfyUI|failed"):
        comfy_processor().process(source, "image/jpeg")


def test_comfyui_process_via_websocket_success(monkeypatch) -> None:
    upscaled_png = image_bytes("PNG", (600, 800))
    prompt_id = "ws-prompt-123"

    class FakeWebSocket:
        def __init__(self):
            self.closed = False

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            self.closed = True

        def recv(self, timeout=None):
            # Return executed event with matching prompt_id
            return json.dumps(
                {
                    "type": "executed",
                    "data": {
                        "prompt_id": prompt_id,
                        "node": "4",
                        "output": {
                            "images": [
                                {
                                    "filename": "ws_out_001.png",
                                    "subfolder": "",
                                    "type": "output",
                                }
                            ]
                        },
                    },
                }
            )

    class FakeHTTPResponse:
        def __init__(self, data: bytes):
            self._data = data

        def read(self) -> bytes:
            return self._data

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            pass

    def fake_urlopen(req, *args, **kwargs):
        url = req.full_url if isinstance(req, urllib.request.Request) else req
        if "/upload/image" in url:
            return FakeHTTPResponse(json.dumps({"name": "uploaded_ws.webp"}).encode("utf-8"))
        elif "/prompt" in url:
            return FakeHTTPResponse(json.dumps({"prompt_id": prompt_id}).encode("utf-8"))
        elif "/view" in url:
            return FakeHTTPResponse(upscaled_png)
        raise ValueError(url)

    monkeypatch.setattr("image_proxy.processor.ws_connect", lambda *args, **kwargs: FakeWebSocket())
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    source = image_bytes("WEBP", (300, 400))
    result = comfy_processor().process(source, "image/webp")

    assert result.mime_type == "image/webp"
    assert result.format_name == "WEBP"
    with Image.open(BytesIO(result.data)) as img:
        assert img.format == "WEBP"
        assert img.size == (600, 800)


def test_comfyui_process_via_websocket_handles_execution_error(monkeypatch) -> None:
    prompt_id = "ws-prompt-err"

    class FakeWebSocket:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            pass

        def recv(self, timeout=None):
            return json.dumps(
                {
                    "type": "execution_error",
                    "data": {
                        "prompt_id": prompt_id,
                        "exception_message": "CUDA out of memory",
                    },
                }
            )

    class FakeHTTPResponse:
        def __init__(self, data: bytes):
            self._data = data

        def read(self) -> bytes:
            return self._data

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            pass

    def fake_urlopen(req, *args, **kwargs):
        url = req.full_url if isinstance(req, urllib.request.Request) else req
        if "/upload/image" in url:
            return FakeHTTPResponse(json.dumps({"name": "uploaded_ws.webp"}).encode("utf-8"))
        elif "/prompt" in url:
            return FakeHTTPResponse(json.dumps({"prompt_id": prompt_id}).encode("utf-8"))
        raise ValueError(url)

    monkeypatch.setattr("image_proxy.processor.ws_connect", lambda *args, **kwargs: FakeWebSocket())
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    source = image_bytes("WEBP", (300, 400))
    with pytest.raises(ProcessingError, match="execution error|failed"):
        comfy_processor().process(source, "image/webp")


