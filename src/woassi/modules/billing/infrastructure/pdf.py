"""PDF invoices and credit notes with fpdf2: pure Python, no system library.

DejaVu Sans is bundled (fonts/, Bitstream Vera license) so that French accents and Ewe or
Kabiyè letters (ɖ, ɛ, ɔ...) in product and customer names render. A4 portrait. The empty box at
the bottom right is reserved for the OTR QR code and certification number.
"""

from collections.abc import Callable
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo

import anyio.to_thread
import structlog
from fpdf import FPDF
from fpdf.fonts import FontFace

from woassi.modules.billing.application.texts import (
    NO_VAT_MENTION,
    amount,
    date,
    language_of,
    rate,
    text,
)
from woassi.modules.billing.domain.document import (
    BillingDocument,
    CertificationStatus,
    DocumentType,
)

log = structlog.get_logger()

FONTS = Path(__file__).parent / "fonts"
FAMILY = "DejaVu"
LOCAL_TIMEZONE = ZoneInfo("Africa/Lome")
MARGIN = 15
LOGO_SIZE = 28
QR_BOX = 38
STAMP_RED = (200, 30, 30)
GREY = (110, 110, 110)


def _figure(value: int) -> str:
    """`12 500`: amounts in table cells, the currency is in the header note."""
    return f"{value:,}".replace(",", " ")


class FpdfDocumentRenderer:
    """`DocumentRenderer` on fpdf2. Rendering runs in a worker thread."""

    async def render(self, document: BillingDocument, logo: bytes | None) -> bytes:
        return await anyio.to_thread.run_sync(render_pdf, document, logo)


def render_pdf(document: BillingDocument, logo: bytes | None = None) -> bytes:
    """The PDF of `document`, deterministic for the same input (dated at issue)."""
    language = language_of(document.language)

    def t(key: str, **values: object) -> str:
        return text(language, key, **values)

    pdf = FPDF(orientation="portrait", unit="mm", format="A4")
    pdf.set_margins(MARGIN, MARGIN, MARGIN)
    pdf.set_auto_page_break(auto=True, margin=MARGIN + 5)
    pdf.add_font(FAMILY, "", str(FONTS / "DejaVuSans.ttf"))
    pdf.add_font(FAMILY, "B", str(FONTS / "DejaVuSans-Bold.ttf"))
    pdf.set_creation_date(document.issued_at)
    pdf.set_title(document.number)
    pdf.set_author(document.seller.name)
    pdf.set_creator("Woassi")
    pdf.add_page()

    is_invoice = document.document_type is DocumentType.INVOICE
    seller = document.seller
    buyer = document.buyer
    top = pdf.get_y()

    # Seller, with the logo on its left when there is one.
    text_x = MARGIN
    if logo:
        try:
            pdf.image(
                BytesIO(logo), x=MARGIN, y=top, w=LOGO_SIZE, h=LOGO_SIZE, keep_aspect_ratio=True
            )
            text_x = MARGIN + LOGO_SIZE + 4
        except Exception:  # an unreadable logo must never block an invoice
            log.warning("invoice_logo_skipped", document_id=str(document.id))
    pdf.set_xy(text_x, top)
    pdf.set_font(FAMILY, "B", 13)
    pdf.multi_cell(95 - (text_x - MARGIN), 6, seller.legal_name or seller.name, new_x="LMARGIN")
    pdf.set_font(FAMILY, "", 9)
    seller_lines = []
    if seller.legal_name and seller.legal_name != seller.name:
        seller_lines.append(seller.name)
    if seller.address:
        seller_lines.append(seller.address)
    if seller.phone:
        seller_lines.append(f"{t('phone')} {seller.phone}")
    if seller.tax_id:
        seller_lines.append(f"{t('tax_id')} : {seller.tax_id}")
    if seller.trade_register:
        seller_lines.append(f"{t('trade_register')} : {seller.trade_register}")
    if seller.tax_regime:
        seller_lines.append(f"{t('tax_regime')} : {t('regime.' + seller.tax_regime)}")
    for line in seller_lines:
        pdf.set_x(text_x)
        pdf.multi_cell(95 - (text_x - MARGIN), 4.5, line, new_x="LMARGIN")
    seller_bottom = max(pdf.get_y(), top + (LOGO_SIZE if text_x > MARGIN else 0))

    # Title block, on the right.
    right_x = 120
    pdf.set_xy(right_x, top)
    pdf.set_font(FAMILY, "B", 18)
    pdf.cell(75, 9, t("invoice" if is_invoice else "credit_note"), align="R", new_x="LMARGIN")
    pdf.set_font(FAMILY, "", 9.5)
    issued_local = document.issued_at.astimezone(LOCAL_TIMEZONE)
    details = [
        f"{t('number')} {document.number}",
        f"{t('issued_at')} : {date(issued_local)}",
    ]
    if document.order_number is not None:
        details.append(t("order", number=document.order_number))
    if not is_invoice and document.original_number:
        details.append(t("refers_to", number=document.original_number))
    pdf.set_y(top + 10)
    for line in details:
        pdf.set_x(right_x)
        pdf.cell(75, 5, line, align="R", new_x="LMARGIN", new_y="NEXT")
    title_bottom = pdf.get_y()

    # Buyer.
    pdf.set_y(max(seller_bottom, title_bottom) + 6)
    buyer_top = pdf.get_y()
    pdf.set_x(right_x - 10)
    pdf.set_font(FAMILY, "B", 9.5)
    pdf.cell(85, 5, t("buyer"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(FAMILY, "", 9.5)
    buyer_lines = [buyer.name or t("customer")]
    if buyer.phone:
        buyer_lines.append(f"{t('phone')} {buyer.phone}")
    if buyer.tax_id:
        buyer_lines.append(f"{t('tax_id')} : {buyer.tax_id}")
    for line in buyer_lines:
        pdf.set_x(right_x - 10)
        pdf.multi_cell(85, 5, line, new_x="LMARGIN")

    # The PAID stamp, under the seller block.
    if is_invoice:
        _stamp(pdf, t("paid_stamp"), x=MARGIN + 20, y=buyer_top + 10)

    # Lines.
    pdf.set_y(max(pdf.get_y(), buyer_top + 26) + 6)
    pdf.set_font(FAMILY, "", 8.5)
    pdf.set_text_color(*GREY)
    pdf.cell(0, 4, t("prices_note"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)
    pdf.ln(1)
    headings = FontFace(emphasis="BOLD", fill_color=(235, 235, 235))
    with pdf.table(
        col_widths=(64, 12, 24, 24, 14, 26),
        text_align=("LEFT", "RIGHT", "RIGHT", "RIGHT", "RIGHT", "RIGHT"),
        line_height=5.5,
        headings_style=headings,
        borders_layout="HORIZONTAL_LINES",
    ) as table:
        row = table.row()
        for key in ("item", "quantity", "unit_price", "excl_tax", "vat_rate", "line_total"):
            row.cell(t(key))
        for item in document.lines:
            row = table.row()
            row.cell(item.name)
            row.cell(str(item.quantity))
            row.cell(_figure(item.unit_price))
            row.cell(_figure(item.amount_excl_tax))
            row.cell(rate(item.tax_rate_bp))
            row.cell(_figure(item.total))

    # Totals.
    pdf.ln(4)
    totals: list[tuple[str, str, bool]] = [
        (t("total_excl_tax"), amount(document.total_excl_tax), False)
    ]
    for group in document.tax_totals:
        if group.rate_bp:
            label = t("vat_on", rate=rate(group.rate_bp), base=amount(group.amount_excl_tax))
            totals.append((label, amount(group.tax_amount), False))
    totals.append((t("total_tax"), amount(document.total_tax), False))
    totals.append((t("total"), amount(document.total), True))
    for label, value, bold in totals:
        pdf.set_font(FAMILY, "B" if bold else "", 11 if bold else 9.5)
        pdf.set_x(95)
        pdf.cell(60, 6, label, align="R")
        pdf.cell(40, 6, value, align="R", new_x="LMARGIN", new_y="NEXT")

    pdf.ln(3)
    pdf.set_font(FAMILY, "B", 9.5)
    if not seller.vat_registered:
        pdf.cell(0, 5, NO_VAT_MENTION, new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(FAMILY, "", 9.5)
    if is_invoice and document.payment is not None:
        paid_local = document.payment.paid_at.astimezone(LOCAL_TIMEZONE)
        method = t(f"method.{document.payment.method}")
        pdf.cell(
            0, 5, t("paid_on", date=date(paid_local), method=method), new_x="LMARGIN", new_y="NEXT"
        )
    if not is_invoice:
        if document.reason:
            pdf.multi_cell(0, 5, f"{t('reason')} : {document.reason}", new_x="LMARGIN")
        if document.refund_reference:
            pdf.cell(
                0,
                5,
                f"{t('refund_reference')} : {document.refund_reference}",
                new_x="LMARGIN",
                new_y="NEXT",
            )

    _certification_block(pdf, document, t)
    return bytes(pdf.output())


def _stamp(pdf: FPDF, label: str, *, x: float, y: float) -> None:
    """A tilted red frame with `label`, like a rubber stamp."""
    pdf.set_draw_color(*STAMP_RED)
    pdf.set_text_color(*STAMP_RED)
    pdf.set_line_width(0.8)
    pdf.set_font(FAMILY, "B", 22)
    width = pdf.get_string_width(label) + 10
    with pdf.rotation(12, x=x + width / 2, y=y + 6):
        pdf.rect(x, y, width, 12)
        pdf.set_xy(x, y)
        pdf.cell(width, 12, label, align="C")
    pdf.set_draw_color(0, 0, 0)
    pdf.set_text_color(0, 0, 0)
    pdf.set_line_width(0.2)


def _certification_block(pdf: FPDF, document: BillingDocument, t: Callable[..., str]) -> None:
    """Bottom of the page: the reserved OTR box, and the document's fingerprint."""
    box_y = pdf.h - MARGIN - QR_BOX - 8
    if pdf.get_y() > box_y - 4:
        pdf.add_page()
    box_x = pdf.w - MARGIN - QR_BOX
    pdf.set_draw_color(*GREY)
    pdf.set_line_width(0.2)
    pdf.set_dash_pattern(dash=1, gap=1)
    pdf.rect(box_x, box_y, QR_BOX, QR_BOX)
    pdf.set_dash_pattern()
    pdf.set_draw_color(0, 0, 0)
    pdf.set_font(FAMILY, "B", 8)
    pdf.set_xy(MARGIN, box_y)
    pdf.cell(box_x - MARGIN - 4, 4, t("certification"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(FAMILY, "", 8)
    certification = document.certification
    if certification.status is CertificationStatus.CERTIFIED and certification.certified_number:
        lines = [f"{t('certified_number')} : {certification.certified_number}"]
    else:
        lines = [t("not_certified")]
    lines.append(f"SHA-256 : {document.content_sha256[:32]}")
    pdf.set_text_color(*GREY)
    for line in lines:
        pdf.set_x(MARGIN)
        pdf.multi_cell(box_x - MARGIN - 4, 4, line, new_x="LMARGIN")
    pdf.set_text_color(0, 0, 0)
