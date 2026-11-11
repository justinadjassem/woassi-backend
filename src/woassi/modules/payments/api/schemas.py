"""Request and response bodies of the payments api. Amounts in XOF."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from woassi.modules.payments.application.use_cases.provider import ProviderAccountView
from woassi.modules.payments.domain.mobile_money import Operator
from woassi.modules.payments.domain.payment import (
    Channel,
    Payment,
    PaymentAttempt,
    PaymentMethod,
    PaymentStatus,
)
from woassi.modules.payments.domain.provider_account import ProviderMode


class ProviderAccountBody(BaseModel):
    """A KPrimePay key, scoped to `read` and `payments:write`. It is never returned."""

    api_key: str = Field(min_length=8, max_length=200, repr=False)


class ProviderAccountResponse(BaseModel):
    """Whether the shop has a KPrimePay account. Neither the key nor the callback URL."""

    configured: bool
    provider: str
    mode: ProviderMode | None
    updated_at: datetime | None

    @classmethod
    def from_view(cls, view: ProviderAccountView) -> "ProviderAccountResponse":
        return cls(
            configured=view.configured,
            provider=view.provider,
            mode=view.mode,
            updated_at=view.updated_at,
        )


class ManualPaymentBody(BaseModel):
    """Optional details of a payment received outside Woassi, e.g. a mobile money reference."""

    reference: str | None = Field(default=None, max_length=100)
    note: str | None = Field(default=None, max_length=1000)


class PaymentRequestBody(BaseModel):
    """`checkout` (default): send the usable link, or a new one. `ussd_push`: push again to the
    last number (at most 3 pushes per payment).
    """

    channel: Channel = Channel.CHECKOUT


class AttemptResponse(BaseModel):
    """One KPrimePay transaction of a payment. The payer number is masked."""

    id: UUID
    channel: Channel
    operator: Operator | None
    payer: str | None
    status: PaymentStatus
    provider_reference: str | None
    checkout_url: str | None
    expires_at: datetime | None
    failure_reason: str | None
    submitted_at: datetime | None
    created_at: datetime

    @classmethod
    def from_attempt(cls, attempt: PaymentAttempt) -> "AttemptResponse":
        payer = attempt.payer
        return cls(
            id=attempt.id,
            channel=attempt.channel,
            operator=attempt.operator,
            payer=payer.masked if payer else None,
            status=attempt.status,
            provider_reference=attempt.provider_reference,
            checkout_url=attempt.checkout_url,
            expires_at=attempt.expires_at,
            failure_reason=attempt.failure_reason,
            submitted_at=attempt.submitted_at,
            created_at=attempt.created_at,
        )


class PaymentResponse(BaseModel):
    """A payment of an order, and its KPrimePay attempts."""

    id: UUID
    order_id: UUID
    amount: int
    method: PaymentMethod
    status: PaymentStatus
    checkout_url: str | None
    expires_at: datetime | None
    paid_at: datetime | None
    closed_at: datetime | None
    failure_reason: str | None
    awaiting_payer: bool
    pushes: int
    attempts: list[AttemptResponse]
    reference: str | None
    note: str | None
    recorded_by: UUID | None
    created_at: datetime

    @classmethod
    def from_payment(cls, payment: Payment) -> "PaymentResponse":
        return cls(
            id=payment.id,
            order_id=payment.order_id,
            amount=payment.amount.value,
            method=payment.method,
            status=payment.status,
            checkout_url=payment.checkout_url,
            expires_at=payment.expires_at,
            paid_at=payment.paid_at,
            closed_at=payment.closed_at,
            failure_reason=payment.failure_reason,
            awaiting_payer=payment.awaiting_payer,
            pushes=payment.pushes,
            attempts=[AttemptResponse.from_attempt(a) for a in payment.attempts],
            reference=payment.manual_reference,
            note=payment.note,
            recorded_by=payment.recorded_by,
            created_at=payment.created_at,
        )
