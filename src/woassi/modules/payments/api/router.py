"""Payment routes of a shop, and the public KPrimePay webhook.

The webhook is public on purpose: KPrimePay signs nothing, so the route only finds the shop by
the secret token in its URL, and the worker asks KPrimePay itself before marking anything paid.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response, status
from fastapi.responses import PlainTextResponse

from woassi.modules.payments.api.deps import PaymentsDep
from woassi.modules.payments.api.schemas import (
    ManualPaymentBody,
    PaymentRequestBody,
    PaymentResponse,
    ProviderAccountBody,
    ProviderAccountResponse,
)
from woassi.modules.payments.domain.payment import PaymentStatus
from woassi.shared.api.deps import CurrentAccount, client_ip, get_authorized_shop
from woassi.shared.api.idempotency import idempotent
from woassi.shared.api.pagination import Limit, Offset, Page

router = APIRouter(
    prefix="/shops/{shop_id}",
    tags=["payments"],
    dependencies=[Depends(get_authorized_shop)],
)
webhook_router = APIRouter(tags=["payments"])


@router.get("/payment-provider")
async def get_payment_provider(shop_id: UUID, payments: PaymentsDep) -> ProviderAccountResponse:
    """Whether the shop has a KPrimePay account, and in which mode. Never the key."""
    view = await payments.get_payment_provider().execute(shop_id)
    return ProviderAccountResponse.from_view(view)


@router.put("/payment-provider")
async def set_payment_provider(
    shop_id: UUID,
    body: ProviderAccountBody,
    request: Request,
    account: CurrentAccount,
    payments: PaymentsDep,
) -> ProviderAccountResponse:
    """Checks the key with KPrimePay, then stores it encrypted. 422 if refused, 502 if down."""
    view = await payments.configure_payment_provider().execute(
        shop_id=shop_id, account_id=account.id, api_key=body.api_key, ip=client_ip(request)
    )
    return ProviderAccountResponse.from_view(view)


@router.delete("/payment-provider", status_code=status.HTTP_204_NO_CONTENT)
async def delete_payment_provider(
    shop_id: UUID, request: Request, account: CurrentAccount, payments: PaymentsDep
) -> None:
    """Forgets the key: payments can then only be recorded by hand."""
    await payments.remove_payment_provider().execute(
        shop_id=shop_id, account_id=account.id, ip=client_ip(request)
    )


@router.get("/payments")
async def list_payments(
    shop_id: UUID,
    payments: PaymentsDep,
    status: PaymentStatus | None = None,
    order_id: UUID | None = None,
    offset: Offset = 0,
    limit: Limit = 50,
) -> Page[PaymentResponse]:
    """The shop's payments, newest first, optionally by status or order."""
    result = await payments.list_payments().execute(
        shop_id=shop_id, status=status, order_id=order_id, offset=offset, limit=limit
    )
    return Page[PaymentResponse].from_result(result, PaymentResponse.from_payment)


@router.get("/payments/{payment_id}")
async def get_payment(shop_id: UUID, payment_id: UUID, payments: PaymentsDep) -> PaymentResponse:
    """One payment of the shop."""
    payment = await payments.get_payment().execute(shop_id=shop_id, payment_id=payment_id)
    return PaymentResponse.from_payment(payment)


@router.post(
    "/orders/{order_id}/payments/manual",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(idempotent)],
)
async def record_manual_payment(
    shop_id: UUID,
    order_id: UUID,
    body: ManualPaymentBody,
    request: Request,
    account: CurrentAccount,
    payments: PaymentsDep,
) -> PaymentResponse:
    """The merchant received the order's total (cash, mobile money...). Cancels a pending link.

    409 unless the order is CONFIRMED and not paid yet.
    """
    payment = await payments.record_manual_payment().execute(
        shop_id=shop_id,
        order_id=order_id,
        account_id=account.id,
        reference=body.reference,
        note=body.note,
        ip=client_ip(request),
    )
    return PaymentResponse.from_payment(payment)


@router.post("/orders/{order_id}/payments/request", dependencies=[Depends(idempotent)])
async def request_payment(
    shop_id: UUID,
    order_id: UUID,
    request: Request,
    account: CurrentAccount,
    payments: PaymentsDep,
    body: PaymentRequestBody | None = None,
) -> PaymentResponse:
    """Asks the customer again: a KPrimePay link (default), or a USSD push to the last number.

    409 without a KPrimePay account, without a known number for a push, after 3 pushes, or
    unless the order is CONFIRMED and not paid yet.
    """
    channel = (body or PaymentRequestBody()).channel
    payment = await payments.request_payment().execute(
        shop_id=shop_id,
        order_id=order_id,
        channel=channel,
        account_id=account.id,
        ip=client_ip(request),
    )
    return PaymentResponse.from_payment(payment)


@webhook_router.post("/webhooks/kprimepay/{token}")
async def receive_kprimepay_webhook(
    token: str,
    request: Request,
    payments: PaymentsDep,
    event_id: Annotated[str | None, Header(alias="X-KPP-EVENT-ID", max_length=100)] = None,
) -> Response:
    """Queues a check of the payment and answers 200 at once. 404 for an unknown token."""
    await payments.receive_kprimepay_webhook().execute(
        token=token, body=await request.body(), event_id_header=event_id, ip=client_ip(request)
    )
    return Response(status_code=status.HTTP_200_OK)


@webhook_router.get("/payments/return", response_class=PlainTextResponse)
async def payment_return() -> str:
    """Where KPrimePay sends the customer back. Proves nothing: the worker checks the payment."""
    return "Merci ! Vous pouvez retourner sur WhatsApp. / Thank you! You can go back to WhatsApp."
