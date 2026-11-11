"""Ports of the catalog module."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from woassi.modules.catalog.domain.product import Product
from woassi.shared.application.ports import UnitOfWork


class ProductRepository(Protocol):
    """Stores products with their photos."""

    async def add(self, product: Product) -> None:
        """Raises RetailerIdAlreadyUsed if the shop already has this `retailer_id`."""
        ...

    async def get(self, shop_id: UUID, product_id: UUID) -> Product | None:
        """The product, only if it belongs to `shop_id`."""
        ...

    async def get_by_photo(self, photo_id: UUID) -> Product | None:
        """The product owning `photo_id`, in any shop (for the worker)."""
        ...

    async def list(self, shop_id: UUID, offset: int, limit: int) -> Sequence[Product]:
        """A page of the shop's products, oldest first."""
        ...

    async def count(self, shop_id: UUID) -> int:
        """Number of products of the shop."""
        ...

    async def list_for_update(
        self, shop_id: UUID, *, retailer_ids: Sequence[str] = (), ids: Sequence[UUID] = ()
    ) -> Sequence[Product]:
        """Lock the matching products until the end of the transaction."""
        ...

    async def list_shop_for_update(self, shop_id: UUID) -> Sequence[Product]:
        """Every product of the shop, locked until the end of the transaction, in id order."""
        ...

    async def list_meta_sync_due(
        self, shop_id: UUID, now: datetime, limit: int
    ) -> Sequence[Product]:
        """Products of the shop to send to Meta: PENDING ones, and FAILED ones due again."""
        ...

    async def shops_with_meta_sync_due(self, now: datetime, limit: int) -> Sequence[UUID]:
        """Shops having a PENDING or FAILED product whose `next_attempt_at` has passed."""
        ...

    async def save(self, product: Product) -> None:
        """Writes back a product loaded from this repository, photos included."""
        ...


class CatalogUnitOfWork(UnitOfWork, Protocol):
    """Unit of Work of the catalog module."""

    @property
    def products(self) -> ProductRepository: ...


class PhotoJobQueue(Protocol):
    """Queues photo processing for the worker."""

    async def enqueue_processing(self, photo_id: UUID) -> None:
        """Queues the processing of `photo_id`; queuing it twice runs it once."""
        ...


class ImageProcessor(Protocol):
    """Turns an uploaded image into clean, resized WebP variants."""

    async def webp_variants(self, data: bytes, sizes: Sequence[int]) -> dict[int, bytes]:
        """Return one WebP image per size. Raise UnreadableImage if `data` is unusable."""
        ...


class CatalogSyncQueue(Protocol):
    """Queues the sync of a shop's catalog to Meta for the worker."""

    async def enqueue_sync(self, shop_id: UUID) -> None:
        """Queues a sync of `shop_id`; while one is queued, queuing again does nothing."""
        ...


@dataclass(frozen=True, slots=True)
class MetaCatalogAccess:
    """The Meta catalog linked to a shop's connected WhatsApp number. Never log it."""

    catalog_id: str
    access_token: str = field(repr=False)
    brand: str  # the shop's name: Meta requires a brand


class MetaCatalogAccessPort(Protocol):
    """Gives the Meta catalog of a shop, implemented by the shops module."""

    async def for_shop(self, shop_id: UUID) -> MetaCatalogAccess | None:
        """None unless the shop is ACTIVE with a CONNECTED number and a linked catalog."""
        ...


@dataclass(frozen=True, slots=True)
class MetaCatalogItem:
    """A product as Meta's catalog shows it. `price` is in XOF."""

    retailer_id: str
    title: str
    description: str
    price: int
    in_stock: bool
    image_urls: tuple[str, ...]
    link: str
    brand: str


class MetaCatalogFailure(StrEnum):
    """Why a whole batch failed. Values are the error codes stored on the products."""

    UNAVAILABLE = "meta.unavailable"
    RATE_LIMITED = "meta.rate_limited"
    INVALID_TOKEN = "meta.invalid_token"
    REJECTED = "meta.rejected"


class MetaCatalogError(Exception):
    """Meta did not take the batch. Carries no Meta text, only our failure and Meta's code."""

    def __init__(self, failure: MetaCatalogFailure, *, meta_code: int | None = None) -> None:
        super().__init__(failure.value)
        self.failure = failure
        self.meta_code = meta_code


# Code stored on a product Meta refused on its own, while the rest of the batch went through.
ITEM_REJECTED = "meta.item_rejected"


class MetaCatalogPort(Protocol):
    """Writes items to a Meta catalog."""

    async def write(
        self,
        access: MetaCatalogAccess,
        upserts: Sequence[MetaCatalogItem],
        deletes: Sequence[str],
    ) -> Mapping[str, str]:
        """Creates or updates `upserts`, deletes the `deletes` retailer ids, in one batch.

        Returns the retailer ids Meta refused, each with our error code. Raises
        MetaCatalogError if the whole batch failed.
        """
        ...
