"""Ports of the billing module: storage, PDF rendering, OTR certification, and what it needs
from the orders, shops, customers and messaging modules.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
from uuid import UUID

from woassi.modules.billing.domain.document import (
    BillingDocument,
    Certification,
    DocumentType,
)
from woassi.modules.billing.domain.taxes import TaxCode
from woassi.shared.application.ports import UnitOfWork


class DuplicateInvoice(Exception):
    """The order already has an invoice (one per order, enforced by the database)."""


@dataclass(frozen=True, slots=True)
class DocumentFilter:
    """Filters of a document list. Dates are UTC bounds: [issued_from, issued_before)."""

    document_type: DocumentType
    order_id: UUID | None = None
    invoice_id: UUID | None = None  # credit notes of this invoice
    issued_from: datetime | None = None
    issued_before: datetime | None = None


@dataclass(frozen=True, slots=True)
class AccessToken:
    """A link to a document's PDF. Only the token's SHA-256 is stored."""

    token_hash: str
    document_id: UUID
    shop_id: UUID
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class TaxSummaryRow:
    """Totals of one document type and tax code over a period."""

    document_type: DocumentType
    tax_code: TaxCode
    rate_bp: int
    amount_excl_tax: int
    tax_amount: int
    total: int


class BillingDocumentRepository(Protocol):
    """Stores invoices and credit notes. Nothing is ever deleted: 10-year retention."""

    async def next_sequence(self, shop_id: UUID, document_type: DocumentType) -> int:
        """The next number of the series, locked until the commit: gapless, never twice."""
        ...

    async def add(self, document: BillingDocument) -> None:
        """Raise DuplicateInvoice if the order already has an invoice."""
        ...

    async def get(
        self, shop_id: UUID, document_id: UUID, *, lock: bool = False
    ) -> BillingDocument | None:
        """The document, only if it belongs to `shop_id`."""
        ...

    async def invoice_for_order(self, shop_id: UUID, order_id: UUID) -> BillingDocument | None:
        """The order's invoice, if it has one."""
        ...

    async def credit_notes_of(self, shop_id: UUID, invoice_id: UUID) -> Sequence[BillingDocument]:
        """Every credit note of an invoice, oldest first."""
        ...

    async def list(
        self, shop_id: UUID, filters: DocumentFilter, offset: int, limit: int
    ) -> Sequence[BillingDocument]:
        """A page of the shop's documents, newest first."""
        ...

    async def count(self, shop_id: UUID, filters: DocumentFilter) -> int: ...

    async def save(self, document: BillingDocument) -> None:
        """Writes back the tracking fields of a loaded document. Issued content never changes."""
        ...

    async def add_access_token(self, token: AccessToken) -> None: ...

    async def find_access_token(self, token_hash: str) -> AccessToken | None: ...

    async def tax_summary(
        self, shop_id: UUID, issued_from: datetime, issued_before: datetime
    ) -> Sequence[TaxSummaryRow]:
        """Totals per document type and tax code of the documents issued in the period."""
        ...


class BillingUnitOfWork(UnitOfWork, Protocol):
    """Unit of Work of the billing module."""

    @property
    def documents(self) -> BillingDocumentRepository: ...


class DocumentRenderer(Protocol):
    """Turns a document into a PDF. CPU work: only from the worker."""

    async def render(self, document: BillingDocument, logo: bytes | None) -> bytes:
        """The PDF bytes. `logo` is an image the renderer may skip if unreadable."""
        ...


class InvoiceCertifier(Protocol):
    """The OTR certified e-invoice platform (art. 62 LPF), once it exists.

    Clearance model: a document of a shop in scope is submitted at issue, and its PDF is only
    rendered and given out once certified. Today every shop is out of scope: NOT_REQUIRED.
    """

    async def required(self, shop_id: UUID) -> bool:
        """True if this shop's documents must be certified before they are given out."""
        ...

    async def certify(self, document: BillingDocument) -> Certification:
        """Submits the document, idempotently per document id, outside any transaction.

        Returns the new certification state (CERTIFIED, REJECTED, FAILED_RETRYABLE...).
        """
        ...


# Other modules


@dataclass(frozen=True, slots=True)
class BillableLine:
    name: str
    quantity: int
    unit_price: int
    product_id: UUID | None
    retailer_id: str | None


@dataclass(frozen=True, slots=True)
class BillableOrder:
    """What billing needs from an order: the reserved lines and their prices."""

    order_id: UUID
    shop_id: UUID
    number: int
    customer_id: UUID
    total: int
    lines: tuple[BillableLine, ...]


class OrdersPort(Protocol):
    async def get(self, shop_id: UUID, order_id: UUID) -> BillableOrder | None:
        """None if the shop has no such order."""
        ...


@dataclass(frozen=True, slots=True)
class SellerProfile:
    """The shop's legal details, from its settings. Empty strings when not filled in."""

    shop_id: UUID
    account_id: UUID
    name: str
    language: str
    invoice_prefix: str
    legal_name: str
    address: str
    phone: str
    tax_id: str
    trade_register: str
    tax_regime: str | None
    vat_registered: bool
    logo_key: str | None


class ShopsPort(Protocol):
    async def profile(self, shop_id: UUID) -> SellerProfile | None:
        """None if the shop does not exist or is archived."""
        ...

    async def logo(self, profile: SellerProfile) -> bytes | None:
        """The shop's logo image, or None if it has none or it cannot be read."""
        ...


@dataclass(frozen=True, slots=True)
class Contact:
    """Where to reach a customer, and when they last wrote (the 24-hour window)."""

    name: str | None = field(default=None, repr=False)
    user_id: str | None = field(default=None, repr=False)
    phone: str | None = field(default=None, repr=False)
    last_message_at: datetime | None = None


class CustomersPort(Protocol):
    async def contact(self, shop_id: UUID, customer_id: UUID) -> Contact:
        """An empty Contact if the customer is gone."""
        ...


@dataclass(frozen=True, slots=True)
class Document:
    """A PDF sent as a WhatsApp document, only within the 24-hour window."""

    link: str = field(repr=False)
    filename: str
    caption: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class Template:
    """An approved template, for outside the 24-hour window."""

    name: str
    language: str
    parameters: Mapping[str, str] = field(repr=False)


type Notification = Document | Template


class NotificationNotSent(Exception):
    """Carries no customer data. The invoice stands regardless."""


class MessagingPort(Protocol):
    async def send(
        self, shop_id: UUID, to: Contact, notification: Notification, *, kind: str
    ) -> str:
        """Return the message id. Raise NotificationNotSent otherwise."""
        ...
