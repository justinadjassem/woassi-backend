"""KPrimePay API v2, called with each shop's own key.

Checked on 2026-11-10 against https://developers.kprimepay.com/index?v=v2.

The key travels in the `Authorization` header. Still, nothing here logs a URL, a header or a
response body (it holds the customer's name and phone), and no httpx exception, which holds the
request, leaves this module.
"""

import asyncio
import re
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from woassi.modules.payments.application.ports import (
    Checkout,
    CheckoutRequest,
    ProviderError,
    ProviderFailure,
    ProviderTransaction,
    PushRequest,
)
from woassi.modules.payments.domain.payment import PaymentStatus
from woassi.modules.payments.domain.provider_account import ProviderMode
from woassi.shared.infrastructure.logging import silence_url_logging

BASE_URL = "https://api.kprimepay.com/v2"
# https://developers.kprimepay.com/index?v=v2#errors
INVALID_KEY_CODES = frozenset(
    {
        "AUTH_TOKEN_NOT_FOUND",
        "INVALID_AUTH_TOKEN",
        "INSUFFICIENT_SCOPE",
        "MERCHANT_MISMATCH",
        "BUSINESS_ACCOUNT_BLOCKED",
    }
)
CODE = re.compile(r"[A-Z][A-Z0-9_]{0,59}")
# Only for money-moving calls, which carry an Idempotency-Key: retrying them is always safe.
RETRY_DELAYS_S = (1.0, 3.0)
DEFAULT_CHECKOUT_LIFETIME = timedelta(hours=24)


def map_status(value: object) -> PaymentStatus:
    """KPrimePay's status, from `debit-status` or a webhook, as one of ours.

    Only `success` (and the webhook's `succeeded`) means paid, and only once the amount is
    checked. `cancelled` and `expired` are not documented, but mapped defensively. Anything
    unknown stays PENDING: the poller asks again.
    https://developers.kprimepay.com/index?v=v2#status
    """
    match str(value).lower():
        case "success" | "succeeded":
            return PaymentStatus.PAID
        case "failed" | "cancelled" | "canceled" | "expired":
            return PaymentStatus.FAILED
        case _:
            return PaymentStatus.PENDING


def _code(data: dict[str, Any]) -> str | None:
    """KPrimePay's `message`, only when it is a code like VALIDATION_ERROR, never free text."""
    message = data.get("message")
    return message if isinstance(message, str) and CODE.fullmatch(message) else None


def _failure(status: int, code: str | None) -> ProviderFailure:
    """Classifies an error answer by KPrimePay's code, then by HTTP status."""
    if code in INVALID_KEY_CODES or status == 401:
        return ProviderFailure.INVALID_KEY
    if code == "TRANSACTION_NOT_FOUND" or status == 404:
        return ProviderFailure.NOT_FOUND
    if status in (409, 429) or status >= 500:
        # 409 REQUEST_IN_PROGRESS, 429 TOO_MANY_REQUESTS: try again later.
        return ProviderFailure.UNAVAILABLE
    return ProviderFailure.REJECTED


def _datetime(value: object, default: datetime) -> datetime:
    """KPrimePay writes `2026-08-17 09:12:44` without a zone; Lomé is on UTC all year."""
    if not isinstance(value, str):
        return default
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return default
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


class HttpxKPrimePay:
    """`PaymentGateway` on KPrimePay's REST API."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        base_url: str = BASE_URL,
        timeout: httpx.Timeout | None = None,
        retry_delays_s: Sequence[float] = RETRY_DELAYS_S,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        silence_url_logging()
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout or httpx.Timeout(15.0, connect=5.0)
        self._retry_delays_s = retry_delays_s
        self._sleep = sleep
        self._now = now

    async def _request(
        self,
        method: str,
        path: str,
        api_key: str,
        *,
        json: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """The `data` of a successful answer. Raises ProviderError otherwise."""
        headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        try:
            response = await self._client.request(
                method,
                f"{self._base_url}{path}",
                json=json,
                headers=headers,
                timeout=self._timeout,
            )
            body = response.json()
        except (httpx.ConnectError, httpx.ConnectTimeout):
            # Nothing reached KPrimePay.
            raise ProviderError(ProviderFailure.UNAVAILABLE, request_sent=False) from None
        except (httpx.HTTPError, ValueError):
            # A read timeout included: KPrimePay may have acted. `from None`: the httpx
            # exception holds the request, and so the key.
            raise ProviderError(ProviderFailure.UNAVAILABLE) from None
        body = body if isinstance(body, dict) else {}
        if 200 <= response.status_code < 300 and body.get("status") is not False:
            data = body.get("data")
            return data if isinstance(data, dict) else {}
        code = _code(body)
        raise ProviderError(_failure(response.status_code, code), code=code)

    async def verify_key(self, api_key: str) -> None:
        # https://developers.kprimepay.com/index?v=v2#gateways (scope `read`, moves nothing)
        await self._request("GET", "/gateways", api_key)

    async def create_checkout(self, api_key: str, request: CheckoutRequest) -> Checkout:
        # https://developers.kprimepay.com/index?v=v2#checkout
        # https://developers.kprimepay.com/index?v=v2#idempotency
        """Retries when KPrimePay gave no usable answer, timeouts included.

        Unlike a WhatsApp message, this is safe: the Idempotency-Key is our payment id, so a
        checkout KPrimePay already created is returned again (`Idempotent-Replay`), never doubled.
        """
        body: dict[str, Any] = {
            "transaction_id": request.transaction_id,
            "amount": request.amount,
            "currency": "XOF",
            "mode": 1 if request.mode is ProviderMode.SANDBOX else 2,
            # 0: the merchant bears the fee, so the customer pays exactly the order total.
            "with_fees": 0,
            "description": request.description,
            "return_url": request.return_url,
            "locale": request.locale,
        }
        if request.callback_url:
            body["callback_url"] = request.callback_url
        if request.metadata:
            body["custom_meta_data"] = dict(request.metadata)
        data = await self._idempotent("/checkout", api_key, body, request.transaction_id)
        url = data.get("checkout_url")
        if not isinstance(url, str) or not url.startswith("https://"):
            raise ProviderError(ProviderFailure.UNAVAILABLE)
        reference = data.get("kpp_tx_reference")
        return Checkout(
            reference=reference if isinstance(reference, str) else None,
            url=url,
            expires_at=_datetime(data.get("expires_at"), self._now() + DEFAULT_CHECKOUT_LIFETIME),
        )

    async def ussd_push(self, api_key: str, request: PushRequest) -> str | None:
        # https://developers.kprimepay.com/index?v=v2#ussd
        # https://developers.kprimepay.com/index?v=v2#idempotency
        """202 Accepted: the payer approves on their phone; the outcome comes from `debit-status`
        or a webhook. Retried like the checkout, as the Idempotency-Key is the attempt id.

        The documented example also has `customer_name` and `customer_email`, without saying
        whether they are required: they are left out, as Woassi has no email and the name is
        personal data KPrimePay does not need.
        """
        body: dict[str, Any] = {
            "transaction_id": request.transaction_id,
            "amount": request.amount,
            "with_fees": 0,
            "gateway": request.gateway.value,
            "phone_number": request.phone_number,
            "description": request.description,
        }
        if request.callback_url:
            body["callback_url"] = request.callback_url
        data = await self._idempotent("/payments/ussd-push", api_key, body, request.transaction_id)
        # The 202 body is not documented: keep a reference only if one is there.
        reference = data.get("kpp_tx_reference")
        return reference if isinstance(reference, str) else None

    async def _idempotent(
        self, path: str, api_key: str, body: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any]:
        """POSTs a money-moving request, retried while KPrimePay gives no usable answer."""
        delays = list(self._retry_delays_s)
        while True:
            try:
                return await self._request(
                    "POST", path, api_key, json=body, idempotency_key=idempotency_key
                )
            except ProviderError as error:
                if error.failure is not ProviderFailure.UNAVAILABLE or not delays:
                    raise
                await self._sleep(delays.pop(0))

    async def transaction(self, api_key: str, transaction_id: str) -> ProviderTransaction:
        # https://developers.kprimepay.com/index?v=v2#status
        data = await self._request(
            "POST", "/transactions/debit-status", api_key, json={"transaction_id": transaction_id}
        )
        currency = data.get("transaction_currency", data.get("currency"))
        reference = data.get("kpp_tx_reference")
        return ProviderTransaction(
            transaction_id=str(data.get("transaction_id", "")),
            status=map_status(data.get("status")),
            # `transaction_amount`, not `total_amount`: the latter includes the customer's fee.
            amount=_int(data.get("transaction_amount")),
            currency=currency if isinstance(currency, str) else None,
            reference=reference if isinstance(reference, str) else None,
        )
