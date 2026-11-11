"""Issuing invoices once payments are confirmed, and giving them out."""

from datetime import datetime
from uuid import UUID

import structlog

from woassi.modules.billing.application.ports import (
    BillingUnitOfWork,
    CustomersPort,
    DuplicateInvoice,
    InvoiceCertifier,
    OrdersPort,
    SellerProfile,
    ShopsPort,
)
from woassi.modules.billing.application.use_cases.release import DocumentRelease
from woassi.modules.billing.domain.document import (
    BillingDocument,
    Buyer,
    DeliveryStatus,
    DocumentType,
    Item,
    PaymentInfo,
    Seller,
    document_number,
)
from woassi.modules.billing.domain.errors import InvoiceNotFound, InvoiceNotSent, PdfNotReady
from woassi.shared.application.ports import Clock

log = structlog.get_logger()


def seller_of(profile: SellerProfile) -> Seller:
    """The seller snapshot printed on the shop's documents."""
    return Seller(
        name=profile.name,
        legal_name=profile.legal_name,
        address=profile.address,
        phone=profile.phone,
        tax_id=profile.tax_id,
        trade_register=profile.trade_register,
        tax_regime=profile.tax_regime,
        vat_registered=profile.vat_registered,
    )


class IssueInvoice:
    """`billing.issue_invoice`, after `PaymentConfirmed`: numbers, renders and sends the invoice.

    One invoice per order (unique in the database): a replayed event finds the invoice and only
    finishes what was left undone (PDF, delivery).
    """

    def __init__(
        self,
        uow: BillingUnitOfWork,
        orders: OrdersPort,
        shops: ShopsPort,
        customers: CustomersPort,
        certifier: InvoiceCertifier,
        release: DocumentRelease,
        clock: Clock,
    ) -> None:
        self._uow = uow
        self._orders = orders
        self._shops = shops
        self._customers = customers
        self._certifier = certifier
        self._release = release
        self._clock = clock

    async def execute(
        self,
        *,
        shop_id: UUID,
        order_id: UUID,
        payment_id: UUID,
        method: str,
        amount: int,
        paid_at: datetime,
    ) -> BillingDocument | None:
        """The order's invoice, or None if it cannot be issued (order or shop gone, wrong
        amount), which is logged.
        """
        async with self._uow as uow:
            invoice = await uow.documents.invoice_for_order(shop_id, order_id)
        if invoice is None:
            invoice = await self._issue(
                shop_id=shop_id,
                order_id=order_id,
                payment=PaymentInfo(
                    payment_id=payment_id, method=method, amount=amount, paid_at=paid_at
                ),
            )
            if invoice is None:
                return None
        invoice = await self._release.prepare(invoice)
        if invoice.delivery_status is DeliveryStatus.PENDING:
            await self._release.deliver(invoice)
        return invoice

    async def _issue(
        self, *, shop_id: UUID, order_id: UUID, payment: PaymentInfo
    ) -> BillingDocument | None:
        order = await self._orders.get(shop_id, order_id)
        profile = await self._shops.profile(shop_id)
        if order is None or profile is None or not order.lines:
            log.error("invoice_not_issued", order_id=str(order_id), reason="order_or_shop_gone")
            return None
        if order.total != payment.amount:
            log.error("invoice_not_issued", order_id=str(order_id), reason="amount_mismatch")
            return None
        contact = await self._customers.contact(shop_id, order.customer_id)
        required = await self._certifier.required(shop_id)
        now = self._clock.now()
        year = self._clock.local_now().year

        async with self._uow as uow:
            sequence = await uow.documents.next_sequence(shop_id, DocumentType.INVOICE)
            invoice = BillingDocument.issue_invoice(
                shop_id=shop_id,
                number=document_number(profile.invoice_prefix, year, sequence),
                sequence=sequence,
                now=now,
                language=profile.language,
                seller=seller_of(profile),
                buyer=Buyer(
                    customer_id=order.customer_id,
                    name=contact.name or "",
                    phone=contact.phone,
                ),
                order_id=order_id,
                order_number=order.number,
                items=[
                    Item(
                        name=line.name,
                        quantity=line.quantity,
                        unit_price=line.unit_price,
                        product_id=line.product_id,
                        retailer_id=line.retailer_id,
                    )
                    for line in order.lines
                    if line.quantity > 0
                ],
                payment=payment,
                certification_required=required,
            )
            try:
                await uow.documents.add(invoice)
            except DuplicateInvoice:
                duplicate = True  # a concurrent job won: its number stands, ours rolls back
            else:
                duplicate = False
                await uow.audit.record(
                    account_id=profile.account_id,  # system action, on behalf of the owner
                    action="invoice.issued",
                    target_type="invoice",
                    target_id=str(invoice.id),
                    shop_id=shop_id,
                    details={
                        "number": invoice.number,
                        "order_id": str(order_id),
                        "order_number": order.number,
                        "total": invoice.total,
                        "payment_id": str(payment.payment_id),
                        "content_sha256": invoice.content_sha256,
                        "by": "system",
                    },
                )
                await uow.commit()
        if duplicate:
            async with self._uow as uow:
                return await uow.documents.invoice_for_order(shop_id, order_id)
        log.info("invoice_issued", document_id=str(invoice.id), shop_id=str(shop_id))
        return invoice


class PrepareDocument:
    """`billing.render_credit_note`: certifies if required, then renders a document's PDF."""

    def __init__(self, uow: BillingUnitOfWork, release: DocumentRelease) -> None:
        self._uow = uow
        self._release = release

    async def execute(self, *, shop_id: UUID, document_id: UUID) -> BillingDocument | None:
        async with self._uow as uow:
            document = await uow.documents.get(shop_id, document_id)
        if document is None:
            return None
        return await self._release.prepare(document)


class ResendInvoice:
    """The merchant sends the invoice to the customer again."""

    def __init__(self, uow: BillingUnitOfWork, release: DocumentRelease) -> None:
        self._uow = uow
        self._release = release

    async def execute(self, *, shop_id: UUID, invoice_id: UUID) -> BillingDocument:
        """Raises InvoiceNotFound, PdfNotReady, or InvoiceNotSent if WhatsApp refused it."""
        async with self._uow as uow:
            invoice = await uow.documents.get(shop_id, invoice_id)
        if invoice is None or invoice.document_type is not DocumentType.INVOICE:
            raise InvoiceNotFound(f"Invoice {invoice_id} not found.")
        if invoice.pdf_key is None or not invoice.releasable:
            raise PdfNotReady("The invoice PDF is not ready yet.")
        if not await self._release.deliver(invoice):
            raise InvoiceNotSent("WhatsApp did not take the invoice. Try again later.")
        async with self._uow as uow:
            current = await uow.documents.get(shop_id, invoice_id)
        return current or invoice
