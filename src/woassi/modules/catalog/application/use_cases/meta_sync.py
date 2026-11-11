"""Publishing products to the Meta catalog linked to the shop's WhatsApp number.

Carts refer to products by `retailer_id`, so Meta must know each of them. A change records
`ProductMetaSyncRequested`, whose subscriber queues one `sync_catalog` job per shop: many
changes in a row cost a single Graph call. The job reads the products' current state, so the
order in which events arrive does not matter. A cron retries what is left PENDING or FAILED.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from uuid import UUID

import structlog

from woassi.modules.catalog.application.ports import (
    CatalogSyncQueue,
    CatalogUnitOfWork,
    MetaCatalogAccess,
    MetaCatalogAccessPort,
    MetaCatalogError,
    MetaCatalogItem,
    MetaCatalogPort,
)
from woassi.modules.catalog.domain.product import Product
from woassi.shared.application.ports import Clock, StoragePort

log = structlog.get_logger()

# Meta accepts up to 5,000 requests per batch and recommends fewer than 3,000.
BATCH_SIZE = 500
MAX_BATCHES = 20
MAX_SHOPS_PER_RUN = 1000

NO_CATALOG = "catalog.no_meta_catalog"
NO_PHOTO = "catalog.photo_missing"


@dataclass(frozen=True, slots=True)
class _Plan:
    """What one batch does with each product, keyed by product id with its version."""

    upserts: dict[UUID, MetaCatalogItem]
    deletes: dict[UUID, str]
    unpublished: list[UUID]  # inactive and never sent: nothing to tell Meta
    no_photo: list[UUID]


class SyncCatalogToMeta:
    """Sends a shop's pending products to its Meta catalog, in batches. Run by the worker.

    Graph calls run outside any transaction; results are written under the products' row
    locks, and only to the version that was sent.
    """

    def __init__(
        self,
        uow: CatalogUnitOfWork,
        catalogs: MetaCatalogAccessPort,
        meta: MetaCatalogPort,
        images: StoragePort,
        clock: Clock,
        *,
        link_base_url: str,
        batch_size: int = BATCH_SIZE,
        max_batches: int = MAX_BATCHES,
    ) -> None:
        self._uow = uow
        self._catalogs = catalogs
        self._meta = meta
        self._images = images
        self._clock = clock
        self._link_base_url = link_base_url.rstrip("/")
        self._batch_size = batch_size
        self._max_batches = max_batches

    async def execute(self, shop_id: UUID) -> int:
        """Returns how many products were sent to Meta (upserted or deleted)."""
        sent = 0
        access: MetaCatalogAccess | None = None
        for _ in range(self._max_batches):
            async with self._uow as uow:
                products = await uow.products.list_meta_sync_due(
                    shop_id, self._clock.now(), self._batch_size
                )
            if not products:
                break
            versions = {product.id: product.meta_sync.version for product in products}

            access = access or await self._catalogs.for_shop(shop_id)
            if access is None:
                # Stays PENDING: connecting the number resyncs the whole shop.
                await self._apply(shop_id, versions, waiting=dict.fromkeys(versions, NO_CATALOG))
                break

            plan = self._plan(products, access)
            failure: str | None = None
            rejected: Mapping[str, str] = {}
            if plan.upserts or plan.deletes:
                try:
                    rejected = await self._meta.write(
                        access, list(plan.upserts.values()), list(plan.deletes.values())
                    )
                except MetaCatalogError as error:
                    failure = error.failure.value
                    log.warning(
                        "meta_catalog_sync_failed",
                        shop_id=str(shop_id),
                        failure=failure,
                        meta_code=error.meta_code,
                    )
            await self._apply(shop_id, versions, plan=plan, failure=failure, rejected=rejected)
            sent += len(plan.upserts) + len(plan.deletes)
            if failure is not None:
                break
        return sent

    def _plan(self, products: Sequence[Product], access: MetaCatalogAccess) -> _Plan:
        """Sorts the products: upsert, delete, or nothing to send (yet)."""
        plan = _Plan(upserts={}, deletes={}, unpublished=[], no_photo=[])
        for product in products:
            if not product.active:
                if product.meta_sync.published:
                    plan.deletes[product.id] = product.retailer_id
                else:
                    plan.unpublished.append(product.id)
                continue
            image_keys = product.meta_image_keys
            if not image_keys:
                # Meta requires an image: the photo's processing will ask for a sync again.
                plan.no_photo.append(product.id)
                continue
            plan.upserts[product.id] = MetaCatalogItem(
                retailer_id=product.retailer_id,
                title=product.name,
                # Meta requires a description.
                description=product.description.strip() or product.name,
                price=product.price.value,
                in_stock=product.in_stock,
                image_urls=tuple(self._images.public_url(key) for key in image_keys),
                link=f"{self._link_base_url}/shops/{product.shop_id}/products/{product.id}",
                brand=access.brand,
            )
        return plan

    async def _apply(
        self,
        shop_id: UUID,
        versions: Mapping[UUID, int],
        *,
        plan: _Plan | None = None,
        failure: str | None = None,
        rejected: Mapping[str, str] | None = None,
        waiting: Mapping[UUID, str] | None = None,
    ) -> None:
        """Writes the outcome on each product, to the version that was read."""
        rejected = rejected or {}
        waiting = dict(waiting or {})
        if plan is not None:
            waiting.update(dict.fromkeys(plan.no_photo, NO_PHOTO))
        now = self._clock.now()
        async with self._uow as uow:
            for product in await uow.products.list_for_update(shop_id, ids=list(versions)):
                version = versions[product.id]
                if product.id in waiting:
                    product.wait_for_meta_sync(version, waiting[product.id])
                elif plan is not None and product.id in plan.unpublished:
                    product.mark_meta_synced(version, published=False, now=now)
                elif failure is not None:
                    product.mark_meta_sync_failed(version, failure, now)
                elif plan is not None and product.id in plan.upserts:
                    sent = plan.upserts[product.id].retailer_id
                    if sent in rejected:
                        product.mark_meta_sync_failed(version, rejected[sent], now)
                    else:
                        product.mark_meta_synced(version, published=True, now=now)
                elif plan is not None and product.id in plan.deletes:
                    sent = plan.deletes[product.id]
                    if sent in rejected:
                        product.mark_meta_sync_failed(version, rejected[sent], now)
                    else:
                        product.mark_meta_synced(version, published=False, now=now)
                await uow.products.save(product)
            await uow.commit()


class RequestCatalogSync:
    """Queues the sync of a shop's catalog; called on every `ProductMetaSyncRequested`."""

    def __init__(self, queue: CatalogSyncQueue) -> None:
        self._queue = queue

    async def execute(self, shop_id: UUID) -> None:
        await self._queue.enqueue_sync(shop_id)


class RequestCatalogResync:
    """Sends every product of a shop again: after a WhatsApp connection, or on demand."""

    def __init__(self, uow: CatalogUnitOfWork, queue: CatalogSyncQueue, clock: Clock) -> None:
        self._uow = uow
        self._queue = queue
        self._clock = clock

    async def execute(self, shop_id: UUID) -> int:
        """Marks the products PENDING and queues one sync job; returns how many are pending.

        Inactive products never sent to Meta are left alone.
        """
        now = self._clock.now()
        async with self._uow as uow:
            products = await uow.products.list_shop_for_update(shop_id)
            pending = 0
            for product in products:
                if product.active or product.meta_sync.published:
                    product.mark_meta_sync_pending(now)
                    await uow.products.save(product)
                    pending += 1
            await uow.commit()
        if pending:
            await self._queue.enqueue_sync(shop_id)
        return pending


class RetryCatalogSync:
    """Cron: queues a sync for every shop with a product due again (lost event, backoff)."""

    def __init__(self, uow: CatalogUnitOfWork, queue: CatalogSyncQueue, clock: Clock) -> None:
        self._uow = uow
        self._queue = queue
        self._clock = clock

    async def execute(self) -> int:
        """Returns how many shops were queued."""
        async with self._uow as uow:
            shop_ids = await uow.products.shops_with_meta_sync_due(
                self._clock.now(), MAX_SHOPS_PER_RUN
            )
        for shop_id in shop_ids:
            await self._queue.enqueue_sync(shop_id)
        return len(shop_ids)
