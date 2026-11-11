"""Ports of the payments module: storage, KPrimePay, the worker queue, and what it needs
from the orders, shops, customers and messaging modules.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import Protocol
from uuid import UUID

from woassi.modules.payments.domain.mobile_money import Operator
from woassi.modules.payments.domain.payment import Payment, PaymentStatus
from woassi.modules.payments.domain.provider_account import ProviderAccount, ProviderMode
from woassi.shared.application.ports import UnitOfWork


class DuplicatePayment(Exception):
    """The order already has a PAID payment, or already has a PENDING one."""


class PaymentRepository(Protocol):
    """Stores payments. At most one PAID and one PENDING payment per order (DB constraints)."""

    async def add(self, payment: Payment) -> None:
        """Raise DuplicatePayment if the order already has a PAID or PENDING payment."""
        ...

    async def get(self, shop_id: UUID, payment_id: UUID, *, lock: bool = False) -> Payment | None:
        """The payment, only if it belongs to `shop_id`. `lock` holds the row until the commit."""
        ...

    async def get_by_attempt(
        self, shop_id: UUID, attempt_id: UUID, *, lock: bool = False
    ) -> Payment | None:
        """The payment one of whose attempts has this id (KPrimePay's `transaction_id`)."""
        ...

    async def awaiting_payer(self, shop_id: UUID, customer_id: UUID) -> Payment | None:
        """The customer's latest pending payment that waits for them to type a number."""
        ...

    async def for_order(
        self, shop_id: UUID, order_id: UUID, *, lock: bool = False
    ) -> Sequence[Payment]:
        """Every payment of an order, oldest first."""
        ...

    async def list(
        self,
        shop_id: UUID,
        status: PaymentStatus | None,
        order_id: UUID | None,
        offset: int,
        limit: int,
    ) -> Sequence[Payment]:
        """A page of the shop's payments, newest first."""
        ...

    async def count(
        self, shop_id: UUID, status: PaymentStatus | None, order_id: UUID | None
    ) -> int:
        """Number of payments matching the filters."""
        ...

    async def list_due(self, now: datetime, limit: int) -> Sequence[tuple[UUID, UUID]]:
        """(shop_id, payment_id) of pending payments to check now, earliest first."""
        ...

    async def save(self, payment: Payment) -> None:
        """Writes back a loaded payment. Raise DuplicatePayment if it breaks a constraint."""
        ...


class ProviderAccountRepository(Protocol):
    """Stores each shop's KPrimePay account."""

    async def get(self, shop_id: UUID) -> ProviderAccount | None: ...

    async def get_by_token_hash(self, token_hash: str) -> ProviderAccount | None:
        """The account whose callback token has this SHA-256 (hex)."""
        ...

    async def save(self, account: ProviderAccount) -> None:
        """Insert or replace."""
        ...

    async def delete(self, shop_id: UUID) -> None: ...


class Inbox(Protocol):
    """Remembers processed webhook events, so a duplicate is handled once."""

    async def claim(self, source: str, key: str) -> bool:
        """Reserve the key in the current transaction. False if already processed."""
        ...


class PaymentsUnitOfWork(UnitOfWork, Protocol):
    """Unit of Work of the payments module."""

    @property
    def payments(self) -> PaymentRepository: ...

    @property
    def provider_accounts(self) -> ProviderAccountRepository: ...

    @property
    def inbox(self) -> Inbox: ...


# KPrimePay


class ProviderFailure(Enum):
    """Why KPrimePay did not do what was asked.

    UNAVAILABLE: no usable answer (network, timeout, 5xx, 429, request in progress).
    INVALID_KEY: the key is unknown, revoked, lacks a scope, or the account is blocked.
    NOT_FOUND: no such transaction. REJECTED: any other refusal (validation, gateway...).
    """

    UNAVAILABLE = auto()
    INVALID_KEY = auto()
    NOT_FOUND = auto()
    REJECTED = auto()


class ProviderError(Exception):
    """Carries neither the key, the URL nor KPrimePay's text: safe to log."""

    def __init__(
        self, failure: ProviderFailure, *, code: str | None = None, request_sent: bool = True
    ) -> None:
        super().__init__(f"{failure.name} ({code})")
        self.failure = failure
        self.code = code  # KPrimePay's `message`, e.g. VALIDATION_ERROR, when it is a code
        # False only when the connection failed: then KPrimePay certainly did nothing.
        self.request_sent = request_sent


@dataclass(frozen=True, slots=True)
class CheckoutRequest:
    """A hosted checkout for one payment."""

    transaction_id: str  # the attempt id; also the Idempotency-Key
    amount: int
    mode: ProviderMode
    description: str
    locale: str
    return_url: str
    callback_url: str | None = field(default=None, repr=False)  # holds the callback token
    metadata: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PushRequest:
    """A USSD push: KPrimePay prompts the payer's phone, who approves with their PIN."""

    transaction_id: str  # the attempt id; also the Idempotency-Key
    amount: int
    gateway: Operator
    phone_number: str = field(repr=False)  # local form, 8 digits
    description: str
    callback_url: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class Checkout:
    """What KPrimePay answered: the link to send the customer, and until when it works."""

    reference: str | None
    url: str = field(repr=False)
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ProviderTransaction:
    """A payment as KPrimePay knows it. `status` is already mapped to ours: PENDING, PAID or
    FAILED. PAID still has to be checked against our payment (reference, amount, currency).
    """

    transaction_id: str
    status: PaymentStatus
    amount: int | None
    currency: str | None
    reference: str | None = None


class PaymentGateway(Protocol):
    """KPrimePay, called with the shop's own key. Raise ProviderError on failure."""

    async def verify_key(self, api_key: str) -> None:
        """A harmless read-only call that only succeeds with a working key."""
        ...

    async def create_checkout(self, api_key: str, request: CheckoutRequest) -> Checkout:
        """Creates the checkout. Safe to call again with the same transaction id."""
        ...

    async def ussd_push(self, api_key: str, request: PushRequest) -> str | None:
        """Sends the push; returns KPrimePay's reference if it gives one. Safe to call again
        with the same transaction id.
        """
        ...

    async def transaction(self, api_key: str, transaction_id: str) -> ProviderTransaction:
        """The current state of a payment. ProviderError(NOT_FOUND) if KPrimePay has none."""
        ...


class PaymentCheckQueue(Protocol):
    """Hands a 'go and check this payment' signal over to the worker."""

    async def enqueue(self, shop_id: UUID, payment_id: UUID) -> None: ...


# Other modules


@dataclass(frozen=True, slots=True)
class PayableOrder:
    """What payments needs from an order."""

    order_id: UUID
    shop_id: UUID
    number: int
    total: int
    status: str  # orders' status; only CONFIRMED can be paid
    customer_id: UUID


class OrdersPort(Protocol):
    """Orders, from the orders module."""

    async def get(self, shop_id: UUID, order_id: UUID) -> PayableOrder | None:
        """None if the shop has no such order."""
        ...


@dataclass(frozen=True, slots=True)
class ShopInfo:
    """What payments needs from a shop."""

    name: str
    language: str


class ShopsPort(Protocol):
    """Shops, from the shops module."""

    async def info(self, shop_id: UUID) -> ShopInfo | None:
        """None if the shop does not exist or is archived."""
        ...


@dataclass(frozen=True, slots=True)
class Contact:
    """Where to reach a customer, and when they last wrote (the 24-hour window)."""

    user_id: str | None = field(default=None, repr=False)
    phone: str | None = field(default=None, repr=False)
    last_message_at: datetime | None = None


class CustomersPort(Protocol):
    """Customers, from the customers module."""

    async def contact(self, shop_id: UUID, customer_id: UUID) -> Contact:
        """An empty Contact if the customer is gone: the link is then not sent."""
        ...


@dataclass(frozen=True, slots=True)
class Text:
    """A free-form text, only within the 24-hour window."""

    body: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class Buttons:
    """A text with reply buttons (id, title), only within the 24-hour window."""

    body: str = field(repr=False)
    buttons: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class Template:
    """An approved template, for outside the 24-hour window."""

    name: str
    language: str
    parameters: Mapping[str, str] = field(repr=False)
    button_payloads: tuple[str, ...] = ()


type Notification = Text | Buttons | Template


class NotificationNotSent(Exception):
    """Carries no customer data. The payment goes on regardless."""


class MessagingPort(Protocol):
    """WhatsApp messages, through the messaging module."""

    async def send(
        self, shop_id: UUID, to: Contact, notification: Notification, *, kind: str
    ) -> str:
        """Return the message id. Raise NotificationNotSent otherwise."""
        ...
