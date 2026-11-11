from datetime import UTC, datetime
from io import BytesIO
from uuid import uuid4

from PIL import Image
from pypdf import PdfReader

from woassi.modules.billing.domain.document import (
    BillingDocument,
    Buyer,
    CreditRequest,
    Item,
    PaymentInfo,
    Seller,
)
from woassi.modules.billing.infrastructure.pdf import FpdfDocumentRenderer, render_pdf

NOW = datetime(2026, 11, 10, 14, 5, tzinfo=UTC)


def invoice(*, vat_registered: bool, language: str = "fr") -> BillingDocument:
    return BillingDocument.issue_invoice(
        shop_id=uuid4(),
        number="FAC-2026-000001",
        sequence=1,
        now=NOW,
        language=language,
        seller=Seller(
            name="Chez Ama",
            legal_name="Ama Mode SARL",
            address="12 rue du Marché, Lomé",
            tax_id="1000123456",
            trade_register="TG-LOM 2020 B 1234",
            tax_regime="REAL" if vat_registered else "TPU",
            vat_registered=vat_registered,
        ),
        buyer=Buyer(customer_id=uuid4(), name="Kɔdzo Éléonore", phone="+22891234567"),
        order_id=uuid4(),
        order_number=42,
        items=[Item("Pagne wax « Fleurs » très élégant", 2, 7_500), Item("Sac à main", 1, 7_500)],
        payment=PaymentInfo(payment_id=uuid4(), method="kprimepay", amount=22_500, paid_at=NOW),
    )


def text_of(pdf: bytes) -> str:
    return "\n".join(page.extract_text() for page in PdfReader(BytesIO(pdf)).pages)


def test_a_french_invoice_without_vat() -> None:
    pdf = render_pdf(invoice(vat_registered=False))
    assert pdf.startswith(b"%PDF")
    content = text_of(pdf)
    for expected in (
        "FACTURE",
        "PAYÉE",
        "FAC-2026-000001",
        "Commande n°42",
        "Kɔdzo Éléonore",
        "Pagne wax « Fleurs » très élégant",
        "Sac à main",
        "22 500 FCFA",
        "NE FACTURE PAS LA TVA",
        "NIF : 1000123456",
        "Régime fiscal : TPU",
        "Certification OTR",
        "10/11/2026 14:05",
    ):
        assert expected in content, expected


def test_an_english_vat_invoice() -> None:
    pdf = render_pdf(invoice(vat_registered=True, language="en"))
    content = text_of(pdf)
    for expected in ("INVOICE", "PAID", "VAT 18 % on 19 068 FCFA", "3 432 FCFA", "22 500 FCFA"):
        assert expected in content, expected
    assert "NE FACTURE PAS LA TVA" not in content


def test_a_credit_note_has_no_paid_stamp() -> None:
    document = invoice(vat_registered=True)
    note = document.credit(
        CreditRequest(reason="Article retourné", amount=5_000),
        previous=[],
        number="AV-2026-000001",
        sequence=1,
        now=NOW,
        issued_by=None,
        amount_label="Remboursement sur la facture FAC-2026-000001",
    )
    content = text_of(render_pdf(note))
    assert "AVOIR" in content
    assert "Avoir sur la facture FAC-2026-000001" in content
    assert "Motif : Article retourné" in content
    assert "PAYÉE" not in content


def test_rendering_is_deterministic_and_takes_a_logo() -> None:
    document = invoice(vat_registered=False)
    assert render_pdf(document) == render_pdf(document)
    logo = BytesIO()
    Image.new("RGB", (64, 64), (20, 120, 200)).save(logo, format="WEBP")
    with_logo = render_pdf(document, logo.getvalue())
    assert with_logo != render_pdf(document)
    # An unreadable logo is skipped, never fatal.
    assert render_pdf(document, b"not an image").startswith(b"%PDF")


async def test_the_adapter_renders_in_a_thread() -> None:
    pdf = await FpdfDocumentRenderer().render(invoice(vat_registered=False), None)
    assert pdf.startswith(b"%PDF")
