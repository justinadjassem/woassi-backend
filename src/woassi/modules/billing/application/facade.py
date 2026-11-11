"""Use cases the billing api needs, provided by the composition root."""

from typing import Protocol

from woassi.modules.billing.application.use_cases.credit_notes import IssueCreditNote
from woassi.modules.billing.application.use_cases.issuing import ResendInvoice
from woassi.modules.billing.application.use_cases.queries import (
    GetBillingDocument,
    GetBillingSummary,
    GetDocumentPdf,
    ListBillingDocuments,
    OpenInvoiceLink,
)


class BillingUseCases(Protocol):
    """What this module expects from the composition root (see catalog.application.facade)."""

    def list_billing_documents(self) -> ListBillingDocuments: ...

    def get_billing_document(self) -> GetBillingDocument: ...

    def get_document_pdf(self) -> GetDocumentPdf: ...

    def resend_invoice(self) -> ResendInvoice: ...

    def issue_credit_note(self) -> IssueCreditNote: ...

    def open_invoice_link(self) -> OpenInvoiceLink: ...

    def get_billing_summary(self) -> GetBillingSummary: ...
