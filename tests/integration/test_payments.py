import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.payments_fakes import SANDBOX_KEY, PaymentsStack
from woassi.modules.payments.application.ports import DuplicatePayment
from woassi.modules.payments.application.use_cases.manual import RecordManualPayment
from woassi.modules.payments.application.use_cases.provider import ConfigurePaymentProvider
from woassi.modules.payments.application.use_cases.verification import (
    PollPendingPayments,
    ReceiveKPrimePayWebhook,
)
from woassi.modules.payments.domain.errors import OrderAlreadyPaid
from woassi.modules.payments.domain.events import PaymentConfirmed
from woassi.modules.payments.application.ports import Contact
from woassi.modules.payments.domain.mobile_money import Operator
from woassi.modules.payments.domain.payment import Channel, Payment, PaymentStatus
from woassi.modules.payments.infrastructure.uow import SqlAlchemyPaymentsUoW
from woassi.shared.domain.events import DomainEvent
from woassi.shared.domain.values import Amount

pytestmark = pytest.mark.integration


class RecordingBus:
    def __init__(self) -> None:
        self.events: list[DomainEvent] = []

    async def publish(self, event: DomainEvent) -> None:
        self.events.append(event)


class Real(PaymentsStack):
    """The payments stack on Postgres, with fakes for KPrimePay and the other modules."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(callback_base_url="https://api.example.com")
        self.sessions = sessions
        self.bus = RecordingBus()

    def uow(self) -> SqlAlchemyPaymentsUoW:  # type: ignore[override]
        return SqlAlchemyPaymentsUoW(self.sessions, self.bus, self.clock)

    async def load(self, payment: Payment) -> Payment:
        async with self.uow() as uow:
            found = await uow.payments.get(self.shop_id, payment.id)
        assert found is not None
        return found

    async def configure(self, key: str = SANDBOX_KEY) -> None:
        await ConfigurePaymentProvider(self.uow(), self.gateway, self.cipher, self.clock).execute(
            shop_id=self.shop_id, account_id=self.account_id, api_key=key
        )
        async with self.uow() as uow:
            account = await uow.provider_accounts.get(self.shop_id)
        assert account is not None
        self.token = self.cipher.decrypt(account.encrypted_callback_token)

    def callback_token(self) -> str:
        return self.token


@pytest.fixture
async def real(sessions: async_sessionmaker[AsyncSession]) -> Real:
    real = Real(sessions)
    await real.configure()
    return real


async def test_the_database_allows_one_paid_payment_per_order(real: Real) -> None:
    order_id = uuid4()
    for _ in range(2):
        payment = Payment.manual(
            shop_id=real.shop_id,
            order_id=order_id,
            amount=Amount(100),
            recorded_by=real.account_id,
            reference=None,
            note=None,
            now=real.clock.now(),
        )
        async with real.uow() as uow:
            try:
                await uow.payments.add(payment)
            except DuplicatePayment:
                break
            await uow.commit()
    else:
        pytest.fail("A second PAID payment was accepted.")


async def test_concurrent_manual_payments_pay_once(real: Real) -> None:
    order_id = real.order(total=7_000)

    async def record() -> Payment | Exception:
        try:
            return await RecordManualPayment(real.uow(), real.orders, real.clock).execute(
                shop_id=real.shop_id, order_id=order_id, account_id=real.account_id
            )
        except OrderAlreadyPaid as error:
            return error

    results = await asyncio.gather(*(record() for _ in range(5)))
    paid = [r for r in results if isinstance(r, Payment)]
    assert len(paid) == 1
    assert all(isinstance(r, OrderAlreadyPaid) for r in results if r not in paid)
    assert len([e for e in real.bus.events if isinstance(e, PaymentConfirmed)]) == 1


async def test_round_trip_and_verification(real: Real) -> None:
    order_id = real.order(total=12_000)
    payment = await real.confirmed(order_id)  # a push to the customer's number
    _, prompt_id = real.last_message()
    assert await real.tap(f"payment:link:{payment.id}", prompt_id)

    stored = await real.load(payment)
    push, link = stored.attempts
    assert (push.channel, push.operator, push.payer_phone) == (
        Channel.USSD_PUSH,
        Operator.MIXX_YAS_TG,
        "+22891234567",
    )
    assert link.checkout_url and link.provider_reference and link.expires_at
    assert stored.payer_phone == "+22891234567"
    assert stored.prompt_message_ids == [prompt_id]

    real.gateway.settle(stored, attempt=0)
    assert await real.verify_attempt(push.id) is PaymentStatus.PAID
    paid = await real.load(payment)
    assert paid.paid_at is not None and paid.next_check_at is None
    assert [a.status for a in paid.attempts] == [PaymentStatus.PAID, PaymentStatus.CANCELLED]
    [event] = [e for e in real.bus.events if isinstance(e, PaymentConfirmed)]
    assert event.amount == 12_000


async def test_a_typed_number_finds_the_waiting_payment(real: Real) -> None:
    real.customers.contacts[real.customer_id] = Contact(
        user_id="TG.1", last_message_at=real.clock.now()
    )
    payment = await real.confirmed(real.order())
    assert (await real.load(payment)).awaiting_payer

    assert await real.type("96 12 34 56")

    stored = await real.load(payment)
    assert not stored.awaiting_payer
    assert stored.attempts[0].operator is Operator.MOOV_MONEY_TG
    async with real.uow() as uow:
        assert await uow.payments.awaiting_payer(real.shop_id, real.customer_id) is None
        found = await uow.payments.get_by_attempt(real.shop_id, stored.attempts[0].id)
    assert found is not None and found.id == payment.id


async def test_concurrent_pollers_check_once(real: Real) -> None:
    payment = await real.confirmed(real.order())
    real.gateway.settle(payment, PaymentStatus.PENDING)
    real.clock.advance(timedelta(minutes=3))

    counts = await asyncio.gather(*(real.flow(PollPendingPayments).execute() for _ in range(3)))

    # Under the row lock, the first poller moves the next check; the others see it not due.
    assert sum(counts) == 1
    assert len(real.gateway.status_calls) == 1
    assert (await real.load(payment)).checks == 1


async def test_webhook_events_are_claimed_once(real: Real) -> None:
    payment = await real.request(real.order())
    transaction_id = payment.attempts[0].id
    body = (
        '{"event": "collection.succeeded", "event_id": "evt_1", '
        f'"data": {{"transaction_id": "{transaction_id}"}}}}'
    ).encode()

    async def receive() -> bool:
        return await ReceiveKPrimePayWebhook(real.uow(), real.queue, real.limiter).execute(
            token=real.callback_token(), body=body, event_id_header="evt_1", ip="1.1.1.1"
        )

    results = await asyncio.gather(*(receive() for _ in range(3)))
    assert sorted(results) == [False, False, True]
    assert real.queue.checks == [(real.shop_id, transaction_id)]
