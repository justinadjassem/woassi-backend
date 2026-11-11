"""Domain events of the payments module. They are pickled for deferred subscribers: no
personal data.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from woassi.shared.domain.events import DomainEvent


@dataclass(frozen=True, kw_only=True)
class PaymentConfirmed(DomainEvent):
    """A payment of the order's total was validated, by KPrimePay or by the merchant."""

    payment_id: UUID
    shop_id: UUID
    order_id: UUID
    amount: int
    method: str  # "kprimepay" or "manual"
    paid_at: datetime
