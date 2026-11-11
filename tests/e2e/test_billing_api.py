from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.billing_fakes import BillingStack
from tests.fakes import FakeIdempotencyStore
from woassi.config import Settings
from woassi.main import create_app
from woassi.modules.billing.api.deps import get_billing
from woassi.modules.billing.application.ports import Contact, Template
from woassi.modules.billing.application.use_cases.credit_notes import IssueCreditNote
from woassi.modules.billing.application.use_cases.issuing import ResendInvoice
from woassi.modules.billing.application.use_cases.queries import (
    GetBillingDocument,
    GetBillingSummary,
    GetDocumentPdf,
    ListBillingDocuments,
    OpenInvoiceLink,
)
from woassi.modules.billing.domain.document import BillingDocument
from woassi.shared.api.deps import get_authorized_shop, get_current_account, get_shared
from woassi.shared.application.ports import AuthenticatedAccount
from woassi.shared.domain.errors import ShopNotFound

ACCOUNT = uuid4()


class Facade:
    def __init__(self, stack: BillingStack) -> None:
        self.stack = stack

    def list_billing_documents(self) -> ListBillingDocuments:
        return self.stack.list_documents()

    def get_billing_document(self) -> GetBillingDocument:
        return self.stack.get_document()

    def get_document_pdf(self) -> GetDocumentPdf:
        return self.stack.pdf()

    def resend_invoice(self) -> ResendInvoice:
        return self.stack.resend()

    def issue_credit_note(self) -> IssueCreditNote:
        return self.stack.credit_notes()

    def open_invoice_link(self) -> OpenInvoiceLink:
        return self.stack.open_link()

    def get_billing_summary(self) -> GetBillingSummary:
        return self.stack.summary()


class FakeShared:
    def __init__(self) -> None:
        self.store = FakeIdempotencyStore()

    def idempotency_store(self) -> FakeIdempotencyStore:
        return self.store


@pytest.fixture
def stack() -> BillingStack:
    return BillingStack()


def build(settings: Settings, stack: BillingStack, *, authenticated: bool = True) -> TestClient:
    app: FastAPI = create_app(settings)
    app.dependency_overrides[get_billing] = lambda: Facade(stack)
    shared = FakeShared()
    app.dependency_overrides[get_shared] = lambda: shared
    if authenticated:
        app.dependency_overrides[get_current_account] = lambda: AuthenticatedAccount(id=ACCOUNT)

        def only_this_shop(shop_id: UUID) -> UUID:
            if shop_id != stack.shop_id:
                raise ShopNotFound("not yours")
            return shop_id

        app.dependency_overrides[get_authorized_shop] = only_this_shop
    return TestClient(app, follow_redirects=False)


@pytest.fixture
def client(settings: Settings, stack: BillingStack) -> TestClient:
    return build(settings, stack)


@pytest.fixture
async def invoice(stack: BillingStack) -> BillingDocument:
    issued = await stack.pay(stack.order(number=42))
    assert issued is not None
    return issued


def base(stack: BillingStack) -> str:
    return f"/shops/{stack.shop_id}"


def test_routes_need_authentication(settings: Settings, stack: BillingStack) -> None:
    client = build(settings, stack, authenticated=False)
    some = uuid4()
    for method, path in [
        ("GET", "/invoices"),
        ("GET", f"/invoices/{some}"),
        ("GET", f"/invoices/{some}/pdf"),
        ("POST", f"/invoices/{some}/resend"),
        ("POST", f"/invoices/{some}/credit-notes"),
        ("GET", "/credit-notes"),
        ("GET", f"/credit-notes/{some}"),
        ("GET", f"/credit-notes/{some}/pdf"),
        ("GET", "/billing/summary?issued_from=2026-11-01&issued_to=2026-11-30"),
    ]:
        response = client.request(method, base(stack) + path, json={"reason": "x", "full": True})
        assert response.status_code == 401, path


def test_a_foreign_shop_is_not_found(client: TestClient, invoice: BillingDocument) -> None:
    other = f"/shops/{uuid4()}"
    assert client.get(f"{other}/invoices").status_code == 404
    assert client.get(f"{other}/invoices/{invoice.id}").status_code == 404
    assert client.get(f"{other}/invoices/{invoice.id}/pdf").status_code == 404
    response = client.post(
        f"{other}/invoices/{invoice.id}/credit-notes", json={"reason": "x", "full": True}
    )
    assert response.status_code == 404
    assert client.get(f"{other}/credit-notes").status_code == 404


def test_list_and_get_invoices(
    client: TestClient, stack: BillingStack, invoice: BillingDocument
) -> None:
    page = client.get(f"{base(stack)}/invoices").json()
    assert page["total"] == 1
    [item] = page["items"]
    assert item["number"] == "FAC-2026-000001"
    assert item["status"] == "ISSUED"
    assert item["payment_status"] == "PAID"
    assert item["total"] == 22_500
    assert item["total_tax"] == 0
    assert item["tax_totals"] == [
        {
            "tax_code": "NOT_SUBJECT",
            "rate_bp": 0,
            "amount_excl_tax": 22_500,
            "tax_amount": 0,
            "total": 22_500,
        }
    ]
    assert item["certification"]["status"] == "NOT_REQUIRED"
    assert item["delivery_status"] == "SENT"
    assert item["pdf_ready"] is True
    assert "user_id" not in item["buyer"]
    assert "TG.123" not in client.get(f"{base(stack)}/invoices").text  # never the BSUID

    filtered = client.get(f"{base(stack)}/invoices", params={"order_id": str(uuid4())}).json()
    assert filtered["total"] == 0
    dated = client.get(
        f"{base(stack)}/invoices", params={"issued_from": "2026-11-11", "issued_to": "2026-11-30"}
    ).json()
    assert dated["total"] == 0

    one = client.get(f"{base(stack)}/invoices/{invoice.id}")
    assert one.status_code == 200
    assert one.json()["order_number"] == 42
    assert client.get(f"{base(stack)}/invoices/{uuid4()}").json()["code"] == (
        "billing.invoice_not_found"
    )
    # An invoice is not a credit note.
    assert client.get(f"{base(stack)}/credit-notes/{invoice.id}").status_code == 404


def test_the_pdf_link(client: TestClient, stack: BillingStack, invoice: BillingDocument) -> None:
    response = client.get(f"{base(stack)}/invoices/{invoice.id}/pdf")
    assert response.status_code == 200
    assert response.json() == {
        "url": f"http://signed/get/shops/{stack.shop_id}/invoices/FAC-2026-000001.pdf?expire=300",
        "expires_in_s": 300,
    }
    redirect = client.get(f"{base(stack)}/invoices/{invoice.id}/pdf", params={"redirect": True})
    assert redirect.status_code == 302
    assert redirect.headers["location"] == response.json()["url"]


def test_credit_notes(client: TestClient, stack: BillingStack, invoice: BillingDocument) -> None:
    url = f"{base(stack)}/invoices/{invoice.id}/credit-notes"
    assert client.post(url, json={"reason": "x"}).status_code == 422
    assert client.post(url, json={"reason": "x", "full": True, "amount": 5}).status_code == 422
    assert client.post(url, json={"reason": "x", "amount": -1}).status_code == 422
    assert (
        client.post(url, json={"reason": "x", "lines": [{"position": 9, "quantity": 1}]}).json()[
            "code"
        ]
        == "billing.invalid_credit_note"
    )

    created = client.post(
        url,
        json={
            "reason": "Pagne abîmé",
            "lines": [{"position": 1, "quantity": 1}],
            "refund_reference": "MX123",
        },
        headers={"Idempotency-Key": "credit-1"},
    )
    assert created.status_code == 201
    note = created.json()
    assert note["number"] == "AV-2026-000001"
    assert note["document_type"] == "CREDIT_NOTE"
    assert note["total"] == 7_500
    assert note["original_number"] == "FAC-2026-000001"
    assert note["issued_by"] == str(ACCOUNT)
    assert note["pdf_ready"] is False  # rendered by the worker

    # A retried request with the same key is replayed, not issued twice.
    replay = client.post(
        url,
        json={
            "reason": "Pagne abîmé",
            "lines": [{"position": 1, "quantity": 1}],
            "refund_reference": "MX123",
        },
        headers={"Idempotency-Key": "credit-1"},
    )
    assert replay.json()["id"] == note["id"]
    assert len(stack.documents) == 2

    too_much = client.post(url, json={"reason": "x", "amount": 20_000})
    assert too_much.status_code == 409
    assert too_much.json()["code"] == "billing.credit_exceeds_invoice"

    assert client.get(f"{base(stack)}/invoices/{invoice.id}").json()["status"] == (
        "PARTIALLY_CREDITED"
    )
    listed = client.get(f"{base(stack)}/credit-notes", params={"invoice_id": str(invoice.id)})
    assert [item["id"] for item in listed.json()["items"]] == [note["id"]]
    assert client.get(f"{base(stack)}/credit-notes/{note['id']}").status_code == 200
    pending = client.get(f"{base(stack)}/credit-notes/{note['id']}/pdf")
    assert pending.status_code == 409
    assert pending.json()["code"] == "billing.pdf_not_ready"
    [entry] = [e for e in stack.audit_log.entries if e.action == "credit_note.issued"]
    assert entry.account_id == ACCOUNT


def test_resend(client: TestClient, stack: BillingStack, invoice: BillingDocument) -> None:
    response = client.post(f"{base(stack)}/invoices/{invoice.id}/resend")
    assert response.status_code == 200
    assert len(stack.messaging.sent) == 2
    stack.messaging.failing = True
    failed = client.post(f"{base(stack)}/invoices/{invoice.id}/resend")
    assert failed.status_code == 502
    assert failed.json()["code"] == "billing.invoice_not_sent"


def test_summary(client: TestClient, stack: BillingStack, invoice: BillingDocument) -> None:
    response = client.get(
        f"{base(stack)}/billing/summary",
        params={"issued_from": "2026-11-01", "issued_to": "2026-11-30"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["invoices"]["documents"] == 1
    assert body["invoices"]["total"] == 22_500
    assert body["net"]["amount_excl_tax"] == 22_500
    reversed_period = client.get(
        f"{base(stack)}/billing/summary",
        params={"issued_from": "2026-11-30", "issued_to": "2026-11-01"},
    )
    assert reversed_period.status_code == 422


async def test_the_public_invoice_link(settings: Settings, stack: BillingStack) -> None:
    stack.customers.contacts[stack.customer_id] = Contact(user_id="TG.123")
    await stack.pay(stack.order())
    [(_, sent, _)] = stack.messaging.sent
    assert isinstance(sent, Template)
    token = sent.parameters["link"].rsplit("/", 1)[1]
    client = build(settings, stack, authenticated=False)  # public: no account needed

    response = client.get(f"/invoices/{token}")
    assert response.status_code == 302
    assert response.headers["location"].startswith(f"http://signed/get/shops/{stack.shop_id}/")
    assert response.headers["cache-control"] == "no-store"

    for unknown in (token[:-1] + ("A" if token[-1] != "A" else "B"), "short"):
        missing = client.get(f"/invoices/{unknown}")
        assert missing.status_code == 404
        assert missing.json()["code"] == "billing.link_not_found"
    stack.clock.advance(timedelta(days=31))
    assert client.get(f"/invoices/{token}").status_code == 404
