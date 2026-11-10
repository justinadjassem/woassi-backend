"""Ports of the catalog module."""

from collections.abc import Sequence
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
