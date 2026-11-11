import asyncio
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.billing_fakes import BillingStack
from woassi.modules.billing.application.ports import AccessToken
from woassi.modules.billing.domain.document import (
    BillingDocument,
    CreditRequest,
    DeliveryStatus,
    DocumentType,
)
from woassi.modules.billing.domain.errors import CreditExceedsInvoice
from woassi.modules.billing.infrastructure.models import BillingCounterModel, BillingTaxTotalModel
from woassi.modules.billing.infrastructure.uow import SqlAlchemyBillingUoW
from woassi.shared.domain.events import DomainEvent
from woassi.shared.infrastructure.audit import AuditEntryModel

pytestmark = pytest.mark.integration


class RecordingBus:
    def __init__(self) -> None:
        self.events: list[DomainEvent] = []

    async def publish(self, event: DomainEvent) -> None:
        self.events.append(event)


class Real(BillingStack):
    """The billing stack on Postgres, with fakes for storage, rendering and other modules."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession], **options: bool) -> None:
        super().__init__(**options)  # type: ignore[arg-type]
        self.sessions = sessions
        self.bus = RecordingBus()

    def uow(self) -> SqlAlchemyBillingUoW:  # type: ignore[override]
        return SqlAlchemyBillingUoW(self.sessions, self.bus, self.clock)

    async def load(self, document: BillingDocument) -> BillingDocument:
        async with self.uow() as uow:
            found = await uow.documents.get(self.shop_id, document.id)
        assert found is not None
        return found


@pytest.fixture
def real(sessions: async_sessionmaker[AsyncSession]) -> Real:
    return Real(sessions)


async def test_numbers_are_gapless_under_concurrency(real: Real) -> None:
    orders = [real.order(number=n) for n in range(1, 11)]
    issued = await asyncio.gather(*(real.pay(order) for order in orders))
    numbers = sorted(document.sequence for document in issued if document)
    assert numbers == list(range(1, 11))
    assert len({document.number for document in issued if document}) == 10


async def test_one_invoice_per_order_even_when_replayed_concurrently(
    real: Real, sessions: async_sessionmaker[AsyncSession]
) -> None:
    order = real.order()
    issued = await asyncio.gather(*(real.pay(order) for _ in range(5)))
    assert len({document.id for document in issued if document}) == 1
    async with sessions() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(AuditEntryModel)
            .where(AuditEntryModel.action == "invoice.issued")
        )
        counter = await session.get(BillingCounterModel, (real.shop_id, "INVOICE"))
    assert count == 1
    # The losers rolled their number back: the next invoice gets 2, not 6.
    assert counter is not None and counter.last_sequence == 1
    following = await real.pay(real.order(number=2))
    assert following is not None and following.sequence == 2


async def test_a_document_round_trips(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    real = Real(sessions, vat_registered=True)
    invoice = await real.pay(real.order(("Pagne wax « Fleurs »", 2, 7_500), ("Sac", 1, 7_500)))
    assert invoice is not None
    loaded = await real.load(invoice)
    assert loaded.lines == invoice.lines
    assert loaded.seller == invoice.seller
    assert loaded.buyer == invoice.buyer
    assert loaded.payment == invoice.payment
    assert loaded.content_sha256 == invoice.content_sha256
    assert loaded.pdf_key == f"shops/{real.shop_id}/invoices/FAC-2026-000001.pdf"
    assert loaded.delivery_status is DeliveryStatus.SENT
    assert (loaded.total_excl_tax, loaded.total_tax) == (19_068, 3_432)
    async with sessions() as session:
        [row] = (await session.scalars(select(BillingTaxTotalModel))).all()
    assert (row.tax_code, row.rate_bp, row.tax_amount) == ("VAT_18", 1800, 3_432)

    assert invoice.order_id is not None
    async with real.uow() as uow:
        assert (await uow.documents.invoice_for_order(real.shop_id, invoice.order_id)) is not None
        assert await uow.documents.get(uuid4(), invoice.id) is None


async def test_concurrent_credit_notes_cannot_overdraw(real: Real) -> None:
    invoice = await real.pay(real.order())
    assert invoice is not None
    results = await asyncio.gather(
        real.credit(invoice, CreditRequest(reason="a", amount=15_000)),
        real.credit(invoice, CreditRequest(reason="b", amount=15_000)),
        return_exceptions=True,
    )
    notes = [r for r in results if isinstance(r, BillingDocument)]
    refused = [r for r in results if isinstance(r, CreditExceedsInvoice)]
    assert (len(notes), len(refused)) == (1, 1)
    assert notes[0].number == "AV-2026-000001"
    assert (await real.load(invoice)).credited_total == 15_000
    rest = await real.credit(invoice, CreditRequest(reason="c", full=True))
    assert rest.number == "AV-2026-000002"
    assert rest.total == 7_500


async def test_lists_summary_and_links(real: Real) -> None:
    invoice = await real.pay(real.order())
    assert invoice is not None
    await real.credit(invoice, CreditRequest(reason="x", amount=2_500))
    page = await real.list_documents().execute(
        shop_id=real.shop_id,
        document_type=DocumentType.CREDIT_NOTE,
        invoice_id=invoice.id,
        issued_from=date(2026, 11, 10),
        issued_to=date(2026, 11, 10),
    )
    assert page.total == 1
    summary = await real.summary().execute(
        shop_id=real.shop_id, issued_from=date(2026, 11, 1), issued_to=date(2026, 11, 30)
    )
    assert (summary.invoices.total, summary.credit_notes.total, summary.net.total) == (
        22_500,
        2_500,
        20_000,
    )

    now = datetime(2026, 11, 10, tzinfo=UTC)
    async with real.uow() as uow:
        await uow.documents.add_access_token(
            AccessToken("a" * 64, invoice.id, real.shop_id, now, now)
        )
        await uow.commit()
    async with real.uow() as uow:
        found = await uow.documents.find_access_token("a" * 64)
    assert found is not None and found.document_id == invoice.id
