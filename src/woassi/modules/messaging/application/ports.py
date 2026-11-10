"""Ports of the messaging module: event queue, shop lookup, inbound handling, storage,
and the Cloud API (sending and templates).
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Protocol
from uuid import UUID

from woassi.modules.messaging.domain.content import MessageContent, Recipient
from woassi.modules.messaging.domain.event import InboundMessage, MetaEvent
from woassi.modules.messaging.domain.outbound import OutboundMessage
from woassi.modules.messaging.domain.templates import MessageTemplate, TemplateDefinition
from woassi.shared.application.ports import UnitOfWork


class EventQueue(Protocol):
    """Hands webhook events over to the worker."""

    async def enqueue(self, event: MetaEvent) -> None:
        """Queues one event for `process_meta_event`."""
        ...


@dataclass(frozen=True, slots=True)
class ShopConnection:
    """What is needed to act for a shop on WhatsApp. The token is decrypted: never log it."""

    shop_id: UUID
    waba_id: str
    phone_number_id: str
    access_token: str = field(repr=False)


class ShopConnectionsPort(Protocol):
    """None for an unknown shop or number, or a shop that may not process messages."""

    async def by_phone_number_id(self, phone_number_id: str) -> ShopConnection | None:
        """The shop behind a number, to route an inbound webhook."""
        ...

    async def by_shop_id(self, shop_id: UUID) -> ShopConnection | None:
        """A shop's connection, to send from its number."""
        ...


class WhatsAppAccountsPort(Protocol):
    """Lets the shops module react to changes on a WhatsApp Business Account."""

    async def account_removed(self, waba_id: str) -> int:
        """Woassi lost access to this account. Return how many shops it affected."""
        ...


class InboundMessageHandler(Protocol):
    """What to do with a message a customer or merchant sent to a shop (orders, customers)."""

    async def handle(self, shop_id: UUID, message: InboundMessage) -> None:
        """Raise to have the message retried: its key is only kept if this returns."""
        ...


class Inbox(Protocol):
    """Remembers processed inbound messages, so a duplicate is handled once."""

    async def claim(self, source: str, key: str) -> bool:
        """Reserve the key in the current transaction. False if already processed."""
        ...


class OutboundMessageRepository(Protocol):
    """Stores the messages shops sent, to follow their delivery status."""

    async def add(self, message: OutboundMessage) -> None:
        """Records a message Meta accepted."""
        ...

    async def get(self, message_id: str) -> OutboundMessage | None:
        """The message with this Meta id (wamid), locked until the transaction ends."""
        ...

    async def save(self, message: OutboundMessage) -> None:
        """Writes back a message loaded from this repository."""
        ...


class MessageTemplateRepository(Protocol):
    """Stores the review status of each template, per shop and language."""

    async def get(self, shop_id: UUID, name: str, language: str) -> MessageTemplate | None:
        """The template of a shop in one language, if it was submitted."""
        ...

    async def list_by_waba(self, waba_id: str, name: str) -> Sequence[MessageTemplate]:
        """Every record of template `name` on an account, any language or shop."""
        ...

    async def save(self, template: MessageTemplate) -> None:
        """Insert or update."""
        ...


class MessagingUnitOfWork(UnitOfWork, Protocol):
    """Unit of Work of the messaging module."""

    @property
    def inbox(self) -> Inbox: ...

    @property
    def outbound(self) -> OutboundMessageRepository: ...

    @property
    def templates(self) -> MessageTemplateRepository: ...


class SendFailure(Enum):
    """Why Meta did not take a message or template.

    UNAVAILABLE means no usable answer (network, 5xx); REJECTED is any other refusal.
    """

    OUTSIDE_WINDOW = auto()
    RATE_LIMITED = auto()
    INVALID_RECIPIENT = auto()
    INVALID_TOKEN = auto()
    UNAVAILABLE = auto()
    REJECTED = auto()


class SendError(Exception):
    """Carries no text from Meta on purpose: it is safe to log."""

    def __init__(self, failure: SendFailure, *, meta_code: int | None = None) -> None:
        super().__init__(f"{failure.name} (Meta code {meta_code})")
        self.failure = failure
        self.meta_code = meta_code


class WhatsAppSender(Protocol):
    """Sends messages through the Cloud API."""

    async def send(
        self, connection: ShopConnection, recipient: Recipient, content: MessageContent
    ) -> str:
        """Return Meta's message id. Raise SendError otherwise."""
        ...


@dataclass(frozen=True, slots=True)
class SubmittedTemplate:
    """A template as Meta knows it: its id and its review status."""

    meta_template_id: str | None
    status: str  # as Meta writes it


class TemplateRegistry(Protocol):
    """Raise SendError on failure."""

    async def submit(
        self, connection: ShopConnection, definition: TemplateDefinition
    ) -> SubmittedTemplate:
        """Create the template, or return the existing one if it is already there."""
        ...

    async def status(
        self, connection: ShopConnection, name: str, language: str
    ) -> SubmittedTemplate | None:
        """The template as Meta knows it, or None if it was never submitted."""
        ...
