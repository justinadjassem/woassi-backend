"""In-memory adapters for the payments module, and a stack wiring them to the real use cases."""

from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Self
from uuid import UUID, uuid4

from cryptography.fernet import Fernet

from tests.fakes import FakeAuditLog, FakeRateLimiter
from woassi.modules.payments.application.ports import (
    Checkout,
    CheckoutRequest,
    Contact,
    DuplicatePayment,
    Notification,
    NotificationNotSent,
    PayableOrder,
    ProviderError,
    ProviderFailure,
    ProviderTransaction,
    PushRequest,
    ShopInfo,
)
from woassi.modules.payments.application.use_cases.conversation import (
    ReceivePayerNumber,
    ReplyToPayment,
)
from woassi.modules.payments.application.use_cases.flow import CheckoutSettings, PaymentFlow
from woassi.modules.payments.application.use_cases.manual import RecordManualPayment
from woassi.modules.payments.application.use_cases.provider import ConfigurePaymentProvider
from woassi.modules.payments.application.use_cases.requests import RequestPayment
from woassi.modules.payments.application.use_cases.verification import (
    PollPendingPayments,
    ReceiveKPrimePayWebhook,
    VerifyPayment,
)
from woassi.modules.payments.domain.payment import Channel, Payment, PaymentStatus
from woassi.modules.payments.domain.provider_account import ProviderAccount
from woassi.shared.domain.events import DomainEvent
from woassi.shared.infrastructure.clock import FrozenClock
from woassi.shared.infrastructure.crypto import FernetSecretCipher
from woassi.shared.infrastructure.phone import PhonenumbersNormalizer

SANDBOX_KEY = "kpp_sbk_" + "s3cr3t" * 4
LIVE_KEY = "kpp_lk_" + "l1v3" * 6
CHECKOUT_URL = "https://payments.kprimepay.com/checkout/web/abc"


class FakePayments:
    def __init__(self, uow: "FakePaymentsUoW") -> None:
        self._uow = uow

    def _check_unique(self, payment: Payment) -> None:
        for other in self._uow.state.values():
            if (
                other.id != payment.id
                and other.order_id == payment.order_id
                and other.status is payment.status
                and payment.status in (PaymentStatus.PAID, PaymentStatus.PENDING)
            ):
                raise DuplicatePayment(payment.status.value)

    def _store(self, payment: Payment) -> None:
        self._check_unique(payment)
        stored = deepcopy(payment)
        stored.pull_events()
        self._uow.state[payment.id] = stored
        self._uow.tracked.append(payment)

    async def add(self, payment: Payment) -> None:
        self._store(payment)

    async def get(self, shop_id: UUID, payment_id: UUID, *, lock: bool = False) -> Payment | None:
        payment = self._uow.state.get(payment_id)
        return deepcopy(payment) if payment and payment.shop_id == shop_id else None

    async def get_by_attempt(
        self, shop_id: UUID, attempt_id: UUID, *, lock: bool = False
    ) -> Payment | None:
        for payment in self._uow.state.values():
            if payment.shop_id == shop_id and any(a.id == attempt_id for a in payment.attempts):
                return deepcopy(payment)
        return None

    async def awaiting_payer(self, shop_id: UUID, customer_id: UUID) -> Payment | None:
        found = [
            p
            for p in self._uow.state.values()
            if p.shop_id == shop_id
            and p.customer_id == customer_id
            and p.awaiting_payer
            and p.is_pending
        ]
        return deepcopy(max(found, key=lambda p: p.created_at)) if found else None

    async def for_order(
        self, shop_id: UUID, order_id: UUID, *, lock: bool = False
    ) -> Sequence[Payment]:
        found = [
            p for p in self._uow.state.values() if p.shop_id == shop_id and p.order_id == order_id
        ]
        return deepcopy(sorted(found, key=lambda p: p.created_at))

    def _matching(
        self, shop_id: UUID, status: PaymentStatus | None, order_id: UUID | None
    ) -> list[Payment]:
        return [
            p
            for p in self._uow.state.values()
            if p.shop_id == shop_id
            and (status is None or p.status is status)
            and (order_id is None or p.order_id == order_id)
        ]

    async def list(
        self,
        shop_id: UUID,
        status: PaymentStatus | None,
        order_id: UUID | None,
        offset: int,
        limit: int,
    ) -> Sequence[Payment]:
        found = sorted(self._matching(shop_id, status, order_id), key=lambda p: p.created_at)
        return deepcopy(list(reversed(found))[offset : offset + limit])

    async def count(
        self, shop_id: UUID, status: PaymentStatus | None, order_id: UUID | None
    ) -> int:
        return len(self._matching(shop_id, status, order_id))

    async def list_due(self, now: datetime, limit: int) -> Sequence[tuple[UUID, UUID]]:
        due = [p for p in self._uow.state.values() if p.is_due(now)]
        due.sort(key=lambda p: p.next_check_at)  # type: ignore[arg-type, return-value]
        return [(p.shop_id, p.id) for p in due[:limit]]

    async def save(self, payment: Payment) -> None:
        self._store(payment)


class FakeProviderAccounts:
    def __init__(self, uow: "FakePaymentsUoW") -> None:
        self._uow = uow

    async def get(self, shop_id: UUID) -> ProviderAccount | None:
        return deepcopy(self._uow.accounts.get(shop_id))

    async def get_by_token_hash(self, token_hash: str) -> ProviderAccount | None:
        for account in self._uow.accounts.values():
            if account.callback_token_hash == token_hash:
                return deepcopy(account)
        return None

    async def save(self, account: ProviderAccount) -> None:
        self._uow.accounts[account.shop_id] = deepcopy(account)

    async def delete(self, shop_id: UUID) -> None:
        self._uow.accounts.pop(shop_id, None)


class FakePaymentsInbox:
    def __init__(self, uow: "FakePaymentsUoW") -> None:
        self._uow = uow

    async def claim(self, source: str, key: str) -> bool:
        if (source, key) in self._uow.claimed:
            return False
        self._uow.claimed.add((source, key))
        return True


class FakePaymentsUoW:
    """Works on a copy of the stack's state: what is not committed is lost."""

    def __init__(self, stack: "PaymentsStack") -> None:
        self.stack = stack
        self.state: dict[UUID, Payment] = {}
        self.accounts: dict[UUID, ProviderAccount] = {}
        self.claimed: set[tuple[str, str]] = set()
        self.tracked: list[Payment] = []

    @property
    def payments(self) -> FakePayments:
        return FakePayments(self)

    @property
    def provider_accounts(self) -> FakeProviderAccounts:
        return FakeProviderAccounts(self)

    @property
    def inbox(self) -> FakePaymentsInbox:
        return FakePaymentsInbox(self)

    @property
    def audit(self) -> FakeAuditLog:
        return self.stack.audit_log

    def _load(self) -> None:
        self.state = deepcopy(self.stack.payments)
        self.accounts = deepcopy(self.stack.accounts)
        self.claimed = set(self.stack.claimed)
        self.tracked = []

    async def __aenter__(self) -> Self:
        self._load()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.tracked = []

    async def commit(self) -> None:
        self.stack.payments = deepcopy(self.state)
        self.stack.accounts = deepcopy(self.accounts)
        self.stack.claimed = set(self.claimed)
        for payment in self.tracked:
            self.stack.events.extend(payment.pull_events())
        self.tracked = []

    async def rollback(self) -> None:
        self._load()


@dataclass
class FakeGateway:
    """KPrimePay: checkouts it created, and what `debit-status` answers for each payment."""

    clock: FrozenClock
    refuse_key: bool = False
    unavailable: bool = False
    checkout_error: ProviderError | None = None
    push_error: ProviderError | None = None
    pushes: list[tuple[str, PushRequest]] = field(default_factory=list)
    status_error: ProviderError | None = None
    checkouts: list[tuple[str, CheckoutRequest]] = field(default_factory=list)
    transactions: dict[str, ProviderTransaction] = field(default_factory=dict)
    status_calls: list[tuple[str, str]] = field(default_factory=list)
    verified_keys: list[str] = field(default_factory=list)

    async def verify_key(self, api_key: str) -> None:
        if self.unavailable:
            raise ProviderError(ProviderFailure.UNAVAILABLE)
        if self.refuse_key:
            raise ProviderError(ProviderFailure.INVALID_KEY, code="INVALID_AUTH_TOKEN")
        self.verified_keys.append(api_key)

    async def create_checkout(self, api_key: str, request: CheckoutRequest) -> Checkout:
        if self.checkout_error is not None:
            raise self.checkout_error
        self.checkouts.append((api_key, request))
        return Checkout(
            reference=f"KPP_{request.transaction_id[:8]}",
            url=CHECKOUT_URL,
            expires_at=self.clock.now() + timedelta(hours=24),
        )

    async def ussd_push(self, api_key: str, request: PushRequest) -> str | None:
        if self.push_error is not None:
            raise self.push_error
        self.pushes.append((api_key, request))
        return None

    async def transaction(self, api_key: str, transaction_id: str) -> ProviderTransaction:
        self.status_calls.append((api_key, transaction_id))
        if self.status_error is not None:
            raise self.status_error
        found = self.transactions.get(transaction_id)
        if found is None:
            raise ProviderError(ProviderFailure.NOT_FOUND, code="TRANSACTION_NOT_FOUND")
        return found

    def settle(
        self,
        payment: Payment,
        status: PaymentStatus = PaymentStatus.PAID,
        *,
        attempt: int = -1,
        amount: int | None = None,
        currency: str = "XOF",
    ) -> None:
        """What KPrimePay will answer for one attempt of `payment` (the last by default)."""
        transaction_id = str(payment.attempts[attempt].id)
        self.transactions[transaction_id] = ProviderTransaction(
            transaction_id=transaction_id,
            status=status,
            amount=payment.amount.value if amount is None else amount,
            currency=currency,
        )


class FakeOrdersPort:
    def __init__(self) -> None:
        self.orders: dict[UUID, PayableOrder] = {}

    async def get(self, shop_id: UUID, order_id: UUID) -> PayableOrder | None:
        order = self.orders.get(order_id)
        return order if order and order.shop_id == shop_id else None


class FakeShopsPort:
    def __init__(self, info: ShopInfo | None) -> None:
        self.current = info

    async def info(self, shop_id: UUID) -> ShopInfo | None:
        return self.current


class FakeCustomersPort:
    def __init__(self) -> None:
        self.contacts: dict[UUID, Contact] = {}

    async def contact(self, shop_id: UUID, customer_id: UUID) -> Contact:
        return self.contacts.get(customer_id, Contact())


class RecordingPaymentMessaging:
    """Refuses free text outside the window, like Meta, and templates if told so."""

    def __init__(self, clock: FrozenClock) -> None:
        self.clock = clock
        self.sent: list[tuple[Contact, Notification, str]] = []
        self.refuse_templates = False
        self.refuse_texts = False
        self.failing = False

    async def send(
        self, shop_id: UUID, to: Contact, notification: Notification, *, kind: str
    ) -> str:
        from woassi.modules.payments.application.ports import Buttons, Template, Text

        if self.failing:
            raise NotificationNotSent("messaging.message_not_sent")
        if isinstance(notification, Text | Buttons) and (
            self.refuse_texts
            or to.last_message_at is None
            or self.clock.now() - to.last_message_at > timedelta(hours=24)
        ):
            raise NotificationNotSent("messaging.outside_customer_service_window")
        if isinstance(notification, Template) and self.refuse_templates:
            raise NotificationNotSent("messaging.template_not_approved")
        self.sent.append((to, notification, kind))
        return f"wamid.{len(self.sent)}"


class FakeCheckQueue:
    def __init__(self) -> None:
        self.checks: list[tuple[UUID, UUID]] = []

    async def enqueue(self, shop_id: UUID, payment_id: UUID) -> None:
        self.checks.append((shop_id, payment_id))


class PaymentsStack:
    """Real payments use cases on in-memory adapters."""

    def __init__(self, *, language: str = "fr", callback_base_url: str | None = None) -> None:
        self.shop_id = uuid4()
        self.customer_id = uuid4()
        self.account_id = uuid4()
        self.clock = FrozenClock(datetime(2026, 1, 1, 9, tzinfo=UTC))
        self.payments: dict[UUID, Payment] = {}
        self.accounts: dict[UUID, ProviderAccount] = {}
        self.claimed: set[tuple[str, str]] = set()
        self.events: list[DomainEvent] = []
        self.audit_log = FakeAuditLog()
        self.cipher = FernetSecretCipher([Fernet.generate_key().decode()])
        self.phones = PhonenumbersNormalizer()
        self.gateway = FakeGateway(self.clock)
        self.orders = FakeOrdersPort()
        self.shops = FakeShopsPort(ShopInfo(name="Chez Ama", language=language))
        self.customers = FakeCustomersPort()
        self.customers.contacts[self.customer_id] = Contact(
            user_id="TG.123", phone="+22891234567", last_message_at=self.clock.now()
        )
        self.messaging = RecordingPaymentMessaging(self.clock)
        self.queue = FakeCheckQueue()
        self.limiter = FakeRateLimiter()
        self.settings = CheckoutSettings(
            return_url="https://api.example.com/payments/return",
            callback_base_url=callback_base_url,
        )

    def uow(self) -> FakePaymentsUoW:
        return FakePaymentsUoW(self)

    def flow[F: PaymentFlow](self, use_case: type[F]) -> F:
        return use_case(
            self.uow(),
            self.gateway,
            self.orders,
            self.shops,
            self.customers,
            self.messaging,
            self.cipher,
            self.phones,
            self.clock,
            self.settings,
        )

    def order(self, *, total: int = 10_000, status: str = "CONFIRMED", number: int = 1) -> UUID:
        order_id = uuid4()
        self.orders.orders[order_id] = PayableOrder(
            order_id=order_id,
            shop_id=self.shop_id,
            number=number,
            total=total,
            status=status,
            customer_id=self.customer_id,
        )
        return order_id

    async def configure(self, key: str = SANDBOX_KEY) -> None:
        await ConfigurePaymentProvider(self.uow(), self.gateway, self.cipher, self.clock).execute(
            shop_id=self.shop_id, account_id=self.account_id, api_key=key
        )

    def callback_token(self) -> str:
        return self.cipher.decrypt(self.accounts[self.shop_id].encrypted_callback_token)

    async def request(self, order_id: UUID, channel: Channel | None = Channel.CHECKOUT) -> Payment:
        """A checkout link by default; `channel=None` is what follows an order confirmation."""
        return await self.flow(RequestPayment).execute(
            shop_id=self.shop_id, order_id=order_id, channel=channel
        )

    async def confirmed(self, order_id: UUID) -> Payment:
        return await self.request(order_id, channel=None)

    async def tap(self, payload: str, context: str | None) -> bool:
        return await self.flow(ReplyToPayment).execute(
            shop_id=self.shop_id, payload=payload, context_message_id=context
        )

    async def type(self, body: str) -> bool:
        return await self.flow(ReceivePayerNumber).execute(
            shop_id=self.shop_id, customer_id=self.customer_id, body=body
        )

    def last_message(self) -> tuple[Notification, str]:
        """(notification, message id) of the last message the customer got."""
        _, notification, _ = self.messaging.sent[-1]
        return notification, f"wamid.{len(self.messaging.sent)}"

    async def manual(self, order_id: UUID, **options: str) -> Payment:
        return await RecordManualPayment(self.uow(), self.orders, self.clock).execute(
            shop_id=self.shop_id,
            order_id=order_id,
            account_id=self.account_id,
            **options,  # type: ignore[arg-type]
        )

    async def verify_attempt(self, attempt_id: UUID) -> PaymentStatus | None:
        """The worker job after a webhook naming this transaction id."""
        return await self.flow(VerifyPayment).execute(shop_id=self.shop_id, payment_id=attempt_id)

    async def poll(self) -> int:
        return await self.flow(PollPendingPayments).execute()

    def webhook(self) -> ReceiveKPrimePayWebhook:
        return ReceiveKPrimePayWebhook(self.uow(), self.queue, self.limiter)

    def payment(self, payment: Payment) -> Payment:
        return self.payments[payment.id]
