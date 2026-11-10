"""Messages sent by shops, and their delivery status."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class OutboundStatus(StrEnum):
    """Delivery status. Only moves forward, and FAILED is final."""

    ACCEPTED = "ACCEPTED"  # by the API; delivery is reported later through webhooks
    SENT = "SENT"
    DELIVERED = "DELIVERED"
    READ = "READ"
    FAILED = "FAILED"


# Webhooks may come out of order: a status never goes back to an earlier one.
_RANK = {
    OutboundStatus.ACCEPTED: 0,
    OutboundStatus.SENT: 1,
    OutboundStatus.DELIVERED: 2,
    OutboundStatus.READ: 3,
}
_FROM_META = {
    "sent": OutboundStatus.SENT,
    "delivered": OutboundStatus.DELIVERED,
    "read": OutboundStatus.READ,
    "failed": OutboundStatus.FAILED,
}


def status_from_meta(value: str) -> OutboundStatus | None:
    """Status matching Meta's `status` value, or None for one we do not track."""
    return _FROM_META.get(value)


@dataclass(slots=True)
class OutboundMessage:
    """A message a shop sent. `kind` tells what for, e.g. `order.validation`."""

    id: str  # Meta's message id (wamid)
    shop_id: UUID
    recipient: str = field(repr=False)
    kind: str
    created_at: datetime
    status: OutboundStatus = OutboundStatus.ACCEPTED
    error_code: int | None = None
    updated_at: datetime | None = None

    def apply(self, status: OutboundStatus, error_code: int | None, now: datetime) -> bool:
        """Return False when the update brings nothing new."""
        if self.status is OutboundStatus.FAILED:
            return False
        if status is OutboundStatus.FAILED:
            self.error_code = error_code
        elif _RANK[status] <= _RANK[self.status]:
            return False
        self.status = status
        self.updated_at = now
        return True
