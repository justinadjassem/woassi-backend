import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from io import BytesIO
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import respx
import structlog
from cryptography.fernet import Fernet
from fastapi import FastAPI, Header
from fastapi.testclient import TestClient
from PIL import Image

from tests.fakes import FakeAuditLog, FakeIdempotencyStore, FakeShopsUoW, FakeStorage
from woassi.config import Settings
from woassi.integration.images import CatalogLogoProcessor
from woassi.integration.shops import ShopsConnections, ShopsMetaCatalogs, ShopsOwnership
from woassi.main import create_app
from woassi.modules.catalog.infrastructure.image_processor import PillowImageProcessor
from woassi.modules.shops.api.deps import get_shops
from woassi.modules.shops.application.use_cases.logo import ConfirmLogo, RequestLogoUpload
from woassi.modules.shops.application.use_cases.receiving_account import (
    DeleteReceivingAccount,
    GetReceivingAccount,
    SetReceivingAccount,
)
from woassi.modules.shops.application.use_cases.settings import GetSettings, ReplaceSettings
from woassi.modules.shops.application.use_cases.shops import (
    ArchiveShop,
    CheckShopOwnership,
    CreateShop,
    GetShop,
    ListShops,
    ResolveShopByPhoneNumber,
    ResolveShopCatalog,
    ResolveShopConnection,
    UpdateShop,
)
from woassi.modules.shops.application.use_cases.whatsapp import (
    CheckWhatsAppTokens,
    ConnectWhatsApp,
    DisconnectWhatsApp,
    GetWhatsApp,
)
from woassi.modules.shops.domain.events import (
    ReceivingAccountChanged,
    ShopCreated,
    WhatsAppConnected,
)
from woassi.modules.shops.domain.shop import Shop, ShopStatus, logo_key, original_logo_key
from woassi.modules.shops.infrastructure.meta_onboarding import HttpxMetaOnboarding
from woassi.shared.api.deps import get_current_account, get_shared
from woassi.shared.application.audit import ListAuditEntries
from woassi.shared.application.ports import AuthenticatedAccount
from woassi.shared.domain.events import DomainEvent
from woassi.shared.infrastructure.clock import FrozenClock
from woassi.shared.infrastructure.crypto import FernetSecretCipher
from woassi.shared.infrastructure.meta_graph import GraphClient
from woassi.shared.infrastructure.phone import PhonenumbersNormalizer

GRAPH = "https://graph.facebook.com/v27.0"
TOKEN = "EAAG-a-recognizable-business-token"
APP_SECRET = "a-recognizable-app-secret"
WABA = "1001"
PHONE_ID = "2002"
CONNECT = {"code": "signup-code", "waba_id": WABA, "phone_number_id": PHONE_ID}
PHONE_DETAILS = {
    "display_phone_number": "+228 90 12 34 56",
    "verified_name": "Chez Ama",
    "is_on_biz_app": True,
    "platform_type": "CLOUD_API",
}


class Stack:
    def __init__(self) -> None:
        self.database: dict[UUID, Shop] = {}
        self.events: list[DomainEvent] = []
        self.audit_log = FakeAuditLog()
        self.uploads = FakeStorage()
        self.images = FakeStorage("http://images")
        self.clock = FrozenClock(datetime(2026, 1, 1, tzinfo=UTC))
        self.cipher = FernetSecretCipher([Fernet.generate_key().decode()])
        self.phones = PhonenumbersNormalizer()
        self.http = httpx.AsyncClient()
        self.meta = HttpxMetaOnboarding(
            GraphClient(self.http, graph_version="v27.0"), app_id="app-id", app_secret=APP_SECRET
        )

    def uow(self) -> FakeShopsUoW:
        return FakeShopsUoW(self.database, self.events, self.audit_log)

    def create_shop(self) -> CreateShop:
        return CreateShop(self.uow, self.images, self.clock)

    def list_shops(self) -> ListShops:
        return ListShops(self.uow(), self.images)

    def get_shop(self) -> GetShop:
        return GetShop(self.uow(), self.images)

    def update_shop(self) -> UpdateShop:
        return UpdateShop(self.uow(), self.images)

    def archive_shop(self) -> ArchiveShop:
        return ArchiveShop(self.uow(), self.meta, self.cipher, self.clock)

    def get_settings(self) -> GetSettings:
        return GetSettings(self.uow())

    def replace_settings(self) -> ReplaceSettings:
        return ReplaceSettings(self.uow(), self.phones)

    def connect_whatsapp(self) -> ConnectWhatsApp:
        return ConnectWhatsApp(self.uow(), self.meta, self.cipher, self.clock)

    def get_whatsapp(self) -> GetWhatsApp:
        return GetWhatsApp(self.uow())

    def disconnect_whatsapp(self) -> DisconnectWhatsApp:
        return DisconnectWhatsApp(self.uow(), self.meta, self.cipher)

    def check_whatsapp_tokens(self) -> CheckWhatsAppTokens:
        return CheckWhatsAppTokens(self.uow(), self.meta, self.cipher)

    def get_receiving_account(self) -> GetReceivingAccount:
        return GetReceivingAccount(self.uow(), self.cipher)

    def set_receiving_account(self) -> SetReceivingAccount:
        return SetReceivingAccount(self.uow(), self.phones, self.cipher)

    def delete_receiving_account(self) -> DeleteReceivingAccount:
        return DeleteReceivingAccount(self.uow())

    def request_logo_upload(self) -> RequestLogoUpload:
        return RequestLogoUpload(self.uow(), self.uploads)

    def confirm_logo(self) -> ConfirmLogo:
        processor = CatalogLogoProcessor(PillowImageProcessor())
        return ConfirmLogo(self.uow(), self.uploads, self.images, processor)

    # shared
    def shop_ownership(self) -> ShopsOwnership:
        return ShopsOwnership(CheckShopOwnership(self.uow()))

    def list_audit_entries(self) -> ListAuditEntries:
        return ListAuditEntries(self.uow())

    def idempotency_store(self) -> FakeIdempotencyStore:
        return FakeIdempotencyStore()

    def lookup(self) -> ShopsConnections:
        return ShopsConnections(
            ResolveShopByPhoneNumber(self.uow(), self.cipher),
            ResolveShopConnection(self.uow(), self.cipher),
        )

    def catalogs(self) -> ShopsMetaCatalogs:
        return ShopsMetaCatalogs(ResolveShopCatalog(self.uow(), self.cipher))


@pytest.fixture
def stack() -> Stack:
    return Stack()


def account_from_header(
    x_test_account: str = Header(default=""),
) -> AuthenticatedAccount:
    return AuthenticatedAccount(id=UUID(x_test_account))


@pytest.fixture
def app(settings: Settings, stack: Stack) -> FastAPI:
    app = create_app(settings)
    app.dependency_overrides[get_shops] = lambda: stack
    app.dependency_overrides[get_shared] = lambda: stack
    app.dependency_overrides[get_current_account] = account_from_header
    return app


def client_for(app: FastAPI) -> TestClient:
    client = TestClient(app)
    client.headers["X-Test-Account"] = str(uuid4())
    return client


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return client_for(app)


@pytest.fixture
def stranger(app: FastAPI) -> TestClient:
    return client_for(app)


def create(client: TestClient, name: str = "Chez Ama") -> str:
    response = client.post("/shops", json={"name": name, "city": "Lomé"})
    assert response.status_code == 201, response.text
    return response.json()["id"]


@pytest.fixture
def shop_id(client: TestClient) -> str:
    return create(client)


@pytest.fixture
def meta() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{GRAPH}/oauth/access_token").respond(
            200, json={"access_token": TOKEN, "token_type": "bearer"}
        )
        router.post(f"{GRAPH}/{WABA}/subscribed_apps").respond(200, json={"success": True})
        router.delete(f"{GRAPH}/{WABA}/subscribed_apps").respond(200, json={"success": True})
        router.get(f"{GRAPH}/{PHONE_ID}").respond(200, json=PHONE_DETAILS)
        router.get(f"{GRAPH}/{WABA}/product_catalogs").respond(
            200, json={"data": [{"id": "3003", "name": "Catalogue"}]}
        )
        router.post(f"{GRAPH}/{WABA}/product_catalogs").respond(200, json={"success": True})
        router.post(f"{GRAPH}/{PHONE_ID}/whatsapp_commerce_settings").respond(
            200, json={"success": True}
        )
        router.post(f"{GRAPH}/{PHONE_ID}/smb_app_data").respond(200, json={"request_id": "9"})
        yield router


def meta_error(status: int = 400, code: int = 100) -> httpx.Response:
    return httpx.Response(
        status,
        json={
            "error": {"message": "secret detail from Meta", "type": "OAuthException", "code": code}
        },
    )


def connect(client: TestClient, shop_id: str, **body: str) -> httpx.Response:
    return client.post(f"/shops/{shop_id}/whatsapp/connect", json={**CONNECT, **body})


def status_of(client: TestClient, shop_id: str) -> str:
    return client.get(f"/shops/{shop_id}").json()["status"]


def calls(meta: respx.MockRouter) -> list[str]:
    return [f"{call.request.method} {call.request.url.path}" for call in meta.calls]


# Shops and isolation


def test_an_account_creates_two_shops(client: TestClient, stack: Stack) -> None:
    first, second = create(client, "Chez Ama"), create(client, "Marché Kofi")

    page = client.get("/shops").json()

    assert page["total"] == 2
    assert {shop["id"] for shop in page["items"]} == {first, second}
    assert {shop["slug"] for shop in page["items"]} == {"chez-ama", "marche-kofi"}
    assert all(shop["status"] == "DRAFT" for shop in page["items"])
    assert [type(event) for event in stack.events] == [ShopCreated, ShopCreated]


def test_two_shops_with_the_same_name_get_different_slugs(client: TestClient) -> None:
    create(client), create(client)
    slugs = [shop["slug"] for shop in client.get("/shops").json()["items"]]
    assert slugs[0] == "chez-ama"
    assert slugs[1].startswith("chez-ama-")


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", ""),
        ("patch", ""),
        ("delete", ""),
        ("get", "/settings"),
        ("put", "/settings"),
        ("post", "/whatsapp/connect"),
        ("get", "/whatsapp"),
        ("delete", "/whatsapp"),
        ("get", "/receiving-account"),
        ("put", "/receiving-account"),
        ("delete", "/receiving-account"),
        ("post", "/logo/upload-url"),
        ("post", "/logo/confirm"),
        ("get", "/audit-log"),
        ("get", "/products"),
    ],
)
def test_another_account_gets_404_everywhere(
    stranger: TestClient, shop_id: str, method: str, path: str
) -> None:
    response = stranger.request(method, f"/shops/{shop_id}{path}", json={})
    assert response.status_code == 404
    assert response.json()["code"] == "shops.not_found"


def test_a_stranger_sees_the_same_as_for_a_missing_shop(stranger: TestClient, shop_id: str) -> None:
    theirs = stranger.get(f"/shops/{shop_id}").json()
    missing = stranger.get(f"/shops/{uuid4()}").json()
    assert theirs["code"] == missing["code"]


def test_lists_are_separate(client: TestClient, stranger: TestClient, shop_id: str) -> None:
    assert stranger.get("/shops").json()["total"] == 0


def test_shop_routes_require_a_token(settings: Settings, stack: Stack) -> None:
    app = create_app(settings)
    app.dependency_overrides[get_shops] = lambda: stack
    app.dependency_overrides[get_shared] = lambda: stack
    client = TestClient(app)
    assert client.get("/shops").status_code == 401
    assert client.post("/shops", json={"name": "x"}).status_code == 401
    assert client.get(f"/shops/{uuid4()}").status_code == 401


def test_update(client: TestClient, shop_id: str) -> None:
    updated = client.patch(f"/shops/{shop_id}", json={"name": "Nouveau", "city": "Kara"}).json()
    assert (updated["name"], updated["city"], updated["slug"]) == ("Nouveau", "Kara", "chez-ama")


def test_archiving_hides_the_shop_but_keeps_it(
    client: TestClient, stack: Stack, shop_id: str
) -> None:
    assert client.delete(f"/shops/{shop_id}").status_code == 204

    assert client.get(f"/shops/{shop_id}").status_code == 404
    assert client.get("/shops").json()["total"] == 0
    assert stack.database[UUID(shop_id)].archived_at is not None


def test_an_archived_slug_stays_taken(client: TestClient, shop_id: str) -> None:
    client.delete(f"/shops/{shop_id}")
    create(client)
    assert client.get("/shops").json()["items"][0]["slug"] != "chez-ama"


def test_archiving_frees_the_whatsapp_number(
    client: TestClient, shop_id: str, meta: respx.MockRouter
) -> None:
    connect(client, shop_id)
    client.delete(f"/shops/{shop_id}")
    assert connect(client, create(client, "Autre")).status_code == 200


# Settings


def test_default_settings_come_with_the_shop(client: TestClient, shop_id: str) -> None:
    settings = client.get(f"/shops/{shop_id}/settings").json()
    assert settings["payment_modes"] == ["CASH"]
    assert settings["reminder_1_minutes"] == 15
    assert settings["reminder_2_minutes"] == 60
    assert settings["expiration_hours"] == 24
    assert settings["invoice_prefix"] == "FAC"


def test_replace_settings(client: TestClient, shop_id: str) -> None:
    body = {
        "notification_number": "90 12 34 56",
        "payment_modes": ["CASH", "MIXX", "MOOV"],
        "reminder_1_minutes": 10,
        "reminder_2_minutes": 30,
        "expiration_hours": 12,
        "welcome_message": "Bienvenue !",
        "invoice_details": {"company_name": "Ama SARL", "tax_id": "123", "trade_register": "RCCM"},
        "invoice_prefix": "AMA",
    }
    response = client.put(f"/shops/{shop_id}/settings", json=body)

    assert response.status_code == 200
    saved = client.get(f"/shops/{shop_id}/settings").json()
    assert saved["notification_number"] == "+22890123456"
    assert saved["payment_modes"] == ["CASH", "MIXX", "MOOV"]
    assert saved["invoice_details"]["trade_register"] == "RCCM"


def test_tax_settings(client: TestClient, shop_id: str) -> None:
    defaults = client.get(f"/shops/{shop_id}/settings").json()["invoice_details"]
    assert (defaults["tax_regime"], defaults["vat_registered"]) == (None, False)

    details = {"tax_id": "1000123456", "tax_regime": "REAL", "vat_registered": True}
    body = {"invoice_details": details}
    assert client.put(f"/shops/{shop_id}/settings", json=body).status_code == 200
    saved = client.get(f"/shops/{shop_id}/settings").json()["invoice_details"]
    assert (saved["tax_regime"], saved["vat_registered"]) == ("REAL", True)
    bad = {"invoice_details": {"tax_regime": "FLAT"}}
    assert client.put(f"/shops/{shop_id}/settings", json=bad).status_code == 422


def test_order_settings(client: TestClient, shop_id: str) -> None:
    defaults = client.get(f"/shops/{shop_id}/settings").json()
    assert (defaults["language"], defaults["validation_mode"]) == ("fr", "MANUAL")
    assert defaults["auto_validation_max_amount"] is None

    body = {"language": "en", "validation_mode": "AUTOMATIC", "auto_validation_max_amount": 50000}
    assert client.put(f"/shops/{shop_id}/settings", json=body).status_code == 200
    saved = client.get(f"/shops/{shop_id}/settings").json()
    assert (saved["language"], saved["validation_mode"]) == ("en", "AUTOMATIC")
    assert saved["auto_validation_max_amount"] == 50000


@pytest.mark.parametrize(
    "body",
    [
        {"language": "ee"},
        {"validation_mode": "SOMETIMES"},
        {"auto_validation_max_amount": 0},
        {"payment_modes": []},
        {"payment_modes": ["CHEQUE"]},
        {"reminder_1_minutes": 90},
        {"invoice_prefix": "fac"},
        {"notification_number": "123"},
    ],
)
def test_invalid_settings(client: TestClient, shop_id: str, body: dict[str, Any]) -> None:
    assert client.put(f"/shops/{shop_id}/settings", json=body).status_code == 422


# WhatsApp connection


def test_connect_success(
    client: TestClient, stack: Stack, shop_id: str, meta: respx.MockRouter
) -> None:
    assert status_of(client, shop_id) == "DRAFT"

    response = connect(client, shop_id)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "CONNECTED"
    assert body["phone_number_id"] == PHONE_ID
    assert body["display_phone_number"] == "+228 90 12 34 56"
    assert body["verified_name"] == "Chez Ama"
    assert body["catalog_id"] == "3003"
    assert status_of(client, shop_id) == "ACTIVE"
    assert WhatsAppConnected in [type(event) for event in stack.events]


def test_connect_calls_meta_in_order(
    client: TestClient, shop_id: str, meta: respx.MockRouter
) -> None:
    connect(client, shop_id)
    assert calls(meta) == [
        "GET /v27.0/oauth/access_token",
        f"POST /v27.0/{WABA}/subscribed_apps",
        f"GET /v27.0/{PHONE_ID}",
        f"GET /v27.0/{WABA}/product_catalogs",
        f"POST /v27.0/{PHONE_ID}/whatsapp_commerce_settings",
        f"POST /v27.0/{PHONE_ID}/smb_app_data",
        f"POST /v27.0/{PHONE_ID}/smb_app_data",
    ]


def test_connect_sends_what_meta_expects(
    client: TestClient, shop_id: str, meta: respx.MockRouter
) -> None:
    connect(client, shop_id)
    exchange, subscribe, phone, _, commerce, contacts, history = [c.request for c in meta.calls]

    assert exchange.url.params["client_id"] == "app-id"
    assert exchange.url.params["client_secret"] == APP_SECRET
    assert exchange.url.params["code"] == "signup-code"
    assert subscribe.headers["authorization"] == f"Bearer {TOKEN}"
    assert "is_on_biz_app" in phone.url.params["fields"]
    assert commerce.url.params["is_catalog_visible"] == "true"
    assert commerce.url.params["is_cart_enabled"] == "true"
    assert json.loads(contacts.content) == {
        "messaging_product": "whatsapp",
        "sync_type": "smb_app_state_sync",
    }
    assert json.loads(history.content)["sync_type"] == "history"


def test_a_number_outside_the_business_app_needs_no_sync(
    client: TestClient, shop_id: str, meta: respx.MockRouter
) -> None:
    meta.get(f"{GRAPH}/{PHONE_ID}").respond(200, json={**PHONE_DETAILS, "is_on_biz_app": False})
    assert connect(client, shop_id).status_code == 200
    assert not any("smb_app_data" in call for call in calls(meta))


def test_a_failed_history_sync_does_not_fail_the_connection(
    client: TestClient, shop_id: str, meta: respx.MockRouter
) -> None:
    meta.post(f"{GRAPH}/{PHONE_ID}/smb_app_data").mock(return_value=meta_error())
    assert connect(client, shop_id).status_code == 200


def test_a_number_not_on_cloud_api_is_refused(
    client: TestClient, shop_id: str, meta: respx.MockRouter
) -> None:
    meta.get(f"{GRAPH}/{PHONE_ID}").respond(
        200, json={**PHONE_DETAILS, "platform_type": "NOT_APPLICABLE"}
    )
    response = connect(client, shop_id)

    assert response.status_code == 422
    assert response.json()["code"] == "shops.phone_number_not_usable"
    assert client.get(f"/shops/{shop_id}/whatsapp").json()["status"] == "ERROR"


def test_catalog_chosen_in_signup_is_linked(
    client: TestClient, shop_id: str, meta: respx.MockRouter
) -> None:
    meta.get(f"{GRAPH}/{WABA}/product_catalogs").respond(200, json={"data": []})

    response = connect(client, shop_id, catalog_id="4004")

    assert response.json()["catalog_id"] == "4004"
    linked = [c.request for c in meta.calls if c.request.method == "POST"]
    assert any(r.url.params.get("catalog_id") == "4004" for r in linked)


def test_no_catalog_is_not_a_failure(
    client: TestClient, shop_id: str, meta: respx.MockRouter
) -> None:
    meta.get(f"{GRAPH}/{WABA}/product_catalogs").respond(200, json={"data": []})

    response = connect(client, shop_id)

    assert response.status_code == 200
    assert response.json()["catalog_id"] is None
    assert not any("commerce" in call for call in calls(meta))


def test_invalid_code(client: TestClient, shop_id: str, meta: respx.MockRouter) -> None:
    meta.get(f"{GRAPH}/oauth/access_token").mock(return_value=meta_error(400, 100))

    response = connect(client, shop_id)

    assert response.status_code == 422
    assert response.json()["code"] == "shops.invalid_signup_code"
    whatsapp = client.get(f"/shops/{shop_id}/whatsapp").json()
    assert whatsapp["status"] == "ERROR"
    assert "connect again" in whatsapp["error_message"]
    assert status_of(client, shop_id) == "DRAFT"
    assert len(meta.calls) == 1


@pytest.mark.parametrize(
    ("route", "failure"),
    [
        (("post", f"{GRAPH}/{WABA}/subscribed_apps"), meta_error(400, 200)),
        (("post", f"{GRAPH}/{WABA}/subscribed_apps"), meta_error(401, 190)),
        (("get", f"{GRAPH}/{PHONE_ID}"), meta_error(400, 100)),
        (("get", f"{GRAPH}/{WABA}/product_catalogs"), meta_error(403, 200)),
        (("get", f"{GRAPH}/oauth/access_token"), meta_error(500, 1)),
        (("get", f"{GRAPH}/oauth/access_token"), httpx.ConnectError("no route")),
        (("post", f"{GRAPH}/{WABA}/subscribed_apps"), httpx.ReadTimeout("slow")),
    ],
    ids=["refused", "bad-token", "phone", "catalog", "meta-500", "network", "timeout"],
)
def test_meta_errors_become_a_readable_status(
    client: TestClient,
    shop_id: str,
    meta: respx.MockRouter,
    route: tuple[str, str],
    failure: httpx.Response | Exception,
) -> None:
    method, url = route
    meta.route(method=method.upper(), url=url).mock(
        side_effect=failure if isinstance(failure, Exception) else None,
        return_value=None if isinstance(failure, Exception) else failure,
    )

    response = connect(client, shop_id)

    assert response.status_code == 502
    assert response.json()["code"] == "shops.whatsapp_connection_failed"
    whatsapp = client.get(f"/shops/{shop_id}/whatsapp")
    assert whatsapp.json()["status"] == "ERROR"
    assert whatsapp.json()["error_message"]
    assert "secret detail" not in response.text + whatsapp.text
    assert status_of(client, shop_id) == "DRAFT"


def test_a_failed_connection_can_be_retried(
    client: TestClient, shop_id: str, meta: respx.MockRouter
) -> None:
    meta.get(f"{GRAPH}/oauth/access_token").mock(return_value=meta_error())
    connect(client, shop_id)

    meta.get(f"{GRAPH}/oauth/access_token").respond(200, json={"access_token": TOKEN})

    assert connect(client, shop_id).status_code == 200
    assert client.get(f"/shops/{shop_id}/whatsapp").json()["error_message"] is None


def test_a_number_cannot_serve_two_shops(
    client: TestClient, shop_id: str, meta: respx.MockRouter
) -> None:
    connect(client, shop_id)
    other = create(client, "Autre")
    used_calls = len(meta.calls)

    response = connect(client, other)

    assert response.status_code == 409
    assert response.json()["code"] == "shops.phone_number_already_connected"
    assert len(meta.calls) == used_calls


def test_a_connected_shop_must_disconnect_first(
    client: TestClient, shop_id: str, meta: respx.MockRouter
) -> None:
    connect(client, shop_id)
    response = connect(client, shop_id)
    assert response.status_code == 409
    assert response.json()["code"] == "shops.whatsapp_already_connected"


async def test_a_failed_attempt_reserves_no_number(
    client: TestClient, stack: Stack, shop_id: str, meta: respx.MockRouter
) -> None:
    """Otherwise anyone could squat the number of another merchant with a fake code."""
    meta.get(f"{GRAPH}/oauth/access_token").mock(return_value=meta_error())
    connect(client, shop_id)

    assert client.get(f"/shops/{shop_id}/whatsapp").json()["phone_number_id"] is None
    assert await stack.lookup().by_phone_number_id(PHONE_ID) is None

    meta.get(f"{GRAPH}/oauth/access_token").respond(200, json={"access_token": TOKEN})
    assert connect(client, create(client, "Le vrai propriétaire")).status_code == 200


@pytest.mark.parametrize(
    "body",
    [
        {"waba_id": "1/../../me"},
        {"phone_number_id": "2002?fields=x"},
        {"catalog_id": "abc"},
        {"code": ""},
    ],
)
def test_ids_that_would_change_the_url_are_refused(
    client: TestClient, shop_id: str, meta: respx.MockRouter, body: dict[str, str]
) -> None:
    assert connect(client, shop_id, **body).status_code == 422
    assert len(meta.calls) == 0


# The token


def test_the_token_is_never_returned(
    client: TestClient, stack: Stack, shop_id: str, meta: respx.MockRouter
) -> None:
    responses = [
        connect(client, shop_id),
        client.get(f"/shops/{shop_id}/whatsapp"),
        client.get(f"/shops/{shop_id}"),
        client.get("/shops"),
        client.get(f"/shops/{shop_id}/settings"),
    ]
    stored = stack.database[UUID(shop_id)].whatsapp
    assert stored is not None and stored.encrypted_token is not None
    for response in responses:
        assert TOKEN not in response.text
        assert stored.encrypted_token not in response.text
        assert "token" not in response.text


def test_the_token_is_stored_encrypted(
    client: TestClient, stack: Stack, shop_id: str, meta: respx.MockRouter
) -> None:
    connect(client, shop_id)
    stored = stack.database[UUID(shop_id)].whatsapp
    assert stored is not None and stored.encrypted_token is not None
    assert TOKEN not in stored.encrypted_token
    assert stack.cipher.decrypt(stored.encrypted_token) == TOKEN


@pytest.mark.parametrize("failing", [False, True], ids=["success", "meta-failure"])
def test_no_secret_reaches_the_logs(
    client: TestClient,
    stack: Stack,
    shop_id: str,
    meta: respx.MockRouter,
    caplog: pytest.LogCaptureFixture,
    failing: bool,
) -> None:
    if failing:
        meta.post(f"{GRAPH}/{WABA}/subscribed_apps").mock(return_value=meta_error())

    with caplog.at_level(logging.DEBUG), structlog.testing.capture_logs() as logs:
        connect(client, shop_id)
        client.delete(f"/shops/{shop_id}/whatsapp")

    written = caplog.text + repr(logs)
    for secret in (TOKEN, APP_SECRET, "signup-code", "client_secret", "input_token"):
        assert secret not in written


async def test_token_check_does_not_log_its_url(
    client: TestClient,
    stack: Stack,
    shop_id: str,
    meta: respx.MockRouter,
    caplog: pytest.LogCaptureFixture,
) -> None:
    connect(client, shop_id)
    meta.get(f"{GRAPH}/debug_token").respond(200, json={"data": {"is_valid": False}})

    with caplog.at_level(logging.DEBUG), structlog.testing.capture_logs() as logs:
        await stack.check_whatsapp_tokens().execute()

    assert TOKEN not in caplog.text + repr(logs)
    assert APP_SECRET not in caplog.text + repr(logs)


async def test_network_errors_do_not_carry_the_url_away(
    stack: Stack, meta: respx.MockRouter
) -> None:
    meta.get(f"{GRAPH}/oauth/access_token").mock(side_effect=httpx.ConnectError("no route"))
    with pytest.raises(Exception) as caught:
        await stack.meta.exchange_code("signup-code")
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__
    assert APP_SECRET not in str(caught.value)


# Disconnection and the daily check


def test_disconnect(client: TestClient, stack: Stack, shop_id: str, meta: respx.MockRouter) -> None:
    connect(client, shop_id)

    assert client.delete(f"/shops/{shop_id}/whatsapp").status_code == 204

    assert client.get(f"/shops/{shop_id}/whatsapp").json()["status"] == "DISCONNECTED"
    assert status_of(client, shop_id) == "DRAFT"
    assert f"DELETE /v27.0/{WABA}/subscribed_apps" in calls(meta)
    whatsapp = stack.database[UUID(shop_id)].whatsapp
    assert whatsapp is not None and whatsapp.encrypted_token is None


def test_disconnect_works_when_meta_does_not_answer(
    client: TestClient, shop_id: str, meta: respx.MockRouter
) -> None:
    connect(client, shop_id)
    meta.delete(f"{GRAPH}/{WABA}/subscribed_apps").mock(side_effect=httpx.ConnectError("x"))

    assert client.delete(f"/shops/{shop_id}/whatsapp").status_code == 204
    assert client.get(f"/shops/{shop_id}/whatsapp").json()["status"] == "DISCONNECTED"


def test_a_disconnected_number_can_move_to_another_shop(
    client: TestClient, shop_id: str, meta: respx.MockRouter
) -> None:
    connect(client, shop_id)
    client.delete(f"/shops/{shop_id}/whatsapp")
    assert connect(client, create(client, "Autre")).status_code == 200


def test_disconnecting_nothing(client: TestClient, shop_id: str) -> None:
    assert client.delete(f"/shops/{shop_id}/whatsapp").status_code == 422


async def test_daily_check_flags_an_invalid_token(
    client: TestClient, stack: Stack, shop_id: str, meta: respx.MockRouter
) -> None:
    connect(client, shop_id)
    route = meta.get(f"{GRAPH}/debug_token").respond(200, json={"data": {"is_valid": False}})

    assert await stack.check_whatsapp_tokens().execute() == 1

    assert route.calls.last.request.url.params["input_token"] == TOKEN
    assert route.calls.last.request.url.params["access_token"] == f"app-id|{APP_SECRET}"
    whatsapp = client.get(f"/shops/{shop_id}/whatsapp").json()
    assert whatsapp["status"] == "ERROR"
    assert "expired" in whatsapp["error_message"]
    assert status_of(client, shop_id) == "DRAFT"


async def test_daily_check_leaves_a_valid_token_alone(
    client: TestClient, stack: Stack, shop_id: str, meta: respx.MockRouter
) -> None:
    connect(client, shop_id)
    meta.get(f"{GRAPH}/debug_token").respond(200, json={"data": {"is_valid": True}})

    assert await stack.check_whatsapp_tokens().execute() == 0
    assert status_of(client, shop_id) == "ACTIVE"


async def test_a_meta_outage_does_not_disconnect_shops(
    client: TestClient, stack: Stack, shop_id: str, meta: respx.MockRouter
) -> None:
    connect(client, shop_id)
    meta.get(f"{GRAPH}/debug_token").mock(side_effect=httpx.ConnectError("x"))

    assert await stack.check_whatsapp_tokens().execute() == 0
    assert status_of(client, shop_id) == "ACTIVE"


# Lookup for the webhook


async def test_lookup_gives_the_shop_and_the_decrypted_token(
    client: TestClient, stack: Stack, shop_id: str, meta: respx.MockRouter
) -> None:
    connect(client, shop_id)
    found = await stack.lookup().by_phone_number_id(PHONE_ID)
    assert found is not None
    assert str(found.shop_id) == shop_id
    assert found.phone_number_id == PHONE_ID
    assert found.access_token == TOKEN
    assert await stack.lookup().by_shop_id(UUID(shop_id)) == found
    assert TOKEN not in repr(found)


async def test_meta_catalog_of_a_connected_shop(
    client: TestClient, stack: Stack, shop_id: str, meta: respx.MockRouter
) -> None:
    assert await stack.catalogs().for_shop(UUID(shop_id)) is None
    connect(client, shop_id)

    access = await stack.catalogs().for_shop(UUID(shop_id))

    assert access is not None
    assert (access.catalog_id, access.access_token) == ("3003", TOKEN)
    assert access.brand == stack.database[UUID(shop_id)].name
    assert TOKEN not in repr(access)


async def test_no_meta_catalog_without_a_linked_catalog(
    client: TestClient, stack: Stack, shop_id: str, meta: respx.MockRouter
) -> None:
    meta.get(f"{GRAPH}/{WABA}/product_catalogs").respond(200, json={"data": []})
    connect(client, shop_id)
    assert stack.database[UUID(shop_id)].status is ShopStatus.ACTIVE
    assert await stack.catalogs().for_shop(UUID(shop_id)) is None


async def test_lookup_unknown_number(stack: Stack) -> None:
    assert await stack.lookup().by_phone_number_id("999") is None


async def test_lookup_ignores_a_suspended_shop(
    client: TestClient, stack: Stack, shop_id: str, meta: respx.MockRouter
) -> None:
    connect(client, shop_id)
    stack.database[UUID(shop_id)].status = ShopStatus.SUSPENDED
    assert await stack.lookup().by_phone_number_id(PHONE_ID) is None


# Receiving account


RECEIVING = {
    "operator": "MIXX",
    "number": "90 12 34 56",
    "holder_name": "Ama Mensah",
    "aggregator_merchant_id": "merchant-42",
}


def test_no_receiving_account_at_first(client: TestClient, shop_id: str) -> None:
    response = client.get(f"/shops/{shop_id}/receiving-account")
    assert response.status_code == 200
    assert response.json() is None


def test_set_receiving_account_writes_the_audit_log(
    client: TestClient, stack: Stack, shop_id: str
) -> None:
    response = client.put(f"/shops/{shop_id}/receiving-account", json=RECEIVING)

    assert response.status_code == 200
    assert response.json()["number"] == "+22890123456"
    assert response.json()["aggregator_merchant_id"] == "merchant-42"
    assert response.json()["verified"] is False

    [entry] = client.get(f"/shops/{shop_id}/audit-log").json()["items"]
    assert entry["action"] == "receiving_account.updated"
    assert entry["account_id"] == client.headers["X-Test-Account"]
    assert entry["ip"] == "testclient"
    assert entry["details"]["operator"] == "MIXX"
    assert entry["details"]["number"] == "+228****56"
    assert ReceivingAccountChanged in [type(event) for event in stack.events]


def test_the_merchant_id_is_stored_encrypted(
    client: TestClient, stack: Stack, shop_id: str
) -> None:
    client.put(f"/shops/{shop_id}/receiving-account", json=RECEIVING)
    stored = stack.database[UUID(shop_id)].receiving_account
    assert stored is not None and stored.encrypted_merchant_id is not None
    assert "merchant-42" not in stored.encrypted_merchant_id


def test_changing_the_payee_requires_a_new_verification(
    client: TestClient, stack: Stack, shop_id: str
) -> None:
    from dataclasses import replace

    client.put(f"/shops/{shop_id}/receiving-account", json=RECEIVING)
    shop = stack.database[UUID(shop_id)]
    assert shop.receiving_account is not None
    shop.receiving_account = replace(shop.receiving_account, verified=True)

    renamed = {**RECEIVING, "holder_name": "Ama M."}
    same = client.put(f"/shops/{shop_id}/receiving-account", json=renamed)
    assert same.json()["verified"] is True

    other_number = {**RECEIVING, "number": "91 11 11 11"}
    changed = client.put(f"/shops/{shop_id}/receiving-account", json=other_number)
    assert changed.json()["verified"] is False


def test_delete_receiving_account_is_audited(client: TestClient, shop_id: str) -> None:
    client.put(f"/shops/{shop_id}/receiving-account", json=RECEIVING)

    assert client.delete(f"/shops/{shop_id}/receiving-account").status_code == 204

    assert client.get(f"/shops/{shop_id}/receiving-account").json() is None
    actions = [e["action"] for e in client.get(f"/shops/{shop_id}/audit-log").json()["items"]]
    assert actions.count("receiving_account.deleted") == 1


def test_deleting_nothing_writes_nothing(client: TestClient, shop_id: str) -> None:
    assert client.delete(f"/shops/{shop_id}/receiving-account").status_code == 204
    assert client.get(f"/shops/{shop_id}/audit-log").json()["total"] == 0


@pytest.mark.parametrize(
    "body", [{"operator": "ORANGE"}, {"number": "123"}, {"holder_name": "   "}]
)
def test_invalid_receiving_account(client: TestClient, shop_id: str, body: dict[str, str]) -> None:
    response = client.put(f"/shops/{shop_id}/receiving-account", json={**RECEIVING, **body})
    assert response.status_code == 422


# Logo


def png() -> bytes:
    output = BytesIO()
    Image.new("RGB", (1000, 500), "blue").save(output, format="PNG")
    return output.getvalue()


def upload_logo(client: TestClient, stack: Stack, shop_id: str, data: bytes) -> httpx.Response:
    upload = client.post(
        f"/shops/{shop_id}/logo/upload-url", json={"content_type": "image/png", "size": len(data)}
    ).json()
    key = original_logo_key(UUID(shop_id), UUID(upload["logo_id"]))
    stack.uploads.objects[key] = data
    return client.post(f"/shops/{shop_id}/logo/confirm", json={"logo_id": upload["logo_id"]})


def test_logo_journey(client: TestClient, stack: Stack, shop_id: str) -> None:
    response = upload_logo(client, stack, shop_id, png())

    assert response.status_code == 200
    url = response.json()["logo_url"]
    assert url.startswith(f"http://images/shops/{shop_id}/logo/")
    assert url.endswith(".webp")
    key = url.removeprefix("http://images/")
    logo = Image.open(BytesIO(stack.images.objects[key]))
    assert logo.format == "WEBP"
    assert max(logo.size) == 512
    assert stack.uploads.objects == {}


def test_a_new_logo_replaces_the_old_one(client: TestClient, stack: Stack, shop_id: str) -> None:
    upload_logo(client, stack, shop_id, png())
    upload_logo(client, stack, shop_id, png())
    assert len(stack.images.objects) == 1


def test_an_unreadable_logo_is_refused_and_removed(
    client: TestClient, stack: Stack, shop_id: str
) -> None:
    response = upload_logo(client, stack, shop_id, b"not an image")

    assert response.status_code == 422
    assert response.json()["code"] == "shops.invalid_logo"
    assert stack.uploads.objects == {}
    assert client.get(f"/shops/{shop_id}").json()["logo_url"] is None


def test_confirming_without_a_file(client: TestClient, shop_id: str) -> None:
    response = client.post(f"/shops/{shop_id}/logo/confirm", json={"logo_id": str(uuid4())})
    assert response.status_code == 422


@pytest.mark.parametrize(
    "body", [{"content_type": "image/gif", "size": 10}, {"content_type": "image/png", "size": 0}]
)
def test_logo_upload_validation(client: TestClient, shop_id: str, body: dict[str, Any]) -> None:
    assert client.post(f"/shops/{shop_id}/logo/upload-url", json=body).status_code == 422


def test_a_logo_key_belongs_to_its_shop() -> None:
    first, second, logo_id = uuid4(), uuid4(), uuid4()
    assert logo_key(first, logo_id) != logo_key(second, logo_id)


# Meta tells that Woassi lost access


async def test_losing_access_in_meta_frees_the_number(
    client: TestClient, stack: Stack, shop_id: str, meta: respx.MockRouter
) -> None:
    from woassi.modules.shops.application.use_cases.whatsapp import (
        HandleWhatsAppAccountRemoved,
    )

    connect(client, shop_id)
    used_calls = len(meta.calls)

    assert await HandleWhatsAppAccountRemoved(stack.uow()).execute(WABA) == 1

    whatsapp = client.get(f"/shops/{shop_id}/whatsapp").json()
    assert whatsapp["status"] == "DISCONNECTED"
    assert "disconnected from Woassi in Meta" in whatsapp["error_message"]
    assert status_of(client, shop_id) == "DRAFT"
    assert await stack.lookup().by_phone_number_id(PHONE_ID) is None
    # The token is useless by now: Meta is not called.
    assert len(meta.calls) == used_calls
    # Meta may send the event twice.
    assert await HandleWhatsAppAccountRemoved(stack.uow()).execute(WABA) == 0


async def test_losing_access_to_another_account_changes_nothing(
    client: TestClient, stack: Stack, shop_id: str, meta: respx.MockRouter
) -> None:
    from woassi.modules.shops.application.use_cases.whatsapp import (
        HandleWhatsAppAccountRemoved,
    )

    connect(client, shop_id)
    assert await HandleWhatsAppAccountRemoved(stack.uow()).execute("other-waba") == 0
    assert status_of(client, shop_id) == "ACTIVE"
