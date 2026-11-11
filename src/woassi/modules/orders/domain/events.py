"""Domain events of the orders module."""

from dataclasses import dataclass
from uuid import UUID

from woassi.shared.domain.events import DomainEvent


@dataclass(frozen=True, kw_only=True)
class OrderConfirmed(DomainEvent):
    """The merchant (or the automatic validation) accepted the order: it can be invoiced."""

    order_id: UUID
    shop_id: UUID
    total: int


@dataclass(frozen=True, kw_only=True)
class OrderClosed(DomainEvent):
    """Refused, cancelled or expired: it will never be invoiced."""

    order_id: UUID
    shop_id: UUID
    status: str


@dataclass(frozen=True, kw_only=True)
class OrderPaid(DomainEvent):
    """The order is paid in full: it moved from CONFIRMED to PAID."""

    order_id: UUID
    shop_id: UUID
