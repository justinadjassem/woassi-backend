"""Stock reservation and release, used by the orders module."""

from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from woassi.modules.catalog.application.ports import CatalogUnitOfWork


@dataclass(frozen=True, slots=True)
class ReservedItem:
    """Outcome for one cart line: how much was requested, and how much was reserved."""

    retailer_id: str
    product_id: UUID | None  # None for an unknown product
    name: str
    unit_price: int
    requested: int
    reserved: int


class ReserveStock:
    """Reserve what is available, up to the requested quantities, in one transaction.

    Products are locked while they are counted: two carts cannot sell the same last item.
    """

    def __init__(self, uow: CatalogUnitOfWork) -> None:
        self._uow = uow

    async def execute(self, *, shop_id: UUID, requested: Mapping[str, int]) -> list[ReservedItem]:
        """Reserves up to `requested` units per retailer id.

        An unknown or inactive product reserves nothing; the caller decides what to do with a
        partial cart.
        """
        async with self._uow as uow:
            products = {
                product.retailer_id: product
                for product in await uow.products.list_for_update(
                    shop_id, retailer_ids=list(requested)
                )
            }
            items: list[ReservedItem] = []
            for retailer_id, quantity in requested.items():
                product = products.get(retailer_id)
                if product is None:
                    items.append(ReservedItem(retailer_id, None, retailer_id, 0, quantity, 0))
                    continue
                reserved = min(max(quantity, 0), product.stock) if product.active else 0
                if reserved:
                    product.adjust_stock(-reserved)
                    await uow.products.save(product)
                items.append(
                    ReservedItem(
                        retailer_id=retailer_id,
                        product_id=product.id,
                        name=product.name,
                        unit_price=product.price.value,
                        requested=quantity,
                        reserved=reserved,
                    )
                )
            await uow.commit()
        return items


class ReleaseStock:
    """Gives reserved units back to the stock, e.g. when an order is closed."""

    def __init__(self, uow: CatalogUnitOfWork) -> None:
        self._uow = uow

    async def execute(self, *, shop_id: UUID, quantities: Mapping[UUID, int]) -> None:
        """A deleted product is skipped: there is no stock left to give back to."""
        async with self._uow as uow:
            for product in await uow.products.list_for_update(shop_id, ids=list(quantities)):
                product.adjust_stock(quantities[product.id])
                await uow.products.save(product)
            await uow.commit()
