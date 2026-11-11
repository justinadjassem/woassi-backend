from uuid import uuid4

import pytest

from tests.fakes import NOW, FakeStorage, FakeUoW
from woassi.modules.catalog.application.use_cases.products import (
    CreateProduct,
    GetProduct,
    ListProducts,
)
from woassi.modules.catalog.domain.errors import ProductNotFound
from woassi.modules.catalog.domain.product import PhotoStatus, Product
from woassi.shared.domain.values import Amount
from woassi.shared.infrastructure.clock import FrozenClock


async def test_create_then_get() -> None:
    uow, images = FakeUoW(), FakeStorage("http://images")
    shop_id = uuid4()
    created = await CreateProduct(uow, images, FrozenClock(NOW)).execute(
        shop_id=shop_id, name="Pagne", price=5000, stock=2, retailer_id="SKU-1"
    )
    fetched = await GetProduct(uow, images).execute(shop_id=shop_id, product_id=created.id)
    assert fetched == created


async def test_product_not_visible_from_another_shop() -> None:
    uow, images = FakeUoW(), FakeStorage("http://images")
    created = await CreateProduct(uow, images, FrozenClock(NOW)).execute(
        shop_id=uuid4(), name="Pagne", price=5000, stock=2, retailer_id="SKU-1"
    )
    with pytest.raises(ProductNotFound):
        await GetProduct(uow, images).execute(shop_id=uuid4(), product_id=created.id)


async def test_only_ready_photos_have_urls() -> None:
    uow, images = FakeUoW(), FakeStorage("http://images")
    product = Product.create(
        now=NOW, shop_id=uuid4(), name="Pagne", price=Amount(1), stock=0, retailer_id="S"
    )
    ready = product.add_photo(content_type="image/jpeg")
    ready.mark_ready({400: "a_400.webp"})
    product.add_photo(content_type="image/jpeg")
    uow.repository.products[product.id] = product

    page = await ListProducts(uow, images).execute(shop_id=product.shop_id, offset=0, limit=10)
    [view] = page.items

    assert view.photos[0].status is PhotoStatus.READY
    assert view.photos[0].urls == {400: "http://images/a_400.webp"}
    assert view.photos[1].urls == {}
