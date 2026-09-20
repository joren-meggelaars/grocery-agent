"""Untrusted bytes in, a clean JPEG out.

The file type is decided by content (never by extension or Content-Type), size and pixel
counts are capped before decoding, and every image is re-encoded, which drops EXIF/GPS,
embedded payloads and anything that is not pixel data.
"""

import hashlib
from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageOps, UnidentifiedImageError

ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP"}
MAX_PIXELS = 50_000_000  # decoded size cap, checked from the header before any decoding
MAX_LONG_EDGE = 2000
JPEG_QUALITY = 85

Image.MAX_IMAGE_PIXELS = MAX_PIXELS


class UploadError(ValueError):
    """Message is safe to show to the user."""


@dataclass(frozen=True)
class ProcessedImage:
    data: bytes
    width: int
    height: int
    sha256: str


def sniff_kind(data: bytes) -> str | None:
    """'pdf', 'image' or None, from the leading bytes only."""
    head = data[:1024].lstrip()
    if head.startswith(b"%PDF-"):
        return "pdf"
    if data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n" or (
        data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    ):
        return "image"
    return None


def reencode_image(data: bytes, max_long_edge: int = MAX_LONG_EDGE) -> ProcessedImage:
    try:
        with Image.open(BytesIO(data)) as img:
            if img.format not in ALLOWED_FORMATS:
                raise UploadError("Unsupported image type. Use JPEG, PNG or WebP.")
            width, height = img.size
            if width < 1 or height < 1 or width * height > MAX_PIXELS:
                raise UploadError("Image dimensions are too large.")
            img.load()
            img = ImageOps.exif_transpose(img)  # bake in the camera rotation before EXIF is dropped
            if img.mode in ("RGBA", "LA", "P"):
                img = img.convert("RGBA")
                background = Image.new("RGB", img.size, (255, 255, 255))
                background.paste(img, mask=img.getchannel("A"))
                img = background
            else:
                img = img.convert("RGB")
            img.thumbnail((max_long_edge, max_long_edge), Image.Resampling.LANCZOS)
            out = BytesIO()
            img.save(out, "JPEG", quality=JPEG_QUALITY, optimize=True)
    except UploadError:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError, SyntaxError) as exc:
        raise UploadError("That file is not a readable image.") from exc

    jpeg = out.getvalue()
    return ProcessedImage(jpeg, img.width, img.height, hashlib.sha256(jpeg).hexdigest())
