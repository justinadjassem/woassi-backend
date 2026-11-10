"""In-process event bus."""

from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

import structlog

from woassi.shared.domain.events import DomainEvent

log = structlog.get_logger()


class DeferredDispatcher(Protocol):
    """Hands a deferred subscriber's event over to the worker."""

    async def defer(self, subscriber: str, event: DomainEvent) -> None: ...


@dataclass(frozen=True, slots=True)
class _Subscriber:
    """A registered handler. `name` identifies it in queued jobs."""

    name: str
    handler: Callable[[Any], Awaitable[None]]
    deferred: bool


class InProcessEventBus:
    """Calls subscribers registered at startup.

    The bus publishes after the commit: a failing subscriber is logged and does not reach the
    caller, whose action is already saved. Deferred subscribers run in the worker through the
    `dispatch_event` job, so `name` must stay stable across deployments.
    """

    def __init__(self, dispatcher: DeferredDispatcher | None = None) -> None:
        self._dispatcher = dispatcher
        self._by_event: defaultdict[type[DomainEvent], list[_Subscriber]] = defaultdict(list)
        self._by_name: dict[str, _Subscriber] = {}

    def subscribe[E: DomainEvent](
        self,
        event_type: type[E],
        handler: Callable[[E], Awaitable[None]],
        *,
        name: str,
        deferred: bool = False,
    ) -> None:
        """Registers `handler` for `event_type`.

        `name` must be unique. With `deferred=True`, the handler runs in the worker instead of
        in the publishing process.
        """
        if name in self._by_name:
            raise ValueError(f"Subscriber {name} is already registered.")
        if deferred and self._dispatcher is None:
            raise ValueError("A deferred subscriber needs a dispatcher.")
        subscriber = _Subscriber(name=name, handler=handler, deferred=deferred)
        self._by_name[name] = subscriber
        self._by_event[event_type].append(subscriber)

    async def publish(self, event: DomainEvent) -> None:
        """Calls (or defers) every subscriber of `event`'s exact type.

        Errors are logged, never raised: the action that produced the event is already committed.
        """
        for subscriber in self._by_event.get(type(event), ()):
            try:
                if subscriber.deferred:
                    assert self._dispatcher is not None
                    await self._dispatcher.defer(subscriber.name, event)
                else:
                    await subscriber.handler(event)
            except Exception:
                log.exception(
                    "event_subscriber_failed",
                    subscriber=subscriber.name,
                    event_type=type(event).__name__,
                    event_id=str(event.event_id),
                )

    async def dispatch(self, subscriber: str, event: DomainEvent) -> None:
        """Run a deferred subscriber, in the worker. Errors propagate to arq."""
        await self._by_name[subscriber].handler(event)
