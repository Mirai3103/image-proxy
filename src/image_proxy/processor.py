"""Byte-oriented image watermark processing."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
import json
from typing import Protocol, runtime_checkable
import warnings

from PIL import Image, ImageDraw, ImageFont

import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from websockets.sync.client import connect as ws_connect

from image_proxy.config import ComfyUIConfig, ProcessingConfig



class ProcessingError(RuntimeError):
    """Raised when image input cannot be processed safely."""


@dataclass(frozen=True)
class ProcessedImage:
    """The processed image bytes and their authoritative media information."""

    data: bytes
    mime_type: str
    format_name: str


@runtime_checkable
class ImageProcessor(Protocol):
    """Interface implemented by replaceable image-processing adapters."""

    @property
    def fingerprint(self) -> str:
        """A stable identifier for output-affecting configuration."""

    def process(self, data: bytes, content_type: str | None) -> ProcessedImage:
        """Process one encoded image."""


_ALLOWED_CONTENT_TYPES = {
    "",
    "application/octet-stream",
    "image/jpeg",
    "image/jpg",
    "image/webp",
}
_FORMAT_DETAILS = {
    "JPEG": ("image/jpeg", "JPEG"),
    "WEBP": ("image/webp", "WEBP"),
}


def _validate_content_type(content_type: str | None) -> None:
    normalized = "" if content_type is None else content_type.split(";", 1)[0].strip().lower()
    if normalized not in _ALLOWED_CONTENT_TYPES:
        raise ProcessingError("unsupported content type")


def _format_details(format_name: str | None) -> tuple[str, str]:
    try:
        return _FORMAT_DETAILS[format_name or ""]
    except KeyError as exc:
        raise ProcessingError("unsupported image format") from exc


def _has_alpha(image: Image.Image) -> bool:
    return "A" in image.getbands() or "transparency" in image.info


class WatermarkProcessor:
    """Draw a visible watermark over safe static JPEG and WebP images."""

    def __init__(self, config: ProcessingConfig) -> None:
        self._config = config
        fingerprint_data = {
            "jpeg_quality": config.jpeg_quality,
            "name": "pillow-watermark",
            "text": config.text,
            "version": 1,
            "webp_quality": config.webp_quality,
        }
        encoded = json.dumps(
            fingerprint_data, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        self._fingerprint = sha256(encoded).hexdigest()

    @property
    def fingerprint(self) -> str:
        return self._fingerprint

    def process(self, data: bytes, content_type: str | None) -> ProcessedImage:
        _validate_content_type(content_type)
        if len(data) > self._config.max_source_bytes:
            raise ProcessingError("source byte limit exceeded")

        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(BytesIO(data)) as image:
                    format_name = image.format
                    mime_type, encoder_format = _format_details(format_name)
                    if getattr(image, "n_frames", 1) != 1:
                        raise ProcessingError("animated images are not supported")
                    width, height = image.size
                    if width * height > self._config.max_pixels:
                        raise ProcessingError("pixel limit exceeded")

                    target_format = encoder_format
                    target_mime = mime_type
                    if target_format == "WEBP" and (width > 16383 or height > 16383):
                        target_format = "JPEG"
                        target_mime = "image/jpeg"

                    image.load()
                    output_image = self._watermarked_image(image, target_format)
                    output = BytesIO()
                    output_image.save(
                        output,
                        format=target_format,
                        quality=self._quality_for(target_format),
                    )
        except ProcessingError:
            raise
        except Exception as exc:
            raise ProcessingError("image processing failed") from exc

        return ProcessedImage(output.getvalue(), target_mime, target_format)

    def _quality_for(self, format_name: str) -> int:
        if format_name == "JPEG":
            return self._config.jpeg_quality
        return self._config.webp_quality

    def _watermarked_image(self, image: Image.Image, format_name: str) -> Image.Image:
        if format_name == "JPEG":
            result = image.convert("RGB")
        elif _has_alpha(image):
            result = image.convert("RGBA")
        else:
            result = image.convert("RGB")

        draw = ImageDraw.Draw(result)
        font = self._fitting_font(draw, result.width)
        stroke_width = max(1, font.size // 16)
        left, top, right, bottom = draw.textbbox(
            (0, 0), self._config.text, font=font, stroke_width=stroke_width
        )
        position = (
            (result.width - (right - left)) / 2 - left,
            (result.height - (bottom - top)) / 2 - top,
        )
        draw.text(
            position,
            self._config.text,
            font=font,
            fill=(255, 32, 32),
            stroke_fill="black",
            stroke_width=stroke_width,
        )
        return result

    def _fitting_font(self, draw: ImageDraw.ImageDraw, image_width: int) -> ImageFont.ImageFont:
        size = min(160, max(18, image_width // 10))
        while True:
            font = self._font(size)
            stroke_width = max(1, font.size // 16)
            left, _, right, _ = draw.textbbox(
                (0, 0), self._config.text, font=font, stroke_width=stroke_width
            )
            if right - left <= image_width * 0.9 or size == 1:
                return font
            size -= 1

    @staticmethod
    def _font(size: int) -> ImageFont.ImageFont:
        try:
            return ImageFont.truetype("DejaVuSans-Bold.ttf", size)
        except OSError:
            return ImageFont.load_default(size=size)


class ComfyUIProcessor:
    """Upscale static JPEG and WebP images via a remote or local ComfyUI instance."""

    def __init__(self, config: ProcessingConfig) -> None:
        self._config = config
        if config.comfyui is None:
            raise ProcessingError("ComfyUI configuration is required")
        self._comfy_config = config.comfyui
        self._server_url = self._comfy_config.server_url.rstrip("/")

        fingerprint_data = {
            "engine": "comfyui",
            "jpeg_quality": config.jpeg_quality,
            "model_name": self._comfy_config.model_name,
            "server_url": self._server_url,
            "version": 1,
            "webp_quality": config.webp_quality,
        }
        encoded = json.dumps(
            fingerprint_data, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        self._fingerprint = sha256(encoded).hexdigest()

    @property
    def fingerprint(self) -> str:
        return self._fingerprint

    def process(self, data: bytes, content_type: str | None) -> ProcessedImage:
        _validate_content_type(content_type)
        if len(data) > self._config.max_source_bytes:
            raise ProcessingError("source byte limit exceeded")

        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(BytesIO(data)) as image:
                    format_name = image.format
                    mime_type, encoder_format = _format_details(format_name)
                    if getattr(image, "n_frames", 1) != 1:
                        raise ProcessingError("animated images are not supported")
                    width, height = image.size
                    if width * height > self._config.max_pixels:
                        raise ProcessingError("pixel limit exceeded")
        except ProcessingError:
            raise
        except Exception as exc:
            raise ProcessingError("image validation failed") from exc

        try:
            upscaled_raw = self._execute_comfyui_upscale(data, encoder_format)
            with Image.open(BytesIO(upscaled_raw)) as upscaled_image:
                target_format = encoder_format
                target_mime = mime_type

                # WebP specification limit: maximum 16,383 x 16,383 pixels
                if target_format == "WEBP" and (
                    upscaled_image.width > 16383 or upscaled_image.height > 16383
                ):
                    target_format = "JPEG"
                    target_mime = "image/jpeg"

                if target_format == "JPEG":
                    converted = upscaled_image.convert("RGB")
                elif _has_alpha(upscaled_image):
                    converted = upscaled_image.convert("RGBA")
                else:
                    converted = upscaled_image.convert("RGB")

                output = BytesIO()
                converted.save(
                    output,
                    format=target_format,
                    quality=self._quality_for(target_format),
                )
                output_bytes = output.getvalue()
        except ProcessingError:
            raise
        except Exception as exc:
            raise ProcessingError("ComfyUI processing failed") from exc

        return ProcessedImage(output_bytes, target_mime, target_format)

    def _quality_for(self, format_name: str) -> int:
        if format_name == "JPEG":
            return self._config.jpeg_quality
        return self._config.webp_quality

    def _execute_comfyui_upscale(self, data: bytes, format_name: str) -> bytes:
        uploaded_name = self._upload_image(data, format_name)
        client_id = uuid.uuid4().hex
        prompt_id: str | None = None
        try:
            ws_url = self._ws_url(f"/ws?clientId={client_id}")
            timeout = self._comfy_config.timeout_seconds
            with ws_connect(ws_url, open_timeout=min(5.0, timeout), close_timeout=2.0) as ws:
                prompt_id = self._queue_prompt(uploaded_name, client_id)
                filename, subfolder, img_type = self._listen_ws_output(
                    ws, prompt_id, timeout
                )
        except Exception:
            if prompt_id is None:
                prompt_id = self._queue_prompt(uploaded_name, client_id)
            filename, subfolder, img_type = self._wait_for_output(prompt_id)

        return self._download_output(filename, subfolder, img_type)

    def _ws_url(self, path: str) -> str:
        url = self._server_url
        if url.startswith("http://"):
            base = "ws://" + url[len("http://") :]
        elif url.startswith("https://"):
            base = "wss://" + url[len("https://") :]
        elif url.startswith("ws://") or url.startswith("wss://"):
            base = url
        else:
            base = f"ws://{url}"
        return f"{base}{path}"

    def _listen_ws_output(
        self, ws, prompt_id: str, timeout: float
    ) -> tuple[str, str, str]:
        start = time.monotonic()
        while True:
            remaining = timeout - (time.monotonic() - start)
            if remaining <= 0:
                raise ProcessingError("ComfyUI upscale timed out")
            try:
                message = ws.recv(timeout=remaining)
            except TimeoutError:
                raise ProcessingError("ComfyUI upscale timed out")
            except Exception as exc:
                raise ProcessingError("ComfyUI websocket error") from exc

            if isinstance(message, str):
                try:
                    event = json.loads(message)
                except Exception:
                    continue
                event_type = event.get("type")
                event_data = event.get("data", {})
                if event_type == "execution_error":
                    raise ProcessingError(f"ComfyUI execution error: {event_data}")
                elif event_type == "executed":
                    if event_data.get("prompt_id") == prompt_id:
                        output = event_data.get("output", {})
                        images = output.get("images", [])
                        if images:
                            img_info = images[0]
                            return (
                                img_info["filename"],
                                img_info.get("subfolder", ""),
                                img_info.get("type", "output"),
                            )

    def _upload_image(self, data: bytes, format_name: str) -> str:
        boundary = "----ImageProxyBoundary" + uuid.uuid4().hex
        filename = f"proxy_{uuid.uuid4().hex[:12]}.{format_name.lower()}"
        mime = "image/jpeg" if format_name == "JPEG" else "image/webp"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="image"; filename="{filename}"\r\n'
            f"Content-Type: {mime}\r\n\r\n"
        ).encode("utf-8") + data + f"\r\n--{boundary}--\r\n".encode("utf-8")

        req = urllib.request.Request(
            f"{self._server_url}/upload/image",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
                return payload["name"]
        except Exception as exc:
            raise ProcessingError("ComfyUI image upload failed") from exc

    def _queue_prompt(self, image_name: str, client_id: str | None = None) -> str:
        workflow = {
            "1": {
                "inputs": {"image": image_name},
                "class_type": "LoadImage",
            },
            "2": {
                "inputs": {"model_name": self._comfy_config.model_name},
                "class_type": "UpscaleModelLoader",
            },
            "3": {
                "inputs": {
                    "upscale_model": ["2", 0],
                    "image": ["1", 0],
                },
                "class_type": "ImageUpscaleWithModel",
            },
            "4": {
                "inputs": {
                    "filename_prefix": "proxy_upscaled",
                    "images": ["3", 0],
                },
                "class_type": "SaveImage",
            },
        }
        actual_client_id = client_id or str(uuid.uuid4())
        data = json.dumps({"prompt": workflow, "client_id": actual_client_id}).encode("utf-8")
        req = urllib.request.Request(
            f"{self._server_url}/prompt",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
                if "prompt_id" not in payload:
                    raise ProcessingError(f"ComfyUI prompt submission rejected: {payload}")
                return payload["prompt_id"]
        except ProcessingError:
            raise
        except Exception as exc:
            raise ProcessingError("ComfyUI prompt queue failed") from exc

    def _wait_for_output(self, prompt_id: str) -> tuple[str, str, str]:
        start = time.monotonic()
        timeout = self._comfy_config.timeout_seconds
        while time.monotonic() - start < timeout:
            req = urllib.request.Request(f"{self._server_url}/history/{prompt_id}")
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    history = json.loads(resp.read().decode("utf-8"))
                    if prompt_id in history:
                        entry = history[prompt_id]
                        outputs = entry.get("outputs", {})
                        for node_output in outputs.values():
                            images = node_output.get("images", [])
                            if images:
                                img_info = images[0]
                                return (
                                    img_info["filename"],
                                    img_info.get("subfolder", ""),
                                    img_info.get("type", "output"),
                                )
                        raise ProcessingError("ComfyUI execution finished with no output images")
            except ProcessingError:
                raise
            except Exception as exc:
                raise ProcessingError("ComfyUI history query failed") from exc
            time.sleep(0.5)
        raise ProcessingError("ComfyUI upscale timed out")

    def _download_output(self, filename: str, subfolder: str, img_type: str) -> bytes:
        query = urllib.parse.urlencode(
            {"filename": filename, "subfolder": subfolder, "type": img_type}
        )
        req = urllib.request.Request(f"{self._server_url}/view?{query}")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read()
        except Exception as exc:
            raise ProcessingError("ComfyUI output download failed") from exc

