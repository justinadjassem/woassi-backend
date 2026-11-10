"""Ports of the orders module: its repository, and what it needs from other modules."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
from uuid import UUID

from woassi.modules.orders.domain.order import Order, OrderStatus, ValidationMode
from woassi.shared.application.ports import UnitOfWork


class OrderRepository(Protocol):
    """Stores orders, and hands out order numbers."""

    async def add(self, order: Order) -> None:
        """Raise DuplicateOrder if this cart message already gave an order."""
        ...

    async def get(self, shop_id: UUID, order_id: UUID, *, lock: bool = False) -> Order | None:
        """The order, only if it belongs to `shop_id`. `lock` holds the row until the commit."""
        ...

    async def get_by_source_message(self, shop_id: UUID, message_id: str) -> Order | None:
        """The order created from this cart message, if any."""
        ...

    async def list(
        self, shop_id: UUID, status: OrderStatus | None, offset: int, limit: int
    ) -> Sequence[Order]:
        """A page of the shop's orders, newest first."""
        ...

    async def count(self, shop_id: UUID, status: OrderStatus | None) -> int:
        """Number of orders of the shop, with this status if given."""
        ...

    async def list_due(self, now: datetime, limit: int) -> Sequence[tuple[UUID, UUID]]:
        """(shop_id, order_id) of the open orders that need attention, oldest first."""
        ...

    async def next_number(self, shop_id: UUID) -> int:
        """Next order number of the shop. The counter stays locked until the commit."""
        ...

    async def save(self, order: Order) -> None:
        """Writes back an order loaded from this repository."""
        ...


class OrdersUnitOfWork(UnitOfWork, Protocol):
    """Unit of Work of the orders module."""

    @property
    def orders(self) -> OrderRepository: ...


class DuplicateOrder(Exception):
    """An order already exists for this cart message (a replayed webhook)."""

    pass


@dataclass(frozen=True, slots=True)
class ShopProfile:
    """What the order flow needs from a shop: language, validation rules and deadlines."""

    shop_id: UUID
    account_id: UUID
    name: str
    language: str
    validation_mode: ValidationMode
    auto_validation_max_amount: int | None
    notification_number: str | None = field(repr=False)
    reminder_1_minutes: int
    reminder_2_minutes: int
    expiration_hours: int


class ShopsPort(Protocol):
    """Shop settings, from the shops module."""

    async def profile(self, shop_id: UUID) -> ShopProfile | None:
        """The shop's profile, or None if it does not exist (or is archived)."""
        ...


@dataclass(frozen=True, slots=True)
class StockItem:
    """Outcome of a reservation for one cart line."""

    retailer_id: str
    product_id: UUID | None
    name: str
    unit_price: int
    requested: int
    reserved: int


class CatalogPort(Protocol):
    """Stock, from the catalog module."""

    async def reserve(self, shop_id: UUID, requested: Mapping[str, int]) -> list[StockItem]:
        """Reserves up to the requested quantity of each retailer id, in one transaction."""
        ...

    async def release(self, shop_id: UUID, quantities: Mapping[UUID, int]) -> None:
        """Gives reserved quantities back, by product id."""
        ...


@dataclass(frozen=True, slots=True)
class Contact:
    """Where to reach a customer: by WhatsApp user id, or by phone number."""

    user_id: str | None = field(default=None, repr=False)
    phone: str | None = field(default=None, repr=False)
    name: str | None = field(default=None, repr=False)


class CustomersPort(Protocol):
    """Customer contacts, from the customers module."""

    async def contact(self, shop_id: UUID, customer_id: UUID) -> Contact:
        """An empty Contact if the customer is gone: messages to them are then skipped."""
        ...


@dataclass(frozen=True, slots=True)
class Text:
    """A free-form text message."""

    body: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class Buttons:
    """A message with reply buttons."""

    body: str = field(repr=False)
    buttons: tuple[tuple[str, str], ...]  # (id, title)


@dataclass(frozen=True, slots=True)
class Template:
    """An approved template, needed to reach someone outside the 24-hour window."""

    name: str
    language: str
    parameters: Mapping[str, str] = field(repr=False)
    button_payloads: tuple[str, ...] = ()


type Notification = Text | Buttons | Template


class NotificationNotSent(Exception):
    """Carries no customer data. The order goes on: the dashboard shows it anyway."""


class MessagingPort(Protocol):
    """WhatsApp messages, through the messaging module."""

    async def send(
        self, shop_id: UUID, to: Contact, notification: Notification, *, kind: str
    ) -> str:
        """Return the message id. Raise NotificationNotSent otherwise."""
        ...
