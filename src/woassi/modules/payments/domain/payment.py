"""Payments of orders: KPrimePay USSD pushes and checkout links, or recorded by the merchant.

A KPrimePay payment has attempts: each USSD push and each checkout is one KPrimePay
transaction, with its own `transaction_id` and Idempotency-Key (the attempt id), as KPrimePay
binds a key to its first request. Whichever attempt is paid first pays the payment.

PENDING -> PAID, FAILED, EXPIRED or CANCELLED. Statuses only move forward: a FAILED or EXPIRED
payment (or attempt) may still become PAID when KPrimePay later confirms the money arrived, but
nothing ever leaves PAID or CANCELLED.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID, uuid4

from woassi.modules.payments.domain.errors import (
    AmountOutOfRange,
    InvalidPaymentTransition,
    PushLimitReached,
)
from woassi.modules.payments.domain.events import PaymentConfirmed
from woassi.modules.payments.domain.mobile_money import MobileMoneyNumber, Operator
from woassi.shared.domain.events import Entity
from woassi.shared.domain.values import Amount

# https://developers.kprimepay.com/index?v=v2#checkout: collections take 10 to 2,000,000 XOF.
MIN_PROVIDER_AMOUNT = Amount(10)
MAX_PROVIDER_AMOUNT = Amount(2_000_000)
MAX_PUSHES = 3
# How long the customer has to pay, unless a checkout lives longer.
LIFETIME = timedelta(hours=24)
# Status checks of a pending payment, counted from the previous one, then hourly.
CHECK_DELAYS = (
    timedelta(minutes=2),
    timedelta(minutes=5),
    timedelta(minutes=10),
    timedelta(minutes=30),
)
HOURLY = timedelta(hours=1)


class PaymentMethod(StrEnum):
    """How the customer paid."""

    KPRIMEPAY = "kprimepay"
    MANUAL = "manual"  # cash, or mobile money received directly: the merchant says so


class PaymentStatus(StrEnum):
    """Only PENDING is open. Also the status of each attempt."""

    PENDING = "PENDING"
    PAID = "PAID"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class Channel(StrEnum):
    """How KPrimePay asks the customer for the money."""

    USSD_PUSH = "ussd_push"  # a prompt on the payer's phone, approved with their PIN
    CHECKOUT = "checkout"  # a hosted page: card, Mobile Money, foreign payers


# Where each status may go.
_NEXT = {
    PaymentStatus.PENDING: frozenset(
        {
            PaymentStatus.PAID,
            PaymentStatus.FAILED,
            PaymentStatus.EXPIRED,
            PaymentStatus.CANCELLED,
        }
    ),
    PaymentStatus.FAILED: frozenset({PaymentStatus.PAID}),
    PaymentStatus.EXPIRED: frozenset({PaymentStatus.PAID}),
    PaymentStatus.PAID: frozenset(),
    PaymentStatus.CANCELLED: frozenset(),
}


def _require_move(current: PaymentStatus, target: PaymentStatus, label: str) -> None:
    if target not in _NEXT[current]:
        raise InvalidPaymentTransition(f"{label} cannot go from {current.value} to {target.value}.")


def check_provider_amount(amount: Amount) -> None:
    """Raises AmountOutOfRange outside what KPrimePay collects."""
    if not MIN_PROVIDER_AMOUNT <= amount <= MAX_PROVIDER_AMOUNT:
        raise AmountOutOfRange(
            f"KPrimePay collects {MIN_PROVIDER_AMOUNT} to {MAX_PROVIDER_AMOUNT}."
        )


@dataclass(slots=True)
class PaymentAttempt:
    """One KPrimePay transaction. `id` is its `transaction_id` and its Idempotency-Key."""

    id: UUID
    channel: Channel
    status: PaymentStatus
    created_at: datetime
    updated_at: datetime
    operator: Operator | None = None
    payer_phone: str | None = field(default=None, repr=False)  # E.164, pushes only
    submitted_at: datetime | None = None  # when KPrimePay accepted it
    provider_reference: str | None = None  # KPrimePay's `kpp_tx_reference`
    checkout_url: str | None = field(default=None, repr=False)
    expires_at: datetime | None = None  # checkouts only
    failure_reason: str | None = None

    @property
    def is_open(self) -> bool:
        return self.status is PaymentStatus.PENDING

    @property
    def submitted(self) -> bool:
        return self.submitted_at is not None

    @property
    def payer(self) -> MobileMoneyNumber | None:
        if self.payer_phone is None or self.operator is None:
            return None
        return MobileMoneyNumber(e164=self.payer_phone, operator=self.operator)

    def _move(self, status: PaymentStatus, now: datetime) -> None:
        _require_move(self.status, status, f"Attempt {self.id}")
        self.status = status
        self.updated_at = now


@dataclass(slots=True)
class Payment(Entity):
    """One payment of an order. Its amount is always the order's total."""

    id: UUID
    shop_id: UUID
    order_id: UUID
    amount: Amount
    method: PaymentMethod
    status: PaymentStatus
    created_at: datetime
    updated_at: datetime
    customer_id: UUID | None = None
    expires_at: datetime | None = None
    paid_at: datetime | None = None
    closed_at: datetime | None = None
    failure_reason: str | None = None
    manual_reference: str | None = field(default=None, repr=False)
    note: str | None = field(default=None, repr=False)
    recorded_by: UUID | None = None  # the account that recorded a manual payment
    next_check_at: datetime | None = None
    checks: int = 0
    attempts: list[PaymentAttempt] = field(default_factory=list)
    # The last number a push went to: personal data.
    payer_phone: str | None = field(default=None, repr=False)
    # True while we wait for the customer to type the Mobile Money number that will pay.
    awaiting_payer: bool = False
    # Messages whose buttons may act on this payment: only replies to them are trusted.
    prompt_message_ids: list[str] = field(default_factory=list)

    @classmethod
    def request(
        cls,
        *,
        shop_id: UUID,
        order_id: UUID,
        customer_id: UUID | None,
        amount: Amount,
        now: datetime,
    ) -> "Payment":
        """A KPrimePay payment, before any attempt. Raises AmountOutOfRange."""
        check_provider_amount(amount)
        return cls(
            id=uuid4(),
            shop_id=shop_id,
            order_id=order_id,
            customer_id=customer_id,
            amount=amount,
            method=PaymentMethod.KPRIMEPAY,
            status=PaymentStatus.PENDING,
            created_at=now,
            updated_at=now,
            expires_at=now + LIFETIME,
            next_check_at=now + CHECK_DELAYS[0],
        )

    @classmethod
    def manual(
        cls,
        *,
        shop_id: UUID,
        order_id: UUID,
        amount: Amount,
        recorded_by: UUID,
        reference: str | None,
        note: str | None,
        now: datetime,
    ) -> "Payment":
        """A payment the merchant received outside Woassi. Paid at once."""
        payment = cls(
            id=uuid4(),
            shop_id=shop_id,
            order_id=order_id,
            amount=amount,
            method=PaymentMethod.MANUAL,
            status=PaymentStatus.PENDING,
            created_at=now,
            updated_at=now,
            manual_reference=reference,
            note=note,
            recorded_by=recorded_by,
        )
        payment.confirm(now)
        return payment

    # Reading

    @property
    def is_pending(self) -> bool:
        return self.status is PaymentStatus.PENDING

    @property
    def pushes(self) -> int:
        return sum(1 for a in self.attempts if a.channel is Channel.USSD_PUSH)

    @property
    def can_push(self) -> bool:
        return self.is_pending and self.pushes < MAX_PUSHES

    @property
    def payer(self) -> MobileMoneyNumber | None:
        """The last number a push went to."""
        pushes = [a for a in self.attempts if a.channel is Channel.USSD_PUSH and a.payer]
        return pushes[-1].payer if pushes else None

    @property
    def checkout_url(self) -> str | None:
        """The link of the latest checkout, if any."""
        links = [a.checkout_url for a in self.attempts if a.checkout_url]
        return links[-1] if links else None

    def attempt(self, attempt_id: UUID) -> PaymentAttempt:
        """Raises KeyError if the payment has no such attempt."""
        for attempt in self.attempts:
            if attempt.id == attempt_id:
                return attempt
        raise KeyError(attempt_id)

    def open_checkout(self, now: datetime) -> PaymentAttempt | None:
        """A checkout still usable: its link can be sent again rather than replaced."""
        for attempt in reversed(self.attempts):
            if (
                attempt.channel is Channel.CHECKOUT
                and attempt.is_open
                and (attempt.expires_at is None or now < attempt.expires_at)
            ):
                return attempt
        return None

    def trusts(self, message_id: str | None) -> bool:
        """True if a button reply answers one of the messages we sent for this payment."""
        return message_id is not None and message_id in self.prompt_message_ids

    # Changing

    def add_attempt(
        self, channel: Channel, now: datetime, *, payer: MobileMoneyNumber | None = None
    ) -> PaymentAttempt:
        """A new push (to `payer`) or checkout. Raises PushLimitReached after 3 pushes."""
        self._require_pending()
        if channel is Channel.USSD_PUSH:
            if payer is None:
                raise ValueError("A push needs a payer number.")
            if self.pushes >= MAX_PUSHES:
                raise PushLimitReached(f"At most {MAX_PUSHES} payment requests by phone.")
            self.payer_phone = payer.e164
        attempt = PaymentAttempt(
            id=uuid4(),
            channel=channel,
            status=PaymentStatus.PENDING,
            created_at=now,
            updated_at=now,
            operator=payer.operator if payer else None,
            payer_phone=payer.e164 if payer else None,
        )
        self.attempts.append(attempt)
        self.awaiting_payer = False
        self.updated_at = now
        return attempt

    def attempt_submitted(
        self,
        attempt_id: UUID,
        *,
        reference: str | None,
        now: datetime,
        url: str | None = None,
        expires_at: datetime | None = None,
    ) -> None:
        """KPrimePay accepted the push, or created the checkout."""
        attempt = self.attempt(attempt_id)
        if not attempt.is_open:
            return
        attempt.submitted_at = now
        attempt.provider_reference = reference
        attempt.checkout_url = url
        attempt.expires_at = expires_at
        attempt.updated_at = now
        if expires_at is not None and (self.expires_at is None or expires_at > self.expires_at):
            self.expires_at = expires_at
        self.checks = 0
        self._schedule(min(now + CHECK_DELAYS[0], self.expires_at or now + CHECK_DELAYS[0]))

    def attempt_failed(self, attempt_id: UUID, reason: str, now: datetime) -> None:
        """KPrimePay refused or reports a failure. The payment stays open for other tries."""
        attempt = self.attempt(attempt_id)
        attempt._move(PaymentStatus.FAILED, now)
        attempt.failure_reason = reason[:100]
        self.updated_at = now

    def attempt_paid(self, attempt_id: UUID, now: datetime) -> None:
        """KPrimePay confirmed this attempt: the payment is paid."""
        attempt = self.attempt(attempt_id)
        _require_move(self.status, PaymentStatus.PAID, f"Payment {self.id}")
        attempt._move(PaymentStatus.PAID, now)
        self.confirm(now)
        # The others can no longer pay this payment.
        self._close_attempts(PaymentStatus.CANCELLED, now)

    def ask_payer(self, now: datetime) -> None:
        """We asked the customer to type the number that will pay."""
        self._require_pending()
        self.awaiting_payer = True
        self.updated_at = now

    def record_prompt(self, message_id: str) -> None:
        """Remembers a message we sent, whose buttons are then trusted."""
        self.prompt_message_ids.append(message_id)

    def confirm(self, now: datetime) -> None:
        """The money arrived. Records `PaymentConfirmed`."""
        self._move(PaymentStatus.PAID, now)
        self.paid_at = now
        self.failure_reason = None
        self.record_event(
            PaymentConfirmed(
                payment_id=self.id,
                shop_id=self.shop_id,
                order_id=self.order_id,
                amount=self.amount.value,
                method=self.method.value,
                paid_at=now,
            )
        )

    def fail(self, reason: str, now: datetime) -> None:
        """Nothing more can be tried, e.g. KPrimePay refuses the shop's key."""
        self._move(PaymentStatus.FAILED, now)
        self.failure_reason = reason[:100]
        self._close_attempts(PaymentStatus.FAILED, now)

    def expire(self, now: datetime) -> None:
        """Still unpaid at the deadline."""
        self._move(PaymentStatus.EXPIRED, now)
        self._close_attempts(PaymentStatus.EXPIRED, now)

    def cancel(self, now: datetime) -> None:
        """Superseded, e.g. by a manual payment of the same order."""
        self._move(PaymentStatus.CANCELLED, now)
        self._close_attempts(PaymentStatus.CANCELLED, now)

    # Scheduling

    def is_due(self, now: datetime) -> bool:
        """True if the poller should look at this payment now."""
        return self.is_pending and self.next_check_at is not None and now >= self.next_check_at

    def is_past_deadline(self, now: datetime) -> bool:
        return self.is_pending and self.expires_at is not None and now >= self.expires_at

    def checked(self, now: datetime) -> None:
        """Schedules the next check, with a growing delay, and a last one at the deadline."""
        delay = CHECK_DELAYS[self.checks] if self.checks < len(CHECK_DELAYS) else HOURLY
        self.checks += 1
        self.updated_at = now
        upcoming = now + delay
        if self.expires_at is not None and now < self.expires_at:
            upcoming = min(upcoming, self.expires_at)
        self._schedule(upcoming)

    def _schedule(self, moment: datetime) -> None:
        self.next_check_at = moment if self.is_pending else None

    def _close_attempts(self, status: PaymentStatus, now: datetime) -> None:
        for attempt in self.attempts:
            if attempt.is_open:
                attempt._move(status, now)

    def _move(self, status: PaymentStatus, now: datetime) -> None:
        """Raises InvalidPaymentTransition unless `status` comes after the current one."""
        _require_move(self.status, status, f"Payment {self.id}")
        self.status = status
        self.updated_at = now
        self.next_check_at = None
        self.closed_at = now
        if status is not PaymentStatus.PENDING:
            self.awaiting_payer = False

    def _require_pending(self) -> None:
        if not self.is_pending:
            raise InvalidPaymentTransition(f"Payment {self.id} is {self.status.value}.")
