"""Byte-oriented image watermark processing."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
import json
from typing import Protocol, runtime_checkable
import warnings

from PIL import Image, ImageDraw, ImageFont

from image_proxy.config import ProcessingConfig


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
        self._validate_content_type(content_type)
        if len(data) > self._config.max_source_bytes:
            raise ProcessingError("source byte limit exceeded")

        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(BytesIO(data)) as image:
                    format_name = image.format
                    mime_type, encoder_format = self._format_details(format_name)
                    if getattr(image, "n_frames", 1) != 1:
                        raise ProcessingError("animated images are not supported")
                    width, height = image.size
                    if width * height > self._config.max_pixels:
                        raise ProcessingError("pixel limit exceeded")

                    image.load()
                    output_image = self._watermarked_image(image, encoder_format)
                    output = BytesIO()
                    output_image.save(
                        output,
                        format=encoder_format,
                        quality=self._quality_for(encoder_format),
                    )
        except ProcessingError:
            raise
        except Exception as exc:
            raise ProcessingError("image processing failed") from exc

        return ProcessedImage(output.getvalue(), mime_type, format_name)

    def _validate_content_type(self, content_type: str | None) -> None:
        normalized = "" if content_type is None else content_type.split(";", 1)[0].strip().lower()
        if normalized not in _ALLOWED_CONTENT_TYPES:
            raise ProcessingError("unsupported content type")

    def _format_details(self, format_name: str | None) -> tuple[str, str]:
        try:
            return _FORMAT_DETAILS[format_name or ""]
        except KeyError as exc:
            raise ProcessingError("unsupported image format") from exc

    def _quality_for(self, format_name: str) -> int:
        if format_name == "JPEG":
            return self._config.jpeg_quality
        return self._config.webp_quality

    def _watermarked_image(self, image: Image.Image, format_name: str) -> Image.Image:
        if format_name == "JPEG":
            result = image.convert("RGB")
        elif self._has_alpha(image):
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
            left, _, right, _ = draw.textbbox((0, 0), self._config.text, font=font)
            if right - left <= image_width * 0.9 or size == 1:
                return font
            size -= 1

    @staticmethod
    def _font(size: int) -> ImageFont.ImageFont:
        try:
            return ImageFont.truetype("DejaVuSans-Bold.ttf", size)
        except OSError:
            return ImageFont.load_default(size=size)

    @staticmethod
    def _has_alpha(image: Image.Image) -> bool:
        return "A" in image.getbands() or "transparency" in image.info
