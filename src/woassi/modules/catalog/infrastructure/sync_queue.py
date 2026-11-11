"""Catalog sync jobs, queued in arq."""

from uuid import UUID

from arq.connections import ArqRedis

# Gathers the changes of the next few seconds (a merchant editing several products, a cart
# emptying several stocks) into one job, so one Graph call.
SYNC_DELAY_S = 5


class ArqCatalogSyncQueue:
    """Queues `sync_catalog` jobs for the worker, one per shop at a time."""

    def __init__(self, pool: ArqRedis, *, delay_s: int = SYNC_DELAY_S) -> None:
        self._pool = pool
        self._delay_s = delay_s

    async def enqueue_sync(self, shop_id: UUID) -> None:
        # Deterministic job id: while a sync of this shop is queued or running, arq refuses a
        # second one. The running job loops until nothing is due, and the cron covers the rest.
        await self._pool.enqueue_job(
            "sync_catalog",
            str(shop_id),
            _job_id=f"sync_catalog:{shop_id}",
            _defer_by=self._delay_s,
        )
