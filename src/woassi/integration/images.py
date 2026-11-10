"""Catalog image processing reused by `shops` for logos."""

from woassi.modules.catalog.application.ports import ImageProcessor
from woassi.modules.catalog.domain.errors import UnreadableImage
from woassi.modules.shops.domain.errors import InvalidLogo

LOGO_SIZE = 512


class CatalogLogoProcessor:
    """LogoProcessor for the shops module, on top of the catalog's ImageProcessor port."""

    def __init__(self, images: ImageProcessor) -> None:
        self._images = images

    async def to_webp(self, data: bytes) -> bytes:
        """Resizes a logo to a 512 px WebP. Raises InvalidLogo if the image is unreadable."""
        try:
            variants = await self._images.webp_variants(data, (LOGO_SIZE,))
        except UnreadableImage as error:
            raise InvalidLogo("Unreadable image.") from error
        return variants[LOGO_SIZE]
