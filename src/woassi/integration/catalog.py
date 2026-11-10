"""Catalog module seen from `orders`: stock reservation."""

from collections.abc import Mapping
from uuid import UUID

from woassi.modules.catalog.application.use_cases.stock import ReleaseStock, ReserveStock
from woassi.modules.orders.application.ports import StockItem


class CatalogStock:
    """CatalogPort, for the orders module, backed by the catalog module."""

    def __init__(self, reserve: ReserveStock, release: ReleaseStock) -> None:
        self._reserve = reserve
        self._release = release

    async def reserve(self, shop_id: UUID, requested: Mapping[str, int]) -> list[StockItem]:
        """Reserves what it can of each requested product, by retailer id.

        Each item tells how much was requested and how much could be reserved.
        """
        items = await self._reserve.execute(shop_id=shop_id, requested=requested)
        return [
            StockItem(
                retailer_id=item.retailer_id,
                product_id=item.product_id,
                name=item.name,
                unit_price=item.unit_price,
                requested=item.requested,
                reserved=item.reserved,
            )
            for item in items
        ]

    async def release(self, shop_id: UUID, quantities: Mapping[UUID, int]) -> None:
        """Gives reserved quantities back to the stock, by product id."""
        await self._release.execute(shop_id=shop_id, quantities=quantities)
