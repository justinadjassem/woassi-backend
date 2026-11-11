from datetime import date, timedelta
from urllib.parse import urlparse

import pytest

from tests.billing_fakes import BillingStack
from woassi.modules.billing.application.ports import Contact, Document, Template
from woassi.modules.billing.application.use_cases.release import token_hash
from woassi.modules.billing.domain.document import (
    Certification,
    CertificationStatus,
    CreditedLine,
    CreditRequest,
    DeliveryStatus,
    DocumentType,
)
from woassi.modules.billing.domain.errors import (
    CreditExceedsInvoice,
    CreditNoteNotFound,
    InvoiceLinkNotFound,
    InvoiceNotFound,
    InvoiceNotSent,
    PdfNotReady,
)
from woassi.modules.billing.domain.events import CreditNoteIssued, InvoiceIssued
from woassi.shared.domain.errors import InvalidData


@pytest.fixture
def stack() -> BillingStack:
    return BillingStack()


async def test_a_confirmed_payment_issues_renders_and_sends_the_invoice(
    stack: BillingStack,
) -> None:
    order = stack.order(number=42)
    invoice = await stack.pay(order)
    assert invoice is not None
    stored = stack.stored(invoice)
    assert stored.number == "FAC-2026-000001"
    assert stored.order_number == 42
    assert stored.total == 22_500
    assert stored.buyer.name == "Kɔdzo"
    assert stored.seller.address == "Grand marché, Lomé"
    # Stored privately, under its number, with the hash of what was uploaded.
    key = f"shops/{stack.shop_id}/invoices/FAC-2026-000001.pdf"
    assert stored.pdf_key == key
    assert stack.storage.objects[key].startswith(b"%PDF")
    assert stored.pdf_sha256 is not None and len(stored.pdf_sha256) == 64
    # The customer wrote recently: the PDF itself goes out.
    [(_, sent, kind)] = stack.messaging.sent
    assert kind == "billing.invoice"
    assert isinstance(sent, Document)
    assert sent.filename == "FAC-2026-000001.pdf"
    assert "expire=86400" in sent.link
    assert "22 500 FCFA" in sent.caption
    assert stored.delivery_status is DeliveryStatus.SENT
    assert stored.delivery_message_id == "wamid.1"
    # Audited in the issuing transaction, on behalf of the owner.
    [entry] = stack.audit_log.entries
    assert entry.action == "invoice.issued"
    assert entry.details["number"] == "FAC-2026-000001"
    assert entry.details["by"] == "system"
    assert [type(event) for event in stack.events] == [InvoiceIssued]


async def test_numbers_follow_each_other(stack: BillingStack) -> None:
    first = await stack.pay(stack.order(number=1))
    second = await stack.pay(stack.order(number=2))
    assert first is not None and second is not None
    assert (first.number, second.number) == ("FAC-2026-000001", "FAC-2026-000002")


async def test_a_replayed_payment_issues_nothing_more(stack: BillingStack) -> None:
    order = stack.order()
    first = await stack.pay(order)
    again = await stack.pay(order)
    assert first is not None and again is not None
    assert again.id == first.id
    assert len(stack.documents) == 1
    assert len(stack.messaging.sent) == 1
    assert len(stack.renderer.rendered) == 1
    assert len(stack.audit_log.entries) == 1


async def test_a_replay_finishes_what_a_crash_left(stack: BillingStack) -> None:
    order = stack.order()
    stack.renderer.failing = True
    with pytest.raises(RuntimeError):
        await stack.pay(order)
    [invoice] = stack.documents.values()
    assert invoice.pdf_key is None  # issued and numbered, not rendered yet
    stack.renderer.failing = False
    await stack.pay(order)
    invoice = stack.stored(invoice)
    assert invoice.pdf_key is not None
    assert invoice.delivery_status is DeliveryStatus.SENT
    assert len(stack.documents) == 1


async def test_outside_the_window_the_template_carries_a_link(stack: BillingStack) -> None:
    stack.customers.contacts[stack.customer_id] = Contact(
        user_id="TG.123", last_message_at=stack.clock.now() - timedelta(days=3)
    )
    invoice = await stack.pay(stack.order())
    assert invoice is not None
    [(_, sent, _)] = stack.messaging.sent
    assert isinstance(sent, Template)
    assert sent.name == "woassi_invoice_ready"
    assert sent.language == "fr"
    assert sent.parameters["invoice_number"] == "FAC-2026-000001"
    assert sent.parameters["total"] == "22 500 FCFA"
    link = sent.parameters["link"]
    assert link.startswith("https://api.example.com/invoices/")
    token = urlparse(link).path.rsplit("/", 1)[1]
    # Only the hash is stored.
    assert token not in stack.tokens
    assert token_hash(token) in stack.tokens
    url = await stack.open_link().execute(token)
    assert url == f"http://signed/get/shops/{stack.shop_id}/invoices/FAC-2026-000001.pdf?expire=300"


async def test_invoice_links_expire(stack: BillingStack) -> None:
    stack.customers.contacts[stack.customer_id] = Contact(user_id="TG.123")
    await stack.pay(stack.order())
    [(_, sent, _)] = stack.messaging.sent
    assert isinstance(sent, Template)
    token = sent.parameters["link"].rsplit("/", 1)[1]
    stack.clock.advance(timedelta(days=31))
    with pytest.raises(InvoiceLinkNotFound):
        await stack.open_link().execute(token)
    for unknown in ("x" * 43, "short", "y" * 500):
        with pytest.raises(InvoiceLinkNotFound):
            await stack.open_link().execute(unknown)


async def test_a_message_failure_never_fails_the_invoice(stack: BillingStack) -> None:
    stack.messaging.failing = True
    invoice = await stack.pay(stack.order())
    assert invoice is not None
    stored = stack.stored(invoice)
    assert stored.pdf_key is not None
    assert stored.delivery_status is DeliveryStatus.FAILED
    # The merchant can send it again from the dashboard.
    with pytest.raises(InvoiceNotSent):
        await stack.resend().execute(shop_id=stack.shop_id, invoice_id=invoice.id)
    stack.messaging.failing = False
    resent = await stack.resend().execute(shop_id=stack.shop_id, invoice_id=invoice.id)
    assert resent.delivery_status is DeliveryStatus.SENT


async def test_without_a_public_url_nothing_goes_out_of_the_window() -> None:
    stack = BillingStack(public_base_url=None)
    stack.customers.contacts[stack.customer_id] = Contact(user_id="TG.123")
    invoice = await stack.pay(stack.order())
    assert invoice is not None
    assert stack.messaging.sent == []
    assert stack.stored(invoice).delivery_status is DeliveryStatus.FAILED
    assert stack.tokens == {}


async def test_an_amount_that_does_not_match_issues_nothing(stack: BillingStack) -> None:
    assert await stack.pay(stack.order(), amount=1) is None
    assert stack.documents == {}
    assert stack.counters == {}


async def test_an_english_vat_shop() -> None:
    stack = BillingStack(vat_registered=True, language="en")
    invoice = await stack.pay(stack.order())
    assert invoice is not None
    assert invoice.total_tax == 3_432
    assert invoice.language == "en"
    [(_, sent, _)] = stack.messaging.sent
    assert isinstance(sent, Document)
    assert sent.caption.startswith("Your invoice")


async def test_certification_holds_the_pdf_and_delivery_back(stack: BillingStack) -> None:
    stack.certifier.is_required = True
    stack.certifier.outcome = Certification(
        status=CertificationStatus.FAILED_RETRYABLE, attempts=1, error_code="unavailable"
    )
    order = stack.order()
    invoice = await stack.pay(order)
    assert invoice is not None
    stored = stack.stored(invoice)
    assert stored.certification.status is CertificationStatus.FAILED_RETRYABLE
    assert stored.pdf_key is None
    assert stack.messaging.sent == []
    with pytest.raises(PdfNotReady):
        await stack.pdf().execute(
            shop_id=stack.shop_id, document_id=invoice.id, document_type=DocumentType.INVOICE
        )

    # A replay (a future retry job) certifies it, then releases it.
    stack.certifier.outcome = Certification(
        status=CertificationStatus.CERTIFIED, certified_number="OTR-1"
    )
    await stack.pay(order)
    stored = stack.stored(invoice)
    assert stored.certification.certified_number == "OTR-1"
    assert stored.pdf_key is not None
    assert stored.delivery_status is DeliveryStatus.SENT


async def test_credit_notes_are_numbered_audited_and_rendered_later(
    stack: BillingStack,
) -> None:
    invoice = await stack.pay(stack.order())
    assert invoice is not None
    stack.events.clear()
    note = await stack.credit(invoice, CreditRequest(reason="Retour", lines=(CreditedLine(2, 1),)))
    assert note.number == "AV-2026-000001"
    assert note.total == 7_500
    assert note.issued_by == stack.account_id
    assert stack.stored(invoice).credited_total == 7_500
    entry = stack.audit_log.entries[-1]
    assert entry.action == "credit_note.issued"
    assert entry.account_id == stack.account_id
    assert entry.details["invoice_number"] == "FAC-2026-000001"
    [event] = stack.events
    assert isinstance(event, CreditNoteIssued)
    # The PDF is rendered by the worker, not in the request.
    assert stack.stored(note).pdf_key is None
    await stack.prepare().execute(shop_id=stack.shop_id, document_id=note.id)
    assert stack.stored(note).pdf_key == (f"shops/{stack.shop_id}/credit-notes/AV-2026-000001.pdf")

    second = await stack.credit(invoice, CreditRequest(reason="Le reste", full=True))
    assert second.number == "AV-2026-000002"
    assert second.total == 15_000
    with pytest.raises(CreditExceedsInvoice):
        await stack.credit(invoice, CreditRequest(reason="Trop", amount=1))
    # A refused credit note takes no number.
    third = await stack.pay(stack.order(number=2))
    assert third is not None
    note = await stack.credit(third, CreditRequest(reason="x", amount=100))
    assert note.number == "AV-2026-000003"


async def test_credit_notes_need_an_invoice_of_the_shop(stack: BillingStack) -> None:
    invoice = await stack.pay(stack.order())
    assert invoice is not None
    note = await stack.credit(invoice, CreditRequest(reason="x", amount=100))
    with pytest.raises(InvoiceNotFound):
        await stack.credit(note, CreditRequest(reason="x", amount=1))
    other = BillingStack()
    with pytest.raises(InvoiceNotFound):
        await other.credit(invoice, CreditRequest(reason="x", amount=1))


async def test_lists_and_summary(stack: BillingStack) -> None:
    first = await stack.pay(stack.order(("Pagne", 1, 10_000), number=1))
    stack.clock.advance(timedelta(days=1))
    second = await stack.pay(stack.order(("Sac", 2, 5_000), number=2))
    assert first is not None and second is not None
    await stack.credit(second, CreditRequest(reason="Retour", amount=2_000))

    page = await stack.list_documents().execute(
        shop_id=stack.shop_id, document_type=DocumentType.INVOICE
    )
    assert [d.number for d in page.items] == ["FAC-2026-000002", "FAC-2026-000001"]
    assert page.total == 2
    day = await stack.list_documents().execute(
        shop_id=stack.shop_id,
        document_type=DocumentType.INVOICE,
        issued_from=date(2026, 11, 10),
        issued_to=date(2026, 11, 10),
    )
    assert [d.number for d in day.items] == ["FAC-2026-000001"]
    by_order = await stack.list_documents().execute(
        shop_id=stack.shop_id, document_type=DocumentType.INVOICE, order_id=second.order_id
    )
    assert [d.id for d in by_order.items] == [second.id]
    notes = await stack.list_documents().execute(
        shop_id=stack.shop_id, document_type=DocumentType.CREDIT_NOTE, invoice_id=second.id
    )
    assert notes.total == 1

    summary = await stack.summary().execute(
        shop_id=stack.shop_id, issued_from=date(2026, 11, 1), issued_to=date(2026, 11, 30)
    )
    assert summary.invoices.documents == 2
    assert summary.invoices.total == 20_000
    assert summary.invoices.tax_amount == 0
    assert summary.credit_notes.documents == 1
    assert summary.credit_notes.total == 2_000
    assert summary.net.total == 18_000
    assert summary.net.amount_excl_tax == 18_000
    with pytest.raises(InvalidData):
        await stack.summary().execute(
            shop_id=stack.shop_id, issued_from=date(2026, 11, 2), issued_to=date(2026, 11, 1)
        )


async def test_documents_are_scoped_to_their_shop(stack: BillingStack) -> None:
    invoice = await stack.pay(stack.order())
    assert invoice is not None
    other = BillingStack()
    other.documents = stack.documents
    with pytest.raises(InvoiceNotFound):
        await other.get_document().execute(
            shop_id=other.shop_id, document_id=invoice.id, document_type=DocumentType.INVOICE
        )
    with pytest.raises(InvoiceNotFound):
        await other.resend().execute(shop_id=other.shop_id, invoice_id=invoice.id)
    with pytest.raises(CreditNoteNotFound):
        await stack.get_document().execute(
            shop_id=stack.shop_id, document_id=invoice.id, document_type=DocumentType.CREDIT_NOTE
        )
