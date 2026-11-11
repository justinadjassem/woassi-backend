"""What every KPrimePay use case shares: sending a USSD push or creating a checkout, telling the
customer, and asking KPrimePay what became of an attempt.

KPrimePay calls run outside any transaction; the payment is reloaded under a row lock before
each write, and its status checked again there.
"""

import hashlib
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

import structlog

from woassi.modules.payments.application.ports import (
    Buttons,
    CheckoutRequest,
    Contact,
    CustomersPort,
    DuplicatePayment,
    MessagingPort,
    Notification,
    NotificationNotSent,
    OrdersPort,
    PayableOrder,
    PaymentGateway,
    PaymentsUnitOfWork,
    ProviderError,
    ProviderFailure,
    ProviderTransaction,
    PushRequest,
    ShopInfo,
    ShopsPort,
    Template,
    Text,
)
from woassi.modules.payments.application.texts import (
    NUMBER_TEMPLATE,
    PAYMENT_TEMPLATE,
    PUSH_TEMPLATE,
    amount,
    language_of,
    text,
)
from woassi.modules.payments.domain.mobile_money import MobileMoneyNumber
from woassi.modules.payments.domain.payment import (
    Channel,
    Payment,
    PaymentAttempt,
    PaymentStatus,
)
from woassi.modules.payments.domain.provider_account import ProviderAccount
from woassi.shared.application.ports import Clock, PhoneNormalizer, SecretCipher

log = structlog.get_logger()

CURRENCY = "XOF"
CUSTOMER_SERVICE_WINDOW = timedelta(hours=24)
# Leaves time for the message to reach Meta before the window closes.
WINDOW_MARGIN = timedelta(minutes=5)
CONFIRMED = "CONFIRMED"
PAID = "PAID"
# Button payloads come back in the reply webhook: `payment:<action>:<payment id>`.
OTHER_NUMBER, GET_LINK = "number", "link"


def payload(action: str, payment_id: UUID) -> str:
    """Button payload for `action` on `payment_id`."""
    return f"payment:{action}:{payment_id}"


def token_hash(token: str) -> str:
    """SHA-256 (hex) of a callback token: what finds the shop when a webhook comes in."""
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class CheckoutSettings:
    """Where KPrimePay sends the customer back, and the webhooks.

    `callback_base_url` is the API's public https URL; without it, only polling sees payments.
    """

    return_url: str
    callback_base_url: str | None = None


class PaymentFlow:
    """Base of the use cases that talk to KPrimePay and to the customer for a payment."""

    def __init__(
        self,
        uow: PaymentsUnitOfWork,
        gateway: PaymentGateway,
        orders: OrdersPort,
        shops: ShopsPort,
        customers: CustomersPort,
        messaging: MessagingPort,
        cipher: SecretCipher,
        phones: PhoneNormalizer,
        clock: Clock,
        settings: CheckoutSettings,
    ) -> None:
        self._uow = uow
        self._gateway = gateway
        self._orders = orders
        self._shops = shops
        self._customers = customers
        self._messaging = messaging
        self._cipher = cipher
        self._phones = phones
        self._clock = clock
        self._settings = settings

    # Context

    async def _context(self, shop_id: UUID, order_id: UUID) -> tuple[PayableOrder, ShopInfo] | None:
        """The order and its shop, or None if either is gone."""
        order = await self._orders.get(shop_id, order_id)
        shop = await self._shops.info(shop_id)
        if order is None or shop is None:
            return None
        return order, shop

    async def _account(self, shop_id: UUID) -> ProviderAccount | None:
        async with self._uow as uow:
            return await uow.provider_accounts.get(shop_id)

    def _callback_url(self, account: ProviderAccount) -> str | None:
        """`{base}/webhooks/kprimepay/{token}`. KPrimePay only accepts public https URLs."""
        base = self._settings.callback_base_url
        if not base or not base.startswith("https://"):
            return None
        token = self._cipher.decrypt(account.encrypted_callback_token)
        return f"{base.rstrip('/')}/webhooks/kprimepay/{token}"

    # Attempts

    async def _new_attempt(
        self, payment: Payment, channel: Channel, payer: MobileMoneyNumber | None = None
    ) -> tuple[Payment, PaymentAttempt] | None:
        """Adds an attempt under the row lock; None if the payment is no longer pending."""
        async with self._uow as uow:
            current = await uow.payments.get(payment.shop_id, payment.id, lock=True)
            if current is None or not current.is_pending:
                return None
            if channel is Channel.USSD_PUSH and not current.can_push:
                return None
            attempt = current.add_attempt(channel, self._clock.now(), payer=payer)
            await uow.payments.save(current)
            await uow.commit()
        return current, attempt

    async def _submit(
        self,
        payment: Payment,
        attempt: PaymentAttempt,
        account: ProviderAccount,
        order: PayableOrder,
        shop: ShopInfo,
    ) -> Payment:
        """Sends the push, or creates the checkout, and records the outcome.

        No usable answer: the attempt stays PENDING and the poller sends it again, with the
        same Idempotency-Key, so that what KPrimePay did receive is replayed, not doubled.
        A refusal: the attempt FAILS, as KPrimePay binds the key to its first answer.
        """
        language = language_of(shop.language)
        description = text(language, "description", number=order.number, shop=shop.name)[:500]
        key = self._cipher.decrypt(account.encrypted_api_key)
        try:
            if attempt.channel is Channel.CHECKOUT:
                checkout = await self._gateway.create_checkout(
                    key,
                    CheckoutRequest(
                        transaction_id=str(attempt.id),
                        amount=payment.amount.value,
                        mode=account.mode,
                        description=description,
                        locale=language,
                        return_url=self._settings.return_url,
                        callback_url=self._callback_url(account),
                        metadata={"payment_id": str(payment.id), "order_id": str(order.order_id)},
                    ),
                )
                reference, url, expires_at = checkout.reference, checkout.url, checkout.expires_at
            else:
                payer = attempt.payer
                assert payer is not None
                reference = await self._gateway.ussd_push(
                    key,
                    PushRequest(
                        transaction_id=str(attempt.id),
                        amount=payment.amount.value,
                        gateway=payer.operator,
                        phone_number=payer.local,
                        description=description,
                        callback_url=self._callback_url(account),
                    ),
                )
                url, expires_at = None, None
        except ProviderError as error:
            log.warning(
                "payment_attempt_not_submitted",
                payment_id=str(payment.id),
                attempt_id=str(attempt.id),
                channel=attempt.channel.value,
                failure=error.failure.name,
                provider_code=error.code,
            )
            return await self._submit_failed(payment, attempt.id, error)

        async with self._uow as uow:
            current = await uow.payments.get(payment.shop_id, payment.id, lock=True)
            if current is None:
                return payment
            current.attempt_submitted(
                attempt.id,
                reference=reference,
                url=url,
                expires_at=expires_at,
                now=self._clock.now(),
            )
            await uow.payments.save(current)
            await uow.commit()
        return current

    async def _submit_failed(
        self, payment: Payment, attempt_id: UUID, error: ProviderError
    ) -> Payment:
        now = self._clock.now()
        async with self._uow as uow:
            current = await uow.payments.get(payment.shop_id, payment.id, lock=True)
            if current is None:
                return payment
            if not current.is_pending or not current.attempt(attempt_id).is_open:
                return current
            if error.failure is ProviderFailure.UNAVAILABLE:
                # Already scheduled: the poller sends it again.
                return current
            if error.failure is ProviderFailure.INVALID_KEY:
                current.fail("invalid_provider_key", now)
            else:
                current.attempt_failed(attempt_id, (error.code or "rejected").lower(), now)
            await uow.payments.save(current)
            await uow.commit()
        return current

    async def _push(
        self,
        payment: Payment,
        payer: MobileMoneyNumber,
        account: ProviderAccount,
        order: PayableOrder,
        shop: ShopInfo,
        *,
        announce: str = "push",
    ) -> Payment:
        """A USSD push to `payer`, then tells the customer. After 3 pushes, a link instead."""
        if not payment.can_push:
            return await self._link(payment, account, order, shop)
        created = await self._new_attempt(payment, Channel.USSD_PUSH, payer)
        if created is None:
            return payment
        current, attempt = created
        current = await self._submit(current, attempt, account, order, shop)
        attempt = current.attempt(attempt.id)
        if not current.is_pending:
            return current
        if attempt.status is PaymentStatus.FAILED:
            await self._announce_push_failed(current, attempt, order, shop)
        else:
            await self._announce_push(current, attempt, order, shop, key=announce)
        return current

    async def _link(
        self, payment: Payment, account: ProviderAccount, order: PayableOrder, shop: ShopInfo
    ) -> Payment:
        """Sends a usable checkout link: the open one, or a new one."""
        attempt = payment.open_checkout(self._clock.now())
        current = payment
        if attempt is None:
            created = await self._new_attempt(payment, Channel.CHECKOUT)
            if created is None:
                return payment
            current, attempt = created
        if not attempt.submitted:
            current = await self._submit(current, attempt, account, order, shop)
        await self._send_link(current, order, shop)
        return current

    # Messages

    def _inside_window(self, contact: Contact) -> bool:
        """True if the customer wrote recently enough for a free-form message."""
        last = contact.last_message_at
        if last is None:
            return False
        return self._clock.now() - last < CUSTOMER_SERVICE_WINDOW - WINDOW_MARGIN

    async def _notify(
        self,
        payment: Payment,
        order: PayableOrder,
        free_form: Notification,
        template: Template | None,
        *,
        kind: str,
    ) -> str | None:
        """Free form inside the window, else (or if refused) the template. Never raises."""
        contact = await self._customers.contact(payment.shop_id, order.customer_id)
        if not (contact.user_id or contact.phone):
            return None
        choices: list[Notification] = [template] if template else []
        if self._inside_window(contact):
            choices.insert(0, free_form)
        for notification in choices:
            try:
                return await self._messaging.send(payment.shop_id, contact, notification, kind=kind)
            except NotificationNotSent:
                continue
        log.warning(
            "payment_message_not_sent",
            payment_id=str(payment.id),
            shop_id=str(payment.shop_id),
            kind=kind,
        )
        return None

    async def _record_prompt(self, payment: Payment, message_id: str | None) -> None:
        """Button replies are only trusted when they answer one of these messages."""
        if message_id is None:
            return
        async with self._uow as uow:
            current = await uow.payments.get(payment.shop_id, payment.id, lock=True)
            if current is None:
                return
            current.record_prompt(message_id)
            await uow.payments.save(current)
            await uow.commit()

    def _buttons(
        self, payment: Payment, language: str, *, number: bool
    ) -> tuple[tuple[str, str], ...]:
        buttons = []
        if number and payment.can_push:
            buttons.append((payload(OTHER_NUMBER, payment.id), text(language, "other_number")))
        buttons.append((payload(GET_LINK, payment.id), text(language, "get_link")))
        return tuple(buttons)

    async def _send_link(self, payment: Payment, order: PayableOrder, shop: ShopInfo) -> None:
        """Sends the checkout link to the customer."""
        url = payment.checkout_url
        attempt = payment.open_checkout(self._clock.now())
        if url is None or attempt is None or not attempt.submitted or not payment.is_pending:
            return
        language = language_of(shop.language)
        total = amount(order.total)
        await self._notify(
            payment,
            order,
            Text(text(language, "link", number=order.number, total=total, link=url)),
            Template(
                name=PAYMENT_TEMPLATE,
                language=language,
                parameters={"order_number": str(order.number), "total": total, "link": url},
            ),
            kind="payment.link",
        )

    async def _announce_push(
        self,
        payment: Payment,
        attempt: PaymentAttempt,
        order: PayableOrder,
        shop: ShopInfo,
        *,
        key: str,
    ) -> None:
        """'Confirm on your phone', with buttons for another number or a link."""
        payer = attempt.payer
        assert payer is not None
        language = language_of(shop.language)
        total = amount(order.total)
        body = text(language, key, number=order.number, total=total, masked=payer.masked)
        sent = await self._notify(
            payment,
            order,
            Buttons(body, self._buttons(payment, language, number=True)),
            Template(
                name=PUSH_TEMPLATE,
                language=language,
                parameters={"order_number": str(order.number), "total": total},
                button_payloads=(
                    payload(OTHER_NUMBER, payment.id),
                    payload(GET_LINK, payment.id),
                ),
            ),
            kind="payment.push",
        )
        await self._record_prompt(payment, sent)

    async def _announce_push_failed(
        self, payment: Payment, attempt: PaymentAttempt, order: PayableOrder, shop: ShopInfo
    ) -> None:
        """The push was refused or not approved: another number, or a link."""
        payer = attempt.payer
        assert payer is not None
        language = language_of(shop.language)
        body = text(language, "push_failed", total=amount(order.total), masked=payer.masked)
        sent = await self._notify(
            payment,
            order,
            Buttons(body, self._buttons(payment, language, number=True)),
            None,
            kind="payment.push_failed",
        )
        await self._record_prompt(payment, sent)

    async def _ask_number(self, payment: Payment, order: PayableOrder, shop: ShopInfo) -> None:
        """Asks which Mobile Money number will pay, with a link as the alternative."""
        async with self._uow as uow:
            current = await uow.payments.get(payment.shop_id, payment.id, lock=True)
            if current is None or not current.is_pending:
                return
            current.ask_payer(self._clock.now())
            await uow.payments.save(current)
            await uow.commit()
        language = language_of(shop.language)
        total = amount(order.total)
        sent = await self._notify(
            current,
            order,
            Buttons(
                text(language, "ask_number", number=order.number, total=total),
                self._buttons(current, language, number=False),
            ),
            Template(
                name=NUMBER_TEMPLATE,
                language=language,
                parameters={"order_number": str(order.number), "total": total},
                button_payloads=(payload(GET_LINK, current.id),),
            ),
            kind="payment.ask_number",
        )
        await self._record_prompt(current, sent)

    # Checking

    async def _check_attempt(
        self, payment: Payment, attempt_id: UUID, account: ProviderAccount
    ) -> None:
        """Asks KPrimePay about one attempt, then applies the answer under a row lock.

        Raises ProviderError when KPrimePay gives no usable answer, so that the caller retries.
        """
        try:
            transaction: ProviderTransaction | None = await self._gateway.transaction(
                self._cipher.decrypt(account.encrypted_api_key), str(attempt_id)
            )
        except ProviderError as error:
            if error.failure is not ProviderFailure.NOT_FOUND:
                raise
            transaction = None
        if transaction is None or transaction.status is PaymentStatus.PENDING:
            return

        now = self._clock.now()
        failed_push = None
        async with self._uow as uow:
            current = await uow.payments.get(payment.shop_id, payment.id, lock=True)
            if current is None:
                return
            attempt = current.attempt(attempt_id)
            if transaction.status is PaymentStatus.PAID:
                if not await self._apply_success(uow, current, attempt, transaction):
                    return
            else:
                if not attempt.is_open:
                    return
                current.attempt_failed(attempt_id, "provider_failed", now)
                if attempt.channel is Channel.USSD_PUSH and current.is_pending:
                    failed_push = attempt
            try:
                await uow.payments.save(current)
            except DuplicatePayment:
                log.error("payment_received_twice", payment_id=str(current.id))
                return
            await uow.commit()

        if failed_push is not None:
            context = await self._context(current.shop_id, current.order_id)
            if context is not None:
                await self._announce_push_failed(current, failed_push, *context)

    async def _apply_success(
        self,
        uow: PaymentsUnitOfWork,
        payment: Payment,
        attempt: PaymentAttempt,
        transaction: ProviderTransaction,
    ) -> bool:
        """Confirms the payment if KPrimePay's record matches ours. False if nothing changes.

        Only KPrimePay's own answer, fetched with the shop's key, is trusted: never a webhook.
        """
        matches = (
            transaction.transaction_id == str(attempt.id)
            and transaction.amount == payment.amount.value
            and (transaction.currency or "").upper() == CURRENCY
        )
        if not matches:
            log.error(
                "payment_verification_mismatch",
                payment_id=str(payment.id),
                attempt_id=str(attempt.id),
                shop_id=str(payment.shop_id),
            )
            return False
        if payment.status is PaymentStatus.CANCELLED:
            # A manual payment replaced it, but the customer paid KPrimePay too: refund due.
            log.error("payment_received_after_cancellation", payment_id=str(payment.id))
            return False
        if payment.status is PaymentStatus.PAID:
            # Two attempts were both approved (e.g. two pushes): refund due.
            log.error("payment_received_twice", payment_id=str(payment.id))
            return False
        others = await uow.payments.for_order(payment.shop_id, payment.order_id)
        if any(o.status is PaymentStatus.PAID and o.id != payment.id for o in others):
            log.error("payment_received_twice", payment_id=str(payment.id))
            return False
        payment.attempt_paid(attempt.id, self._clock.now())
        return True

    async def _expire_if_due(self, payment: Payment) -> None:
        now = self._clock.now()
        async with self._uow as uow:
            current = await uow.payments.get(payment.shop_id, payment.id, lock=True)
            if current is None or not current.is_past_deadline(now):
                return
            current.expire(now)
            await uow.payments.save(current)
            await uow.commit()

    async def _resubmit(
        self, payment: Payment, attempt: PaymentAttempt, account: ProviderAccount
    ) -> None:
        """For the poller: sends again an attempt KPrimePay did not answer for."""
        context = await self._context(payment.shop_id, payment.order_id)
        if context is None or context[0].status != CONFIRMED:
            async with self._uow as uow:
                current = await uow.payments.get(payment.shop_id, payment.id, lock=True)
                if current is not None and current.is_pending:
                    current.cancel(self._clock.now())
                    await uow.payments.save(current)
                    await uow.commit()
            return
        order, shop = context
        updated = await self._submit(payment, attempt, account, order, shop)
        if attempt.channel is Channel.CHECKOUT:
            await self._send_link(updated, order, shop)
