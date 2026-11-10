"""Products, their stock and their photos."""

from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID, uuid4

from woassi.modules.catalog.domain.errors import (
    InsufficientStock,
    InvalidProduct,
    PhotoNotFound,
    TooManyPhotos,
)
from woassi.modules.catalog.domain.events import ProductCreated
from woassi.shared.domain.events import Entity
from woassi.shared.domain.values import Amount

DEFAULT_MAX_PHOTOS = 5


def original_key(shop_id: UUID, product_id: UUID, photo_id: UUID) -> str:
    """Object key of an uploaded original, in the private uploads bucket."""
    return f"shops/{shop_id}/products/{product_id}/originals/{photo_id}"


def variant_key(shop_id: UUID, product_id: UUID, photo_id: UUID, size: int) -> str:
    """Object key of a WebP variant `size` pixels wide, in the public products bucket."""
    return f"shops/{shop_id}/products/{product_id}/{photo_id}_{size}.webp"


class PhotoStatus(StrEnum):
    """PENDING until processed, then READY, or REJECTED if the file was unusable."""

    PENDING = "PENDING"
    READY = "READY"
    REJECTED = "REJECTED"


@dataclass(slots=True)
class ProductPhoto:
    """A photo of a product. `variant_keys` maps each width to its WebP object key."""

    id: UUID
    product_id: UUID
    original_key: str
    content_type: str
    status: PhotoStatus
    position: int
    variant_keys: dict[int, str] = field(default_factory=dict)

    def mark_ready(self, variant_keys: dict[int, str]) -> None:
        """The variants are stored: the photo can be shown."""
        self.status = PhotoStatus.READY
        self.variant_keys = dict(variant_keys)

    def reject(self) -> None:
        """The file was missing, too large or unreadable."""
        self.status = PhotoStatus.REJECTED


@dataclass(slots=True)
class Product(Entity):
    """A product of a shop's catalog.

    `retailer_id` is the SKU shared with Meta's catalog: carts refer to products by it.
    """

    id: UUID
    shop_id: UUID
    name: str
    price: Amount
    stock: int
    retailer_id: str
    description: str = ""
    active: bool = True
    photos: list[ProductPhoto] = field(default_factory=list)

    @classmethod
    def create(
        cls,
        *,
        shop_id: UUID,
        name: str,
        price: Amount,
        stock: int,
        retailer_id: str,
        description: str = "",
    ) -> "Product":
        """Creates a product and records `ProductCreated`. Raises InvalidProduct for bad data."""
        name = name.strip()
        retailer_id = retailer_id.strip()
        if not name:
            raise InvalidProduct("Product name is required.")
        if not retailer_id:
            raise InvalidProduct("Product retailer_id is required.")
        if stock < 0:
            raise InvalidProduct("Stock cannot be negative.")
        product = cls(
            id=uuid4(),
            shop_id=shop_id,
            name=name,
            price=price,
            stock=stock,
            retailer_id=retailer_id,
            description=description,
        )
        product.record_event(
            ProductCreated(product_id=product.id, shop_id=shop_id, retailer_id=retailer_id)
        )
        return product

    def adjust_stock(self, delta: int) -> None:
        """Adds `delta` (negative to remove). Raises InsufficientStock below zero."""
        if self.stock + delta < 0:
            raise InsufficientStock(f"Insufficient stock for {self.name}: {self.stock} available.")
        self.stock += delta

    def add_photo(self, *, content_type: str, max_photos: int = DEFAULT_MAX_PHOTOS) -> ProductPhoto:
        # A rejected photo must not use up a slot for good.
        """Adds a pending photo at the next position. Raises TooManyPhotos at the limit."""
        active_photos = [p for p in self.photos if p.status is not PhotoStatus.REJECTED]
        if len(active_photos) >= max_photos:
            raise TooManyPhotos(f"A product cannot have more than {max_photos} photos.")
        photo_id = uuid4()
        photo = ProductPhoto(
            id=photo_id,
            product_id=self.id,
            original_key=original_key(self.shop_id, self.id, photo_id),
            content_type=content_type,
            status=PhotoStatus.PENDING,
            position=max((p.position for p in self.photos), default=-1) + 1,
        )
        self.photos.append(photo)
        return photo

    def photo(self, photo_id: UUID) -> ProductPhoto:
        """The photo with this id; raises PhotoNotFound otherwise."""
        for photo in self.photos:
            if photo.id == photo_id:
                return photo
        raise PhotoNotFound(f"Photo {photo_id} not found.")
