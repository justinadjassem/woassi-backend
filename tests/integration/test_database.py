import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from woassi.modules.catalog.domain.errors import RetailerIdAlreadyUsed
from woassi.modules.catalog.domain.events import ProductCreated, ProductMetaSyncRequested
from woassi.modules.catalog.domain.product import Product
from woassi.modules.catalog.infrastructure.models import ProductModel
from woassi.modules.catalog.infrastructure.uow import SqlAlchemyCatalogUoW
from woassi.shared.domain.events import DomainEvent
from woassi.shared.domain.values import Amount
from woassi.shared.infrastructure.audit import AuditEntryModel
from woassi.shared.infrastructure.clock import FrozenClock
from woassi.shared.infrastructure.db.uow import SqlAlchemyUnitOfWork
from woassi.shared.infrastructure.idempotency import ProcessedKeyModel, process_once

pytestmark = pytest.mark.integration

NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)


class RecordingBus:
    def __init__(self) -> None:
        self.events: list[DomainEvent] = []

    async def publish(self, event: DomainEvent) -> None:
        self.events.append(event)


def product(shop_id: object = None) -> Product:
    return Product.create(
        now=NOW,
        shop_id=shop_id or uuid4(),  # type: ignore[arg-type]
        name="Pagne",
        price=Amount(5000),
        stock=1,
        retailer_id="SKU-1",
    )


async def test_events_reach_the_bus_once_the_row_is_visible(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    visible: list[int] = []

    class CheckingBus:
        async def publish(self, event: DomainEvent) -> None:
            async with sessions() as other_session:
                total = await other_session.scalar(select(func.count()).select_from(ProductModel))
                visible.append(total or 0)

    async with SqlAlchemyCatalogUoW(sessions, CheckingBus(), FrozenClock(NOW)) as uow:
        await uow.products.add(product())
        await uow.commit()

    assert visible == [1, 1]  # ProductCreated, then ProductMetaSyncRequested


async def test_rollback_publishes_nothing_and_saves_nothing(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    bus = RecordingBus()
    shop_id = uuid4()
    async with SqlAlchemyCatalogUoW(sessions, bus, FrozenClock(NOW)) as uow:
        await uow.products.add(product(shop_id))
        await uow.rollback()

    async with SqlAlchemyCatalogUoW(sessions, bus, FrozenClock(NOW)) as uow:
        assert await uow.products.count(shop_id) == 0
    assert bus.events == []


async def test_commit_publishes_the_entity_events(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    bus = RecordingBus()
    created = product()
    async with SqlAlchemyCatalogUoW(sessions, bus, FrozenClock(NOW)) as uow:
        await uow.products.add(created)
        await uow.commit()

    event, sync = bus.events
    assert isinstance(event, ProductCreated)
    assert event.product_id == created.id
    assert isinstance(sync, ProductMetaSyncRequested)


async def test_a_refused_insert_publishes_nothing(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    bus = RecordingBus()
    shop_id = uuid4()
    async with SqlAlchemyCatalogUoW(sessions, bus, FrozenClock(NOW)) as uow:
        await uow.products.add(product(shop_id))
        await uow.commit()
    bus.events.clear()

    with pytest.raises(RetailerIdAlreadyUsed):
        async with SqlAlchemyCatalogUoW(sessions, bus, FrozenClock(NOW)) as uow:
            await uow.products.add(product(shop_id))
            await uow.commit()

    assert bus.events == []


async def record_entry(uow: SqlAlchemyUnitOfWork, shop_id: object) -> None:
    await uow.audit.record(
        account_id=uuid4(),
        action="order.validated",
        target_type="order",
        target_id="42",
        shop_id=shop_id,  # type: ignore[arg-type]
        details={"total": 5000},
        ip="203.0.113.7",
    )


async def test_audit_entry_is_committed_with_the_action(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    shop_id = uuid4()
    async with SqlAlchemyCatalogUoW(sessions, RecordingBus(), FrozenClock(NOW)) as uow:
        await uow.products.add(product(shop_id))
        await record_entry(uow, shop_id)
        await uow.commit()

    async with SqlAlchemyUnitOfWork(sessions, RecordingBus(), FrozenClock(NOW)) as uow:
        [entry] = await uow.audit.list(shop_id, 0, 10)
        assert await uow.audit.count(shop_id) == 1
    assert entry.action == "order.validated"
    assert entry.details == {"total": 5000}
    assert entry.created_at == NOW


async def test_audit_entry_is_rolled_back_with_the_action(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    shop_id = uuid4()
    async with SqlAlchemyCatalogUoW(sessions, RecordingBus(), FrozenClock(NOW)) as uow:
        await uow.products.add(product(shop_id))
        await record_entry(uow, shop_id)
        await uow.rollback()

    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(AuditEntryModel)) == 0


async def test_audit_entry_needs_no_shop(sessions: async_sessionmaker[AsyncSession]) -> None:
    async with SqlAlchemyUnitOfWork(sessions, RecordingBus(), FrozenClock(NOW)) as uow:
        await record_entry(uow, None)
        await uow.commit()

    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(AuditEntryModel)) == 1


async def run_once(
    sessions: async_sessionmaker[AsyncSession], calls: list[str], key: str = "wamid.1"
) -> None:
    async with sessions() as session:
        async with process_once(session, "meta", key) as is_new:
            if is_new:
                calls.append(key)
        await session.commit()


async def test_same_key_twice_runs_once(sessions: async_sessionmaker[AsyncSession]) -> None:
    calls: list[str] = []
    await run_once(sessions, calls)
    await run_once(sessions, calls)
    assert calls == ["wamid.1"]


async def test_another_key_or_source_runs_again(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    calls: list[str] = []
    await run_once(sessions, calls, "a")
    await run_once(sessions, calls, "b")
    async with sessions() as session:
        async with process_once(session, "aggregator", "a") as is_new:
            assert is_new
        await session.commit()
    assert calls == ["a", "b"]


async def test_concurrent_duplicates_run_once(sessions: async_sessionmaker[AsyncSession]) -> None:
    calls: list[str] = []
    await asyncio.gather(*(run_once(sessions, calls) for _ in range(10)))
    assert calls == ["wamid.1"]


async def test_a_failed_block_releases_the_key(sessions: async_sessionmaker[AsyncSession]) -> None:
    async with sessions() as session:
        with pytest.raises(RuntimeError):
            async with process_once(session, "meta", "wamid.1"):
                raise RuntimeError("boom")
        await session.commit()

    calls: list[str] = []
    await run_once(sessions, calls)
    assert calls == ["wamid.1"]


async def test_a_rolled_back_transaction_releases_the_key(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as session:
        async with process_once(session, "meta", "wamid.1") as is_new:
            assert is_new
        await session.rollback()

    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(ProcessedKeyModel)) == 0
