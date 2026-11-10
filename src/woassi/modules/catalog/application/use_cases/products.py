"""Product creation and reading."""

from uuid import UUID

from woassi.modules.catalog.application.dto import ProductView, product_view
from woassi.modules.catalog.application.ports import CatalogUnitOfWork
from woassi.modules.catalog.domain.errors import ProductNotFound
from woassi.modules.catalog.domain.product import Product
from woassi.shared.application.pagination import PageResult
from woassi.shared.application.ports import StoragePort
from woassi.shared.domain.values import Amount


class CreateProduct:
    """Creates a product in a shop."""

    def __init__(self, uow: CatalogUnitOfWork, images: StoragePort) -> None:
        self._uow = uow
        self._images = images

    async def execute(
        self,
        *,
        shop_id: UUID,
        name: str,
        price: int,
        stock: int,
        retailer_id: str,
        description: str = "",
    ) -> ProductView:
        """Raises:
        InvalidProduct: blank name or retailer id, or negative stock.
        RetailerIdAlreadyUsed: the shop already has a product with this retailer id.
        """
        product = Product.create(
            shop_id=shop_id,
            name=name,
            price=Amount(price),
            stock=stock,
            retailer_id=retailer_id,
            description=description,
        )
        async with self._uow as uow:
            await uow.products.add(product)
            await uow.commit()
        return product_view(product, self._images)


class ListProducts:
    """Lists a shop's products, one page at a time."""

    def __init__(self, uow: CatalogUnitOfWork, images: StoragePort) -> None:
        self._uow = uow
        self._images = images

    async def execute(self, *, shop_id: UUID, offset: int, limit: int) -> PageResult[ProductView]:
        async with self._uow as uow:
            products = await uow.products.list(shop_id, offset, limit)
            total = await uow.products.count(shop_id)
        return PageResult(
            items=[product_view(product, self._images) for product in products],
            total=total,
            offset=offset,
            limit=limit,
        )


class GetProduct:
    """Returns one product of a shop."""

    def __init__(self, uow: CatalogUnitOfWork, images: StoragePort) -> None:
        self._uow = uow
        self._images = images

    async def execute(self, *, shop_id: UUID, product_id: UUID) -> ProductView:
        """Raises ProductNotFound if the shop has no such product."""
        async with self._uow as uow:
            product = await uow.products.get(shop_id, product_id)
        if product is None:
            raise ProductNotFound(f"Product {product_id} not found.")
        return product_view(product, self._images)
