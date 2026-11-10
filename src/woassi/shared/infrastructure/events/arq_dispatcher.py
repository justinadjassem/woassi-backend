"""Sends deferred event subscribers to the arq worker."""

from arq.connections import ArqRedis

from woassi.shared.domain.events import DomainEvent


class ArqDeferredDispatcher:
    """Queues a `dispatch_event` job per deferred subscriber."""

    def __init__(self, pool: ArqRedis) -> None:
        self._pool = pool

    async def defer(self, subscriber: str, event: DomainEvent) -> None:
        # arq logs the repr of job arguments: personal data in an event needs repr=False.
        """Queues `event` for `subscriber`. The event is pickled into the job."""
        await self._pool.enqueue_job("dispatch_event", subscriber, event)
