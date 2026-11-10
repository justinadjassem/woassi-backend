"""Webhook events, queued in arq."""

from arq.connections import ArqRedis

from woassi.modules.messaging.domain.event import MetaEvent


class ArqEventQueue:
    """Queues one `process_meta_event` job per webhook event."""

    def __init__(self, pool: ArqRedis) -> None:
        self._pool = pool

    async def enqueue(self, event: MetaEvent) -> None:
        # The object, not a dict: arq logs the repr of its arguments, see MetaEvent.
        await self._pool.enqueue_job("process_meta_event", event)
