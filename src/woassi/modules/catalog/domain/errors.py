"""Errors of the catalog module."""

from woassi.shared.domain.errors import Conflict, InvalidData, NotFound


class ProductNotFound(NotFound):
    """The product does not exist in this shop."""

    code = "catalog.product_not_found"


class PhotoNotFound(NotFound):
    """The photo does not exist on this product."""

    code = "catalog.photo_not_found"


class InvalidProduct(InvalidData):
    """The product data breaks a business rule."""

    code = "catalog.invalid_product"


class InsufficientStock(Conflict):
    """Not enough stock to remove that many units."""

    code = "catalog.insufficient_stock"


class RetailerIdAlreadyUsed(Conflict):
    """The shop already has a product with this retailer id (SKU)."""

    code = "catalog.retailer_id_already_used"


class InvalidPhoto(InvalidData):
    """The photo's type or size is not accepted, or the file was not uploaded."""

    code = "catalog.invalid_photo"


class TooManyPhotos(Conflict):
    """The product already has the maximum number of photos."""

    code = "catalog.too_many_photos"


class PhotoAlreadyProcessed(Conflict):
    """The photo was already confirmed and processed."""

    code = "catalog.photo_already_processed"


class UnreadableImage(InvalidData):
    """The file is not a usable JPEG, PNG or WebP image."""

    code = "catalog.unreadable_image"
