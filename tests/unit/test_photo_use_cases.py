from uuid import uuid4

import pytest

from tests.fakes import NOW, FakeImageProcessor, FakeJobQueue, FakeStorage, FakeUoW
from woassi.modules.catalog.application.use_cases.photos import (
    ConfirmPhoto,
    ProcessPhoto,
    RequestPhotoUpload,
)
from woassi.modules.catalog.domain.errors import (
    InvalidPhoto,
    PhotoAlreadyProcessed,
    PhotoNotFound,
    ProductNotFound,
    TooManyPhotos,
)
from woassi.modules.catalog.domain.product import PhotoStatus, Product, ProductPhoto
from woassi.shared.domain.values import Amount
from woassi.shared.infrastructure.clock import FrozenClock

EIGHT_MB = 8 * 1024 * 1024


@pytest.fixture
def uow() -> FakeUoW:
    return FakeUoW()


@pytest.fixture
def uploads() -> FakeStorage:
    return FakeStorage()


@pytest.fixture
def images() -> FakeStorage:
    return FakeStorage("http://images")


@pytest.fixture
def product(uow: FakeUoW) -> Product:
    product = Product.create(
        now=NOW, shop_id=uuid4(), name="Pagne", price=Amount(5000), stock=1, retailer_id="SKU-1"
    )
    uow.repository.products[product.id] = product
    return product


@pytest.fixture
def photo(product: Product) -> ProductPhoto:
    return product.add_photo(content_type="image/jpeg")


# RequestPhotoUpload


async def test_request_upload_creates_pending_photo(
    uow: FakeUoW, uploads: FakeStorage, product: Product
) -> None:
    upload = await RequestPhotoUpload(uow, uploads).execute(
        shop_id=product.shop_id,
        product_id=product.id,
        content_type="image/png",
        size=EIGHT_MB,
    )

    photo = uow.repository.products[product.id].photo(upload.photo_id)
    assert photo.status is PhotoStatus.PENDING
    assert photo.original_key in upload.upload_url
    assert upload.expire_s == 600


@pytest.mark.parametrize(
    ("content_type", "size"),
    [("image/gif", 10), ("application/pdf", 10), ("image/png", EIGHT_MB + 1), ("image/png", 0)],
)
async def test_request_upload_rejects_type_or_size(
    uow: FakeUoW, uploads: FakeStorage, product: Product, content_type: str, size: int
) -> None:
    with pytest.raises(InvalidPhoto):
        await RequestPhotoUpload(uow, uploads).execute(
            shop_id=product.shop_id,
            product_id=product.id,
            content_type=content_type,
            size=size,
        )


async def test_request_upload_unknown_product(uow: FakeUoW, uploads: FakeStorage) -> None:
    with pytest.raises(ProductNotFound):
        await RequestPhotoUpload(uow, uploads).execute(
            shop_id=uuid4(), product_id=uuid4(), content_type="image/png", size=10
        )


async def test_request_upload_enforces_configured_maximum(
    uow: FakeUoW, uploads: FakeStorage, product: Product
) -> None:
    use_case = RequestPhotoUpload(uow, uploads, max_photos=1)
    arguments = {
        "shop_id": product.shop_id,
        "product_id": product.id,
        "content_type": "image/png",
        "size": 10,
    }
    await use_case.execute(**arguments)  # type: ignore[arg-type]
    with pytest.raises(TooManyPhotos):
        await use_case.execute(**arguments)  # type: ignore[arg-type]


# ConfirmPhoto


async def test_confirm_enqueues(
    uow: FakeUoW, uploads: FakeStorage, product: Product, photo: ProductPhoto
) -> None:
    uploads.objects[photo.original_key] = b"image"
    queue = FakeJobQueue()

    await ConfirmPhoto(uow, uploads, queue).execute(
        shop_id=product.shop_id, product_id=product.id, photo_id=photo.id
    )

    assert queue.photos == [photo.id]


async def test_confirm_without_file_fails(
    uow: FakeUoW, uploads: FakeStorage, product: Product, photo: ProductPhoto
) -> None:
    queue = FakeJobQueue()
    with pytest.raises(InvalidPhoto):
        await ConfirmPhoto(uow, uploads, queue).execute(
            shop_id=product.shop_id, product_id=product.id, photo_id=photo.id
        )
    assert queue.photos == []


async def test_confirm_deletes_oversized_file(
    uow: FakeUoW, uploads: FakeStorage, product: Product, photo: ProductPhoto
) -> None:
    uploads.objects[photo.original_key] = b"12345"
    queue = FakeJobQueue()

    with pytest.raises(InvalidPhoto):
        await ConfirmPhoto(uow, uploads, queue, max_size_bytes=4).execute(
            shop_id=product.shop_id, product_id=product.id, photo_id=photo.id
        )

    assert uploads.objects == {}
    assert queue.photos == []
    assert uow.repository.products[product.id].photo(photo.id).status is PhotoStatus.REJECTED


async def test_confirm_already_processed_photo_is_a_conflict(
    uow: FakeUoW, uploads: FakeStorage, product: Product, photo: ProductPhoto
) -> None:
    photo.mark_ready({400: "x"})
    with pytest.raises(PhotoAlreadyProcessed):
        await ConfirmPhoto(uow, uploads, FakeJobQueue()).execute(
            shop_id=product.shop_id, product_id=product.id, photo_id=photo.id
        )


async def test_confirm_photo_of_another_product_fails(
    uow: FakeUoW, uploads: FakeStorage, product: Product
) -> None:
    with pytest.raises(PhotoNotFound):
        await ConfirmPhoto(uow, uploads, FakeJobQueue()).execute(
            shop_id=product.shop_id, product_id=product.id, photo_id=uuid4()
        )


# ProcessPhoto


async def test_process_writes_variants_and_deletes_original(
    uow: FakeUoW,
    uploads: FakeStorage,
    images: FakeStorage,
    product: Product,
    photo: ProductPhoto,
) -> None:
    uploads.objects[photo.original_key] = b"image"

    await ProcessPhoto(uow, uploads, images, FakeImageProcessor(), FrozenClock(NOW)).execute(
        photo.id
    )

    base = f"shops/{product.shop_id}/products/{product.id}/{photo.id}"
    assert images.objects == {f"{base}_400.webp": b"webp-400", f"{base}_1024.webp": b"webp-1024"}
    assert uploads.objects == {}
    processed = uow.repository.products[product.id].photo(photo.id)
    assert processed.status is PhotoStatus.READY
    assert processed.variant_keys == {400: f"{base}_400.webp", 1024: f"{base}_1024.webp"}


async def test_process_rejects_unreadable_image(
    uow: FakeUoW,
    uploads: FakeStorage,
    images: FakeStorage,
    product: Product,
    photo: ProductPhoto,
) -> None:
    uploads.objects[photo.original_key] = b"not an image"

    await ProcessPhoto(
        uow, uploads, images, FakeImageProcessor(unreadable=True), FrozenClock(NOW)
    ).execute(photo.id)

    assert uow.repository.products[product.id].photo(photo.id).status is PhotoStatus.REJECTED
    assert images.objects == {}
    assert uploads.objects == {}


async def test_process_without_original_rejects(
    uow: FakeUoW,
    uploads: FakeStorage,
    images: FakeStorage,
    product: Product,
    photo: ProductPhoto,
) -> None:
    await ProcessPhoto(uow, uploads, images, FakeImageProcessor(), FrozenClock(NOW)).execute(
        photo.id
    )
    assert uow.repository.products[product.id].photo(photo.id).status is PhotoStatus.REJECTED


async def test_process_is_noop_on_ready_photo(
    uow: FakeUoW,
    uploads: FakeStorage,
    images: FakeStorage,
    photo: ProductPhoto,
) -> None:
    photo.mark_ready({400: "x"})
    await ProcessPhoto(uow, uploads, images, FakeImageProcessor(), FrozenClock(NOW)).execute(
        photo.id
    )
    assert images.objects == {}


async def test_process_unknown_photo_fails(
    uow: FakeUoW, uploads: FakeStorage, images: FakeStorage
) -> None:
    with pytest.raises(PhotoNotFound):
        await ProcessPhoto(uow, uploads, images, FakeImageProcessor(), FrozenClock(NOW)).execute(
            uuid4()
        )
