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


def test_fingerprint_changes_when_output_configuration_changes() -> None:
    base = processor().fingerprint
    changed = WatermarkProcessor(
        ProcessingConfig("DIFFERENT", 90, 90, 1024**2, 1_000_000, 2)
    ).fingerprint

    assert changed != base
