"""In-memory adapters for the billing module, and a stack wiring them to the real use cases."""

from collections.abc import Sequence
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Self
from uuid import UUID, uuid4

from tests.fakes import FakeAuditLog, FakeStorage
from woassi.modules.billing.application.ports import (
    AccessToken,
    BillableLine,
    BillableOrder,
    Contact,
    DocumentFilter,
    DuplicateInvoice,
    Notification,
    NotificationNotSent,
    SellerProfile,
    TaxSummaryRow,
    Template,
)
from woassi.modules.billing.application.use_cases.credit_notes import IssueCreditNote
from woassi.modules.billing.application.use_cases.issuing import (
    IssueInvoice,
    PrepareDocument,
    ResendInvoice,
)
from woassi.modules.billing.application.use_cases.queries import (
    GetBillingDocument,
    GetBillingSummary,
    GetDocumentPdf,
    ListBillingDocuments,
    OpenInvoiceLink,
)
from woassi.modules.billing.application.use_cases.release import BillingSettings, DocumentRelease
from woassi.modules.billing.domain.document import (
    BillingDocument,
    Certification,
    CertificationStatus,
    CreditRequest,
    DocumentType,
)
from woassi.modules.billing.domain.taxes import tax_totals
from woassi.shared.domain.events import DomainEvent
from woassi.shared.infrastructure.clock import FrozenClock

PUBLIC_BASE_URL = "https://api.example.com"


class FakeDocuments:
    def __init__(self, uow: "FakeBillingUoW") -> None:
        self._uow = uow

    async def next_sequence(self, shop_id: UUID, document_type: DocumentType) -> int:
        key = (shop_id, document_type)
        self._uow.counters[key] = self._uow.counters.get(key, 0) + 1
        return self._uow.counters[key]

    def _store(self, document: BillingDocument) -> None:
        stored = deepcopy(document)
        stored.pull_events()
        self._uow.state[document.id] = stored
        self._uow.tracked.append(document)

    async def add(self, document: BillingDocument) -> None:
        for other in self._uow.state.values():
            if (
                document.document_type is DocumentType.INVOICE
                and other.document_type is DocumentType.INVOICE
                and other.shop_id == document.shop_id
                and other.order_id == document.order_id
            ):
                raise DuplicateInvoice(str(document.order_id))
        self._store(document)

    async def get(
        self, shop_id: UUID, document_id: UUID, *, lock: bool = False
    ) -> BillingDocument | None:
        found = self._uow.state.get(document_id)
        return deepcopy(found) if found and found.shop_id == shop_id else None

    async def invoice_for_order(self, shop_id: UUID, order_id: UUID) -> BillingDocument | None:
        for document in self._uow.state.values():
            if (
                document.shop_id == shop_id
                and document.order_id == order_id
                and document.document_type is DocumentType.INVOICE
            ):
                return deepcopy(document)
        return None

    async def credit_notes_of(self, shop_id: UUID, invoice_id: UUID) -> Sequence[BillingDocument]:
        notes = [
            d
            for d in self._uow.state.values()
            if d.shop_id == shop_id and d.original_document_id == invoice_id
        ]
        return deepcopy(sorted(notes, key=lambda d: d.sequence))

    def _matching(self, shop_id: UUID, filters: DocumentFilter) -> list[BillingDocument]:
        return [
            d
            for d in self._uow.state.values()
            if d.shop_id == shop_id
            and d.document_type is filters.document_type
            and (filters.order_id is None or d.order_id == filters.order_id)
            and (filters.invoice_id is None or d.original_document_id == filters.invoice_id)
            and (filters.issued_from is None or d.issued_at >= filters.issued_from)
            and (filters.issued_before is None or d.issued_at < filters.issued_before)
        ]

    async def list(
        self, shop_id: UUID, filters: DocumentFilter, offset: int, limit: int
    ) -> Sequence[BillingDocument]:
        found = sorted(
            self._matching(shop_id, filters), key=lambda d: (d.issued_at, d.sequence), reverse=True
        )
        return deepcopy(found[offset : offset + limit])

    async def count(self, shop_id: UUID, filters: DocumentFilter) -> int:
        return len(self._matching(shop_id, filters))

    async def save(self, document: BillingDocument) -> None:
        assert document.id in self._uow.state
        self._store(document)

    async def add_access_token(self, token: AccessToken) -> None:
        self._uow.tokens[token.token_hash] = token

    async def find_access_token(self, token_hash: str) -> AccessToken | None:
        return self._uow.tokens.get(token_hash)

    async def tax_summary(
        self, shop_id: UUID, issued_from: datetime, issued_before: datetime
    ) -> Sequence[TaxSummaryRow]:
        rows: dict[tuple[DocumentType, object, int], list[int]] = {}
        for document in self._uow.state.values():
            if document.shop_id != shop_id or not issued_from <= document.issued_at < issued_before:
                continue
            for total in tax_totals(document.lines):
                values = rows.setdefault(
                    (document.document_type, total.tax_code, total.rate_bp), [0, 0, 0]
                )
                values[0] += total.amount_excl_tax
                values[1] += total.tax_amount
                values[2] += total.total
        return [
            TaxSummaryRow(kind, code, rate, *values)  # type: ignore[arg-type]
            for (kind, code, rate), values in rows.items()
        ]


class FakeBillingUoW:
    """Works on a copy of the stack's state: what is not committed is lost."""

    def __init__(self, stack: "BillingStack") -> None:
        self.stack = stack
        self.state: dict[UUID, BillingDocument] = {}
        self.counters: dict[tuple[UUID, DocumentType], int] = {}
        self.tokens: dict[str, AccessToken] = {}
        self.tracked: list[BillingDocument] = []

    @property
    def documents(self) -> FakeDocuments:
        return FakeDocuments(self)

    @property
    def audit(self) -> FakeAuditLog:
        return self.stack.audit_log

    def _load(self) -> None:
        self.state = deepcopy(self.stack.documents)
        self.counters = dict(self.stack.counters)
        self.tokens = dict(self.stack.tokens)
        self.tracked = []

    async def __aenter__(self) -> Self:
        self._load()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.tracked = []

    async def commit(self) -> None:
        self.stack.documents = deepcopy(self.state)
        self.stack.counters = dict(self.counters)
        self.stack.tokens = dict(self.tokens)
        for document in self.tracked:
            self.stack.events.extend(document.pull_events())
        self.tracked = []

    async def rollback(self) -> None:
        self._load()


class FakeRenderer:
    def __init__(self) -> None:
        self.rendered: list[tuple[UUID, bytes | None]] = []
        self.failing = False

    async def render(self, document: BillingDocument, logo: bytes | None) -> bytes:
        if self.failing:
            raise RuntimeError("renderer down")
        self.rendered.append((document.id, logo))
        return b"%PDF-1.7 " + document.number.encode()


class FakeCertifier:
    def __init__(self) -> None:
        self.is_required = False
        self.outcome = Certification(
            status=CertificationStatus.CERTIFIED,
            certified_number="OTR-0001",
            qr_payload="https://otr.example/verify/1",
        )
        self.submitted: list[UUID] = []

    async def required(self, shop_id: UUID) -> bool:
        return self.is_required

    async def certify(self, document: BillingDocument) -> Certification:
        self.submitted.append(document.id)
        return self.outcome


class FakeOrders:
    def __init__(self) -> None:
        self.orders: dict[UUID, BillableOrder] = {}

    async def get(self, shop_id: UUID, order_id: UUID) -> BillableOrder | None:
        order = self.orders.get(order_id)
        return order if order and order.shop_id == shop_id else None


class FakeShops:
    def __init__(self, profile: SellerProfile | None) -> None:
        self.current = profile
        self.logo_bytes: bytes | None = None

    async def profile(self, shop_id: UUID) -> SellerProfile | None:
        return self.current

    async def logo(self, profile: SellerProfile) -> bytes | None:
        return self.logo_bytes


class FakeCustomers:
    def __init__(self) -> None:
        self.contacts: dict[UUID, Contact] = {}

    async def contact(self, shop_id: UUID, customer_id: UUID) -> Contact:
        return self.contacts.get(customer_id, Contact())


class RecordingBillingMessaging:
    """Refuses documents outside the window, like Meta, and templates if told so."""

    def __init__(self, clock: FrozenClock) -> None:
        self.clock = clock
        self.sent: list[tuple[Contact, Notification, str]] = []
        self.refuse_templates = False
        self.failing = False

    async def send(
        self, shop_id: UUID, to: Contact, notification: Notification, *, kind: str
    ) -> str:
        if self.failing:
            raise NotificationNotSent("messaging.message_not_sent")
        if not isinstance(notification, Template) and (
            to.last_message_at is None
            or self.clock.now() - to.last_message_at > timedelta(hours=24)
        ):
            raise NotificationNotSent("messaging.outside_customer_service_window")
        if isinstance(notification, Template) and self.refuse_templates:
            raise NotificationNotSent("messaging.template_not_approved")
        self.sent.append((to, notification, kind))
        return f"wamid.{len(self.sent)}"


def seller_profile(
    shop_id: UUID, *, vat_registered: bool = False, language: str = "fr"
) -> SellerProfile:
    return SellerProfile(
        shop_id=shop_id,
        account_id=uuid4(),
        name="Chez Ama",
        language=language,
        invoice_prefix="FAC",
        legal_name="",
        address="Grand marché, Lomé",
        phone="+22890000000",
        tax_id="",
        trade_register="",
        tax_regime="TPU" if not vat_registered else "REAL",
        vat_registered=vat_registered,
        logo_key=None,
    )


class BillingStack:
    """Real billing use cases on in-memory adapters."""

    def __init__(
        self,
        *,
        vat_registered: bool = False,
        language: str = "fr",
        public_base_url: str | None = PUBLIC_BASE_URL,
    ) -> None:
        self.shop_id = uuid4()
        self.customer_id = uuid4()
        self.account_id = uuid4()
        self.clock = FrozenClock(datetime(2026, 11, 10, 9, tzinfo=UTC))
        self.documents: dict[UUID, BillingDocument] = {}
        self.counters: dict[tuple[UUID, DocumentType], int] = {}
        self.tokens: dict[str, AccessToken] = {}
        self.events: list[DomainEvent] = []
        self.audit_log = FakeAuditLog()
        self.storage = FakeStorage()
        self.renderer = FakeRenderer()
        self.certifier = FakeCertifier()
        self.orders = FakeOrders()
        self.shops = FakeShops(
            seller_profile(self.shop_id, vat_registered=vat_registered, language=language)
        )
        self.customers = FakeCustomers()
        self.customers.contacts[self.customer_id] = Contact(
            name="Kɔdzo",
            user_id="TG.123",
            phone="+22891234567",
            last_message_at=self.clock.now(),
        )
        self.messaging = RecordingBillingMessaging(self.clock)
        self.settings = BillingSettings(public_base_url=public_base_url)

    def uow(self) -> FakeBillingUoW:
        return FakeBillingUoW(self)

    def release(self) -> DocumentRelease:
        return DocumentRelease(
            self.uow(),
            self.storage,
            self.renderer,
            self.certifier,
            self.shops,
            self.customers,
            self.messaging,
            self.clock,
            self.settings,
        )

    def issue_invoice(self) -> IssueInvoice:
        return IssueInvoice(
            self.uow(),
            self.orders,
            self.shops,
            self.customers,
            self.certifier,
            self.release(),
            self.clock,
        )

    def prepare(self) -> PrepareDocument:
        return PrepareDocument(self.uow(), self.release())

    def resend(self) -> ResendInvoice:
        return ResendInvoice(self.uow(), self.release())

    def credit_notes(self) -> IssueCreditNote:
        return IssueCreditNote(self.uow(), self.certifier, self.clock)

    def list_documents(self) -> ListBillingDocuments:
        return ListBillingDocuments(self.uow())

    def get_document(self) -> GetBillingDocument:
        return GetBillingDocument(self.uow())

    def pdf(self) -> GetDocumentPdf:
        return GetDocumentPdf(self.uow(), self.storage)

    def open_link(self) -> OpenInvoiceLink:
        return OpenInvoiceLink(self.uow(), self.storage, self.clock)

    def summary(self) -> GetBillingSummary:
        return GetBillingSummary(self.uow())

    def order(self, *lines: tuple[str, int, int], number: int = 1) -> BillableOrder:
        """An order of (name, quantity, unit price) lines."""
        items = lines or (("Pagne wax", 2, 7_500), ("Sac à main", 1, 7_500))
        order = BillableOrder(
            order_id=uuid4(),
            shop_id=self.shop_id,
            number=number,
            customer_id=self.customer_id,
            total=sum(quantity * price for _, quantity, price in items),
            lines=tuple(
                BillableLine(
                    name=name,
                    quantity=quantity,
                    unit_price=price,
                    product_id=None,
                    retailer_id=None,
                )
                for name, quantity, price in items
            ),
        )
        self.orders.orders[order.order_id] = order
        return order

    async def pay(
        self, order: BillableOrder, *, amount: int | None = None
    ) -> BillingDocument | None:
        return await self.issue_invoice().execute(
            shop_id=self.shop_id,
            order_id=order.order_id,
            payment_id=uuid4(),
            method="kprimepay",
            amount=order.total if amount is None else amount,
            paid_at=self.clock.now(),
        )

    async def credit(self, invoice: BillingDocument, request: CreditRequest) -> BillingDocument:
        return await self.credit_notes().execute(
            shop_id=self.shop_id,
            invoice_id=invoice.id,
            account_id=self.account_id,
            request=request,
        )

    def stored(self, document: BillingDocument) -> BillingDocument:
        return self.documents[document.id]
