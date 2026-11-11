"""Reading a shop's invoices and credit notes, their PDFs, and the period summary."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from woassi.modules.billing.application.ports import BillingUnitOfWork, DocumentFilter
from woassi.modules.billing.application.use_cases.release import token_hash
from woassi.modules.billing.domain.document import BillingDocument, DocumentType
from woassi.modules.billing.domain.errors import (
    CreditNoteNotFound,
    InvoiceLinkNotFound,
    InvoiceNotFound,
    PdfNotReady,
)
from woassi.modules.billing.domain.taxes import TaxCode
from woassi.shared.application.pagination import PageResult
from woassi.shared.application.ports import Clock, StoragePort
from woassi.shared.domain.errors import InvalidData

LOCAL_TIMEZONE = ZoneInfo("Africa/Lome")
# A PDF link from the dashboard or the public link: only long enough to start the download.
PDF_LINK_TTL_S = 300
MAX_SUMMARY_DAYS = 366


def local_day_start(day: date) -> datetime:
    """Midnight of `day` in Africa/Lome, in UTC: periods are local calendar days."""
    return datetime.combine(day, time(), LOCAL_TIMEZONE).astimezone(UTC)


def _not_found(document_type: DocumentType, document_id: UUID) -> Exception:
    if document_type is DocumentType.INVOICE:
        return InvoiceNotFound(f"Invoice {document_id} not found.")
    return CreditNoteNotFound(f"Credit note {document_id} not found.")


class ListBillingDocuments:
    """A page of the shop's invoices or credit notes, newest first."""

    def __init__(self, uow: BillingUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        *,
        shop_id: UUID,
        document_type: DocumentType,
        order_id: UUID | None = None,
        invoice_id: UUID | None = None,
        issued_from: date | None = None,
        issued_to: date | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> PageResult[BillingDocument]:
        """`issued_from` and `issued_to` are local days, both included."""
        filters = DocumentFilter(
            document_type=document_type,
            order_id=order_id,
            invoice_id=invoice_id,
            issued_from=local_day_start(issued_from) if issued_from else None,
            issued_before=local_day_start(issued_to + timedelta(days=1)) if issued_to else None,
        )
        async with self._uow as uow:
            items = await uow.documents.list(shop_id, filters, offset, limit)
            total = await uow.documents.count(shop_id, filters)
        return PageResult(items=list(items), total=total, offset=offset, limit=limit)


class GetBillingDocument:
    """One invoice or credit note of the shop."""

    def __init__(self, uow: BillingUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self, *, shop_id: UUID, document_id: UUID, document_type: DocumentType
    ) -> BillingDocument:
        """Raises InvoiceNotFound or CreditNoteNotFound."""
        async with self._uow as uow:
            document = await uow.documents.get(shop_id, document_id)
        if document is None or document.document_type is not document_type:
            raise _not_found(document_type, document_id)
        return document


@dataclass(frozen=True, slots=True)
class PdfLink:
    url: str
    expires_in_s: int


class GetDocumentPdf:
    """A short-lived download URL of a document's PDF, from the private bucket."""

    def __init__(self, uow: BillingUnitOfWork, storage: StoragePort) -> None:
        self._uow = uow
        self._storage = storage

    async def execute(
        self, *, shop_id: UUID, document_id: UUID, document_type: DocumentType
    ) -> PdfLink:
        """Raises InvoiceNotFound, CreditNoteNotFound, or PdfNotReady while it renders."""
        document = await GetBillingDocument(self._uow).execute(
            shop_id=shop_id, document_id=document_id, document_type=document_type
        )
        if document.pdf_key is None:
            raise PdfNotReady("The PDF is not ready yet: try again in a few seconds.")
        url = await self._storage.presigned_get(document.pdf_key, PDF_LINK_TTL_S)
        return PdfLink(url=url, expires_in_s=PDF_LINK_TTL_S)


class OpenInvoiceLink:
    """`GET /invoices/{token}`, public: the link of the `woassi_invoice_ready` template."""

    def __init__(self, uow: BillingUnitOfWork, storage: StoragePort, clock: Clock) -> None:
        self._uow = uow
        self._storage = storage
        self._clock = clock

    async def execute(self, token: str) -> str:
        """A fresh presigned URL of the PDF. InvoiceLinkNotFound for any unknown, expired or
        unusable token, without saying which.
        """
        if not 20 <= len(token) <= 100:
            raise InvoiceLinkNotFound("Unknown or expired link.")
        async with self._uow as uow:
            access = await uow.documents.find_access_token(token_hash(token))
            document = (
                await uow.documents.get(access.shop_id, access.document_id) if access else None
            )
        if (
            access is None
            or access.expires_at <= self._clock.now()
            or document is None
            or document.pdf_key is None
        ):
            raise InvoiceLinkNotFound("Unknown or expired link.")
        return await self._storage.presigned_get(document.pdf_key, PDF_LINK_TTL_S)


@dataclass(frozen=True, slots=True)
class TaxLine:
    tax_code: TaxCode
    rate_bp: int
    amount_excl_tax: int
    tax_amount: int
    total: int


@dataclass(frozen=True, slots=True)
class Totals:
    documents: int
    amount_excl_tax: int
    tax_amount: int
    total: int
    by_tax: tuple[TaxLine, ...]


@dataclass(frozen=True, slots=True)
class BillingSummary:
    """Invoices minus credit notes over local days [issued_from, issued_to]."""

    issued_from: date
    issued_to: date
    invoices: Totals
    credit_notes: Totals
    net: Totals


def _totals(lines: list[TaxLine], documents: int) -> Totals:
    return Totals(
        documents=documents,
        amount_excl_tax=sum(line.amount_excl_tax for line in lines),
        tax_amount=sum(line.tax_amount for line in lines),
        total=sum(line.total for line in lines),
        by_tax=tuple(lines),
    )


class GetBillingSummary:
    """Turnover before tax, VAT per rate, credit notes and net, for tax returns."""

    def __init__(self, uow: BillingUnitOfWork) -> None:
        self._uow = uow

    async def execute(self, *, shop_id: UUID, issued_from: date, issued_to: date) -> BillingSummary:
        """Raises InvalidData for a reversed or longer than a year period."""
        if issued_to < issued_from or (issued_to - issued_from).days >= MAX_SUMMARY_DAYS:
            raise InvalidData("The period must run forward and last at most a year.")
        start = local_day_start(issued_from)
        end = local_day_start(issued_to + timedelta(days=1))
        async with self._uow as uow:
            rows = await uow.documents.tax_summary(shop_id, start, end)
            counts = {
                kind: await uow.documents.count(
                    shop_id,
                    DocumentFilter(document_type=kind, issued_from=start, issued_before=end),
                )
                for kind in DocumentType
            }
        per_type: dict[DocumentType, list[TaxLine]] = {kind: [] for kind in DocumentType}
        for row in rows:
            per_type[row.document_type].append(
                TaxLine(row.tax_code, row.rate_bp, row.amount_excl_tax, row.tax_amount, row.total)
            )
        invoiced = per_type[DocumentType.INVOICE]
        credited = per_type[DocumentType.CREDIT_NOTE]
        net: dict[tuple[TaxCode, int], list[int]] = {}
        for line, sign in [(line, 1) for line in invoiced] + [(line, -1) for line in credited]:
            values = net.setdefault((line.tax_code, line.rate_bp), [0, 0, 0])
            values[0] += sign * line.amount_excl_tax
            values[1] += sign * line.tax_amount
            values[2] += sign * line.total
        net_lines = [TaxLine(code, rate, *values) for (code, rate), values in net.items()]
        return BillingSummary(
            issued_from=issued_from,
            issued_to=issued_to,
            invoices=_totals(invoiced, counts[DocumentType.INVOICE]),
            credit_notes=_totals(credited, counts[DocumentType.CREDIT_NOTE]),
            net=_totals(net_lines, counts[DocumentType.INVOICE]),
        )
