"""Credit notes: the only way to correct an issued invoice.

KPrimePay has no refund API: a credit note documents a refund the merchant made by hand.
"""

from uuid import UUID

from woassi.modules.billing.application.ports import BillingUnitOfWork, InvoiceCertifier
from woassi.modules.billing.application.texts import text
from woassi.modules.billing.domain.document import (
    CREDIT_NOTE_PREFIX,
    BillingDocument,
    CreditRequest,
    DocumentType,
    document_number,
)
from woassi.modules.billing.domain.errors import InvoiceNotFound
from woassi.shared.application.ports import Clock


class IssueCreditNote:
    """Issues a credit note on an invoice, audited. The worker renders its PDF afterwards."""

    def __init__(self, uow: BillingUnitOfWork, certifier: InvoiceCertifier, clock: Clock) -> None:
        self._uow = uow
        self._certifier = certifier
        self._clock = clock

    async def execute(
        self,
        *,
        shop_id: UUID,
        invoice_id: UUID,
        account_id: UUID,
        request: CreditRequest,
        ip: str | None = None,
    ) -> BillingDocument:
        """Raises InvoiceNotFound, InvalidCreditNote or CreditExceedsInvoice.

        The invoice row stays locked until the commit, so two concurrent credit notes cannot
        both take what is left of it.
        """
        required = await self._certifier.required(shop_id)
        now = self._clock.now()
        year = self._clock.local_now().year
        async with self._uow as uow:
            invoice = await uow.documents.get(shop_id, invoice_id, lock=True)
            if invoice is None or invoice.document_type is not DocumentType.INVOICE:
                raise InvoiceNotFound(f"Invoice {invoice_id} not found.")
            previous = await uow.documents.credit_notes_of(shop_id, invoice_id)
            sequence = await uow.documents.next_sequence(shop_id, DocumentType.CREDIT_NOTE)
            note = invoice.credit(
                request,
                previous=previous,
                number=document_number(CREDIT_NOTE_PREFIX, year, sequence),
                sequence=sequence,
                now=now,
                issued_by=account_id,
                amount_label=text(invoice.language, "amount_line", number=invoice.number),
                certification_required=required,
            )
            await uow.documents.add(note)
            await uow.documents.save(invoice)
            await uow.audit.record(
                account_id=account_id,
                action="credit_note.issued",
                target_type="credit_note",
                target_id=str(note.id),
                shop_id=shop_id,
                details={
                    "number": note.number,
                    "invoice_id": str(invoice.id),
                    "invoice_number": invoice.number,
                    "total": note.total,
                    "credited_total": invoice.credited_total,
                    "content_sha256": note.content_sha256,
                },
                ip=ip,
            )
            await uow.commit()
        return note
