import json
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, Header
from fastapi.testclient import TestClient

from tests.fakes import FakeIdempotencyStore
from tests.payments_fakes import LIVE_KEY, SANDBOX_KEY, PaymentsStack
from woassi.config import Settings
from woassi.main import create_app
from woassi.modules.payments.api.deps import get_payments
from woassi.modules.payments.application.use_cases.manual import RecordManualPayment
from woassi.modules.payments.application.use_cases.provider import (
    ConfigurePaymentProvider,
    GetPaymentProvider,
    RemovePaymentProvider,
)
from woassi.modules.payments.application.use_cases.queries import GetPayment, ListPayments
from woassi.modules.payments.application.use_cases.requests import RequestPayment
from woassi.modules.payments.application.use_cases.verification import ReceiveKPrimePayWebhook
from woassi.modules.payments.domain.payment import PaymentStatus
from woassi.shared.api.deps import get_authorized_shop, get_current_account, get_shared
from woassi.shared.application.ports import AuthenticatedAccount
from woassi.shared.domain.errors import ShopNotFound

ACCOUNT = uuid4()


class Facade:
    def __init__(self, stack: PaymentsStack) -> None:
        self.stack = stack

    def configure_payment_provider(self) -> ConfigurePaymentProvider:
        s = self.stack
        return ConfigurePaymentProvider(s.uow(), s.gateway, s.cipher, s.clock)

    def get_payment_provider(self) -> GetPaymentProvider:
        return GetPaymentProvider(self.stack.uow())

    def remove_payment_provider(self) -> RemovePaymentProvider:
        return RemovePaymentProvider(self.stack.uow())

    def list_payments(self) -> ListPayments:
        return ListPayments(self.stack.uow())

    def get_payment(self) -> GetPayment:
        return GetPayment(self.stack.uow())

    def record_manual_payment(self) -> RecordManualPayment:
        return RecordManualPayment(self.stack.uow(), self.stack.orders, self.stack.clock)

    def request_payment(self) -> RequestPayment:
        return self.stack.flow(RequestPayment)

    def receive_kprimepay_webhook(self) -> ReceiveKPrimePayWebhook:
        return self.stack.webhook()


class FakeShared:
    def __init__(self, store: FakeIdempotencyStore) -> None:
        self.store = store

    def idempotency_store(self) -> FakeIdempotencyStore:
        return self.store


@pytest.fixture
def stack() -> PaymentsStack:
    return PaymentsStack(callback_base_url="https://api.example.com")


def build(settings: Settings, stack: PaymentsStack, *, authenticated: bool = True) -> TestClient:
    app: FastAPI = create_app(settings)
    app.dependency_overrides[get_payments] = lambda: Facade(stack)
    shared = FakeShared(FakeIdempotencyStore())
    app.dependency_overrides[get_shared] = lambda: shared
    if authenticated:
        app.dependency_overrides[get_current_account] = lambda: AuthenticatedAccount(id=ACCOUNT)

        def only_this_shop(shop_id: UUID, x_test: str = Header(default="")) -> UUID:
            if shop_id != stack.shop_id:
                raise ShopNotFound("not yours")
            return shop_id

        app.dependency_overrides[get_authorized_shop] = only_this_shop
    return TestClient(app)


@pytest.fixture
def client(settings: Settings, stack: PaymentsStack) -> TestClient:
    return build(settings, stack)


def base(stack: PaymentsStack) -> str:
    return f"/shops/{stack.shop_id}"


def test_routes_need_authentication(settings: Settings, stack: PaymentsStack) -> None:
    client = build(settings, stack, authenticated=False)
    for method, path in [
        ("GET", "/payment-provider"),
        ("PUT", "/payment-provider"),
        ("GET", "/payments"),
        ("POST", f"/orders/{uuid4()}/payments/manual"),
        ("POST", f"/orders/{uuid4()}/payments/request"),
    ]:
        response = client.request(method, base(stack) + path, json={"api_key": SANDBOX_KEY})
        assert response.status_code == 401, path


def test_a_foreign_shop_is_not_found(client: TestClient) -> None:
    other = f"/shops/{uuid4()}"
    assert client.get(f"{other}/payment-provider").status_code == 404
    assert client.get(f"{other}/payments").status_code == 404
    response = client.post(f"{other}/orders/{uuid4()}/payments/manual", json={})
    assert response.status_code == 404


def test_the_key_is_never_returned(client: TestClient, stack: PaymentsStack) -> None:
    assert client.get(f"{base(stack)}/payment-provider").json() == {
        "configured": False,
        "provider": "kprimepay",
        "mode": None,
        "updated_at": None,
    }

    put = client.put(f"{base(stack)}/payment-provider", json={"api_key": f"  {LIVE_KEY} "})
    assert put.status_code == 200
    got = client.get(f"{base(stack)}/payment-provider")
    for response in (put, got):
        assert response.json()["configured"] is True
        assert response.json()["mode"] == "live"
        assert set(response.json()) == {"configured", "provider", "mode", "updated_at"}
        assert LIVE_KEY not in response.text
        assert stack.callback_token() not in response.text
    assert stack.gateway.verified_keys == [LIVE_KEY]

    assert client.delete(f"{base(stack)}/payment-provider").status_code == 204
    assert client.get(f"{base(stack)}/payment-provider").json()["configured"] is False
    actions = [entry.action for entry in stack.audit_log.entries]
    assert actions == ["payment_provider.updated", "payment_provider.removed"]


def test_a_refused_key(client: TestClient, stack: PaymentsStack) -> None:
    stack.gateway.refuse_key = True
    response = client.put(f"{base(stack)}/payment-provider", json={"api_key": SANDBOX_KEY})
    assert response.status_code == 422
    assert response.json()["code"] == "payments.invalid_provider_key"
    assert SANDBOX_KEY not in response.text
    stack.gateway.refuse_key, stack.gateway.unavailable = False, True
    response = client.put(f"{base(stack)}/payment-provider", json={"api_key": SANDBOX_KEY})
    assert response.status_code == 502


def test_manual_validation(client: TestClient, stack: PaymentsStack) -> None:
    order_id = stack.order(total=12_500)
    url = f"{base(stack)}/orders/{order_id}/payments/manual"
    headers = {"Idempotency-Key": "k1"}

    response = client.post(url, json={"reference": "TM-1", "note": "cash"}, headers=headers)

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "PAID" and body["method"] == "manual"
    assert body["amount"] == 12_500 and body["reference"] == "TM-1"
    assert body["recorded_by"] == str(ACCOUNT)
    [entry] = stack.audit_log.entries
    assert entry.action == "payment.recorded" and entry.account_id == ACCOUNT

    replay = client.post(url, json={"reference": "TM-1", "note": "cash"}, headers=headers)
    assert replay.status_code == 201 and replay.json()["id"] == body["id"]
    again = client.post(url, json={})
    assert again.status_code == 409
    assert again.json()["code"] == "payments.order_already_paid"


def test_manual_validation_needs_a_confirmed_order(
    client: TestClient, stack: PaymentsStack
) -> None:
    order_id = stack.order(status="AWAITING_MERCHANT")
    response = client.post(f"{base(stack)}/orders/{order_id}/payments/manual", json={})
    assert response.status_code == 409
    assert response.json()["code"] == "payments.order_not_payable"
    response = client.post(f"{base(stack)}/orders/{uuid4()}/payments/manual", json={})
    assert response.status_code == 404


async def test_request_list_and_get(client: TestClient, stack: PaymentsStack) -> None:
    order_id = stack.order()
    assert client.post(f"{base(stack)}/orders/{order_id}/payments/request").status_code == 409
    await stack.configure()

    response = client.post(f"{base(stack)}/orders/{order_id}/payments/request")
    assert response.status_code == 200
    payment = response.json()
    assert payment["status"] == "PENDING" and payment["checkout_url"]

    other = stack.order(number=2)
    client.post(f"{base(stack)}/orders/{other}/payments/manual", json={})
    page = client.get(f"{base(stack)}/payments").json()
    assert page["total"] == 2
    pending = client.get(f"{base(stack)}/payments", params={"status": "PENDING"}).json()
    assert [item["id"] for item in pending["items"]] == [payment["id"]]
    by_order = client.get(f"{base(stack)}/payments", params={"order_id": str(other)}).json()
    assert by_order["total"] == 1 and by_order["items"][0]["method"] == "manual"

    assert client.get(f"{base(stack)}/payments/{payment['id']}").json() == payment
    assert client.get(f"{base(stack)}/payments/{uuid4()}").status_code == 404


def webhook(payment_id: object, event_id: str, amount: int) -> bytes:
    return json.dumps(
        {
            "api_version": "2.0",
            "event": "collection.succeeded",
            "event_id": event_id,
            "data": {
                "transaction_id": str(payment_id),
                "status": "succeeded",
                "transaction_details": {"currency": "XOF", "amount": amount},
            },
        }
    ).encode()


async def test_the_webhook_is_deduplicated(client: TestClient, stack: PaymentsStack) -> None:
    await stack.configure()
    payment = await stack.request(stack.order())
    url = f"/webhooks/kprimepay/{stack.callback_token()}"
    transaction_id = payment.attempts[0].id
    body = webhook(transaction_id, "evt_1", payment.amount.value)
    headers = {"X-KPP-EVENT": "collection.succeeded", "X-KPP-EVENT-ID": "evt_1"}

    assert client.post(url, content=body, headers=headers).status_code == 200
    assert client.post(url, content=body, headers=headers).status_code == 200

    assert stack.queue.checks == [(stack.shop_id, transaction_id)]


async def test_a_bad_token_is_not_found(client: TestClient, stack: PaymentsStack) -> None:
    await stack.configure()
    payment = await stack.request(stack.order())
    response = client.post(
        "/webhooks/kprimepay/forged-token", content=webhook(payment.id, "evt_1", 10_000)
    )
    assert response.status_code == 404
    assert stack.queue.checks == []


async def test_a_lying_payload_does_not_pay(client: TestClient, stack: PaymentsStack) -> None:
    await stack.configure()
    payment = await stack.request(stack.order(total=50_000))
    # Someone who learnt the URL claims a success; KPrimePay only saw 500 XOF paid.
    stack.gateway.settle(payment, amount=500)
    url = f"/webhooks/kprimepay/{stack.callback_token()}"
    body = webhook(payment.attempts[0].id, "evt_x", 50_000)
    assert client.post(url, content=body).status_code == 200

    [(_, transaction_id)] = stack.queue.checks
    assert await stack.verify_attempt(transaction_id) is PaymentStatus.PENDING
    detail = client.get(f"{base(stack)}/payments/{payment.id}").json()
    assert detail["status"] == "PENDING" and detail["paid_at"] is None


def test_the_return_page_is_public(client: TestClient) -> None:
    assert client.get("/payments/return").status_code == 200


async def test_push_again_from_the_dashboard(client: TestClient, stack: PaymentsStack) -> None:
    await stack.configure()
    order_id = stack.order()
    await stack.confirmed(order_id)  # a push to the customer's WhatsApp number
    url = f"{base(stack)}/orders/{order_id}/payments/request"

    response = client.post(url, json={"channel": "ussd_push"})

    assert response.status_code == 200
    body = response.json()
    assert body["pushes"] == 2
    attempts = body["attempts"]
    assert [a["channel"] for a in attempts] == ["ussd_push", "ussd_push"]
    assert attempts[0]["operator"] == "MIXX-YAS-TG"
    assert attempts[0]["payer"] == "91 •• •• 67"
    assert "91234567" not in response.text  # the payer number is never returned in full

    link = client.post(url, json={"channel": "checkout"}).json()
    assert link["checkout_url"] and link["attempts"][-1]["channel"] == "checkout"
    assert client.post(url, json={"channel": "sms"}).status_code == 422


async def test_no_number_to_push_to(client: TestClient, stack: PaymentsStack) -> None:
    from woassi.modules.payments.application.ports import Contact

    await stack.configure()
    stack.customers.contacts[stack.customer_id] = Contact(user_id="TG.1")
    order_id = stack.order()
    response = client.post(
        f"{base(stack)}/orders/{order_id}/payments/request", json={"channel": "ussd_push"}
    )
    assert response.status_code == 409
    assert response.json()["code"] == "payments.no_payer_number"
