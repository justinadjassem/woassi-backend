"""Image processing with Pillow."""

from collections.abc import Sequence
from functools import partial
from io import BytesIO

import anyio.to_thread
from PIL import Image, ImageOps, UnidentifiedImageError

from woassi.modules.catalog.domain.errors import UnreadableImage

ACCEPTED_FORMATS = ("JPEG", "PNG", "WEBP")
MAX_PIXELS = 50_000_000  # protects the worker from decompression bombs
WEBP_QUALITY = 85


def _encode(image: Image.Image, size: int) -> bytes:
    """One WebP variant fitting in `size` x `size`, aspect ratio kept."""
    variant = image.copy()
    variant.thumbnail((size, size), Image.Resampling.LANCZOS)  # never upscales
    output = BytesIO()
    variant.save(output, format="WEBP", quality=WEBP_QUALITY, method=6)
    return output.getvalue()


def _generate(data: bytes, sizes: Sequence[int]) -> dict[int, bytes]:
    """Decodes `data` and returns one WebP per size (blocking).

    Applies the EXIF orientation, then drops every metadata. Raises UnreadableImage for an
    unsupported format, a corrupt file, or an image over `MAX_PIXELS`.
    """
    try:
        with Image.open(BytesIO(data), formats=ACCEPTED_FORMATS) as source:
            width, height = source.size
            if width * height > MAX_PIXELS:
                raise UnreadableImage("Image too large.")
            oriented = ImageOps.exif_transpose(source)
            mode = (
                "RGBA" if "A" in oriented.getbands() or "transparency" in oriented.info else "RGB"
            )
            converted = oriented.convert(mode)
            # New image: only pixels are carried over, never metadata (EXIF, GPS, ICC).
            clean = Image.new(mode, converted.size)
            clean.paste(converted)
    except UnreadableImage:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError) as error:
        raise UnreadableImage("Unreadable image.") from error
    return {size: _encode(clean, size) for size in sizes}


class PillowImageProcessor:
    """`ImageProcessor` on Pillow."""

    async def webp_variants(self, data: bytes, sizes: Sequence[int]) -> dict[int, bytes]:
        # CPU work: off the event loop so it does not block other jobs.
        """WebP variants of `data` per width; raises UnreadableImage."""
        return await anyio.to_thread.run_sync(partial(_generate, data, sizes))
