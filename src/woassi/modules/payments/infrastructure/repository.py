"""SQLAlchemy implementations of the payments repositories."""

from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Select, delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from woassi.modules.payments.application.ports import DuplicatePayment
from woassi.modules.payments.domain.mobile_money import Operator
from woassi.modules.payments.domain.payment import (
    Channel,
    Payment,
    PaymentAttempt,
    PaymentMethod,
    PaymentStatus,
)
from woassi.modules.payments.domain.provider_account import ProviderAccount, ProviderMode
from woassi.modules.payments.infrastructure.models import (
    PaymentAttemptModel,
    PaymentModel,
    ProviderAccountModel,
)
from woassi.shared.domain.events import Entity
from woassi.shared.domain.values import Amount
from woassi.shared.infrastructure.idempotency import claim_key

_UNIQUE_PER_ORDER = ("uq_payments_paid_order", "uq_payments_pending_order")


def _attempt(model: PaymentAttemptModel) -> PaymentAttempt:
    return PaymentAttempt(
        id=model.id,
        channel=Channel(model.channel),
        status=PaymentStatus(model.status),
        created_at=model.created_at,
        updated_at=model.updated_at,
        operator=Operator(model.operator) if model.operator else None,
        payer_phone=model.payer_phone,
        submitted_at=model.submitted_at,
        provider_reference=model.provider_reference,
        checkout_url=model.checkout_url,
        expires_at=model.expires_at,
        failure_reason=model.failure_reason,
    )


def _copy_attempt(
    payment_id: UUID, attempt: PaymentAttempt, model: PaymentAttemptModel
) -> PaymentAttemptModel:
    model.id = attempt.id
    model.payment_id = payment_id
    model.channel = attempt.channel.value
    model.operator = attempt.operator.value if attempt.operator else None
    model.payer_phone = attempt.payer_phone
    model.status = attempt.status.value
    model.submitted_at = attempt.submitted_at
    model.provider_reference = attempt.provider_reference
    model.checkout_url = attempt.checkout_url
    model.expires_at = attempt.expires_at
    model.failure_reason = attempt.failure_reason
    model.created_at = attempt.created_at
    model.updated_at = attempt.updated_at
    return model


def _to_domain(model: PaymentModel, attempts: Sequence[PaymentAttemptModel]) -> Payment:
    return Payment(
        id=model.id,
        shop_id=model.shop_id,
        order_id=model.order_id,
        customer_id=model.customer_id,
        amount=Amount(model.amount),
        method=PaymentMethod(model.method),
        status=PaymentStatus(model.status),
        created_at=model.created_at,
        updated_at=model.updated_at,
        expires_at=model.expires_at,
        paid_at=model.paid_at,
        closed_at=model.closed_at,
        failure_reason=model.failure_reason,
        manual_reference=model.manual_reference,
        note=model.note,
        recorded_by=model.recorded_by,
        next_check_at=model.next_check_at,
        checks=model.checks,
        attempts=[_attempt(a) for a in sorted(attempts, key=lambda a: (a.created_at, a.id))],
        payer_phone=model.payer_phone,
        awaiting_payer=model.awaiting_payer,
        prompt_message_ids=list(model.prompt_message_ids),
    )


def _copy(payment: Payment, model: PaymentModel) -> PaymentModel:
    model.id = payment.id
    model.shop_id = payment.shop_id
    model.order_id = payment.order_id
    model.customer_id = payment.customer_id
    model.amount = payment.amount.value
    model.method = payment.method.value
    model.status = payment.status.value
    model.expires_at = payment.expires_at
    model.paid_at = payment.paid_at
    model.closed_at = payment.closed_at
    model.failure_reason = payment.failure_reason
    model.manual_reference = payment.manual_reference
    model.note = payment.note
    model.recorded_by = payment.recorded_by
    model.next_check_at = payment.next_check_at
    model.checks = payment.checks
    model.payer_phone = payment.payer_phone
    model.awaiting_payer = payment.awaiting_payer
    model.prompt_message_ids = list(payment.prompt_message_ids)
    model.created_at = payment.created_at
    model.updated_at = payment.updated_at
    return model


def _filtered(
    query: Select[Any], shop_id: UUID, status: PaymentStatus | None, order_id: UUID | None
) -> Select[Any]:
    query = query.where(PaymentModel.shop_id == shop_id)
    if status is not None:
        query = query.where(PaymentModel.status == status.value)
    if order_id is not None:
        query = query.where(PaymentModel.order_id == order_id)
    return query


class SqlAlchemyPaymentRepository:
    """`PaymentRepository` on SQLAlchemy. Saved payments are tracked for their events."""

    def __init__(self, session: AsyncSession, track: Callable[[Entity], None]) -> None:
        self._session = session
        self._track = track

    async def _flush(self) -> None:
        """Turns a broken one-PAID or one-PENDING-per-order constraint into DuplicatePayment."""
        try:
            await self._session.flush()
        except IntegrityError as error:
            for name in _UNIQUE_PER_ORDER:
                if name in str(error.orig):
                    raise DuplicatePayment(name) from None
            raise

    async def _load(self, models: Sequence[PaymentModel]) -> list[Payment]:
        """Payments with their attempts, in one more query."""
        if not models:
            return []
        rows = await self._session.scalars(
            select(PaymentAttemptModel).where(
                PaymentAttemptModel.payment_id.in_([m.id for m in models])
            )
        )
        by_payment: dict[UUID, list[PaymentAttemptModel]] = {}
        for row in rows:
            by_payment.setdefault(row.payment_id, []).append(row)
        return [_to_domain(m, by_payment.get(m.id, [])) for m in models]

    async def _one(self, query: Select[Any], lock: bool) -> Payment | None:
        model = await self._session.scalar(query.with_for_update() if lock else query)
        if model is None:
            return None
        return (await self._load([model]))[0]

    async def _write_attempts(self, payment: Payment) -> None:
        for attempt in payment.attempts:
            model = await self._session.get(PaymentAttemptModel, attempt.id)
            if model is None:
                self._session.add(_copy_attempt(payment.id, attempt, PaymentAttemptModel()))
            else:
                _copy_attempt(payment.id, attempt, model)

    async def add(self, payment: Payment) -> None:
        self._session.add(_copy(payment, PaymentModel()))
        await self._flush()
        await self._write_attempts(payment)
        await self._flush()
        self._track(payment)

    async def get(self, shop_id: UUID, payment_id: UUID, *, lock: bool = False) -> Payment | None:
        query = select(PaymentModel).where(
            PaymentModel.id == payment_id, PaymentModel.shop_id == shop_id
        )
        return await self._one(query, lock)

    async def get_by_attempt(
        self, shop_id: UUID, attempt_id: UUID, *, lock: bool = False
    ) -> Payment | None:
        query = (
            select(PaymentModel)
            .join(PaymentAttemptModel, PaymentAttemptModel.payment_id == PaymentModel.id)
            .where(PaymentAttemptModel.id == attempt_id, PaymentModel.shop_id == shop_id)
        )
        return await self._one(query, lock)

    async def awaiting_payer(self, shop_id: UUID, customer_id: UUID) -> Payment | None:
        query = (
            select(PaymentModel)
            .where(
                PaymentModel.shop_id == shop_id,
                PaymentModel.customer_id == customer_id,
                PaymentModel.awaiting_payer.is_(True),
                PaymentModel.status == "PENDING",
            )
            .order_by(PaymentModel.created_at.desc())
            .limit(1)
        )
        return await self._one(query, False)

    async def for_order(
        self, shop_id: UUID, order_id: UUID, *, lock: bool = False
    ) -> Sequence[Payment]:
        query = (
            select(PaymentModel)
            .where(PaymentModel.shop_id == shop_id, PaymentModel.order_id == order_id)
            .order_by(PaymentModel.created_at, PaymentModel.id)
        )
        models = await self._session.scalars(query.with_for_update() if lock else query)
        return await self._load(list(models))

    async def list(
        self,
        shop_id: UUID,
        status: PaymentStatus | None,
        order_id: UUID | None,
        offset: int,
        limit: int,
    ) -> Sequence[Payment]:
        query = _filtered(select(PaymentModel), shop_id, status, order_id)
        models = await self._session.scalars(
            query.order_by(PaymentModel.created_at.desc(), PaymentModel.id)
            .offset(offset)
            .limit(limit)
        )
        return await self._load(list(models))

    async def count(
        self, shop_id: UUID, status: PaymentStatus | None, order_id: UUID | None
    ) -> int:
        query = _filtered(select(func.count()).select_from(PaymentModel), shop_id, status, order_id)
        return (await self._session.scalar(query)) or 0

    async def list_due(self, now: datetime, limit: int) -> Sequence[tuple[UUID, UUID]]:
        rows = await self._session.execute(
            select(PaymentModel.shop_id, PaymentModel.id)
            .where(PaymentModel.next_check_at <= now, PaymentModel.status == "PENDING")
            .order_by(PaymentModel.next_check_at)
            .limit(limit)
        )
        return [(row.shop_id, row.id) for row in rows]

    async def save(self, payment: Payment) -> None:
        """Updates an existing payment and its attempts; raises LookupError if it is missing."""
        model = await self._session.get(PaymentModel, payment.id)
        if model is None:
            raise LookupError(f"Payment {payment.id} does not exist.")
        _copy(payment, model)
        await self._write_attempts(payment)
        await self._flush()
        self._track(payment)


def _account(model: ProviderAccountModel) -> ProviderAccount:
    return ProviderAccount(
        shop_id=model.shop_id,
        mode=ProviderMode(model.mode),
        encrypted_api_key=model.encrypted_api_key,
        encrypted_callback_token=model.encrypted_callback_token,
        callback_token_hash=model.callback_token_hash,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


class SqlAlchemyProviderAccounts:
    """`ProviderAccountRepository` on SQLAlchemy."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, shop_id: UUID) -> ProviderAccount | None:
        model = await self._session.get(ProviderAccountModel, shop_id)
        return _account(model) if model else None

    async def get_by_token_hash(self, token_hash: str) -> ProviderAccount | None:
        model = await self._session.scalar(
            select(ProviderAccountModel).where(
                ProviderAccountModel.callback_token_hash == token_hash
            )
        )
        return _account(model) if model else None

    async def save(self, account: ProviderAccount) -> None:
        model = await self._session.get(ProviderAccountModel, account.shop_id)
        if model is None:
            model = ProviderAccountModel(shop_id=account.shop_id)
            self._session.add(model)
        model.mode = account.mode.value
        model.encrypted_api_key = account.encrypted_api_key
        model.encrypted_callback_token = account.encrypted_callback_token
        model.callback_token_hash = account.callback_token_hash
        model.created_at = account.created_at
        model.updated_at = account.updated_at
        await self._session.flush()

    async def delete(self, shop_id: UUID) -> None:
        await self._session.execute(
            delete(ProviderAccountModel).where(ProviderAccountModel.shop_id == shop_id)
        )


class SqlAlchemyPaymentsInbox:
    """`Inbox` on the shared `processed_keys` table (see `process_once`)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim(self, source: str, key: str) -> bool:
        return await claim_key(self._session, source, key)
