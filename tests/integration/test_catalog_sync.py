from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from arq.connections import ArqRedis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from woassi.modules.catalog.domain.product import MetaSyncStatus, Product
from woassi.modules.catalog.infrastructure.sync_queue import ArqCatalogSyncQueue
from woassi.modules.catalog.infrastructure.uow import SqlAlchemyCatalogUoW
from woassi.shared.domain.events import DomainEvent
from woassi.shared.domain.values import Amount
from woassi.shared.infrastructure.clock import FrozenClock

pytestmark = pytest.mark.integration

NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)


class NullBus:
    async def publish(self, event: DomainEvent) -> None:
        return None


def uow(sessions: async_sessionmaker[AsyncSession]) -> SqlAlchemyCatalogUoW:
    return SqlAlchemyCatalogUoW(sessions, NullBus(), FrozenClock(NOW))


async def add(
    sessions: async_sessionmaker[AsyncSession], shop_id: UUID, retailer_id: str
) -> Product:
    product = Product.create(
        now=NOW,
        shop_id=shop_id,
        name="Pagne",
        price=Amount(5000),
        stock=1,
        retailer_id=retailer_id,
    )
    async with uow(sessions) as u:
        await u.products.add(product)
        await u.commit()
    return product


async def test_meta_sync_state_round_trips(sessions: async_sessionmaker[AsyncSession]) -> None:
    product = await add(sessions, uuid4(), "SKU-1")
    async with uow(sessions) as u:
        [locked] = await u.products.list_for_update(product.shop_id, ids=[product.id])
        locked.mark_meta_sync_failed(locked.meta_sync.version, "meta.rate_limited", NOW)
        locked.meta_sync.published = True
        await u.products.save(locked)
        await u.commit()

    async with uow(sessions) as u:
        stored = await u.products.get(product.shop_id, product.id)

    assert stored is not None
    assert stored.meta_sync == locked.meta_sync
    assert stored.meta_sync.status is MetaSyncStatus.FAILED


async def test_due_queries(sessions: async_sessionmaker[AsyncSession]) -> None:
    shop, other_shop = uuid4(), uuid4()
    pending = await add(sessions, shop, "PENDING")
    failed = await add(sessions, shop, "FAILED")
    waiting = await add(sessions, shop, "WAITING")
    synced = await add(sessions, shop, "SYNCED")
    await add(sessions, other_shop, "OTHER")
    async with uow(sessions) as u:
        for product in await u.products.list_shop_for_update(shop):
            version = product.meta_sync.version
            if product.id == failed.id:
                product.mark_meta_sync_failed(version, "meta.unavailable", NOW)
            elif product.id == waiting.id:
                product.wait_for_meta_sync(version, "catalog.no_meta_catalog")
            elif product.id == synced.id:
                product.mark_meta_synced(version, published=True, now=NOW)
            await u.products.save(product)
        await u.commit()

    async with uow(sessions) as u:
        due_now = await u.products.list_meta_sync_due(shop, NOW, 10)
        due_later = await u.products.list_meta_sync_due(shop, NOW + timedelta(minutes=1), 10)
        shops_now = await u.products.shops_with_meta_sync_due(NOW, 10)
        shops_later = await u.products.shops_with_meta_sync_due(NOW + timedelta(minutes=5), 10)

    assert [p.id for p in due_now] == [pending.id]
    assert {p.id for p in due_later} == {pending.id, failed.id}
    assert shops_now == []
    assert set(shops_later) == {shop, other_shop}


async def test_sync_jobs_of_a_shop_are_coalesced(pool: ArqRedis) -> None:
    queue = ArqCatalogSyncQueue(pool)
    shop_id = uuid4()

    for _ in range(3):
        await queue.enqueue_sync(shop_id)
    await queue.enqueue_sync(uuid4())

    assert len(await pool.queued_jobs()) == 2
