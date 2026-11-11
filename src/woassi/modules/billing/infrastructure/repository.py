"""SQLAlchemy implementation of the billing repository. There is no delete, on purpose."""

from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Select, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from woassi.modules.billing.application.ports import (
    AccessToken,
    DocumentFilter,
    DuplicateInvoice,
    TaxSummaryRow,
)
from woassi.modules.billing.domain.document import (
    BillingDocument,
    Buyer,
    BuyerType,
    Certification,
    CertificationStatus,
    DeliveryStatus,
    DocumentLine,
    DocumentType,
    PaymentInfo,
    PaymentStatus,
    Seller,
)
from woassi.modules.billing.domain.taxes import TaxCode
from woassi.modules.billing.infrastructure.models import (
    BillingAccessTokenModel,
    BillingCounterModel,
    BillingDocumentModel,
    BillingTaxTotalModel,
)
from woassi.shared.domain.events import Entity

_UNIQUE_INVOICE = "uq_billing_documents_invoice_order"


def _uuid(value: Any) -> UUID | None:
    return UUID(value) if value else None


def _line_to_json(line: DocumentLine) -> dict[str, Any]:
    return {
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


def _line_from_json(data: dict[str, Any]) -> DocumentLine:
    return DocumentLine(
        position=data["position"],
        name=data["name"],
        quantity=data["quantity"],
        unit_price=data["unit_price"],
        tax_code=TaxCode(data["tax_code"]),
        tax_rate_bp=data["tax_rate_bp"],
        amount_excl_tax=data["amount_excl_tax"],
        tax_amount=data["tax_amount"],
        total=data["total"],
        product_id=_uuid(data.get("product_id")),
        retailer_id=data.get("retailer_id"),
        source_position=data.get("source_position"),
    )


def _seller_to_json(seller: Seller) -> dict[str, Any]:
    return {
        "name": seller.name,
        "legal_name": seller.legal_name,
        "address": seller.address,
        "phone": seller.phone,
        "tax_id": seller.tax_id,
        "trade_register": seller.trade_register,
        "tax_regime": seller.tax_regime,
        "vat_registered": seller.vat_registered,
    }


def _buyer_to_json(buyer: Buyer) -> dict[str, Any]:
    return {
        "name": buyer.name,
        "phone": buyer.phone,
        "buyer_type": buyer.buyer_type.value,
        "tax_id": buyer.tax_id,
    }


def _to_domain(model: BillingDocumentModel) -> BillingDocument:
    buyer = model.buyer
    payment = None
    if model.payment_id is not None:
        assert model.payment_method is not None and model.paid_at is not None
        payment = PaymentInfo(
            payment_id=model.payment_id,
            method=model.payment_method,
            amount=model.payment_amount or 0,
            paid_at=model.paid_at,
        )
    return BillingDocument(
        id=model.id,
        shop_id=model.shop_id,
        document_type=DocumentType(model.document_type),
        number=model.number,
        sequence=model.sequence,
        issued_at=model.issued_at,
        language=model.language,
        seller=Seller(**model.seller),
        buyer=Buyer(
            customer_id=model.customer_id,
            name=buyer.get("name", ""),
            phone=buyer.get("phone"),
            buyer_type=BuyerType(buyer.get("buyer_type", BuyerType.INDIVIDUAL.value)),
            tax_id=buyer.get("tax_id", ""),
        ),
        lines=tuple(_line_from_json(line) for line in model.lines),
        order_id=model.order_id,
        order_number=model.order_number,
        payment=payment,
        payment_status=PaymentStatus(model.payment_status) if model.payment_status else None,
        original_document_id=model.original_document_id,
        original_number=model.original_number,
        original_certified_number=model.original_certified_number,
        reason=model.reason,
        refund_reference=model.refund_reference,
        issued_by=model.issued_by,
        currency=model.currency,
        price_basis=model.price_basis,
        content_sha256=model.content_sha256,
        credited_total=model.credited_total,
        pdf_key=model.pdf_key,
        pdf_sha256=model.pdf_sha256,
        certification=Certification(
            status=CertificationStatus(model.certification_status),
            certified_number=model.otr_certified_number,
            signature=model.otr_signature,
            qr_payload=model.otr_qr_payload,
            certified_at=model.otr_certified_at,
            request_payload=model.otr_request,
            response_payload=model.otr_response,
            attempts=model.certification_attempts,
            error_code=model.certification_error,
        ),
        delivery_status=DeliveryStatus(model.delivery_status),
        delivery_message_id=model.delivery_message_id,
        delivered_at=model.delivered_at,
        updated_at=model.updated_at,
    )


def _new_model(document: BillingDocument) -> BillingDocumentModel:
    """Every column, issued content included: only ever called once per document."""
    payment = document.payment
    model = BillingDocumentModel(
        id=document.id,
        shop_id=document.shop_id,
        document_type=document.document_type.value,
        number=document.number,
        sequence=document.sequence,
        issued_at=document.issued_at,
        language=document.language,
        currency=document.currency,
        price_basis=document.price_basis,
        seller=_seller_to_json(document.seller),
        buyer=_buyer_to_json(document.buyer),
        lines=[_line_to_json(line) for line in document.lines],
        total_excl_tax=document.total_excl_tax,
        total_tax=document.total_tax,
        total=document.total,
        customer_id=document.buyer.customer_id,
        order_id=document.order_id,
        order_number=document.order_number,
        payment_status=document.payment_status.value if document.payment_status else None,
        payment_id=payment.payment_id if payment else None,
        payment_method=payment.method if payment else None,
        payment_amount=payment.amount if payment else None,
        paid_at=payment.paid_at if payment else None,
        original_document_id=document.original_document_id,
        original_number=document.original_number,
        original_certified_number=document.original_certified_number,
        reason=document.reason,
        refund_reference=document.refund_reference,
        issued_by=document.issued_by,
        content_sha256=document.content_sha256,
    )
    return _copy_tracking(document, model)


def _copy_tracking(document: BillingDocument, model: BillingDocumentModel) -> BillingDocumentModel:
    """The fields that may change after issue. Issued content is never written again."""
    certification = document.certification
    model.credited_total = document.credited_total
    model.pdf_key = document.pdf_key
    model.pdf_sha256 = document.pdf_sha256
    model.certification_status = certification.status.value
    model.otr_certified_number = certification.certified_number
    model.otr_signature = certification.signature
    model.otr_qr_payload = certification.qr_payload
    model.otr_certified_at = certification.certified_at
    model.otr_request = certification.request_payload
    model.otr_response = certification.response_payload
    model.certification_attempts = certification.attempts
    model.certification_error = certification.error_code
    model.delivery_status = document.delivery_status.value
    model.delivery_message_id = document.delivery_message_id
    model.delivered_at = document.delivered_at
    model.updated_at = document.updated_at or document.issued_at
    return model


def _filtered(query: Select[Any], shop_id: UUID, filters: DocumentFilter) -> Select[Any]:
    query = query.where(
        BillingDocumentModel.shop_id == shop_id,
        BillingDocumentModel.document_type == filters.document_type.value,
    )
    if filters.order_id is not None:
        query = query.where(BillingDocumentModel.order_id == filters.order_id)
    if filters.invoice_id is not None:
        query = query.where(BillingDocumentModel.original_document_id == filters.invoice_id)
    if filters.issued_from is not None:
        query = query.where(BillingDocumentModel.issued_at >= filters.issued_from)
    if filters.issued_before is not None:
        query = query.where(BillingDocumentModel.issued_at < filters.issued_before)
    return query


class SqlAlchemyBillingDocuments:
    """`BillingDocumentRepository` on SQLAlchemy. Saved documents are tracked for events."""

    def __init__(self, session: AsyncSession, track: Callable[[Entity], None]) -> None:
        self._session = session
        self._track = track

    async def next_sequence(self, shop_id: UUID, document_type: DocumentType) -> int:
        result = await self._session.execute(
            insert(BillingCounterModel)
            .values(shop_id=shop_id, document_type=document_type.value, last_sequence=1)
            .on_conflict_do_update(
                index_elements=[BillingCounterModel.shop_id, BillingCounterModel.document_type],
                set_={"last_sequence": BillingCounterModel.last_sequence + 1},
            )
            .returning(BillingCounterModel.last_sequence)
        )
        return int(result.scalar_one())

    async def add(self, document: BillingDocument) -> None:
        self._session.add(_new_model(document))
        try:
            await self._session.flush()
        except IntegrityError as error:
            if _UNIQUE_INVOICE in str(error.orig):
                raise DuplicateInvoice(str(document.order_id)) from None
            raise
        for total in document.tax_totals:
            self._session.add(
                BillingTaxTotalModel(
                    document_id=document.id,
                    tax_code=total.tax_code.value,
                    rate_bp=total.rate_bp,
                    amount_excl_tax=total.amount_excl_tax,
                    tax_amount=total.tax_amount,
                    total=total.total,
                )
            )
        await self._session.flush()
        self._track(document)

    async def get(
        self, shop_id: UUID, document_id: UUID, *, lock: bool = False
    ) -> BillingDocument | None:
        query = select(BillingDocumentModel).where(
            BillingDocumentModel.id == document_id, BillingDocumentModel.shop_id == shop_id
        )
        model = await self._session.scalar(query.with_for_update() if lock else query)
        return _to_domain(model) if model else None

    async def invoice_for_order(self, shop_id: UUID, order_id: UUID) -> BillingDocument | None:
        model = await self._session.scalar(
            select(BillingDocumentModel).where(
                BillingDocumentModel.shop_id == shop_id,
                BillingDocumentModel.order_id == order_id,
                BillingDocumentModel.document_type == DocumentType.INVOICE.value,
            )
        )
        return _to_domain(model) if model else None

    async def credit_notes_of(self, shop_id: UUID, invoice_id: UUID) -> Sequence[BillingDocument]:
        models = await self._session.scalars(
            select(BillingDocumentModel)
            .where(
                BillingDocumentModel.shop_id == shop_id,
                BillingDocumentModel.original_document_id == invoice_id,
            )
            .order_by(BillingDocumentModel.sequence)
        )
        return [_to_domain(model) for model in models]

    async def list(
        self, shop_id: UUID, filters: DocumentFilter, offset: int, limit: int
    ) -> Sequence[BillingDocument]:
        models = await self._session.scalars(
            _filtered(select(BillingDocumentModel), shop_id, filters)
            .order_by(BillingDocumentModel.issued_at.desc(), BillingDocumentModel.sequence.desc())
            .offset(offset)
            .limit(limit)
        )
        return [_to_domain(model) for model in models]

    async def count(self, shop_id: UUID, filters: DocumentFilter) -> int:
        query = _filtered(select(func.count()).select_from(BillingDocumentModel), shop_id, filters)
        return (await self._session.scalar(query)) or 0

    async def save(self, document: BillingDocument) -> None:
        """Updates the tracking fields; raises LookupError if the document does not exist."""
        model = await self._session.get(BillingDocumentModel, document.id)
        if model is None:
            raise LookupError(f"Document {document.id} does not exist.")
        _copy_tracking(document, model)
        await self._session.flush()
        self._track(document)

    async def add_access_token(self, token: AccessToken) -> None:
        self._session.add(
            BillingAccessTokenModel(
                token_hash=token.token_hash,
                document_id=token.document_id,
                shop_id=token.shop_id,
                created_at=token.created_at,
                expires_at=token.expires_at,
            )
        )
        await self._session.flush()

    async def find_access_token(self, token_hash: str) -> AccessToken | None:
        model = await self._session.get(BillingAccessTokenModel, token_hash)
        if model is None:
            return None
        return AccessToken(
            token_hash=model.token_hash,
            document_id=model.document_id,
            shop_id=model.shop_id,
            created_at=model.created_at,
            expires_at=model.expires_at,
        )

    async def tax_summary(
        self, shop_id: UUID, issued_from: datetime, issued_before: datetime
    ) -> Sequence[TaxSummaryRow]:
        totals = BillingTaxTotalModel
        rows = await self._session.execute(
            select(
                BillingDocumentModel.document_type,
                totals.tax_code,
                totals.rate_bp,
                func.sum(totals.amount_excl_tax).label("amount_excl_tax"),
                func.sum(totals.tax_amount).label("tax_amount"),
                func.sum(totals.total).label("total"),
            )
            .join(BillingDocumentModel, BillingDocumentModel.id == totals.document_id)
            .where(
                BillingDocumentModel.shop_id == shop_id,
                BillingDocumentModel.issued_at >= issued_from,
                BillingDocumentModel.issued_at < issued_before,
            )
            .group_by(BillingDocumentModel.document_type, totals.tax_code, totals.rate_bp)
            .order_by(BillingDocumentModel.document_type, totals.tax_code, totals.rate_bp)
        )
        return [
            TaxSummaryRow(
                document_type=DocumentType(row.document_type),
                tax_code=TaxCode(row.tax_code),
                rate_bp=row.rate_bp,
                amount_excl_tax=int(row.amount_excl_tax),
                tax_amount=int(row.tax_amount),
                total=int(row.total),
            )
            for row in rows
        ]
