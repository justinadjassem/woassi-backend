from collections.abc import Mapping, Sequence
from datetime import timedelta
from uuid import UUID, uuid4

import pytest

from tests.fakes import NOW, FakeCatalogSyncQueue, FakeStorage, FakeUoW
from woassi.modules.catalog.application.ports import (
    ITEM_REJECTED,
    MetaCatalogAccess,
    MetaCatalogError,
    MetaCatalogFailure,
    MetaCatalogItem,
)
from woassi.modules.catalog.application.use_cases.meta_sync import (
    NO_CATALOG,
    NO_PHOTO,
    RequestCatalogResync,
    RetryCatalogSync,
    SyncCatalogToMeta,
)
from woassi.modules.catalog.application.use_cases.products import UpdateProduct
from woassi.modules.catalog.application.use_cases.stock import ReleaseStock, ReserveStock
from woassi.modules.catalog.domain.events import ProductMetaSyncRequested
from woassi.modules.catalog.domain.product import MetaSyncStatus, Product
from woassi.shared.domain.values import Amount
from woassi.shared.infrastructure.clock import FrozenClock

ACCESS = MetaCatalogAccess(catalog_id="CAT-1", access_token="secret-token", brand="Chez Ama")


class FakeCatalogs:
    def __init__(self, access: MetaCatalogAccess | None = ACCESS) -> None:
        self.access = access

    async def for_shop(self, shop_id: UUID) -> MetaCatalogAccess | None:
        return self.access


class FakeMetaCatalog:
    def __init__(
        self, *, rejected: Mapping[str, str] | None = None, error: MetaCatalogError | None = None
    ) -> None:
        self.batches: list[tuple[list[MetaCatalogItem], list[str]]] = []
        self.rejected = dict(rejected or {})
        self.error = error
        self.on_write: object = None

    async def write(
        self,
        access: MetaCatalogAccess,
        upserts: Sequence[MetaCatalogItem],
        deletes: Sequence[str],
    ) -> Mapping[str, str]:
        self.batches.append((list(upserts), list(deletes)))
        if callable(self.on_write):
            await self.on_write()
        if self.error:
            raise self.error
        return self.rejected


class World:
    def __init__(self, meta: FakeMetaCatalog | None = None, *, connected: bool = True) -> None:
        self.uow = FakeUoW()
        self.clock = FrozenClock(NOW)
        self.images = FakeStorage("https://img")
        self.meta = meta or FakeMetaCatalog()
        self.catalogs = FakeCatalogs(ACCESS if connected else None)
        self.queue = FakeCatalogSyncQueue()
        self.shop_id = uuid4()

    def sync(self, *, batch_size: int = 500) -> SyncCatalogToMeta:
        return SyncCatalogToMeta(
            self.uow,
            self.catalogs,
            self.meta,
            self.images,
            self.clock,
            link_base_url="https://woassi.test/",
            batch_size=batch_size,
        )

    async def product(
        self, retailer_id: str = "SKU-1", *, stock: int = 2, photo: bool = True
    ) -> Product:
        product = Product.create(
            now=NOW,
            shop_id=self.shop_id,
            name="Pagne",
            price=Amount(5000),
            stock=stock,
            retailer_id=retailer_id,
        )
        if photo:
            ph = product.add_photo(content_type="image/jpeg")
            product.mark_photo_ready(
                ph.id, {400: f"{retailer_id}_400", 1024: f"{retailer_id}_1024"}, NOW
            )
        async with self.uow as uow:
            await uow.products.add(product)
            await uow.commit()
        return product

    def stored(self, product: Product) -> Product:
        return self.uow.repository.products[product.id]


async def test_sends_pending_products_in_one_batch() -> None:
    world = World()
    first = await world.product("SKU-1")
    await world.product("SKU-2", stock=0)

    assert await world.sync().execute(world.shop_id) == 2

    [(upserts, deletes)] = world.meta.batches
    assert deletes == []
    item = next(i for i in upserts if i.retailer_id == "SKU-1")
    assert item == MetaCatalogItem(
        retailer_id="SKU-1",
        title="Pagne",
        description="Pagne",
        price=5000,
        in_stock=True,
        image_urls=("https://img/SKU-1_1024",),
        link=f"https://woassi.test/shops/{world.shop_id}/products/{first.id}",
        brand="Chez Ama",
    )
    assert next(i for i in upserts if i.retailer_id == "SKU-2").in_stock is False
    sync = world.stored(first).meta_sync
    assert (sync.status, sync.published, sync.synced_at) == (MetaSyncStatus.SYNCED, True, NOW)


async def test_nothing_left_to_send_makes_no_call() -> None:
    world = World()
    await world.product()
    await world.sync().execute(world.shop_id)

    assert await world.sync().execute(world.shop_id) == 0
    assert len(world.meta.batches) == 1


async def test_without_a_connected_catalog_products_wait_silently() -> None:
    world = World(connected=False)
    product = await world.product()

    assert await world.sync().execute(world.shop_id) == 0

    sync = world.stored(product).meta_sync
    assert (sync.status, sync.error_code, sync.next_attempt_at) == (
        MetaSyncStatus.PENDING,
        NO_CATALOG,
        None,
    )
    assert world.meta.batches == []
    # Out of the retry cron until the number connects.
    world.clock.advance(timedelta(days=1))
    assert await RetryCatalogSync(world.uow, world.queue, world.clock).execute() == 0


async def test_product_without_photo_waits_for_it() -> None:
    world = World()
    product = await world.product(photo=False)

    await world.sync().execute(world.shop_id)

    assert world.meta.batches == []
    assert world.stored(product).meta_sync.error_code == NO_PHOTO
    assert world.stored(product).meta_sync.next_attempt_at is None


async def test_deactivated_product_is_deleted_only_if_meta_has_it() -> None:
    world = World()
    published = await world.product("SKU-1")
    await world.sync().execute(world.shop_id)
    never_sent = await world.product("SKU-2", photo=False)
    update = UpdateProduct(world.uow, world.images, world.clock)
    await update.execute(shop_id=world.shop_id, product_id=published.id, active=False)
    await update.execute(shop_id=world.shop_id, product_id=never_sent.id, active=False)

    await world.sync().execute(world.shop_id)

    assert world.meta.batches[-1] == ([], ["SKU-1"])
    for product in (published, never_sent):
        sync = world.stored(product).meta_sync
        assert (sync.status, sync.published) == (MetaSyncStatus.SYNCED, False)


async def test_item_rejected_by_meta_fails_alone() -> None:
    world = World(FakeMetaCatalog(rejected={"SKU-2": ITEM_REJECTED}))
    good = await world.product("SKU-1")
    bad = await world.product("SKU-2")

    await world.sync().execute(world.shop_id)

    assert world.stored(good).meta_sync.status is MetaSyncStatus.SYNCED
    bad_sync = world.stored(bad).meta_sync
    assert (bad_sync.status, bad_sync.error_code) == (MetaSyncStatus.FAILED, ITEM_REJECTED)
    assert bad_sync.next_attempt_at == NOW + timedelta(minutes=1)


async def test_whole_batch_failure_is_retried_by_the_cron_with_backoff() -> None:
    world = World(FakeMetaCatalog(error=MetaCatalogError(MetaCatalogFailure.UNAVAILABLE)))
    product = await world.product()

    await world.sync().execute(world.shop_id)

    sync = world.stored(product).meta_sync
    assert (sync.status, sync.error_code) == (MetaSyncStatus.FAILED, "meta.unavailable")
    retry = RetryCatalogSync(world.uow, world.queue, world.clock)
    assert await retry.execute() == 0  # not due yet
    # An event-triggered run does not resend it before its time either.
    await world.sync().execute(world.shop_id)
    assert len(world.meta.batches) == 1

    world.clock.advance(timedelta(minutes=1))
    assert await retry.execute() == 1
    assert world.queue.shops == [world.shop_id]

    world.meta.error = None
    await world.sync().execute(world.shop_id)
    assert world.stored(product).meta_sync.status is MetaSyncStatus.SYNCED


async def test_change_during_the_graph_call_is_sent_next() -> None:
    world = World()
    product = await world.product()

    async def merchant_edits() -> None:
        if len(world.meta.batches) == 1:
            update = UpdateProduct(world.uow, world.images, world.clock)
            await update.execute(shop_id=world.shop_id, product_id=product.id, price=7000)

    world.meta.on_write = merchant_edits
    await world.sync().execute(world.shop_id)

    assert [batch[0][0].price for batch in world.meta.batches] == [5000, 7000]
    assert world.stored(product).meta_sync.status is MetaSyncStatus.SYNCED


async def test_sends_in_several_batches() -> None:
    world = World()
    for index in range(5):
        await world.product(f"SKU-{index}")

    assert await world.sync(batch_size=2).execute(world.shop_id) == 5
    assert [len(upserts) for upserts, _ in world.meta.batches] == [2, 2, 1]


async def test_lost_event_is_caught_up_by_the_cron() -> None:
    world = World()
    await world.product()
    retry = RetryCatalogSync(world.uow, world.queue, world.clock)

    assert await retry.execute() == 0
    world.clock.advance(timedelta(minutes=5))
    assert await retry.execute() == 1


async def test_resync_marks_every_product_and_queues_one_job() -> None:
    world = World()
    for index in range(3):
        await world.product(f"SKU-{index}")
    await world.sync().execute(world.shop_id)
    world.uow.events.clear()

    pending = await RequestCatalogResync(world.uow, world.queue, world.clock).execute(world.shop_id)

    assert pending == 3
    assert world.queue.shops == [world.shop_id]
    assert world.uow.events == []  # no event per product
    await world.sync().execute(world.shop_id)
    assert len(world.meta.batches[-1][0]) == 3


async def test_resync_of_an_empty_shop_queues_nothing() -> None:
    world = World()
    assert await RequestCatalogResync(world.uow, world.queue, world.clock).execute(uuid4()) == 0
    assert world.queue.shops == []


async def test_stock_events_only_when_availability_flips() -> None:
    world = World()
    product = await world.product(stock=3)
    world.uow.events.clear()
    reserve, release = ReserveStock(world.uow, world.clock), ReleaseStock(world.uow, world.clock)

    await reserve.execute(shop_id=world.shop_id, requested={"SKU-1": 2})
    assert world.uow.events == []

    await reserve.execute(shop_id=world.shop_id, requested={"SKU-1": 5})
    assert [type(e) for e in world.uow.events] == [ProductMetaSyncRequested]

    await release.execute(shop_id=world.shop_id, quantities={product.id: 3})
    assert len(world.uow.events) == 2


@pytest.mark.parametrize("stock", [0, 7])
async def test_update_product_stock_only_syncs_on_a_flip(stock: int) -> None:
    world = World()
    product = await world.product(stock=2)
    world.uow.events.clear()

    view = await UpdateProduct(world.uow, world.images, world.clock).execute(
        shop_id=world.shop_id, product_id=product.id, stock=stock
    )

    assert view.stock == stock
    assert len(world.uow.events) == (1 if stock == 0 else 0)
