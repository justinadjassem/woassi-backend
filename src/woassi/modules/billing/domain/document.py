"""Billing documents: invoices and credit notes.

One entity for both, told apart by `document_type`: they share their lines, taxes, party
snapshots, numbering, PDF, integrity hashes and OTR certification fields, and a credit note is
a document of its own, with its own number, that the OTR will certify like an invoice. A future
advance invoice (acompte) is one more type, not one more table.

An issued document is immutable: number, date, parties, lines and totals are frozen at issue,
and `content_sha256` seals them. Only tracking fields move afterwards: the PDF once rendered,
the OTR certification, the WhatsApp delivery, and an invoice's credited total. An invoice is
corrected by credit notes only, never edited or deleted.
"""

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from woassi.modules.billing.domain.errors import CreditExceedsInvoice, InvalidCreditNote
from woassi.modules.billing.domain.events import CreditNoteIssued, InvoiceIssued
from woassi.modules.billing.domain.taxes import (
    RATES_BP,
    TaxCode,
    TaxTotal,
    split_inclusive,
    tax_code_for,
    tax_totals,
)
from woassi.shared.domain.events import Entity

CURRENCY = "XOF"
PRICE_BASIS = "TTC"  # unit prices include the tax, as in the catalog
CREDIT_NOTE_PREFIX = "AV"
MAX_REASON = 500


class DocumentType(StrEnum):
    """What a document is. Each type has its own gapless number series per shop."""

    INVOICE = "INVOICE"
    CREDIT_NOTE = "CREDIT_NOTE"


class DocumentStatus(StrEnum):
    """Life of the document itself. An invoice is never cancelled: it is credited."""

    ISSUED = "ISSUED"
    PARTIALLY_CREDITED = "PARTIALLY_CREDITED"
    CREDITED = "CREDITED"


class PaymentStatus(StrEnum):
    """Invoices are only issued once paid, but the field stays separate from the document's."""

    PAID = "PAID"


class CertificationStatus(StrEnum):
    """OTR certification (certified e-invoice, art. 62 LPF), independent of the other statuses.

    NOT_REQUIRED today. A shop in scope will get PENDING at issue, then SUBMITTED, and CERTIFIED
    or REJECTED; FAILED_RETRYABLE when the OTR could not be reached.
    """

    NOT_REQUIRED = "NOT_REQUIRED"
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    CERTIFIED = "CERTIFIED"
    REJECTED = "REJECTED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"


RELEASABLE = frozenset({CertificationStatus.NOT_REQUIRED, CertificationStatus.CERTIFIED})


class DeliveryStatus(StrEnum):
    """Whether the document went out on WhatsApp. SENT means Meta accepted the message."""

    PENDING = "PENDING"
    SENT = "SENT"
    FAILED = "FAILED"


class BuyerType(StrEnum):
    """A business buyer with a NIF is what puts a sale in the scope of the certified e-invoice."""

    INDIVIDUAL = "INDIVIDUAL"
    BUSINESS = "BUSINESS"


def document_number(prefix: str, year: int, sequence: int) -> str:
    """`FAC-2026-000001`. The year is only a prefix: the sequence never restarts."""
    return f"{prefix}-{year}-{sequence:06d}"


def pdf_key(shop_id: UUID, document_type: DocumentType, number: str) -> str:
    """Object key of the PDF, in the private documents bucket."""
    folder = "invoices" if document_type is DocumentType.INVOICE else "credit-notes"
    return f"shops/{shop_id}/{folder}/{number}.pdf"


@dataclass(frozen=True, slots=True)
class Seller:
    """The shop's legal details, as printed at issue. Empty strings when unknown."""

    name: str
    legal_name: str = ""
    address: str = ""
    phone: str = ""
    tax_id: str = ""  # NIF: optional, small informal merchants have none
    trade_register: str = ""  # RCCM
    tax_regime: str | None = None  # TPU or REAL
    vat_registered: bool = False


@dataclass(frozen=True, slots=True)
class Buyer:
    """The customer as printed at issue: a display name and phone, never their BSUID."""

    customer_id: UUID | None = None
    name: str = field(default="", repr=False)
    phone: str | None = field(default=None, repr=False)
    buyer_type: BuyerType = BuyerType.INDIVIDUAL
    tax_id: str = ""


@dataclass(frozen=True, slots=True)
class PaymentInfo:
    """The payment an invoice records. Woassi never holds the money: this is a fact, dated."""

    payment_id: UUID
    method: str  # payments' method, e.g. kprimepay or manual
    amount: int
    paid_at: datetime


@dataclass(frozen=True, slots=True)
class Item:
    """What is sold, before taxes are worked out: an order line."""

    name: str
    quantity: int
    unit_price: int  # tax-inclusive
    product_id: UUID | None = None
    retailer_id: str | None = None


@dataclass(frozen=True, slots=True)
class DocumentLine:
    """One line, with its taxes. `source_position` is the invoice line a credit note reverses."""

    position: int
    name: str
    quantity: int
    unit_price: int
    tax_code: TaxCode
    tax_rate_bp: int
    amount_excl_tax: int
    tax_amount: int
    total: int
    product_id: UUID | None = None
    retailer_id: str | None = None
    source_position: int | None = None

    def __post_init__(self) -> None:
        values = (self.quantity, self.unit_price, self.amount_excl_tax, self.tax_amount)
        if min(values) < 0 or self.total != self.amount_excl_tax + self.tax_amount:
            raise ValueError("A line has non-negative amounts that add up.")


def _line(position: int, item: Item, tax_code: TaxCode) -> DocumentLine:
    if item.quantity < 1 or item.unit_price < 0:
        raise ValueError("A line needs a positive quantity and a non-negative price.")
    rate = RATES_BP[tax_code]
    total = item.unit_price * item.quantity
    before_tax, tax = split_inclusive(total, rate)
    return DocumentLine(
        position=position,
        name=item.name,
        quantity=item.quantity,
        unit_price=item.unit_price,
        tax_code=tax_code,
        tax_rate_bp=rate,
        amount_excl_tax=before_tax,
        tax_amount=tax,
        total=total,
        product_id=item.product_id,
        retailer_id=item.retailer_id,
    )


@dataclass(frozen=True, slots=True)
class Certification:
    """What the OTR returned. Every field stays None until a shop is in scope."""

    status: CertificationStatus = CertificationStatus.NOT_REQUIRED
    certified_number: str | None = None
    signature: str | None = None  # the OTR's signature or digital fingerprint
    qr_payload: str | None = None  # the raw QR content, not only an image
    certified_at: datetime | None = None
    request_payload: dict[str, Any] | None = field(default=None, repr=False)
    response_payload: dict[str, Any] | None = field(default=None, repr=False)
    attempts: int = 0
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class CreditedLine:
    """Part of an invoice line to reverse."""

    position: int
    quantity: int


@dataclass(frozen=True, slots=True)
class CreditRequest:
    """Exactly one of: the whole invoice (what remains of it), some lines, or an amount.

    It documents a refund the merchant made by hand: Woassi refunds nothing.
    """

    reason: str
    full: bool = False
    lines: tuple[CreditedLine, ...] = ()
    amount: int | None = None
    refund_reference: str | None = None

    def __post_init__(self) -> None:
        chosen = [self.full, bool(self.lines), self.amount is not None]
        if sum(chosen) != 1:
            raise InvalidCreditNote("Credit the whole invoice, some lines, or an amount.")
        if not self.reason.strip() or len(self.reason) > MAX_REASON:
            raise InvalidCreditNote(f"A reason of 1 to {MAX_REASON} characters is required.")
        if self.amount is not None and self.amount < 1:
            raise InvalidCreditNote("A credited amount must be positive.")
        if any(line.quantity < 1 for line in self.lines):
            raise InvalidCreditNote("A credited quantity must be positive.")
        if len({line.position for line in self.lines}) != len(self.lines):
            raise InvalidCreditNote("Each line can be credited once per credit note.")


@dataclass(slots=True)
class BillingDocument(Entity):
    """An invoice or a credit note of a shop. See the module docstring for what may change."""

    id: UUID
    shop_id: UUID
    document_type: DocumentType
    number: str
    sequence: int
    issued_at: datetime
    language: str
    seller: Seller
    buyer: Buyer
    lines: tuple[DocumentLine, ...]
    order_id: UUID | None = None
    order_number: int | None = None
    payment: PaymentInfo | None = None
    payment_status: PaymentStatus | None = None
    # Credit notes: the invoice they reverse, and why.
    original_document_id: UUID | None = None
    original_number: str | None = None
    original_certified_number: str | None = None
    reason: str | None = field(default=None, repr=False)
    refund_reference: str | None = field(default=None, repr=False)
    issued_by: UUID | None = None  # the merchant account; None when issued by the system
    currency: str = CURRENCY
    price_basis: str = PRICE_BASIS
    content_sha256: str = ""
    # Tracking fields, which may move after issue.
    credited_total: int = 0
    pdf_key: str | None = None
    pdf_sha256: str | None = None
    certification: Certification = field(default_factory=Certification)
    delivery_status: DeliveryStatus = DeliveryStatus.PENDING
    delivery_message_id: str | None = None
    delivered_at: datetime | None = None
    updated_at: datetime | None = None

    @classmethod
    def issue_invoice(
        cls,
        *,
        shop_id: UUID,
        number: str,
        sequence: int,
        now: datetime,
        language: str,
        seller: Seller,
        buyer: Buyer,
        order_id: UUID,
        order_number: int,
        items: Sequence[Item],
        payment: PaymentInfo,
        certification_required: bool = False,
    ) -> "BillingDocument":
        """The invoice of a paid order. Its total must be the amount paid."""
        if not items:
            raise ValueError("An invoice needs at least one line.")
        code = tax_code_for(vat_registered=seller.vat_registered)
        lines = tuple(_line(position, item, code) for position, item in enumerate(items, 1))
        document = cls(
            id=uuid4(),
            shop_id=shop_id,
            document_type=DocumentType.INVOICE,
            number=number,
            sequence=sequence,
            issued_at=now,
            language=language,
            seller=seller,
            buyer=buyer,
            lines=lines,
            order_id=order_id,
            order_number=order_number,
            payment=payment,
            payment_status=PaymentStatus.PAID,
            certification=Certification(
                status=CertificationStatus.PENDING
                if certification_required
                else CertificationStatus.NOT_REQUIRED
            ),
            updated_at=now,
        )
        if document.total <= 0 or document.total != payment.amount:
            raise ValueError("An invoice's total is the amount paid, and positive.")
        document.content_sha256 = content_hash(document)
        document.record_event(
            InvoiceIssued(
                document_id=document.id,
                shop_id=shop_id,
                order_id=order_id,
                total=document.total,
            )
        )
        return document

    def credit(
        self,
        request: CreditRequest,
        *,
        previous: Sequence["BillingDocument"],
        number: str,
        sequence: int,
        now: datetime,
        issued_by: UUID | None,
        amount_label: str,
        certification_required: bool = False,
    ) -> "BillingDocument":
        """A credit note reversing part or all of this invoice, given its `previous` ones.

        Raises InvalidCreditNote or CreditExceedsInvoice. `amount_label` names the line of a
        credit by amount. Also adds the note's total to `credited_total`.
        """
        if self.document_type is not DocumentType.INVOICE:
            raise InvalidCreditNote("Only an invoice can be credited.")
        lines = _credit_lines(self, previous, request, amount_label)
        note = BillingDocument(
            id=uuid4(),
            shop_id=self.shop_id,
            document_type=DocumentType.CREDIT_NOTE,
            number=number,
            sequence=sequence,
            issued_at=now,
            language=self.language,
            seller=self.seller,
            buyer=self.buyer,
            lines=lines,
            order_id=self.order_id,
            order_number=self.order_number,
            original_document_id=self.id,
            original_number=self.number,
            original_certified_number=self.certification.certified_number,
            reason=request.reason.strip(),
            refund_reference=request.refund_reference,
            issued_by=issued_by,
            certification=Certification(
                status=CertificationStatus.PENDING
                if certification_required
                else CertificationStatus.NOT_REQUIRED
            ),
            updated_at=now,
        )
        note.content_sha256 = content_hash(note)
        if self.credited_total + note.total > self.total:
            raise CreditExceedsInvoice("Credit notes cannot add up to more than the invoice.")
        self.credited_total += note.total
        self.updated_at = now
        note.record_event(
            CreditNoteIssued(
                document_id=note.id, shop_id=self.shop_id, invoice_id=self.id, total=note.total
            )
        )
        return note

    # Totals, always worked out from the frozen lines.

    @property
    def tax_totals(self) -> tuple[TaxTotal, ...]:
        """One total per tax code."""
        return tax_totals(self.lines)

    @property
    def total_excl_tax(self) -> int:
        return sum(line.amount_excl_tax for line in self.lines)

    @property
    def total_tax(self) -> int:
        return sum(line.tax_amount for line in self.lines)

    @property
    def total(self) -> int:
        """Tax-inclusive total: what was paid, or what is refunded."""
        return sum(line.total for line in self.lines)

    @property
    def status(self) -> DocumentStatus:
        """ISSUED, or how far credit notes reverse this invoice."""
        if self.credited_total == 0:
            return DocumentStatus.ISSUED
        if self.credited_total >= self.total:
            return DocumentStatus.CREDITED
        return DocumentStatus.PARTIALLY_CREDITED

    @property
    def releasable(self) -> bool:
        """True if the document may be rendered and given out: certified, or no need to."""
        return self.certification.status in RELEASABLE

    @property
    def expected_pdf_key(self) -> str:
        return pdf_key(self.shop_id, self.document_type, self.number)

    # Tracking.

    def attach_pdf(self, key: str, sha256: str, now: datetime) -> bool:
        """Records the rendered PDF, once: False if one is already attached."""
        if self.pdf_key is not None:
            return False
        self.pdf_key = key
        self.pdf_sha256 = sha256
        self.updated_at = now
        return True

    def certification_changed(self, certification: Certification, now: datetime) -> None:
        """Applies what the certifier answered."""
        self.certification = certification
        self.updated_at = now

    def sent(self, message_id: str, now: datetime) -> None:
        """Meta accepted the WhatsApp message carrying the document or its link."""
        self.delivery_status = DeliveryStatus.SENT
        self.delivery_message_id = message_id
        self.delivered_at = now
        self.updated_at = now

    def not_sent(self, now: datetime) -> None:
        """No message could go out. Never undoes an earlier successful delivery."""
        if self.delivery_status is not DeliveryStatus.SENT:
            self.delivery_status = DeliveryStatus.FAILED
        self.updated_at = now


def canonical_content(document: BillingDocument) -> dict[str, Any]:
    """What was issued, without the tracking fields: the input of `content_sha256`."""

    def lines() -> list[dict[str, Any]]:
        return [
            {
                "position": line.position,
                "name": line.name,
                "quantity": line.quantity,
                "unit_price": line.unit_price,
                "tax_code": line.tax_code.value,
                "tax_rate_bp": line.tax_rate_bp,
                "amount_excl_tax": line.amount_excl_tax,
                "tax_amount": line.tax_amount,
                "total": line.total,
                "product_id": str(line.product_id) if line.product_id else None,
                "retailer_id": line.retailer_id,
                "source_position": line.source_position,
            }
            for line in document.lines
        ]

    seller, buyer, payment = document.seller, document.buyer, document.payment
    return {
        "id": str(document.id),
        "shop_id": str(document.shop_id),
        "document_type": document.document_type.value,
        "number": document.number,
        "sequence": document.sequence,
        "issued_at": document.issued_at.isoformat(),
        "currency": document.currency,
        "price_basis": document.price_basis,
        "seller": {
            "name": seller.name,
            "legal_name": seller.legal_name,
            "address": seller.address,
            "phone": seller.phone,
            "tax_id": seller.tax_id,
            "trade_register": seller.trade_register,
            "tax_regime": seller.tax_regime,
            "vat_registered": seller.vat_registered,
        },
        "buyer": {
            "customer_id": str(buyer.customer_id) if buyer.customer_id else None,
            "name": buyer.name,
            "phone": buyer.phone,
            "buyer_type": buyer.buyer_type.value,
            "tax_id": buyer.tax_id,
        },
        "lines": lines(),
        "tax_totals": [
            {
                "tax_code": total.tax_code.value,
                "rate_bp": total.rate_bp,
                "amount_excl_tax": total.amount_excl_tax,
                "tax_amount": total.tax_amount,
                "total": total.total,
            }
            for total in document.tax_totals
        ],
        "total_excl_tax": document.total_excl_tax,
        "total_tax": document.total_tax,
        "total": document.total,
        "order_id": str(document.order_id) if document.order_id else None,
        "order_number": document.order_number,
        "payment": {
            "payment_id": str(payment.payment_id),
            "method": payment.method,
            "amount": payment.amount,
            "paid_at": payment.paid_at.isoformat(),
        }
        if payment
        else None,
        "payment_status": document.payment_status.value if document.payment_status else None,
        "original_document_id": (
            str(document.original_document_id) if document.original_document_id else None
        ),
        "original_number": document.original_number,
        "original_certified_number": document.original_certified_number,
        "reason": document.reason,
        "refund_reference": document.refund_reference,
    }


def content_hash(document: BillingDocument) -> str:
    """SHA-256 (hex) of the canonical JSON of what was issued: sorted keys, no spaces, UTF-8."""
    encoded = json.dumps(
        canonical_content(document), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


# Credit notes


@dataclass(slots=True)
class _Remaining:
    """What is left to credit under one tax code."""

    rate_bp: int
    total: int
    excl_tax: int
    tax: int

    def take(self, total: int) -> tuple[int, int]:
        """(before tax, tax) of `total` taken from what is left, never more than is left.

        Taking all that is left takes its exact split, so that credit notes never reverse a
        franc of VAT more than the invoice charged, whatever the rounding of each line.
        """
        if total > self.total:
            raise CreditExceedsInvoice("Credit notes cannot add up to more than the invoice.")
        if total == self.total:
            excl_tax, tax = self.excl_tax, self.tax
        else:
            excl_tax, tax = split_inclusive(total, self.rate_bp)
            if tax > self.tax:
                tax = self.tax
                excl_tax = total - tax
            if excl_tax > self.excl_tax:
                excl_tax = self.excl_tax
                tax = total - excl_tax
        self.total -= total
        self.excl_tax -= excl_tax
        self.tax -= tax
        return excl_tax, tax


def _remaining(
    invoice: BillingDocument, previous: Sequence[BillingDocument]
) -> dict[TaxCode, _Remaining]:
    left = {
        total.tax_code: _Remaining(
            total.rate_bp, total.total, total.amount_excl_tax, total.tax_amount
        )
        for total in invoice.tax_totals
    }
    for note in previous:
        for line in note.lines:
            group = left.get(line.tax_code)
            if group is None:
                raise CreditExceedsInvoice("A credit note does not match its invoice.")
            group.total -= line.total
            group.excl_tax -= line.amount_excl_tax
            group.tax -= line.tax_amount
    return left


def _credit_lines(
    invoice: BillingDocument,
    previous: Sequence[BillingDocument],
    request: CreditRequest,
    amount_label: str,
) -> tuple[DocumentLine, ...]:
    left = _remaining(invoice, previous)
    credited_quantity: dict[int, int] = {}
    for note in previous:
        for line in note.lines:
            if line.source_position is not None:
                credited_quantity[line.source_position] = (
                    credited_quantity.get(line.source_position, 0) + line.quantity
                )
    by_position = {line.position: line for line in invoice.lines}

    def reverse(source: DocumentLine, quantity: int, position: int) -> DocumentLine:
        available = source.quantity - credited_quantity.get(source.position, 0)
        if quantity > available:
            raise CreditExceedsInvoice(f"Line {source.position} has {available} left to credit.")
        total = source.unit_price * quantity
        excl_tax, tax = left[source.tax_code].take(total)
        return DocumentLine(
            position=position,
            name=source.name,
            quantity=quantity,
            unit_price=source.unit_price,
            tax_code=source.tax_code,
            tax_rate_bp=source.tax_rate_bp,
            amount_excl_tax=excl_tax,
            tax_amount=tax,
            total=total,
            product_id=source.product_id,
            retailer_id=source.retailer_id,
            source_position=source.position,
        )

    lines: list[DocumentLine] = []
    if request.lines:
        for position, wanted in enumerate(request.lines, 1):
            source = by_position.get(wanted.position)
            if source is None:
                raise InvalidCreditNote(f"The invoice has no line {wanted.position}.")
            lines.append(reverse(source, wanted.quantity, position))
    elif request.full and not previous:
        lines = [reverse(source, source.quantity, source.position) for source in invoice.lines]
    else:
        remaining_total = sum(group.total for group in left.values())
        amount = remaining_total if request.full else request.amount
        assert amount is not None
        if remaining_total <= 0:
            raise CreditExceedsInvoice("Nothing is left to credit on this invoice.")
        if amount > remaining_total:
            raise CreditExceedsInvoice(f"Only {remaining_total} XOF is left to credit.")
        for position, (code, share) in enumerate(_shares(left, amount).items(), 1):
            if share == 0:
                continue
            rate = left[code].rate_bp
            excl_tax, tax = left[code].take(share)
            lines.append(
                DocumentLine(
                    position=position,
                    name=amount_label,
                    quantity=1,
                    unit_price=share,
                    tax_code=code,
                    tax_rate_bp=rate,
                    amount_excl_tax=excl_tax,
                    tax_amount=tax,
                    total=share,
                )
            )
    if sum(line.total for line in lines) <= 0:
        raise CreditExceedsInvoice("Nothing is left to credit on this invoice.")
    return tuple(replace(line, position=index) for index, line in enumerate(lines, 1))


def _shares(left: dict[TaxCode, _Remaining], amount: int) -> dict[TaxCode, int]:
    """Splits `amount` over the tax codes in proportion to what is left (largest remainder)."""
    remaining_total = sum(group.total for group in left.values())
    shares: dict[TaxCode, int] = {}
    remainders: list[tuple[int, TaxCode]] = []
    for code, group in left.items():
        exact, rest = divmod(amount * group.total, remaining_total)
        shares[code] = exact
        remainders.append((rest, code))
    missing = amount - sum(shares.values())
    for _, code in sorted(remainders, key=lambda item: item[0], reverse=True)[:missing]:
        shares[code] += 1
    return shares
