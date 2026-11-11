"""Learning that a customer paid: KPrimePay webhooks, checked by the worker, and polling.

KPrimePay signs no webhook: a webhook is only a signal to go and ask KPrimePay, with the shop's
own key, what became of the payment. Its payload is never trusted for money.
https://developers.kprimepay.com/index?v=v2#webhooks
"""

import hmac
import json
import re
from typing import Any
from uuid import UUID

import structlog

from woassi.modules.payments.application.ports import (
    PaymentCheckQueue,
    PaymentsUnitOfWork,
    ProviderError,
)
from woassi.modules.payments.application.use_cases.flow import PaymentFlow, token_hash
from woassi.modules.payments.domain.errors import UnknownCallback
from woassi.modules.payments.domain.payment import PaymentMethod, PaymentStatus
from woassi.shared.application.ports import RateLimiter
from woassi.shared.application.rate_limits import enforce
from woassi.shared.domain.errors import InvalidData

log = structlog.get_logger()

EVENT_SOURCE = "kprimepay.event"
MAX_TOKEN = 128
EVENT_ID = re.compile(r"[A-Za-z0-9_\-]{1,100}")
# Per client IP: generous for KPrimePay, tight enough to make guessing tokens pointless.
WEBHOOK_LIMIT = 300
WEBHOOK_WINDOW_S = 60


class ReceiveKPrimePayWebhook:
    """Finds the shop by its callback token, keeps each event once, and queues a check."""

    def __init__(
        self, uow: PaymentsUnitOfWork, queue: PaymentCheckQueue, limiter: RateLimiter
    ) -> None:
        self._uow = uow
        self._queue = queue
        self._limiter = limiter

    async def execute(
        self, *, token: str, body: bytes, event_id_header: str | None, ip: str
    ) -> bool:
        """True if a check was queued; False for a duplicate or an event that needs none.

        Raises RateLimited, UnknownCallback (no shop has this token) or InvalidData.
        """
        await enforce(self._limiter, f"kprimepay-webhook:{ip}", WEBHOOK_LIMIT, WEBHOOK_WINDOW_S)
        if not 1 <= len(token) <= MAX_TOKEN:
            raise UnknownCallback("Unknown callback.")
        digest = token_hash(token)
        async with self._uow as uow:
            account = await uow.provider_accounts.get_by_token_hash(digest)
            if account is None or not hmac.compare_digest(account.callback_token_hash, digest):
                raise UnknownCallback("Unknown callback.")
            event_id, payment_id = _parse(body, event_id_header)
            if payment_id is None:
                log.info("kprimepay_event_ignored", shop_id=str(account.shop_id))
                return False
            # Scoped by shop: one shop cannot spend another shop's event ids.
            if not await uow.inbox.claim(EVENT_SOURCE, f"{account.shop_id}:{event_id}"):
                log.info("kprimepay_event_duplicate", shop_id=str(account.shop_id))
                return False
            # Queued before the commit: if the commit fails, KPrimePay sends the event again,
            # and an extra check is harmless.
            await self._queue.enqueue(account.shop_id, payment_id)
            await uow.commit()
        return True


def _parse(body: bytes, event_id_header: str | None) -> tuple[str, UUID | None]:
    """(event id, our payment id). The payment id is None for events that need no check."""
    try:
        payload: Any = json.loads(body)
    except ValueError as error:
        raise InvalidData("Unreadable webhook body.") from error
    if not isinstance(payload, dict):
        raise InvalidData("Unexpected webhook body.")
    event_id = payload.get("event_id")
    if not isinstance(event_id, str) or not EVENT_ID.fullmatch(event_id):
        raise InvalidData("Missing event id.")
    if event_id_header is not None and event_id_header != event_id:
        raise InvalidData("The event id header does not match the body.")
    event = payload.get("event")
    data = payload.get("data")
    if (
        not isinstance(event, str)
        or not event.startswith("collection.")
        or not isinstance(data, dict)
    ):
        return event_id, None
    transaction_id = data.get("transaction_id")
    try:
        return event_id, UUID(str(transaction_id))
    except ValueError:
        # Not one of our payments (our transaction id is the payment id).
        return event_id, None


class VerifyPayment(PaymentFlow):
    """Worker job after a webhook: asks KPrimePay and moves the payment if it really changed."""

    async def execute(self, *, shop_id: UUID, payment_id: UUID) -> PaymentStatus | None:
        """`payment_id` is KPrimePay's transaction id: an attempt id (for payments from before
        attempts existed, it is also the payment id). Returns the payment's status afterwards.

        Raises ProviderError when KPrimePay gives no usable answer, so that the job is retried.
        """
        async with self._uow as uow:
            payment = await uow.payments.get_by_attempt(shop_id, payment_id)
            account = await uow.provider_accounts.get(shop_id)
        if payment is None or payment.method is not PaymentMethod.KPRIMEPAY:
            log.info("payment_check_unknown_payment", shop_id=str(shop_id))
            return None
        if payment.status is PaymentStatus.PAID or account is None:
            return payment.status
        attempt = payment.attempt(payment_id)
        if not attempt.submitted and attempt.is_open:
            await self._resubmit(payment, attempt, account)
        else:
            await self._check_attempt(payment, attempt.id, account)
        await self._expire_if_due(payment)
        async with self._uow as uow:
            current = await uow.payments.get(shop_id, payment.id)
        return current.status if current else None


class PollPendingPayments(PaymentFlow):
    """Cron: checks pending payments KPrimePay told us nothing about, with a growing delay,
    and expires them. Covers lost webhooks and shops without a public callback URL.
    """

    async def execute(self, *, limit: int = 100) -> int:
        """Checks up to `limit` due payments; returns how many. One failure skips that one."""
        async with self._uow as uow:
            due = await uow.payments.list_due(self._clock.now(), limit)
        handled = 0
        for shop_id, payment_id in due:
            try:
                handled += await self._one(shop_id, payment_id)
            except Exception:
                log.exception("payment_poll_failed", payment_id=str(payment_id))
        return handled

    async def _one(self, shop_id: UUID, payment_id: UUID) -> int:
        """Claims the check under a row lock, by scheduling the next one, then checks each
        open attempt, and expires the payment past its deadline.
        """
        now = self._clock.now()
        async with self._uow as uow:
            payment = await uow.payments.get(shop_id, payment_id, lock=True)
            # Checked again under the lock: a webhook may have settled it meanwhile.
            if payment is None or not payment.is_due(now):
                return 0
            account = await uow.provider_accounts.get(shop_id)
            payment.checked(now)
            await uow.payments.save(payment)
            await uow.commit()

        if account is not None:
            for attempt in [a for a in payment.attempts if a.is_open]:
                try:
                    if attempt.submitted:
                        await self._check_attempt(payment, attempt.id, account)
                    else:
                        await self._resubmit(payment, attempt, account)
                except ProviderError as error:
                    log.warning(
                        "payment_poll_unavailable",
                        payment_id=str(payment_id),
                        failure=error.failure.name,
                    )
        # Without a key nobody can ask KPrimePay any more: the payment just expires.
        await self._expire_if_due(payment)
        return 1
