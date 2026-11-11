"""Payment checks, queued in arq."""

from uuid import UUID

from arq.connections import ArqRedis


class ArqPaymentCheckQueue:
    """Queues one `verify_kprimepay_payment` job per webhook worth a check."""

    def __init__(self, pool: ArqRedis) -> None:
        self._pool = pool

    async def enqueue(self, shop_id: UUID, payment_id: UUID) -> None:
        # Ids only: arq logs the repr of job arguments.
        await self._pool.enqueue_job("verify_kprimepay_payment", str(shop_id), str(payment_id))
