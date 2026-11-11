"""Billing routes of a shop, and the public invoice link of the WhatsApp template.

Invoices are issued by the worker once a payment is confirmed: there is no route to create
one. Nothing can be edited or deleted: corrections go through credit notes.
"""

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import RedirectResponse

from woassi.modules.billing.api.deps import BillingDep
from woassi.modules.billing.api.schemas import (
    BillingSummaryResponse,
    CreditNoteBody,
    DocumentResponse,
    PdfLinkResponse,
)
from woassi.modules.billing.domain.document import DocumentType
from woassi.shared.api.deps import CurrentAccount, client_ip, get_authorized_shop
from woassi.shared.api.idempotency import idempotent
from woassi.shared.api.pagination import Limit, Offset, Page

router = APIRouter(
    prefix="/shops/{shop_id}",
    tags=["billing"],
    dependencies=[Depends(get_authorized_shop)],
)
public_router = APIRouter(tags=["billing"])

Redirect = Annotated[bool, Query(description="302 to the PDF instead of returning its URL")]


async def _pdf(
    billing: BillingDep,
    shop_id: UUID,
    document_id: UUID,
    document_type: DocumentType,
    redirect: bool,
) -> PdfLinkResponse | Response:
    link = await billing.get_document_pdf().execute(
        shop_id=shop_id, document_id=document_id, document_type=document_type
    )
    if redirect:
        return RedirectResponse(link.url, status_code=status.HTTP_302_FOUND)
    return PdfLinkResponse.from_link(link)


@router.get("/invoices")
async def list_invoices(
    shop_id: UUID,
    billing: BillingDep,
    order_id: UUID | None = None,
    issued_from: date | None = None,
    issued_to: date | None = None,
    offset: Offset = 0,
    limit: Limit = 50,
) -> Page[DocumentResponse]:
    """The shop's invoices, newest first. Dates are local days (Africa/Lome), both included."""
    result = await billing.list_billing_documents().execute(
        shop_id=shop_id,
        document_type=DocumentType.INVOICE,
        order_id=order_id,
        issued_from=issued_from,
        issued_to=issued_to,
        offset=offset,
        limit=limit,
    )
    return Page[DocumentResponse].from_result(result, DocumentResponse.from_document)


@router.get("/invoices/{invoice_id}")
async def get_invoice(shop_id: UUID, invoice_id: UUID, billing: BillingDep) -> DocumentResponse:
    document = await billing.get_billing_document().execute(
        shop_id=shop_id, document_id=invoice_id, document_type=DocumentType.INVOICE
    )
    return DocumentResponse.from_document(document)


@router.get("/invoices/{invoice_id}/pdf", response_model=None)
async def get_invoice_pdf(
    shop_id: UUID, invoice_id: UUID, billing: BillingDep, redirect: Redirect = False
) -> PdfLinkResponse | Response:
    """A presigned URL of the PDF, valid a few minutes. 409 while the worker renders it."""
    return await _pdf(billing, shop_id, invoice_id, DocumentType.INVOICE, redirect)


@router.post("/invoices/{invoice_id}/resend")
async def resend_invoice(shop_id: UUID, invoice_id: UUID, billing: BillingDep) -> DocumentResponse:
    """Sends the invoice to the customer on WhatsApp again. 502 if WhatsApp refused it."""
    document = await billing.resend_invoice().execute(shop_id=shop_id, invoice_id=invoice_id)
    return DocumentResponse.from_document(document)


@router.post(
    "/invoices/{invoice_id}/credit-notes",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(idempotent)],
)
async def issue_credit_note(
    shop_id: UUID,
    invoice_id: UUID,
    body: CreditNoteBody,
    request: Request,
    account: CurrentAccount,
    billing: BillingDep,
) -> DocumentResponse:
    """Credits the whole invoice, some of its lines, or an amount, after a refund made by hand.

    409 if it would credit more than what is left of the invoice. The PDF follows shortly.
    """
    note = await billing.issue_credit_note().execute(
        shop_id=shop_id,
        invoice_id=invoice_id,
        account_id=account.id,
        request=body.to_request(),
        ip=client_ip(request),
    )
    return DocumentResponse.from_document(note)


@router.get("/credit-notes")
async def list_credit_notes(
    shop_id: UUID,
    billing: BillingDep,
    invoice_id: UUID | None = None,
    order_id: UUID | None = None,
    issued_from: date | None = None,
    issued_to: date | None = None,
    offset: Offset = 0,
    limit: Limit = 50,
) -> Page[DocumentResponse]:
    """The shop's credit notes, newest first."""
    result = await billing.list_billing_documents().execute(
        shop_id=shop_id,
        document_type=DocumentType.CREDIT_NOTE,
        order_id=order_id,
        invoice_id=invoice_id,
        issued_from=issued_from,
        issued_to=issued_to,
        offset=offset,
        limit=limit,
    )
    return Page[DocumentResponse].from_result(result, DocumentResponse.from_document)


@router.get("/credit-notes/{credit_note_id}")
async def get_credit_note(
    shop_id: UUID, credit_note_id: UUID, billing: BillingDep
) -> DocumentResponse:
    document = await billing.get_billing_document().execute(
        shop_id=shop_id, document_id=credit_note_id, document_type=DocumentType.CREDIT_NOTE
    )
    return DocumentResponse.from_document(document)


@router.get("/credit-notes/{credit_note_id}/pdf", response_model=None)
async def get_credit_note_pdf(
    shop_id: UUID, credit_note_id: UUID, billing: BillingDep, redirect: Redirect = False
) -> PdfLinkResponse | Response:
    return await _pdf(billing, shop_id, credit_note_id, DocumentType.CREDIT_NOTE, redirect)


@router.get("/billing/summary")
async def billing_summary(
    shop_id: UUID, issued_from: date, issued_to: date, billing: BillingDep
) -> BillingSummaryResponse:
    """Turnover before tax, VAT per rate, credit notes and net over local days (both included)."""
    summary = await billing.get_billing_summary().execute(
        shop_id=shop_id, issued_from=issued_from, issued_to=issued_to
    )
    return BillingSummaryResponse.from_summary(summary)


@public_router.get("/invoices/{token}", status_code=status.HTTP_302_FOUND)
async def open_invoice_link(token: str, billing: BillingDep) -> RedirectResponse:
    """Public: the link in the `woassi_invoice_ready` template. 302 to a fresh, short-lived
    URL of the PDF; 404 for an unknown or expired link. The token is the only credential.
    """
    url = await billing.open_invoice_link().execute(token)
    return RedirectResponse(url, status_code=status.HTTP_302_FOUND, headers=_NO_STORE)


_NO_STORE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
