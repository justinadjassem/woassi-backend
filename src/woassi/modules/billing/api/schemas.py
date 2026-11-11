"""Request and response bodies of the billing api. Amounts in XOF, integers."""

from datetime import date, datetime
from typing import Self
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from woassi.modules.billing.application.use_cases.queries import (
    BillingSummary,
    PdfLink,
    TaxLine,
    Totals,
)
from woassi.modules.billing.domain.document import (
    MAX_REASON,
    BillingDocument,
    BuyerType,
    CertificationStatus,
    CreditedLine,
    CreditRequest,
    DeliveryStatus,
    DocumentStatus,
    PaymentStatus,
)
from woassi.modules.billing.domain.taxes import TaxCode


class LineResponse(BaseModel):
    position: int
    name: str
    quantity: int
    unit_price: int
    tax_code: TaxCode
    tax_rate_bp: int
    amount_excl_tax: int
    tax_amount: int
    total: int
    product_id: UUID | None
    source_position: int | None


class TaxTotalResponse(BaseModel):
    tax_code: TaxCode
    rate_bp: int
    amount_excl_tax: int
    tax_amount: int
    total: int


class SellerResponse(BaseModel):
    name: str
    legal_name: str
    address: str
    phone: str
    tax_id: str
    trade_register: str
    tax_regime: str | None
    vat_registered: bool


class BuyerResponse(BaseModel):
    customer_id: UUID | None
    name: str
    phone: str | None
    buyer_type: BuyerType
    tax_id: str


class PaymentResponse(BaseModel):
    payment_id: UUID
    method: str
    amount: int
    paid_at: datetime


class CertificationResponse(BaseModel):
    status: CertificationStatus
    certified_number: str | None
    qr_payload: str | None
    certified_at: datetime | None


class DocumentResponse(BaseModel):
    """An invoice or a credit note, as issued, with its tracking state."""

    id: UUID
    document_type: str
    number: str
    status: DocumentStatus
    issued_at: datetime
    language: str
    currency: str
    price_basis: str
    seller: SellerResponse
    buyer: BuyerResponse
    lines: list[LineResponse]
    tax_totals: list[TaxTotalResponse]
    total_excl_tax: int
    total_tax: int
    total: int
    credited_total: int
    order_id: UUID | None
    order_number: int | None
    payment_status: PaymentStatus | None
    payment: PaymentResponse | None
    original_document_id: UUID | None
    original_number: str | None
    reason: str | None
    refund_reference: str | None
    issued_by: UUID | None
    content_sha256: str
    pdf_ready: bool
    pdf_sha256: str | None
    certification: CertificationResponse
    delivery_status: DeliveryStatus
    delivered_at: datetime | None

    @classmethod
    def from_document(cls, document: BillingDocument) -> "DocumentResponse":
        seller, buyer, payment = document.seller, document.buyer, document.payment
        return cls(
            id=document.id,
            document_type=document.document_type.value,
            number=document.number,
            status=document.status,
            issued_at=document.issued_at,
            language=document.language,
            currency=document.currency,
            price_basis=document.price_basis,
            seller=SellerResponse(
                name=seller.name,
                legal_name=seller.legal_name,
                address=seller.address,
                phone=seller.phone,
                tax_id=seller.tax_id,
                trade_register=seller.trade_register,
                tax_regime=seller.tax_regime,
                vat_registered=seller.vat_registered,
            ),
            buyer=BuyerResponse(
                customer_id=buyer.customer_id,
                name=buyer.name,
                phone=buyer.phone,
                buyer_type=buyer.buyer_type,
                tax_id=buyer.tax_id,
            ),
            lines=[
                LineResponse(
                    position=line.position,
                    name=line.name,
                    quantity=line.quantity,
                    unit_price=line.unit_price,
                    tax_code=line.tax_code,
                    tax_rate_bp=line.tax_rate_bp,
                    amount_excl_tax=line.amount_excl_tax,
                    tax_amount=line.tax_amount,
                    total=line.total,
                    product_id=line.product_id,
                    source_position=line.source_position,
                )
                for line in document.lines
            ],
            tax_totals=[
                TaxTotalResponse(
                    tax_code=total.tax_code,
                    rate_bp=total.rate_bp,
                    amount_excl_tax=total.amount_excl_tax,
                    tax_amount=total.tax_amount,
                    total=total.total,
                )
                for total in document.tax_totals
            ],
            total_excl_tax=document.total_excl_tax,
            total_tax=document.total_tax,
            total=document.total,
            credited_total=document.credited_total,
            order_id=document.order_id,
            order_number=document.order_number,
            payment_status=document.payment_status,
            payment=PaymentResponse(
                payment_id=payment.payment_id,
                method=payment.method,
                amount=payment.amount,
                paid_at=payment.paid_at,
            )
            if payment
            else None,
            original_document_id=document.original_document_id,
            original_number=document.original_number,
            reason=document.reason,
            refund_reference=document.refund_reference,
            issued_by=document.issued_by,
            content_sha256=document.content_sha256,
            pdf_ready=document.pdf_key is not None,
            pdf_sha256=document.pdf_sha256,
            certification=CertificationResponse(
                status=document.certification.status,
                certified_number=document.certification.certified_number,
                qr_payload=document.certification.qr_payload,
                certified_at=document.certification.certified_at,
            ),
            delivery_status=document.delivery_status,
            delivered_at=document.delivered_at,
        )


class CreditedLineBody(BaseModel):
    position: int = Field(ge=1, description="Position of the invoice line")
    quantity: int = Field(ge=1)


class CreditNoteBody(BaseModel):
    """Exactly one of `full`, `lines` or `amount`. Documents a refund made by hand."""

    reason: str = Field(min_length=1, max_length=MAX_REASON)
    full: bool = False
    lines: list[CreditedLineBody] = Field(default_factory=list, max_length=200)
    amount: int | None = Field(default=None, ge=1, le=1_000_000_000_000)
    refund_reference: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def _one_kind(self) -> Self:
        if sum([self.full, bool(self.lines), self.amount is not None]) != 1:
            raise ValueError("Give exactly one of full, lines or amount.")
        return self

    def to_request(self) -> CreditRequest:
        return CreditRequest(
            reason=self.reason,
            full=self.full,
            lines=tuple(CreditedLine(line.position, line.quantity) for line in self.lines),
            amount=self.amount,
            refund_reference=self.refund_reference,
        )


class PdfLinkResponse(BaseModel):
    """A presigned download URL of the PDF, valid `expires_in_s` seconds."""

    url: str
    expires_in_s: int

    @classmethod
    def from_link(cls, link: PdfLink) -> "PdfLinkResponse":
        return cls(url=link.url, expires_in_s=link.expires_in_s)


class TaxLineResponse(BaseModel):
    tax_code: TaxCode
    rate_bp: int
    amount_excl_tax: int
    tax_amount: int
    total: int

    @classmethod
    def from_line(cls, line: TaxLine) -> "TaxLineResponse":
        return cls(
            tax_code=line.tax_code,
            rate_bp=line.rate_bp,
            amount_excl_tax=line.amount_excl_tax,
            tax_amount=line.tax_amount,
            total=line.total,
        )


class TotalsResponse(BaseModel):
    documents: int
    amount_excl_tax: int
    tax_amount: int
    total: int
    by_tax: list[TaxLineResponse]

    @classmethod
    def from_totals(cls, totals: Totals) -> "TotalsResponse":
        return cls(
            documents=totals.documents,
            amount_excl_tax=totals.amount_excl_tax,
            tax_amount=totals.tax_amount,
            total=totals.total,
            by_tax=[TaxLineResponse.from_line(line) for line in totals.by_tax],
        )


class BillingSummaryResponse(BaseModel):
    """Invoiced, credited and net amounts over local days `issued_from` to `issued_to`."""

    issued_from: date
    issued_to: date
    invoices: TotalsResponse
    credit_notes: TotalsResponse
    net: TotalsResponse

    @classmethod
    def from_summary(cls, summary: BillingSummary) -> "BillingSummaryResponse":
        return cls(
            issued_from=summary.issued_from,
            issued_to=summary.issued_to,
            invoices=TotalsResponse.from_totals(summary.invoices),
            credit_notes=TotalsResponse.from_totals(summary.credit_notes),
            net=TotalsResponse.from_totals(summary.net),
        )
