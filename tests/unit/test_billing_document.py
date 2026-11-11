from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from woassi.modules.billing.domain.document import (
    BillingDocument,
    Buyer,
    CertificationStatus,
    CreditedLine,
    CreditRequest,
    DocumentStatus,
    DocumentType,
    Item,
    PaymentInfo,
    Seller,
    content_hash,
    document_number,
    pdf_key,
)
from woassi.modules.billing.domain.errors import CreditExceedsInvoice, InvalidCreditNote
from woassi.modules.billing.domain.events import CreditNoteIssued, InvoiceIssued
from woassi.modules.billing.domain.taxes import TaxCode, split_inclusive

NOW = datetime(2026, 11, 10, 9, tzinfo=UTC)
SHOP = uuid4()


def invoice(*items: Item, vat_registered: bool = True, total: int | None = None) -> BillingDocument:
    items = items or (Item("Pagne wax", 2, 7_500), Item("Sac à main", 1, 7_500))
    paid = sum(item.quantity * item.unit_price for item in items) if total is None else total
    return BillingDocument.issue_invoice(
        shop_id=SHOP,
        number="FAC-2026-000001",
        sequence=1,
        now=NOW,
        language="fr",
        seller=Seller(name="Chez Ama", vat_registered=vat_registered),
        buyer=Buyer(customer_id=uuid4(), name="Ama", phone="+22891234567"),
        order_id=uuid4(),
        order_number=7,
        items=list(items),
        payment=PaymentInfo(payment_id=uuid4(), method="kprimepay", amount=paid, paid_at=NOW),
    )


def credit(
    document: BillingDocument, request: CreditRequest, previous: list[BillingDocument], n: int = 1
) -> BillingDocument:
    return document.credit(
        request,
        previous=previous,
        number=document_number("AV", 2026, n),
        sequence=n,
        now=NOW,
        issued_by=uuid4(),
        amount_label="Remboursement",
    )


def test_numbers_are_prefixed_by_the_year_and_zero_padded() -> None:
    assert document_number("FAC", 2026, 1) == "FAC-2026-000001"
    assert document_number("AV", 2027, 1234567) == "AV-2027-1234567"
    assert pdf_key(SHOP, DocumentType.INVOICE, "FAC-2026-000001") == (
        f"shops/{SHOP}/invoices/FAC-2026-000001.pdf"
    )
    assert pdf_key(SHOP, DocumentType.CREDIT_NOTE, "AV-2026-000001").endswith(
        "/credit-notes/AV-2026-000001.pdf"
    )


@pytest.mark.parametrize(
    ("total", "rate", "expected"),
    [
        (15_000, 1800, (12_712, 2_288)),  # 12711.86 -> up
        (7_500, 1800, (6_356, 1_144)),  # 6355.93 -> up
        (59, 1800, (50, 9)),  # exactly 50
        (1, 1800, (1, 0)),  # 0.847 -> 1
        (10, 1800, (8, 2)),  # 8.47 -> 8
        (0, 1800, (0, 0)),
        (12_345, 0, (12_345, 0)),
    ],
)
def test_rounding_is_half_up_on_the_amount_before_tax(
    total: int, rate: int, expected: tuple[int, int]
) -> None:
    assert split_inclusive(total, rate) == expected
    assert sum(split_inclusive(total, rate)) == total


def test_a_half_franc_rounds_up() -> None:
    # A 100 % rate halves the total: 3 -> 1.5 before tax, rounded up to 2.
    assert split_inclusive(3, 10_000) == (2, 1)


def test_a_vat_registered_seller_charges_18_percent() -> None:
    document = invoice()
    assert [line.tax_code for line in document.lines] == [TaxCode.VAT_18] * 2
    assert [(line.amount_excl_tax, line.tax_amount) for line in document.lines] == [
        (12_712, 2_288),
        (6_356, 1_144),
    ]
    assert (document.total_excl_tax, document.total_tax, document.total) == (19_068, 3_432, 22_500)
    [group] = document.tax_totals
    assert (group.rate_bp, group.amount_excl_tax, group.tax_amount) == (1800, 19_068, 3_432)


def test_a_seller_without_vat_charges_none() -> None:
    document = invoice(vat_registered=False)
    assert {line.tax_code for line in document.lines} == {TaxCode.NOT_SUBJECT}
    assert {line.tax_rate_bp for line in document.lines} == {0}
    assert (document.total_excl_tax, document.total_tax, document.total) == (22_500, 0, 22_500)


def test_an_issued_invoice_is_paid_sealed_and_announced() -> None:
    document = invoice()
    assert document.status is DocumentStatus.ISSUED
    assert document.payment_status is not None
    assert document.certification.status is CertificationStatus.NOT_REQUIRED
    assert document.releasable
    assert document.content_sha256 == content_hash(document)
    assert len(document.content_sha256) == 64
    [event] = document.pull_events()
    assert isinstance(event, InvoiceIssued)
    assert event.total == 22_500
    # Personal data never shows in a repr (logs, arq job arguments).
    assert "Ama" not in repr(document.buyer)


def test_the_hash_ignores_tracking_fields_but_not_the_content() -> None:
    document = invoice()
    sealed = document.content_sha256
    document.attach_pdf("key.pdf", "abc", NOW)
    document.sent("wamid.1", NOW)
    assert content_hash(document) == sealed
    changed = replace(document, lines=(replace(document.lines[0], name="Autre"),))
    assert content_hash(changed) != sealed


def test_an_invoice_must_match_the_payment() -> None:
    with pytest.raises(ValueError, match="amount paid"):
        invoice(total=1)


def test_a_full_credit_note_mirrors_the_invoice() -> None:
    document = invoice()
    note = credit(document, CreditRequest(reason="Retour", full=True), [])
    assert note.document_type is DocumentType.CREDIT_NOTE
    assert note.original_document_id == document.id
    assert note.original_number == document.number
    assert [(line.amount_excl_tax, line.tax_amount, line.total) for line in note.lines] == [
        (line.amount_excl_tax, line.tax_amount, line.total) for line in document.lines
    ]
    assert document.credited_total == 22_500
    assert document.status is DocumentStatus.CREDITED
    [event] = note.pull_events()
    assert isinstance(event, CreditNoteIssued)
    with pytest.raises(CreditExceedsInvoice):
        credit(document, CreditRequest(reason="Encore", amount=1), [note], 2)


def test_partial_credit_by_line_and_quantity() -> None:
    document = invoice()
    note = credit(document, CreditRequest(reason="Un pagne abîmé", lines=(CreditedLine(1, 1),)), [])
    [line] = note.lines
    assert (line.quantity, line.total, line.source_position) == (1, 7_500, 1)
    assert document.status is DocumentStatus.PARTIALLY_CREDITED
    # One pagne is left on line 1: two more cannot be credited.
    with pytest.raises(CreditExceedsInvoice, match="Line 1"):
        credit(document, CreditRequest(reason="x", lines=(CreditedLine(1, 2),)), [note], 2)
    with pytest.raises(InvalidCreditNote):
        credit(document, CreditRequest(reason="x", lines=(CreditedLine(9, 1),)), [note], 2)


def test_partial_credit_by_amount() -> None:
    document = invoice()
    note = credit(document, CreditRequest(reason="Geste commercial", amount=5_000), [])
    [line] = note.lines
    assert (line.name, line.total, line.amount_excl_tax, line.tax_amount) == (
        "Remboursement",
        5_000,
        4_237,
        763,
    )
    rest = credit(document, CreditRequest(reason="Le reste", full=True), [note], 2)
    assert rest.total == 17_500
    assert document.credited_total == 22_500
    with pytest.raises(CreditExceedsInvoice):
        credit(document, CreditRequest(reason="x", amount=1), [note, rest], 3)


def test_credit_notes_never_reverse_more_vat_than_charged() -> None:
    # 3 x 1 000: VAT 458 on the invoice, but 153 per unit when credited one by one.
    document = invoice(Item("Savon", 3, 1_000))
    assert document.total_tax == 458
    notes: list[BillingDocument] = []
    for n in range(1, 4):
        notes.append(
            credit(document, CreditRequest(reason="x", lines=(CreditedLine(1, 1),)), notes, n)
        )
    assert sum(note.total_tax for note in notes) == document.total_tax
    assert sum(note.total_excl_tax for note in notes) == document.total_excl_tax


def test_a_credit_by_amount_cannot_exceed_what_is_left() -> None:
    document = invoice()
    with pytest.raises(CreditExceedsInvoice):
        credit(document, CreditRequest(reason="x", amount=22_501), [])
    assert document.credited_total == 0


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"full": True, "amount": 10},
        {"amount": 0},
        {"amount": -5},
        {"lines": (CreditedLine(1, 0),)},
        {"lines": (CreditedLine(1, 1), CreditedLine(1, 1))},
        {"full": True, "reason": " "},
    ],
)
def test_invalid_credit_requests(fields: dict[str, object]) -> None:
    values: dict[str, object] = {"reason": "Retour", **fields}
    with pytest.raises(InvalidCreditNote):
        CreditRequest(**values)  # type: ignore[arg-type]


def test_a_credit_note_cannot_be_credited() -> None:
    document = invoice()
    note = credit(document, CreditRequest(reason="x", amount=100), [])
    with pytest.raises(InvalidCreditNote):
        credit(note, CreditRequest(reason="x", amount=1), [], 2)


def test_delivery_failures_never_undo_a_delivery() -> None:
    document = invoice()
    document.sent("wamid.1", NOW)
    document.not_sent(NOW)
    assert document.delivery_message_id == "wamid.1"
    assert document.delivery_status.value == "SENT"


def test_the_pdf_is_attached_once() -> None:
    document = invoice()
    assert document.attach_pdf("a.pdf", "1" * 64, NOW)
    assert not document.attach_pdf("b.pdf", "2" * 64, NOW)
    assert document.pdf_key == "a.pdf"
