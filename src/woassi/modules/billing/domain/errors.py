"""Errors of the billing module."""

from woassi.shared.domain.errors import Conflict, InvalidData, NotFound, UpstreamError


class InvoiceNotFound(NotFound):
    """The shop has no such invoice."""

    code = "billing.invoice_not_found"


class CreditNoteNotFound(NotFound):
    """The shop has no such credit note."""

    code = "billing.credit_note_not_found"


class InvoiceLinkNotFound(NotFound):
    """The invoice link is unknown or expired. Says nothing about which one."""

    code = "billing.link_not_found"


class InvalidCreditNote(InvalidData):
    """A credit note needs exactly one of: the whole invoice, some lines, or an amount."""

    code = "billing.invalid_credit_note"


class CreditExceedsInvoice(Conflict):
    """Credit notes can never add up to more than the invoice, line by line and in total."""

    code = "billing.credit_exceeds_invoice"


class PdfNotReady(Conflict):
    """The PDF is rendered by the worker, a few seconds after the document is issued."""

    code = "billing.pdf_not_ready"


class InvoiceNotSent(UpstreamError):
    """WhatsApp did not take the invoice. The invoice itself is fine; try again later."""

    code = "billing.invoice_not_sent"
