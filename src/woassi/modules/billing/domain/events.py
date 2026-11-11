"""Domain events of the billing module. Pickled for deferred subscribers: ids only."""

from dataclasses import dataclass
from uuid import UUID

from woassi.shared.domain.events import DomainEvent


@dataclass(frozen=True, kw_only=True)
class InvoiceIssued(DomainEvent):
    """An invoice got its number. Its PDF and delivery follow in the same job."""

    document_id: UUID
    shop_id: UUID
    order_id: UUID
    total: int


@dataclass(frozen=True, kw_only=True)
class CreditNoteIssued(DomainEvent):
    """A credit note got its number; the worker renders its PDF."""

    document_id: UUID
    shop_id: UUID
    invoice_id: UUID
    total: int
