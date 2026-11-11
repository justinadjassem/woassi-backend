import pickle
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from woassi.modules.payments.domain.errors import (
    AmountOutOfRange,
    InvalidPaymentTransition,
    InvalidProviderKey,
    PushLimitReached,
)
from woassi.modules.payments.domain.events import PaymentConfirmed
from woassi.modules.payments.domain.mobile_money import Operator, togolese_mobile
from woassi.modules.payments.domain.payment import (
    Channel,
    Payment,
    PaymentMethod,
    PaymentStatus,
)
from woassi.modules.payments.domain.provider_account import ProviderMode, mode_of
from woassi.shared.domain.values import Amount

NOW = datetime(2026, 1, 1, 9, tzinfo=UTC)
MIXX = togolese_mobile("+22890123456")


def pending(amount: int = 10_000) -> Payment:
    return Payment.request(
        shop_id=uuid4(), order_id=uuid4(), customer_id=uuid4(), amount=Amount(amount), now=NOW
    )


def test_mode_comes_from_the_key_prefix() -> None:
    assert mode_of("kpp_sbk_abcdef123") is ProviderMode.SANDBOX
    assert mode_of("kpp_lk_abcdef123") is ProviderMode.LIVE


@pytest.mark.parametrize("key", ["sk_live_abcdef", "kpp_lk_abc def1", "kpp_sbk_" + "x" * 300])
def test_other_keys_are_refused_without_being_quoted(key: str) -> None:
    with pytest.raises(InvalidProviderKey) as error:
        mode_of(key)
    assert key not in error.value.message


def test_a_bare_prefix_is_no_key() -> None:
    with pytest.raises(InvalidProviderKey):
        mode_of("kpp_sbk_")


@pytest.mark.parametrize(
    ("phone", "operator"),
    [
        ("+22870123456", Operator.MIXX_YAS_TG),
        ("+22873123456", Operator.MIXX_YAS_TG),
        ("+22890123456", Operator.MIXX_YAS_TG),
        ("22893123456", Operator.MIXX_YAS_TG),
        ("+22879123456", Operator.MOOV_MONEY_TG),
        ("+22896123456", Operator.MOOV_MONEY_TG),
        ("+22899123456", Operator.MOOV_MONEY_TG),
    ],
)
def test_the_operator_comes_from_the_prefix(phone: str, operator: Operator) -> None:
    number = togolese_mobile(phone)
    assert number is not None and number.operator is operator
    assert len(number.local) == 8


@pytest.mark.parametrize(
    "phone", [None, "", "+22522123456", "+22822123456", "+2289012345", "+228901234567"]
)
def test_only_togolese_mobiles(phone: str | None) -> None:
    assert togolese_mobile(phone) is None


def test_the_number_is_masked_and_out_of_the_repr() -> None:
    assert MIXX is not None
    assert MIXX.masked == "90 •• •• 56"
    assert "90123456" not in repr(MIXX)


@pytest.mark.parametrize("amount", [9, 2_000_001])
def test_kprimepay_bounds(amount: int) -> None:
    with pytest.raises(AmountOutOfRange):
        pending(amount)


def test_attempts_have_their_own_transaction_ids() -> None:
    assert MIXX is not None
    payment = pending()
    assert payment.expires_at == NOW + timedelta(hours=24)
    push = payment.add_attempt(Channel.USSD_PUSH, NOW, payer=MIXX)
    link = payment.add_attempt(Channel.CHECKOUT, NOW)

    assert push.id != link.id != payment.id
    assert push.operator is Operator.MIXX_YAS_TG and push.payer_phone == "+22890123456"
    assert payment.payer_phone == "+22890123456"
    assert "+22890123456" not in repr(payment)
    assert payment.open_checkout(NOW) is link and not link.submitted

    later = NOW + timedelta(hours=30)
    payment.attempt_submitted(
        link.id, reference="KPP_1", url="https://pay/x", expires_at=later, now=NOW
    )
    assert payment.checkout_url == "https://pay/x"
    assert payment.expires_at == later  # the checkout lives longer
    assert payment.pull_events() == []  # no event until paid


def test_at_most_three_pushes() -> None:
    assert MIXX is not None
    payment = pending()
    for _ in range(3):
        payment.add_attempt(Channel.USSD_PUSH, NOW, payer=MIXX)
    assert not payment.can_push
    with pytest.raises(PushLimitReached):
        payment.add_attempt(Channel.USSD_PUSH, NOW, payer=MIXX)
    payment.add_attempt(Channel.CHECKOUT, NOW)  # a link is still possible


def test_one_paid_attempt_pays_the_payment_without_personal_data() -> None:
    assert MIXX is not None
    payment = pending()
    push = payment.add_attempt(Channel.USSD_PUSH, NOW, payer=MIXX)
    payment.attempt_paid(push.id, NOW)

    assert payment.status is PaymentStatus.PAID and payment.paid_at == NOW
    assert push.status is PaymentStatus.PAID
    assert payment.next_check_at is None
    [event] = payment.pull_events()
    assert isinstance(event, PaymentConfirmed)
    assert (event.payment_id, event.order_id, event.shop_id) == (
        payment.id,
        payment.order_id,
        payment.shop_id,
    )
    assert (event.amount, event.method, event.paid_at) == (10_000, "kprimepay", NOW)
    assert "90123456" not in repr(event)
    assert pickle.loads(pickle.dumps(event)) == event


def test_a_failed_attempt_leaves_the_payment_open() -> None:
    assert MIXX is not None
    payment = pending()
    push = payment.add_attempt(Channel.USSD_PUSH, NOW, payer=MIXX)
    payment.attempt_failed(push.id, "provider_failed", NOW)
    assert push.status is PaymentStatus.FAILED and payment.is_pending
    # A late confirmation of that same attempt still pays.
    payment.attempt_paid(push.id, NOW)
    assert payment.status is PaymentStatus.PAID


def test_statuses_only_move_forward() -> None:
    paid = pending()
    paid.confirm(NOW)
    for move in (paid.cancel, paid.expire, lambda now: paid.fail("x", now), paid.confirm):
        with pytest.raises(InvalidPaymentTransition):
            move(NOW)

    cancelled = pending()
    link = cancelled.add_attempt(Channel.CHECKOUT, NOW)
    cancelled.cancel(NOW)
    assert link.status is PaymentStatus.CANCELLED
    with pytest.raises(InvalidPaymentTransition):
        cancelled.attempt_paid(link.id, NOW)


def test_a_late_success_still_counts_after_expiry() -> None:
    payment = pending()
    link = payment.add_attempt(Channel.CHECKOUT, NOW)
    payment.expire(NOW)
    assert link.status is PaymentStatus.EXPIRED and not payment.awaiting_payer
    payment.attempt_paid(link.id, NOW + timedelta(hours=1))
    assert payment.status is PaymentStatus.PAID


def test_buttons_are_trusted_only_on_our_messages() -> None:
    payment = pending()
    payment.record_prompt("wamid.1")
    assert payment.trusts("wamid.1")
    assert not payment.trusts("wamid.2") and not payment.trusts(None)


def test_a_manual_payment_is_paid_at_once() -> None:
    account = uuid4()
    payment = Payment.manual(
        shop_id=uuid4(),
        order_id=uuid4(),
        amount=Amount(5),  # no KPrimePay bounds by hand
        recorded_by=account,
        reference="MM-1",
        note="cash",
        now=NOW,
    )
    assert payment.status is PaymentStatus.PAID
    assert payment.method is PaymentMethod.MANUAL
    assert payment.recorded_by == account
    [event] = payment.pull_events()
    assert isinstance(event, PaymentConfirmed) and event.method == "manual"
    assert "cash" not in repr(payment) and "MM-1" not in repr(payment)


def test_checks_back_off_and_end_at_the_deadline() -> None:
    payment = pending()
    link = payment.add_attempt(Channel.CHECKOUT, NOW)
    expires = NOW + timedelta(minutes=40)
    payment.expires_at = expires
    payment.attempt_submitted(link.id, reference=None, url="https://pay/x", now=NOW)
    moments = []
    now = NOW
    for _ in range(5):
        payment.checked(now)
        assert payment.next_check_at is not None
        moments.append(payment.next_check_at - now)
        now = payment.next_check_at
    assert moments[:3] == [timedelta(minutes=2), timedelta(minutes=5), timedelta(minutes=10)]
    # The 30-minute step is cut to the deadline; past it, hourly.
    assert NOW + sum(moments[:4], timedelta()) == expires
    assert moments[4] == timedelta(hours=1)
