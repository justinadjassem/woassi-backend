"""Domain events and the `Entity` base class that records them."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4


def _utc_now() -> datetime:
    """Default timestamp of an event; the domain has no `Clock` to inject."""
    return datetime.now(UTC)


@dataclass(frozen=True, kw_only=True)
class DomainEvent:
    """Something that happened. Subclasses must also be frozen and kw_only."""

    event_id: UUID = field(default_factory=uuid4)
    occurred_at: datetime = field(default_factory=_utc_now)


@dataclass(slots=True)
class Entity:
    """Records domain events; the Unit of Work publishes them after the commit."""

    _events: list[DomainEvent] = field(default_factory=list, init=False, repr=False, compare=False)

    def record_event(self, event: DomainEvent) -> None:
        """Queues `event`, published once the transaction that saves this entity commits."""
        self._events.append(event)

    def pull_events(self) -> list[DomainEvent]:
        """Returns the queued events and clears the queue, so each is published once."""
        events, self._events = self._events, []
        return events
