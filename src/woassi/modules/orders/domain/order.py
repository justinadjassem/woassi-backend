"""Orders and their life cycle.

AWAITING_CUSTOMER (partial cart) -> AWAITING_MERCHANT -> CONFIRMED, or closed: REJECTED,
CANCELLED or EXPIRED. A complete cart skips AWAITING_CUSTOMER; an automatic validation skips
AWAITING_MERCHANT.
"""

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID, uuid4

from woassi.modules.orders.domain.errors import OrderNotOpen
from woassi.modules.orders.domain.events import OrderClosed, OrderConfirmed
from woassi.shared.domain.events import Entity
from woassi.shared.domain.values import Amount


class OrderStatus(StrEnum):
    """Where an order stands. Only the two AWAITING statuses are open."""

    AWAITING_CUSTOMER = "AWAITING_CUSTOMER"  # part of the cart is missing: does the rest do?
    AWAITING_MERCHANT = "AWAITING_MERCHANT"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


OPEN = frozenset({OrderStatus.AWAITING_CUSTOMER, OrderStatus.AWAITING_MERCHANT})


class ClosingReason(StrEnum):
    """Why an order closed without being confirmed."""

    OUT_OF_STOCK = "OUT_OF_STOCK"
    MERCHANT_REFUSED = "MERCHANT_REFUSED"
    CUSTOMER_DECLINED = "CUSTOMER_DECLINED"
    EXPIRED = "EXPIRED"


class DecidedBy(StrEnum):
    """Who confirmed or closed the order."""

    MERCHANT = "MERCHANT"
    AUTOMATIC = "AUTOMATIC"
    CUSTOMER = "CUSTOMER"
    SYSTEM = "SYSTEM"


class ValidationMode(StrEnum):
    """MANUAL: the merchant confirms every order. AUTOMATIC: up to a maximum amount."""

    MANUAL = "MANUAL"
    AUTOMATIC = "AUTOMATIC"


@dataclass(frozen=True, slots=True)
class ValidationPolicy:
    """How a shop validates orders."""

    mode: ValidationMode
    max_amount: Amount | None = None

    def needs_merchant(self, total: Amount) -> bool:
        """True if an order of `total` must wait for the merchant."""
        if self.mode is ValidationMode.MANUAL:
            return True
        return self.max_amount is not None and total > self.max_amount


@dataclass(frozen=True, slots=True)
class Deadlines:
    """When to remind the merchant, counted from the order reaching them, and when to expire."""

    reminder_1_minutes: int = 15
    reminder_2_minutes: int = 60
    expiration_hours: int = 24


@dataclass(frozen=True, slots=True)
class OrderLine:
    """One cart line, with the price at the time of the order."""

    retailer_id: str
    name: str
    unit_price: Amount
    requested: int
    quantity: int  # what is reserved, at most `requested`
    product_id: UUID | None = None

    @property
    def subtotal(self) -> Amount:
        """Price of the reserved quantity."""
        return self.unit_price * self.quantity

    @property
    def complete(self) -> bool:
        """True if everything requested could be reserved."""
        return self.quantity >= self.requested


class DueAction(StrEnum):
    """What the cron has to do with an order now."""

    REMIND = "REMIND"
    EXPIRE = "EXPIRE"


@dataclass(slots=True)
class Order(Entity):
    """An order from a WhatsApp cart. Numbered per shop."""

    id: UUID
    shop_id: UUID
    number: int
    customer_id: UUID
    source_message_id: str
    lines: tuple[OrderLine, ...]
    status: OrderStatus
    created_at: datetime
    expires_at: datetime
    deadlines: Deadlines = field(default_factory=Deadlines)
    note: str = field(default="", repr=False)
    awaiting_merchant_since: datetime | None = None
    next_reminder_at: datetime | None = None
    reminders_sent: int = 0
    merchant_message_ids: list[str] = field(default_factory=list)
    offer_message_id: str | None = None
    closed_at: datetime | None = None
    closing_reason: ClosingReason | None = None
    decided_by: DecidedBy | None = None

    @classmethod
    def receive(
        cls,
        *,
        shop_id: UUID,
        number: int,
        customer_id: UUID,
        source_message_id: str,
        lines: tuple[OrderLine, ...],
        note: str,
        policy: ValidationPolicy,
        deadlines: Deadlines,
        now: datetime,
    ) -> "Order":
        """Creates the order from reserved lines.

        Nothing reserved: rejected (out of stock). Everything reserved: submitted for
        validation.
        Otherwise it waits for the customer to accept the rest.
        """
        order = cls(
            id=uuid4(),
            shop_id=shop_id,
            number=number,
            customer_id=customer_id,
            source_message_id=source_message_id,
            lines=lines,
            status=OrderStatus.AWAITING_CUSTOMER,
            created_at=now,
            expires_at=now + timedelta(hours=deadlines.expiration_hours),
            deadlines=deadlines,
            note=note,
        )
        if not any(line.quantity for line in lines):
            order._close(OrderStatus.REJECTED, ClosingReason.OUT_OF_STOCK, DecidedBy.SYSTEM, now)
        elif all(line.complete for line in lines):
            order._submit(policy, now)
        return order

    @property
    def total(self) -> Amount:
        """Sum of the line subtotals."""
        total = Amount(0)
        for line in self.lines:
            total = total + line.subtotal
        return total

    @property
    def is_open(self) -> bool:
        """True while the order waits for the customer or the merchant."""
        return self.status in OPEN

    @property
    def available_lines(self) -> tuple[OrderLine, ...]:
        """Lines with something reserved."""
        return tuple(line for line in self.lines if line.quantity)

    @property
    def missing_lines(self) -> tuple[OrderLine, ...]:
        """Lines with less reserved than requested."""
        return tuple(line for line in self.lines if not line.complete)

    @property
    def reserved(self) -> dict[UUID, int]:
        """Stock held by this order, to give back if it closes."""
        return {
            line.product_id: line.quantity
            for line in self.lines
            if line.product_id is not None and line.quantity
        }

    def accept_offer(self, policy: ValidationPolicy, now: datetime) -> None:
        """The customer takes what is available."""
        self._require(OrderStatus.AWAITING_CUSTOMER)
        self.lines = tuple(replace(line, requested=line.quantity) for line in self.available_lines)
        self._submit(policy, now)

    def decline_offer(self, now: datetime) -> None:
        """The customer refuses the partial order: it is cancelled."""
        self._require(OrderStatus.AWAITING_CUSTOMER)
        self._close(OrderStatus.CANCELLED, ClosingReason.CUSTOMER_DECLINED, DecidedBy.CUSTOMER, now)

    def confirm(self, now: datetime, by: DecidedBy = DecidedBy.MERCHANT) -> None:
        """The merchant accepts. Raises OrderNotOpen unless awaiting the merchant."""
        self._require(OrderStatus.AWAITING_MERCHANT)
        self._confirm(by, now)

    def refuse(self, now: datetime) -> None:
        """The merchant refuses. Raises OrderNotOpen unless awaiting the merchant."""
        self._require(OrderStatus.AWAITING_MERCHANT)
        self._close(OrderStatus.REJECTED, ClosingReason.MERCHANT_REFUSED, DecidedBy.MERCHANT, now)

    def expire(self, now: datetime) -> None:
        """Nobody answered in time. Raises OrderNotOpen if already closed."""
        if not self.is_open:
            raise OrderNotOpen(f"Order {self.number} is {self.status.value}.")
        self._close(OrderStatus.EXPIRED, ClosingReason.EXPIRED, DecidedBy.SYSTEM, now)

    def due_action(self, now: datetime) -> DueAction | None:
        """EXPIRE past the expiry, REMIND past the next reminder, None otherwise."""
        if not self.is_open:
            return None
        if now >= self.expires_at:
            return DueAction.EXPIRE
        if self.next_reminder_at is not None and now >= self.next_reminder_at:
            return DueAction.REMIND
        return None

    def reminded(self) -> None:
        """Schedule the second reminder after the first one, and nothing after the second."""
        self.reminders_sent += 1
        since = self.awaiting_merchant_since
        if self.reminders_sent == 1 and since is not None:
            self.next_reminder_at = since + timedelta(minutes=self.deadlines.reminder_2_minutes)
        else:
            self.next_reminder_at = None

    @property
    def next_deadline(self) -> datetime | None:
        """The earliest moment the order needs attention, for the scheduler."""
        if not self.is_open:
            return None
        return min(d for d in (self.next_reminder_at, self.expires_at) if d is not None)

    def record_merchant_message(self, message_id: str) -> None:
        """Remembers a message sent to the merchant, whose buttons are then trusted."""
        self.merchant_message_ids.append(message_id)

    def record_offer_message(self, message_id: str) -> None:
        """Remembers the offer sent to the customer, whose buttons are then trusted."""
        self.offer_message_id = message_id

    def _submit(self, policy: ValidationPolicy, now: datetime) -> None:
        """Confirms automatically if the policy allows it, else waits for the merchant."""
        if policy.needs_merchant(self.total):
            self.status = OrderStatus.AWAITING_MERCHANT
            self.awaiting_merchant_since = now
            self.next_reminder_at = now + timedelta(minutes=self.deadlines.reminder_1_minutes)
        else:
            self._confirm(DecidedBy.AUTOMATIC, now)

    def _confirm(self, by: DecidedBy, now: datetime) -> None:
        """Confirms and records `OrderConfirmed`, for billing."""
        self.status = OrderStatus.CONFIRMED
        self.decided_by = by
        self.closed_at = now
        self.next_reminder_at = None
        self.record_event(
            OrderConfirmed(order_id=self.id, shop_id=self.shop_id, total=self.total.value)
        )

    def _close(
        self, status: OrderStatus, reason: ClosingReason, by: DecidedBy, now: datetime
    ) -> None:
        """Closes without confirming and records `OrderClosed`."""
        self.status = status
        self.closing_reason = reason
        self.decided_by = by
        self.closed_at = now
        self.next_reminder_at = None
        self.record_event(OrderClosed(order_id=self.id, shop_id=self.shop_id, status=status.value))

    def _require(self, status: OrderStatus) -> None:
        """Raises OrderNotOpen unless the order is in `status`."""
        if self.status is not status:
            raise OrderNotOpen(f"Order {self.number} is {self.status.value}.")
