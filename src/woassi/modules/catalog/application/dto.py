"""Data returned by the catalog use cases."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from woassi.modules.catalog.domain.product import (
    MetaSync,
    MetaSyncStatus,
    PhotoStatus,
    Product,
    ProductPhoto,
)
from woassi.shared.application.ports import StoragePort


@dataclass(frozen=True, slots=True)
class PhotoView:
    """A photo, with its public URL per width (empty until READY)."""

    id: UUID
    status: PhotoStatus
    position: int
    urls: dict[int, str]


@dataclass(frozen=True, slots=True)
class MetaSyncView:
    """Where the product stands with Meta's catalog. `error_code` is ours, never Meta's text."""

    status: MetaSyncStatus
    synced_at: datetime | None
    error_code: str | None
    next_attempt_at: datetime | None


def _meta_sync_view(sync: MetaSync) -> MetaSyncView:
    """Builds the view of a product's sync state."""
    return MetaSyncView(
        status=sync.status,
        synced_at=sync.synced_at,
        error_code=sync.error_code,
        next_attempt_at=sync.next_attempt_at,
    )


@dataclass(frozen=True, slots=True)
class ProductView:
    """Read-only view of a product. `price` is in XOF."""

    id: UUID
    shop_id: UUID
    name: str
    price: int
    stock: int
    retailer_id: str
    description: str
    active: bool
    photos: list[PhotoView]
    meta_sync: MetaSyncView


@dataclass(frozen=True, slots=True)
class UploadPhoto:
    """Where to upload a new photo, and for how long the URL is valid."""

    photo_id: UUID
    upload_url: str
    expire_s: int


def _photo_view(photo: ProductPhoto, images: StoragePort) -> PhotoView:
    """Builds the view of `photo`; only a READY photo has URLs."""
    urls = {}
    if photo.status is PhotoStatus.READY:
        urls = {size: images.public_url(key) for size, key in photo.variant_keys.items()}
    return PhotoView(id=photo.id, status=photo.status, position=photo.position, urls=urls)


def product_view(product: Product, images: StoragePort) -> ProductView:
    """Builds the view of `product`, with public URLs from the `images` bucket."""
    return ProductView(
        id=product.id,
        shop_id=product.shop_id,
        name=product.name,
        price=product.price.value,
        stock=product.stock,
        retailer_id=product.retailer_id,
        description=product.description,
        active=product.active,
        photos=[_photo_view(photo, images) for photo in product.photos],
        meta_sync=_meta_sync_view(product.meta_sync),
    )
