from uuid import uuid4

import pytest

from tests.fakes import NOW
from woassi.modules.catalog.domain.errors import (
    InsufficientStock,
    InvalidProduct,
    PhotoNotFound,
    TooManyPhotos,
)
from woassi.modules.catalog.domain.events import ProductCreated, ProductMetaSyncRequested
from woassi.modules.catalog.domain.product import PhotoStatus, Product
from woassi.shared.domain.values import Amount


def product(stock: int = 10) -> Product:
    return Product.create(
        now=NOW, shop_id=uuid4(), name="Pagne", price=Amount(5000), stock=stock, retailer_id="SKU-1"
    )


def test_create_strips_name() -> None:
    assert (
        Product.create(
            now=NOW, shop_id=uuid4(), name="  Pagne ", price=Amount(1), stock=0, retailer_id="S"
        ).name
        == "Pagne"
    )


@pytest.mark.parametrize(
    ("name", "stock", "retailer_id"), [("  ", 1, "S"), ("Pagne", -1, "S"), ("Pagne", 1, " ")]
)
def test_create_rejects_invalid_data(name: str, stock: int, retailer_id: str) -> None:
    with pytest.raises(InvalidProduct):
        Product.create(
            now=NOW,
            shop_id=uuid4(),
            name=name,
            price=Amount(1),
            stock=stock,
            retailer_id=retailer_id,
        )


def test_adjust_stock() -> None:
    p = product(stock=3)
    p.adjust_stock(2, now=NOW)
    p.adjust_stock(-5, now=NOW)
    assert p.stock == 0


def test_adjust_stock_never_goes_below_zero() -> None:
    p = product(stock=3)
    with pytest.raises(InsufficientStock):
        p.adjust_stock(-4, now=NOW)
    assert p.stock == 3


def test_add_photo_builds_key_and_increments_position() -> None:
    p = product()
    first = p.add_photo(content_type="image/jpeg")
    second = p.add_photo(content_type="image/png")

    assert first.status is PhotoStatus.PENDING
    assert (first.position, second.position) == (0, 1)
    assert first.original_key == (f"shops/{p.shop_id}/products/{p.id}/originals/{first.id}")


def test_add_photo_enforces_maximum() -> None:
    p = product()
    for _ in range(5):
        p.add_photo(content_type="image/jpeg")
    with pytest.raises(TooManyPhotos):
        p.add_photo(content_type="image/jpeg")


def test_maximum_is_configurable() -> None:
    p = product()
    p.add_photo(content_type="image/jpeg", max_photos=1)
    with pytest.raises(TooManyPhotos):
        p.add_photo(content_type="image/jpeg", max_photos=1)


def test_rejected_photo_frees_its_slot() -> None:
    p = product()
    photos = [p.add_photo(content_type="image/jpeg") for _ in range(5)]
    photos[0].reject()
    assert p.add_photo(content_type="image/jpeg").position == 5


def test_photo_not_found() -> None:
    with pytest.raises(PhotoNotFound):
        product().photo(uuid4())


def test_create_records_an_event_pulled_only_once() -> None:
    p = product()
    event, sync = p.pull_events()
    assert isinstance(event, ProductCreated)
    assert (event.product_id, event.shop_id, event.retailer_id) == (p.id, p.shop_id, "SKU-1")
    assert isinstance(sync, ProductMetaSyncRequested)
    assert (sync.product_id, sync.shop_id) == (p.id, p.shop_id)
    assert p.pull_events() == []
