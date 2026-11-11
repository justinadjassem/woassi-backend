"""Product creation, update and reading."""

from uuid import UUID

from woassi.modules.catalog.application.dto import ProductView, product_view
from woassi.modules.catalog.application.ports import CatalogUnitOfWork
from woassi.modules.catalog.domain.errors import ProductNotFound
from woassi.modules.catalog.domain.product import Product
from woassi.shared.application.pagination import PageResult
from woassi.shared.application.ports import Clock, StoragePort
from woassi.shared.domain.values import Amount


class CreateProduct:
    """Creates a product in a shop; it is then sent to the shop's Meta catalog."""

    def __init__(self, uow: CatalogUnitOfWork, images: StoragePort, clock: Clock) -> None:
        self._uow = uow
        self._images = images
        self._clock = clock

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
            now=self._clock.now(),
        )
        async with self._uow as uow:
            await uow.products.add(product)
            await uow.commit()
        return product_view(product, self._images)


class UpdateProduct:
    """Changes a product; only the arguments given change.

    `active=False` takes the product out of Meta's catalog (and out of carts). The retailer id
    cannot change: carts and Meta's catalog refer to the product by it.
    """

    def __init__(self, uow: CatalogUnitOfWork, images: StoragePort, clock: Clock) -> None:
        self._uow = uow
        self._images = images
        self._clock = clock

    async def execute(
        self,
        *,
        shop_id: UUID,
        product_id: UUID,
        name: str | None = None,
        price: int | None = None,
        description: str | None = None,
        stock: int | None = None,
        active: bool | None = None,
    ) -> ProductView:
        """Raises ProductNotFound, or InvalidProduct for a blank name or a negative stock."""
        now = self._clock.now()
        async with self._uow as uow:
            # Locked like a stock reservation: the stock written back cannot be stale.
            locked = await uow.products.list_for_update(shop_id, ids=[product_id])
            if not locked:
                raise ProductNotFound(f"Product {product_id} not found.")
            product = locked[0]
            product.update(
                now=now,
                name=name,
                price=None if price is None else Amount(price),
                description=description,
                active=active,
            )
            if stock is not None:
                product.set_stock(stock, now=now)
            await uow.products.save(product)
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
