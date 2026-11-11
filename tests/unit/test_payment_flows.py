import json
from datetime import timedelta
from uuid import uuid4

import pytest

from tests.payments_fakes import CHECKOUT_URL, LIVE_KEY, SANDBOX_KEY, PaymentsStack
from woassi.modules.payments.application.ports import (
    Buttons,
    Contact,
    ProviderError,
    ProviderFailure,
    Template,
    Text,
)
from woassi.modules.payments.application.use_cases.flow import token_hash
from woassi.modules.payments.application.use_cases.provider import (
    GetPaymentProvider,
    RemovePaymentProvider,
)
from woassi.modules.payments.domain.errors import (
    AmountOutOfRange,
    InvalidProviderKey,
    NoPayerNumber,
    OrderAlreadyPaid,
    OrderNotFound,
    OrderNotPayable,
    ProviderNotConfigured,
    ProviderUnavailable,
    PushLimitReached,
    UnknownCallback,
)
from woassi.modules.payments.domain.events import PaymentConfirmed
from woassi.modules.payments.domain.mobile_money import Operator
from woassi.modules.payments.domain.payment import Channel, PaymentMethod, PaymentStatus
from woassi.modules.payments.domain.provider_account import ProviderMode
from woassi.shared.domain.errors import InvalidData, RateLimited


@pytest.fixture
async def stack() -> PaymentsStack:
    stack = PaymentsStack(callback_base_url="https://api.example.com")
    await stack.configure()
    return stack


def no_whatsapp_number(stack: PaymentsStack) -> None:
    """A customer with a WhatsApp username: only a BSUID."""
    stack.customers.contacts[stack.customer_id] = Contact(
        user_id="TG.123", last_message_at=stack.clock.now()
    )


def webhook_body(transaction_id: object, *, event_id: str = "evt_1") -> bytes:
    return json.dumps(
        {
            "api_version": "2.0",
            "event": "collection.succeeded",
            "event_id": event_id,
            "object": "collection",
            "data": {
                "transaction_id": str(transaction_id),
                "status": "succeeded",
                "transaction_details": {"currency": "XOF", "amount": 10_000},
                "customer": {"name": "Ama", "phone": "22890010203"},
            },
        }
    ).encode()


def paid_events(stack: PaymentsStack) -> list[PaymentConfirmed]:
    return [e for e in stack.events if isinstance(e, PaymentConfirmed)]


# Provider account


async def test_the_key_is_checked_then_stored_encrypted(stack: PaymentsStack) -> None:
    account = stack.accounts[stack.shop_id]
    assert stack.gateway.verified_keys == [SANDBOX_KEY]
    assert account.mode is ProviderMode.SANDBOX
    assert SANDBOX_KEY not in account.encrypted_api_key
    assert stack.cipher.decrypt(account.encrypted_api_key) == SANDBOX_KEY
    assert account.callback_token_hash == token_hash(stack.callback_token())
    assert SANDBOX_KEY not in repr(account)
    [entry] = stack.audit_log.entries
    assert entry.action == "payment_provider.updated"
    assert entry.details == {"provider": "kprimepay", "mode": "sandbox"}


async def test_a_new_key_keeps_the_callback_token(stack: PaymentsStack) -> None:
    token = stack.callback_token()
    await stack.configure(LIVE_KEY)
    assert stack.accounts[stack.shop_id].mode is ProviderMode.LIVE
    assert stack.callback_token() == token


async def test_a_refused_key_is_not_stored() -> None:
    stack = PaymentsStack()
    stack.gateway.refuse_key = True
    with pytest.raises(InvalidProviderKey):
        await stack.configure()
    stack.gateway.refuse_key, stack.gateway.unavailable = False, True
    with pytest.raises(ProviderUnavailable):
        await stack.configure()
    with pytest.raises(InvalidProviderKey):
        await stack.configure("not-a-kprimepay-key")
    assert stack.accounts == {} and stack.audit_log.entries == []


async def test_removing_the_account(stack: PaymentsStack) -> None:
    await RemovePaymentProvider(stack.uow()).execute(
        shop_id=stack.shop_id, account_id=stack.account_id
    )
    view = await GetPaymentProvider(stack.uow()).execute(stack.shop_id)
    assert not view.configured and view.mode is None
    assert stack.audit_log.entries[-1].action == "payment_provider.removed"


# After the confirmation: a USSD push to the WhatsApp number


async def test_a_confirmed_order_pushes_to_the_customer_s_number(stack: PaymentsStack) -> None:
    payment = await stack.confirmed(stack.order(total=22_500, number=7))

    [(key, push)] = stack.gateway.pushes
    attempt = payment.attempts[0]
    assert key == SANDBOX_KEY
    assert push.transaction_id == str(attempt.id)  # also the Idempotency-Key
    assert (push.amount, push.gateway, push.phone_number) == (
        22_500,
        Operator.MIXX_YAS_TG,
        "91234567",
    )
    assert (
        push.callback_url == f"https://api.example.com/webhooks/kprimepay/{stack.callback_token()}"
    )
    assert push.description == "Commande n°7 - Chez Ama"
    assert "91234567" not in repr(push)
    assert attempt.channel is Channel.USSD_PUSH and attempt.submitted

    message, message_id = stack.last_message()
    assert isinstance(message, Buttons)
    assert message.body == (
        "Commande n°7 : confirmez sur votre téléphone la demande de paiement de 22 500 FCFA, "
        "envoyée au 91 •• •• 67."
    )
    assert message.buttons == (
        (f"payment:number:{payment.id}", "Autre numéro"),
        (f"payment:link:{payment.id}", "Lien de paiement"),
    )
    assert stack.payment(payment).prompt_message_ids == [message_id]


async def test_outside_the_window_the_push_prompt_is_a_template(stack: PaymentsStack) -> None:
    stack.customers.contacts[stack.customer_id] = Contact(
        phone="+22896000011", last_message_at=stack.clock.now() - timedelta(hours=30)
    )
    payment = await stack.confirmed(stack.order(number=3))

    assert stack.gateway.pushes[0][1].gateway is Operator.MOOV_MONEY_TG
    template, _ = stack.last_message()
    assert isinstance(template, Template)
    assert template.name == "woassi_payment_push" and template.language == "fr"
    assert template.parameters == {"order_number": "3", "total": "10 000 FCFA"}
    assert template.button_payloads == (
        f"payment:number:{payment.id}",
        f"payment:link:{payment.id}",
    )


async def test_without_a_togolese_number_we_ask_which_number_pays(stack: PaymentsStack) -> None:
    no_whatsapp_number(stack)
    payment = await stack.confirmed(stack.order(number=4))

    assert stack.gateway.pushes == []
    assert stack.payment(payment).awaiting_payer
    message, _ = stack.last_message()
    assert isinstance(message, Buttons)
    assert "envoyez le numéro Mobile Money (Flooz ou Mixx by Yas)" in message.body
    assert message.buttons == ((f"payment:link:{payment.id}", "Lien de paiement"),)


async def test_a_foreign_number_is_asked_too(stack: PaymentsStack) -> None:
    stack.customers.contacts[stack.customer_id] = Contact(
        phone="+33612345678", last_message_at=stack.clock.now()
    )
    payment = await stack.confirmed(stack.order())
    assert stack.gateway.pushes == [] and stack.payment(payment).awaiting_payer


async def test_a_replayed_confirmation_sends_nothing_twice(stack: PaymentsStack) -> None:
    order_id = stack.order()
    first = await stack.confirmed(order_id)
    second = await stack.confirmed(order_id)
    assert first.id == second.id
    assert len(stack.gateway.pushes) == 1 and len(stack.messaging.sent) == 1


async def test_a_refused_push_offers_the_alternatives(stack: PaymentsStack) -> None:
    stack.gateway.push_error = ProviderError(ProviderFailure.REJECTED, code="GATEWAY_REJECTED")
    payment = await stack.confirmed(stack.order())

    stored = stack.payment(payment)
    assert stored.is_pending
    assert stored.attempts[0].status is PaymentStatus.FAILED
    assert stored.attempts[0].failure_reason == "gateway_rejected"
    message, _ = stack.last_message()
    assert isinstance(message, Buttons) and "n'a pas abouti" in message.body
    assert len(message.buttons) == 2


async def test_a_message_failure_never_fails_the_payment(stack: PaymentsStack) -> None:
    stack.messaging.failing = True
    payment = await stack.confirmed(stack.order())
    assert payment.is_pending and payment.attempts[0].submitted


# Buttons and typed numbers


async def test_another_number_then_a_typed_number(stack: PaymentsStack) -> None:
    payment = await stack.confirmed(stack.order(total=15_000))
    _, prompt_id = stack.last_message()

    assert await stack.tap(f"payment:number:{payment.id}", prompt_id)
    assert stack.payment(payment).awaiting_payer
    ask, _ = stack.last_message()
    assert isinstance(ask, Buttons) and "Flooz ou Mixx by Yas" in ask.body

    assert await stack.type("C'est le +228 96 12 34 56")

    stored = stack.payment(payment)
    assert not stored.awaiting_payer
    assert [a.channel for a in stored.attempts] == [Channel.USSD_PUSH, Channel.USSD_PUSH]
    push = stack.gateway.pushes[-1][1]
    assert (push.phone_number, push.gateway) == ("96123456", Operator.MOOV_MONEY_TG)
    assert push.transaction_id != stack.gateway.pushes[0][1].transaction_id
    sent, _ = stack.last_message()
    assert isinstance(sent, Buttons)
    assert sent.body == "Demande envoyée au 96 •• •• 56 : validez-la avec votre code secret."


@pytest.mark.parametrize("typed", ["90 12 34 56", "+228 90123456", "0022890123456", "90-12-34-56"])
async def test_typed_number_formats(stack: PaymentsStack, typed: str) -> None:
    no_whatsapp_number(stack)
    await stack.confirmed(stack.order())
    assert await stack.type(typed)
    assert stack.gateway.pushes[-1][1].phone_number == "90123456"


async def test_an_invalid_number_gets_a_helpful_reply(stack: PaymentsStack) -> None:
    no_whatsapp_number(stack)
    payment = await stack.confirmed(stack.order())

    assert await stack.type("22 12 34 56")  # a landline
    assert await stack.type("+225 07 12 34 56 78")  # Ivorian

    assert stack.gateway.pushes == []
    assert stack.payment(payment).awaiting_payer
    reply, _ = stack.last_message()
    assert isinstance(reply, Text) and "pas un numéro Mobile Money togolais" in reply.body


async def test_ordinary_texts_follow_the_usual_path(stack: PaymentsStack) -> None:
    assert not await stack.type("90 12 34 56")  # no payment waits for a number
    no_whatsapp_number(stack)
    await stack.confirmed(stack.order())
    assert not await stack.type("Bonjour, c'est pour quand ?")  # no digits: not an answer
    assert stack.gateway.pushes == []


async def test_get_a_link(stack: PaymentsStack) -> None:
    payment = await stack.confirmed(stack.order(number=5))
    _, prompt_id = stack.last_message()

    assert await stack.tap(f"payment:link:{payment.id}", prompt_id)

    stored = stack.payment(payment)
    assert [a.channel for a in stored.attempts] == [Channel.USSD_PUSH, Channel.CHECKOUT]
    [(_, checkout)] = stack.gateway.checkouts
    assert checkout.transaction_id == str(stored.attempts[1].id)
    link, _ = stack.last_message()
    assert isinstance(link, Text) and CHECKOUT_URL in link.body
    # Tapping again sends the same link.
    assert await stack.tap(f"payment:link:{payment.id}", prompt_id)
    assert len(stack.gateway.checkouts) == 1


async def test_buttons_only_answer_our_messages(stack: PaymentsStack) -> None:
    payment = await stack.confirmed(stack.order())
    _, prompt_id = stack.last_message()
    assert not await stack.tap(f"payment:link:{payment.id}", "wamid.someone-else")
    assert not await stack.tap(f"payment:link:{payment.id}", None)
    assert not await stack.tap(f"payment:link:{uuid4()}", prompt_id)
    assert not await stack.tap("payment:steal:x", prompt_id)
    assert stack.gateway.checkouts == []


async def test_after_three_pushes_only_the_link(stack: PaymentsStack) -> None:
    payment = await stack.confirmed(stack.order())
    for number in ("90 00 00 01", "90 00 00 02"):
        _, prompt_id = stack.last_message()
        assert await stack.tap(f"payment:number:{payment.id}", prompt_id)
        assert await stack.type(number)
    assert len(stack.gateway.pushes) == 3

    last, prompt_id = stack.last_message()
    assert isinstance(last, Buttons)
    assert last.buttons == ((f"payment:link:{payment.id}", "Lien de paiement"),)
    # The 'another number' button of an older message now gives the link.
    assert await stack.tap(
        f"payment:number:{payment.id}", stack.payment(payment).prompt_message_ids[0]
    )
    assert len(stack.gateway.pushes) == 3 and len(stack.gateway.checkouts) == 1
    with pytest.raises(PushLimitReached):
        await stack.request(payment.order_id, Channel.USSD_PUSH)


# From the dashboard


async def test_the_merchant_pushes_again_to_the_last_number(stack: PaymentsStack) -> None:
    payment = await stack.confirmed(stack.order())
    again = await stack.request(payment.order_id, Channel.USSD_PUSH)
    assert again.id == payment.id and again.pushes == 2
    assert stack.gateway.pushes[-1][1].phone_number == "91234567"


async def test_no_number_to_push_to(stack: PaymentsStack) -> None:
    no_whatsapp_number(stack)
    order_id = stack.order()
    await stack.confirmed(order_id)
    with pytest.raises(NoPayerNumber):
        await stack.request(order_id, Channel.USSD_PUSH)


async def test_a_link_from_the_dashboard(stack: PaymentsStack) -> None:
    order_id = stack.order(total=22_500, number=7)
    payment = await stack.request(order_id)

    assert payment.checkout_url == CHECKOUT_URL
    [(key, request)] = stack.gateway.checkouts
    assert key == SANDBOX_KEY
    assert request.transaction_id == str(payment.attempts[0].id)
    assert request.amount == 22_500
    text, _ = stack.last_message()
    assert isinstance(text, Text)
    assert text.body == (
        f"Pour payer votre commande n°7 (22 500 FCFA), utilisez ce lien sécurisé : {CHECKOUT_URL}"
    )
    # Again: the same link, not a second checkout.
    await stack.request(order_id)
    assert len(stack.gateway.checkouts) == 1 and len(stack.messaging.sent) == 2


async def test_outside_the_window_the_link_goes_as_a_template(stack: PaymentsStack) -> None:
    stack.customers.contacts[stack.customer_id] = Contact(
        user_id="TG.123", last_message_at=stack.clock.now() - timedelta(hours=30)
    )
    await stack.request(stack.order(number=3))
    template, _ = stack.last_message()
    assert isinstance(template, Template) and template.name == "woassi_order_payment"
    assert template.parameters == {
        "order_number": "3",
        "total": "10 000 FCFA",
        "link": CHECKOUT_URL,
    }


async def test_a_refused_text_falls_back_to_the_template(stack: PaymentsStack) -> None:
    stack.messaging.refuse_texts = True
    await stack.request(stack.order())
    template, _ = stack.last_message()
    assert isinstance(template, Template)


async def test_without_a_callback_url_polling_does_the_job() -> None:
    stack = PaymentsStack(callback_base_url=None)
    await stack.configure()
    await stack.confirmed(stack.order())
    await stack.request(stack.order(number=2))
    assert stack.gateway.pushes[0][1].callback_url is None
    assert stack.gateway.checkouts[0][1].callback_url is None


async def test_no_account_no_request() -> None:
    stack = PaymentsStack()
    with pytest.raises(ProviderNotConfigured):
        await stack.confirmed(stack.order())


@pytest.mark.parametrize(
    ("status", "error"),
    [
        ("AWAITING_MERCHANT", OrderNotPayable),
        ("REJECTED", OrderNotPayable),
        ("PAID", OrderAlreadyPaid),
    ],
)
async def test_only_confirmed_orders_are_payable(
    stack: PaymentsStack, status: str, error: type[Exception]
) -> None:
    with pytest.raises(error):
        await stack.confirmed(stack.order(status=status))
    assert stack.gateway.pushes == [] and stack.gateway.checkouts == []


async def test_unknown_order_and_amount_bounds(stack: PaymentsStack) -> None:
    with pytest.raises(OrderNotFound):
        await stack.request(uuid4())
    with pytest.raises(AmountOutOfRange):
        await stack.confirmed(stack.order(total=5))


# KPrimePay not answering


async def test_a_timeout_is_resent_by_the_poller_with_the_same_key(stack: PaymentsStack) -> None:
    stack.gateway.push_error = ProviderError(ProviderFailure.UNAVAILABLE)
    payment = await stack.confirmed(stack.order())
    assert not stack.payment(payment).attempts[0].submitted

    stack.gateway.push_error = None
    stack.clock.advance(timedelta(minutes=2))
    assert await stack.poll() == 1

    stored = stack.payment(payment)
    assert len(stored.attempts) == 1 and stored.attempts[0].submitted
    assert stack.gateway.pushes[0][1].transaction_id == str(stored.attempts[0].id)


async def test_an_unanswered_checkout_is_resent_then_its_link_sent(stack: PaymentsStack) -> None:
    stack.gateway.checkout_error = ProviderError(ProviderFailure.UNAVAILABLE)
    payment = await stack.request(stack.order())
    assert stack.messaging.sent == []

    stack.gateway.checkout_error = None
    stack.clock.advance(timedelta(minutes=2))
    await stack.poll()
    assert stack.payment(payment).checkout_url == CHECKOUT_URL
    assert len(stack.messaging.sent) == 1


async def test_an_invalid_key_fails_the_payment(stack: PaymentsStack) -> None:
    stack.gateway.push_error = ProviderError(ProviderFailure.INVALID_KEY, code="INVALID_AUTH_TOKEN")
    payment = await stack.confirmed(stack.order())
    stored = stack.payment(payment)
    assert stored.status is PaymentStatus.FAILED
    assert stored.failure_reason == "invalid_provider_key"


async def test_never_created_payments_expire(stack: PaymentsStack) -> None:
    stack.gateway.checkout_error = ProviderError(ProviderFailure.UNAVAILABLE)
    payment = await stack.request(stack.order())
    for _ in range(30):
        stack.clock.advance(timedelta(hours=1))
        await stack.poll()
    assert stack.payment(payment).status is PaymentStatus.EXPIRED


# Webhook and verification


async def test_a_webhook_queues_a_check_once(stack: PaymentsStack) -> None:
    payment = await stack.confirmed(stack.order())
    transaction_id = payment.attempts[0].id
    body = webhook_body(transaction_id)

    assert await stack.webhook().execute(
        token=stack.callback_token(), body=body, event_id_header="evt_1", ip="1.2.3.4"
    )
    assert not await stack.webhook().execute(
        token=stack.callback_token(), body=body, event_id_header="evt_1", ip="1.2.3.4"
    )
    assert stack.queue.checks == [(stack.shop_id, transaction_id)]
    # The webhook itself changes nothing.
    assert stack.payment(payment).status is PaymentStatus.PENDING


async def test_an_unknown_token_is_refused(stack: PaymentsStack) -> None:
    with pytest.raises(UnknownCallback):
        await stack.webhook().execute(
            token="guess", body=webhook_body(uuid4()), event_id_header=None, ip="1.2.3.4"
        )
    assert stack.queue.checks == []


async def test_malformed_webhooks(stack: PaymentsStack) -> None:
    token = stack.callback_token()
    with pytest.raises(InvalidData):
        await stack.webhook().execute(token=token, body=b"nope", event_id_header=None, ip="ip")
    with pytest.raises(InvalidData):
        await stack.webhook().execute(
            token=token, body=webhook_body(uuid4()), event_id_header="evt_other", ip="ip"
        )
    body = json.dumps({"event": "collection.failed", "event_id": "e2", "data": {}}).encode()
    assert not await stack.webhook().execute(token=token, body=body, event_id_header=None, ip="ip")
    assert stack.queue.checks == []


async def test_the_webhook_is_rate_limited(stack: PaymentsStack) -> None:
    stack.limiter.counts["kprimepay-webhook:9.9.9.9"] = 300
    with pytest.raises(RateLimited):
        await stack.webhook().execute(
            token=stack.callback_token(),
            body=webhook_body(uuid4()),
            event_id_header=None,
            ip="9.9.9.9",
        )


async def test_only_kprimepay_s_own_answer_marks_paid(stack: PaymentsStack) -> None:
    payment = await stack.confirmed(stack.order())
    attempt = payment.attempts[0]
    stack.gateway.settle(payment, PaymentStatus.PENDING)
    assert await stack.verify_attempt(attempt.id) is PaymentStatus.PENDING

    stack.gateway.settle(payment)
    assert await stack.verify_attempt(attempt.id) is PaymentStatus.PAID
    assert stack.gateway.status_calls[-1] == (SANDBOX_KEY, str(attempt.id))
    paid = stack.payment(payment)
    assert paid.attempts[0].status is PaymentStatus.PAID
    [event] = paid_events(stack)
    assert (event.order_id, event.amount, event.method) == (payment.order_id, 10_000, "kprimepay")
    assert "91234567" not in repr(event)


@pytest.mark.parametrize("lie", [{"amount": 100}, {"currency": "EUR"}])
async def test_a_mismatch_never_marks_paid(stack: PaymentsStack, lie: dict[str, object]) -> None:
    payment = await stack.confirmed(stack.order())
    stack.gateway.settle(payment, **lie)  # type: ignore[arg-type]
    assert await stack.verify_attempt(payment.attempts[0].id) is PaymentStatus.PENDING
    assert paid_events(stack) == []


async def test_a_declined_push_offers_the_alternatives(stack: PaymentsStack) -> None:
    payment = await stack.confirmed(stack.order())
    stack.gateway.settle(payment, PaymentStatus.FAILED)
    assert await stack.verify_attempt(payment.attempts[0].id) is PaymentStatus.PENDING
    assert stack.payment(payment).attempts[0].status is PaymentStatus.FAILED
    message, message_id = stack.last_message()
    assert isinstance(message, Buttons) and "91 •• •• 67" in message.body
    assert message_id in stack.payment(payment).prompt_message_ids
    # A late confirmation of that push still wins.
    stack.gateway.settle(payment)
    assert await stack.verify_attempt(payment.attempts[0].id) is PaymentStatus.PAID


async def test_a_push_and_a_link_cannot_pay_twice(stack: PaymentsStack) -> None:
    payment = await stack.confirmed(stack.order())
    _, prompt_id = stack.last_message()
    await stack.tap(f"payment:link:{payment.id}", prompt_id)
    stored = stack.payment(payment)
    stack.gateway.settle(stored, attempt=0)
    stack.gateway.settle(stored, attempt=1)

    assert await stack.verify_attempt(stored.attempts[0].id) is PaymentStatus.PAID
    assert await stack.verify_attempt(stored.attempts[1].id) is PaymentStatus.PAID
    assert len(paid_events(stack)) == 1
    # The other attempt is closed: nothing more to check.
    assert stack.payment(payment).attempts[1].status is PaymentStatus.CANCELLED


async def test_kprimepay_down_lets_the_job_retry(stack: PaymentsStack) -> None:
    payment = await stack.confirmed(stack.order())
    stack.gateway.status_error = ProviderError(ProviderFailure.UNAVAILABLE)
    with pytest.raises(ProviderError):
        await stack.verify_attempt(payment.attempts[0].id)
    assert stack.payment(payment).status is PaymentStatus.PENDING


# Polling


async def test_polling_backs_off_then_expires(stack: PaymentsStack) -> None:
    payment = await stack.confirmed(stack.order())
    assert await stack.poll() == 0  # nothing due yet

    stack.clock.advance(timedelta(minutes=2))
    assert await stack.poll() == 1
    assert len(stack.gateway.status_calls) == 1
    assert await stack.poll() == 0  # next one in 5 minutes

    stack.clock.advance(timedelta(hours=24))
    stack.gateway.settle(payment, PaymentStatus.PENDING)
    await stack.poll()
    stored = stack.payment(payment)
    assert stored.status is PaymentStatus.EXPIRED and stored.next_check_at is None
    assert stored.attempts[0].status is PaymentStatus.EXPIRED


async def test_polling_finds_a_lost_webhook(stack: PaymentsStack) -> None:
    payment = await stack.confirmed(stack.order())
    stack.gateway.settle(payment)
    stack.clock.advance(timedelta(minutes=2))
    await stack.poll()
    assert stack.payment(payment).status is PaymentStatus.PAID


async def test_polling_skips_a_payment_settled_meanwhile(stack: PaymentsStack) -> None:
    payment = await stack.confirmed(stack.order())
    stack.gateway.settle(payment)
    await stack.verify_attempt(payment.attempts[0].id)
    stack.clock.advance(timedelta(minutes=10))
    calls = len(stack.gateway.status_calls)
    assert await stack.poll() == 0
    assert len(stack.gateway.status_calls) == calls


# Manual payments


async def test_a_manual_payment_cancels_the_pending_one(stack: PaymentsStack) -> None:
    order_id = stack.order(total=15_000)
    pending = await stack.confirmed(order_id)

    manual = await stack.manual(order_id, reference="MM-42", note="paid in cash")

    assert manual.status is PaymentStatus.PAID and manual.method is PaymentMethod.MANUAL
    assert manual.amount.value == 15_000
    cancelled = stack.payment(pending)
    assert cancelled.status is PaymentStatus.CANCELLED
    assert cancelled.attempts[0].status is PaymentStatus.CANCELLED
    entry = stack.audit_log.entries[-1]
    assert entry.action == "payment.recorded"
    assert entry.account_id == stack.account_id
    assert entry.details["cancelled_payments"] == [str(pending.id)]
    [event] = paid_events(stack)
    assert event.method == "manual" and event.order_id == order_id


async def test_one_paid_payment_per_order(stack: PaymentsStack) -> None:
    order_id = stack.order()
    await stack.manual(order_id)
    with pytest.raises(OrderAlreadyPaid):
        await stack.manual(order_id)
    with pytest.raises(OrderAlreadyPaid):
        await stack.request(order_id)


async def test_paying_the_cancelled_push_too_is_only_logged(stack: PaymentsStack) -> None:
    order_id = stack.order()
    pending = await stack.confirmed(order_id)
    await stack.manual(order_id)
    stack.gateway.settle(pending)
    assert await stack.verify_attempt(pending.attempts[0].id) is PaymentStatus.CANCELLED
    assert len(paid_events(stack)) == 1


async def test_a_manual_payment_needs_a_confirmed_order(stack: PaymentsStack) -> None:
    with pytest.raises(OrderNotPayable):
        await stack.manual(stack.order(status="AWAITING_MERCHANT"))
    with pytest.raises(OrderNotFound):
        await stack.manual(uuid4())


async def test_manual_payments_work_without_kprimepay() -> None:
    stack = PaymentsStack()
    payment = await stack.manual(stack.order())
    assert payment.status is PaymentStatus.PAID
