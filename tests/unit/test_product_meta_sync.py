from datetime import timedelta
from uuid import uuid4

import pytest

from tests.fakes import NOW
from woassi.modules.catalog.domain.events import ProductMetaSyncRequested
from woassi.modules.catalog.domain.product import (
    META_SYNC_GRACE,
    MetaSyncStatus,
    Product,
    meta_retry_delay,
)
from woassi.shared.domain.values import Amount


def product(stock: int = 3) -> Product:
    p = Product.create(
        now=NOW, shop_id=uuid4(), name="Pagne", price=Amount(5000), stock=stock, retailer_id="S"
    )
    p.pull_events()
    return p


def sync_events(p: Product) -> list[ProductMetaSyncRequested]:
    return [e for e in p.pull_events() if isinstance(e, ProductMetaSyncRequested)]


def test_new_product_is_pending_and_due_after_the_grace_delay() -> None:
    p = Product.create(
        now=NOW, shop_id=uuid4(), name="Pagne", price=Amount(1), stock=0, retailer_id="S"
    )
    assert p.meta_sync.status is MetaSyncStatus.PENDING
    assert p.meta_sync.version == 1
    assert p.meta_sync.next_attempt_at == NOW + META_SYNC_GRACE
    assert len(sync_events(p)) == 1


@pytest.mark.parametrize(("start", "delta"), [(3, -2), (3, 4), (0, 0)])
def test_stock_change_without_availability_flip_costs_nothing(start: int, delta: int) -> None:
    p = product(stock=start)
    version = p.meta_sync.version
    p.adjust_stock(delta, now=NOW)
    assert sync_events(p) == []
    assert p.meta_sync.version == version


@pytest.mark.parametrize(("start", "delta"), [(3, -3), (0, 2)])
def test_availability_flip_asks_for_a_sync(start: int, delta: int) -> None:
    p = product(stock=start)
    p.adjust_stock(delta, now=NOW)
    [event] = sync_events(p)
    assert (event.product_id, event.shop_id) == (p.id, p.shop_id)


def test_one_event_per_transaction_however_many_changes() -> None:
    p = product(stock=1)
    p.update(now=NOW, name="Pagne wax", price=Amount(6000))
    p.adjust_stock(-1, now=NOW)
    assert len(sync_events(p)) == 1


def test_update_without_change_does_not_ask_for_a_sync() -> None:
    p = product()
    p.update(now=NOW, name="Pagne", price=Amount(5000), description="", active=True)
    assert sync_events(p) == []


def test_synced_only_for_the_version_that_was_sent() -> None:
    p = product()
    sent = p.meta_sync.version
    p.update(now=NOW, name="Changed meanwhile")

    p.mark_meta_synced(sent, published=True, now=NOW)

    assert p.meta_sync.status is MetaSyncStatus.PENDING
    assert p.meta_sync.published is True

    p.mark_meta_synced(p.meta_sync.version, published=True, now=NOW)
    assert p.meta_sync.status is MetaSyncStatus.SYNCED
    assert p.meta_sync.synced_at == NOW
    assert p.meta_sync.next_attempt_at is None


def test_failures_back_off_then_a_change_resets_them() -> None:
    p = product()
    version = p.meta_sync.version
    p.mark_meta_sync_failed(version, "meta.unavailable", NOW)
    p.mark_meta_sync_failed(version, "meta.unavailable", NOW)

    assert p.meta_sync.status is MetaSyncStatus.FAILED
    assert p.meta_sync.attempts == 2
    assert p.meta_sync.error_code == "meta.unavailable"
    assert p.meta_sync.next_attempt_at == NOW + timedelta(minutes=2)

    p.update(now=NOW, price=Amount(1))
    assert p.meta_sync.status is MetaSyncStatus.PENDING
    assert (p.meta_sync.attempts, p.meta_sync.error_code) == (0, None)


def test_retry_delay_is_capped() -> None:
    assert meta_retry_delay(1) == timedelta(minutes=1)
    assert meta_retry_delay(4) == timedelta(minutes=8)
    assert meta_retry_delay(100) == timedelta(hours=6)


def test_waiting_leaves_the_retry_cron() -> None:
    p = product()
    p.wait_for_meta_sync(p.meta_sync.version, "catalog.no_meta_catalog")
    assert p.meta_sync.status is MetaSyncStatus.PENDING
    assert p.meta_sync.next_attempt_at is None
    assert p.meta_sync.error_code == "catalog.no_meta_catalog"


def test_ready_photo_asks_for_a_sync_and_gives_the_1024_image() -> None:
    p = product()
    second = p.add_photo(content_type="image/jpeg")
    first = p.add_photo(content_type="image/jpeg")
    first.position, second.position = 0, 1
    p.mark_photo_ready(second.id, {400: "b_400", 1024: "b_1024"}, NOW)
    p.mark_photo_ready(first.id, {400: "a_400", 1024: "a_1024"}, NOW)

    assert len(sync_events(p)) == 1
    assert p.meta_image_keys == ["a_1024", "b_1024"]
