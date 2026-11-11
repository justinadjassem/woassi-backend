import json
import logging
from datetime import UTC, datetime

import httpx
import pytest
import respx

from woassi.modules.payments.application.ports import (
    CheckoutRequest,
    ProviderError,
    ProviderFailure,
    PushRequest,
)
from woassi.modules.payments.domain.mobile_money import Operator
from woassi.modules.payments.domain.payment import PaymentStatus
from woassi.modules.payments.domain.provider_account import ProviderMode
from woassi.modules.payments.infrastructure.kprimepay import HttpxKPrimePay, map_status

BASE = "https://api.kprimepay.com/v2"
KEY = "kpp_sbk_topsecretvalue"
REQUEST = CheckoutRequest(
    transaction_id="6f1c2a8e-0000-4000-8000-000000000001",
    amount=15000,
    mode=ProviderMode.SANDBOX,
    description="Commande n°1 - Chez Ama",
    locale="fr",
    return_url="https://api.example.com/payments/return",
    callback_url="https://api.example.com/webhooks/kprimepay/tok",
    metadata={"payment_id": "p"},
)
CHECKOUT = {
    "status": True,
    "message": "CHECKOUT_CREATED",
    "data": {
        "kpp_tx_reference": "KPP_TXN_A1",
        "transaction_id": REQUEST.transaction_id,
        "amount": 15000,
        "currency": "XOF",
        "expires_at": "2026-08-17 09:12:44",
        "checkout_url": "https://payments.kprimepay.com/checkout/web/abc",
    },
    "status_code": 201,
}


class Sleeps:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


@pytest.fixture
def sleeps() -> Sleeps:
    return Sleeps()


@pytest.fixture
async def client(sleeps: Sleeps) -> HttpxKPrimePay:
    return HttpxKPrimePay(httpx.AsyncClient(), sleep=sleeps)


@pytest.mark.parametrize(
    ("value", "status"),
    [
        ("success", PaymentStatus.PAID),
        ("succeeded", PaymentStatus.PAID),
        ("failed", PaymentStatus.FAILED),
        ("expired", PaymentStatus.FAILED),
        ("pending", PaymentStatus.PENDING),
        ("something_new", PaymentStatus.PENDING),
        (None, PaymentStatus.PENDING),
    ],
)
def test_status_mapping(value: object, status: PaymentStatus) -> None:
    assert map_status(value) is status


@respx.mock
async def test_checkout_request_and_answer(client: HttpxKPrimePay) -> None:
    route = respx.post(f"{BASE}/checkout").respond(201, json=CHECKOUT)

    checkout = await client.create_checkout(KEY, REQUEST)

    assert checkout.url == "https://payments.kprimepay.com/checkout/web/abc"
    assert checkout.reference == "KPP_TXN_A1"
    assert checkout.expires_at == datetime(2026, 8, 17, 9, 12, 44, tzinfo=UTC)
    sent = route.calls.last.request
    assert sent.headers["Authorization"] == f"Bearer {KEY}"
    assert sent.headers["Idempotency-Key"] == REQUEST.transaction_id
    assert json.loads(sent.content) == {
        "transaction_id": REQUEST.transaction_id,
        "amount": 15000,
        "currency": "XOF",
        "mode": 1,
        "with_fees": 0,
        "description": "Commande n°1 - Chez Ama",
        "return_url": "https://api.example.com/payments/return",
        "locale": "fr",
        "callback_url": "https://api.example.com/webhooks/kprimepay/tok",
        "custom_meta_data": {"payment_id": "p"},
    }


@respx.mock
async def test_a_timeout_is_retried_with_the_same_idempotency_key(
    client: HttpxKPrimePay, sleeps: Sleeps
) -> None:
    route = respx.post(f"{BASE}/checkout").mock(
        side_effect=[httpx.ReadTimeout("slow"), httpx.Response(201, json=CHECKOUT)]
    )
    await client.create_checkout(KEY, REQUEST)
    keys = {call.request.headers["Idempotency-Key"] for call in route.calls}
    assert keys == {REQUEST.transaction_id} and len(route.calls) == 2
    assert sleeps.delays == [1.0]


@respx.mock
async def test_request_in_progress_then_give_up(client: HttpxKPrimePay, sleeps: Sleeps) -> None:
    respx.post(f"{BASE}/checkout").respond(
        409, json={"status": False, "message": "REQUEST_IN_PROGRESS", "status_code": 409}
    )
    with pytest.raises(ProviderError) as error:
        await client.create_checkout(KEY, REQUEST)
    assert error.value.failure is ProviderFailure.UNAVAILABLE
    assert sleeps.delays == [1.0, 3.0]


@respx.mock
async def test_a_refusal_is_not_retried(client: HttpxKPrimePay, sleeps: Sleeps) -> None:
    respx.post(f"{BASE}/checkout").respond(
        422,
        json={
            "status": False,
            "message": "VALIDATION_ERROR",
            "errors": {"amount": ["too small"]},
            "status_code": 422,
        },
    )
    with pytest.raises(ProviderError) as error:
        await client.create_checkout(KEY, REQUEST)
    assert (error.value.failure, error.value.code) == (ProviderFailure.REJECTED, "VALIDATION_ERROR")
    assert sleeps.delays == []


@respx.mock
async def test_connection_refused_means_nothing_was_sent(client: HttpxKPrimePay) -> None:
    respx.post(f"{BASE}/transactions/debit-status").mock(side_effect=httpx.ConnectError("no"))
    with pytest.raises(ProviderError) as error:
        await client.transaction(KEY, "t")
    assert error.value.failure is ProviderFailure.UNAVAILABLE
    assert error.value.request_sent is False


@respx.mock
async def test_a_read_timeout_may_have_reached_kprimepay(client: HttpxKPrimePay) -> None:
    respx.post(f"{BASE}/transactions/debit-status").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(ProviderError) as error:
        await client.transaction(KEY, "t")
    assert error.value.request_sent is True


@respx.mock
async def test_debit_status(client: HttpxKPrimePay) -> None:
    route = respx.post(f"{BASE}/transactions/debit-status").respond(
        200,
        json={
            "status": True,
            "message": "TRANSACTION_RETRIEVED",
            "data": {
                "status": "success",
                "transaction_id": "t-1",
                "kpp_tx_reference": "KPP_TXN_A1",
                "transaction_currency": "XOF",
                "transaction_amount": 15000,
                "total_amount": 15300,
            },
        },
    )
    transaction = await client.transaction(KEY, "t-1")
    assert json.loads(route.calls.last.request.content) == {"transaction_id": "t-1"}
    assert transaction.status is PaymentStatus.PAID
    assert (transaction.amount, transaction.currency) == (15000, "XOF")  # not total_amount
    assert transaction.transaction_id == "t-1"


@pytest.mark.parametrize(
    ("status", "message", "failure"),
    [
        (401, "INVALID_AUTH_TOKEN", ProviderFailure.INVALID_KEY),
        (403, "INSUFFICIENT_SCOPE", ProviderFailure.INVALID_KEY),
        (403, "BUSINESS_ACCOUNT_BLOCKED", ProviderFailure.INVALID_KEY),
        (404, "TRANSACTION_NOT_FOUND", ProviderFailure.NOT_FOUND),
        (429, "TOO_MANY_REQUESTS", ProviderFailure.UNAVAILABLE),
        (503, "whatever text", ProviderFailure.UNAVAILABLE),
    ],
)
@respx.mock
async def test_error_mapping(
    client: HttpxKPrimePay, status: int, message: str, failure: ProviderFailure
) -> None:
    respx.get(f"{BASE}/gateways").respond(status, json={"status": False, "message": message})
    with pytest.raises(ProviderError) as error:
        await client.verify_key(KEY)
    assert error.value.failure is failure
    # Free text from KPrimePay is never carried.
    assert error.value.code in (message, None) and error.value.code != "whatever text"


@respx.mock
async def test_a_non_json_answer(client: HttpxKPrimePay) -> None:
    respx.get(f"{BASE}/gateways").respond(200, text="<html>")
    with pytest.raises(ProviderError) as error:
        await client.verify_key(KEY)
    assert error.value.failure is ProviderFailure.UNAVAILABLE


@respx.mock
async def test_the_key_never_leaks(
    client: HttpxKPrimePay, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    respx.get(f"{BASE}/gateways").mock(side_effect=httpx.ReadTimeout(f"Bearer {KEY}"))
    with pytest.raises(ProviderError) as error:
        await client.verify_key(KEY)
    assert error.value.__cause__ is None and error.value.__suppress_context__
    assert KEY not in str(error.value) and KEY not in repr(error.value)
    assert KEY not in caplog.text


PUSH = PushRequest(
    transaction_id="6f1c2a8e-0000-4000-8000-000000000002",
    amount=5000,
    gateway=Operator.MOOV_MONEY_TG,
    phone_number="96010203",
    description="Commande n°2 - Chez Ama",
    callback_url="https://api.example.com/webhooks/kprimepay/tok",
)


async def test_ussd_push_request_and_202() -> None:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(202, json={"status": True, "message": "PUSH_SENT", "data": {}})

    client = HttpxKPrimePay(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await client.ussd_push(KEY, PUSH) is None

    [request] = sent
    assert request.url == f"{BASE}/payments/ussd-push"
    assert request.headers["Authorization"] == f"Bearer {KEY}"
    assert request.headers["Idempotency-Key"] == PUSH.transaction_id
    assert json.loads(request.content) == {
        "transaction_id": PUSH.transaction_id,
        "amount": 5000,
        "with_fees": 0,
        "gateway": "MOOV-MONEY-TG",
        "phone_number": "96010203",
        "description": "Commande n°2 - Chez Ama",
        "callback_url": "https://api.example.com/webhooks/kprimepay/tok",
    }
    assert "96010203" not in repr(PUSH)


async def test_ussd_push_keeps_a_reference_if_any_and_retries(sleeps: Sleeps) -> None:
    answers = iter(
        [
            httpx.ReadTimeout("slow"),
            httpx.Response(202, json={"status": True, "data": {"kpp_tx_reference": "KPP_9"}}),
        ]
    )
    keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        keys.append(request.headers["Idempotency-Key"])
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    client = HttpxKPrimePay(httpx.AsyncClient(transport=httpx.MockTransport(handler)), sleep=sleeps)
    assert await client.ussd_push(KEY, PUSH) == "KPP_9"
    assert keys == [PUSH.transaction_id] * 2 and sleeps.delays == [1.0]


async def test_ussd_push_refused_by_the_operator() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422, json={"status": False, "message": "GATEWAY_REJECTED", "status_code": 422}
        )

    client = HttpxKPrimePay(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(ProviderError) as error:
        await client.ussd_push(KEY, PUSH)
    assert (error.value.failure, error.value.code) == (ProviderFailure.REJECTED, "GATEWAY_REJECTED")
