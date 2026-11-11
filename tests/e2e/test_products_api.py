from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from tests.fakes import NOW, FakeCatalogSyncQueue, FakeJobQueue, FakeStorage, FakeUoW
from woassi.config import Settings
from woassi.main import create_app
from woassi.modules.catalog.api.deps import get_catalog
from woassi.modules.catalog.application.use_cases.meta_sync import RequestCatalogResync
from woassi.modules.catalog.application.use_cases.photos import (
    ConfirmPhoto,
    RequestPhotoUpload,
)
from woassi.modules.catalog.application.use_cases.products import (
    CreateProduct,
    GetProduct,
    ListProducts,
    UpdateProduct,
)
from woassi.shared.api.deps import get_authorized_shop
from woassi.shared.infrastructure.clock import FrozenClock


def allow_shop(shop_id: UUID) -> UUID:
    return shop_id


class FakeCatalog:
    def __init__(self) -> None:
        self.uow = FakeUoW()
        self.uploads = FakeStorage()
        self.images = FakeStorage("http://images")
        self.jobs = FakeJobQueue()
        self.sync_queue = FakeCatalogSyncQueue()
        self.clock = FrozenClock(NOW)

    def create_product(self) -> CreateProduct:
        return CreateProduct(self.uow, self.images, self.clock)

    def list_products(self) -> ListProducts:
        return ListProducts(self.uow, self.images)

    def get_product(self) -> GetProduct:
        return GetProduct(self.uow, self.images)

    def update_product(self) -> UpdateProduct:
        return UpdateProduct(self.uow, self.images, self.clock)

    def request_catalog_resync(self) -> RequestCatalogResync:
        return RequestCatalogResync(self.uow, self.sync_queue, self.clock)

    def request_photo_upload(self) -> RequestPhotoUpload:
        return RequestPhotoUpload(self.uow, self.uploads)

    def confirm_photo(self) -> ConfirmPhoto:
        return ConfirmPhoto(self.uow, self.uploads, self.jobs)


@pytest.fixture
def catalog() -> FakeCatalog:
    return FakeCatalog()


@pytest.fixture
def client(settings: Settings, catalog: FakeCatalog) -> TestClient:
    app = create_app(settings)
    app.dependency_overrides[get_catalog] = lambda: catalog
    app.dependency_overrides[get_authorized_shop] = allow_shop
    return TestClient(app)


PRODUCT = {"name": "Pagne", "price": 5000, "stock": 3, "retailer_id": "SKU-1"}


def test_health(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok"}


def test_create_list_get(client: TestClient) -> None:
    base = f"/shops/{uuid4()}/products"

    created = client.post(base, json=PRODUCT)
    assert created.status_code == 201
    assert created.json()["price"] == 5000
    assert created.json()["photos"] == []

    assert client.get(base).json() == {
        "items": [created.json()],
        "total": 1,
        "offset": 0,
        "limit": 50,
    }
    assert client.get(f"{base}/{created.json()['id']}").json() == created.json()


def test_product_not_found_returns_code_and_message(client: TestClient) -> None:
    response = client.get(f"/shops/{uuid4()}/products/{uuid4()}")
    assert response.status_code == 404
    assert response.json()["code"] == "catalog.product_not_found"
    assert response.json().keys() == {"code", "message"}


def test_name_empty_after_strip(client: TestClient) -> None:
    response = client.post(f"/shops/{uuid4()}/products", json={**PRODUCT, "name": "   "})
    assert response.status_code == 422
    assert response.json()["code"] == "catalog.invalid_product"


@pytest.mark.parametrize("field", [{"price": -1}, {"price": 10.5}, {"stock": -1}])
def test_schema_validation(client: TestClient, field: dict[str, float]) -> None:
    response = client.post(f"/shops/{uuid4()}/products", json={**PRODUCT, **field})
    assert response.status_code == 422


def test_photo_upload_flow(client: TestClient, catalog: FakeCatalog) -> None:
    base = f"/shops/{uuid4()}/products"
    product_id = client.post(base, json=PRODUCT).json()["id"]

    upload = client.post(
        f"{base}/{product_id}/photos/upload-url",
        json={"content_type": "image/jpeg", "size": 1000},
    )
    assert upload.status_code == 200
    photo_id = upload.json()["photo_id"]

    # The file has not been sent to storage yet
    confirm_url = f"{base}/{product_id}/photos/{photo_id}/confirm"
    assert client.post(confirm_url).status_code == 422

    product = next(iter(catalog.uow.repository.products.values()))
    catalog.uploads.objects[product.photos[0].original_key] = b"image"
    assert client.post(confirm_url).status_code == 202
    assert [str(p) for p in catalog.jobs.photos] == [photo_id]


def test_too_many_photos_is_a_conflict(client: TestClient) -> None:
    base = f"/shops/{uuid4()}/products"
    product_id = client.post(base, json=PRODUCT).json()["id"]
    body = {"content_type": "image/jpeg", "size": 1000}
    for _ in range(5):
        assert client.post(f"{base}/{product_id}/photos/upload-url", json=body).status_code == 200

    response = client.post(f"{base}/{product_id}/photos/upload-url", json=body)
    assert response.status_code == 409
    assert response.json()["code"] == "catalog.too_many_photos"


def test_product_exposes_its_meta_sync_state(client: TestClient) -> None:
    created = client.post(f"/shops/{uuid4()}/products", json=PRODUCT).json()
    assert created["meta_sync"] == {
        "status": "PENDING",
        "synced_at": None,
        "error_code": None,
        "next_attempt_at": "2026-01-01T09:05:00Z",
    }


def test_update_product(client: TestClient, catalog: FakeCatalog) -> None:
    base = f"/shops/{uuid4()}/products"
    product_id = client.post(base, json=PRODUCT).json()["id"]
    catalog.uow.events.clear()

    response = client.patch(f"{base}/{product_id}", json={"price": 6000, "active": False})

    assert response.status_code == 200
    assert (response.json()["price"], response.json()["active"]) == (6000, False)
    assert response.json()["stock"] == 3
    assert len(catalog.uow.events) == 1


def test_update_unknown_product_is_404(client: TestClient) -> None:
    response = client.patch(f"/shops/{uuid4()}/products/{uuid4()}", json={"price": 1})
    assert response.status_code == 404


@pytest.mark.parametrize("body", [{"name": ""}, {"stock": -1}, {"price": -5}])
def test_update_validation(client: TestClient, body: dict[str, object]) -> None:
    base = f"/shops/{uuid4()}/products"
    product_id = client.post(base, json=PRODUCT).json()["id"]
    assert client.patch(f"{base}/{product_id}", json=body).status_code == 422


def test_force_resync(client: TestClient, catalog: FakeCatalog) -> None:
    shop_id = uuid4()
    client.post(f"/shops/{shop_id}/products", json=PRODUCT)
    client.post(f"/shops/{shop_id}/products", json={**PRODUCT, "retailer_id": "SKU-2"})

    response = client.post(f"/shops/{shop_id}/products/sync")

    assert response.status_code == 202
    assert response.json() == {"pending": 2}
    assert catalog.sync_queue.shops == [shop_id]
