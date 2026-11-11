"""Product photos: upload request, upload confirmation, and processing by the worker.

The original goes to the private uploads bucket, and the WebP variants to the public products
bucket, as Meta needs stable URLs.
"""

from uuid import UUID

from woassi.modules.catalog.application.dto import UploadPhoto
from woassi.modules.catalog.application.ports import (
    CatalogUnitOfWork,
    ImageProcessor,
    PhotoJobQueue,
)
from woassi.modules.catalog.domain.errors import (
    InvalidPhoto,
    PhotoAlreadyProcessed,
    PhotoNotFound,
    ProductNotFound,
    UnreadableImage,
)
from woassi.modules.catalog.domain.product import (
    DEFAULT_MAX_PHOTOS,
    PhotoStatus,
    Product,
    variant_key,
)
from woassi.shared.application.ports import Clock, StoragePort
from woassi.shared.domain.errors import ObjectNotFound

ALLOWED_CONTENT_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
MAX_SIZE_BYTES = 8 * 1024 * 1024
VARIANT_SIZES = (400, 1024)


async def _load_product(uow: CatalogUnitOfWork, shop_id: UUID, product_id: UUID) -> Product:
    """The shop's product; raises ProductNotFound otherwise."""
    product = await uow.products.get(shop_id, product_id)
    if product is None:
        raise ProductNotFound(f"Product {product_id} not found.")
    return product


class RequestPhotoUpload:
    """Adds a pending photo to a product and returns a presigned upload URL."""

    def __init__(
        self,
        uow: CatalogUnitOfWork,
        uploads: StoragePort,
        *,
        max_photos: int = DEFAULT_MAX_PHOTOS,
        max_size_bytes: int = MAX_SIZE_BYTES,
        expire_s: int = 600,
    ) -> None:
        self._uow = uow
        self._uploads = uploads
        self._max_photos = max_photos
        self._max_size_bytes = max_size_bytes
        self._expire_s = expire_s

    async def execute(
        self, *, shop_id: UUID, product_id: UUID, content_type: str, size: int
    ) -> UploadPhoto:
        """Checks the declared type and size, then reserves a photo slot.

        Raises:
            InvalidPhoto: unsupported type, or size out of bounds.
            ProductNotFound: no such product in this shop.
            TooManyPhotos: the product already has the maximum number of photos.
        """
        if content_type not in ALLOWED_CONTENT_TYPES:
            raise InvalidPhoto(f"Unsupported file type: {content_type}.")
        if size <= 0 or size > self._max_size_bytes:
            raise InvalidPhoto(f"Invalid size: maximum {self._max_size_bytes} bytes.")

        async with self._uow as uow:
            product = await _load_product(uow, shop_id, product_id)
            photo = product.add_photo(content_type=content_type, max_photos=self._max_photos)
            await uow.products.save(product)
            await uow.commit()

        upload_url = await self._uploads.presigned_put(
            photo.original_key, content_type, self._expire_s
        )
        return UploadPhoto(photo_id=photo.id, upload_url=upload_url, expire_s=self._expire_s)


class ConfirmPhoto:
    """Called once the browser has uploaded the file: checks it, then queues its processing."""

    def __init__(
        self,
        uow: CatalogUnitOfWork,
        uploads: StoragePort,
        queue: PhotoJobQueue,
        *,
        max_size_bytes: int = MAX_SIZE_BYTES,
    ) -> None:
        self._uow = uow
        self._uploads = uploads
        self._queue = queue
        self._max_size_bytes = max_size_bytes

    async def execute(self, *, shop_id: UUID, product_id: UUID, photo_id: UUID) -> None:
        """Raises:
        PhotoAlreadyProcessed: the photo is no longer pending.
        InvalidPhoto: the file was not uploaded, or is too large (then the photo is
        rejected).
        """
        async with self._uow as uow:
            product = await _load_product(uow, shop_id, product_id)
            photo = product.photo(photo_id)
            if photo.status is not PhotoStatus.PENDING:
                raise PhotoAlreadyProcessed(f"Photo {photo_id} has already been processed.")

            info = await self._uploads.stat(photo.original_key)
            if info is None:
                raise InvalidPhoto("The file was not received.")

            if info.size > self._max_size_bytes:
                # The declared size proves nothing: only the size of the received object counts.
                await self._uploads.delete(photo.original_key)
                photo.reject()
                await uow.products.save(product)
                await uow.commit()
                raise InvalidPhoto(f"File too large: maximum {self._max_size_bytes} bytes.")

        await self._queue.enqueue_processing(photo_id)


class ProcessPhoto:
    """Run by the worker. No-op if the photo has already been processed.

    A READY photo asks for a Meta sync: Meta's catalog shows the 1024 px variant.
    """

    def __init__(
        self,
        uow: CatalogUnitOfWork,
        uploads: StoragePort,
        images: StoragePort,
        processor: ImageProcessor,
        clock: Clock,
    ) -> None:
        self._uow = uow
        self._uploads = uploads
        self._images = images
        self._processor = processor
        self._clock = clock

    async def execute(self, photo_id: UUID) -> None:
        """Generates the 400 px and 1024 px WebP variants, then deletes the original.

        An unreadable or missing original rejects the photo instead of failing the job.
        """
        async with self._uow as uow:
            product = await uow.products.get_by_photo(photo_id)
        if product is None:
            raise PhotoNotFound(f"Photo {photo_id} not found.")
        photo = product.photo(photo_id)
        if photo.status is not PhotoStatus.PENDING:
            return

        keys: dict[int, str] | None = None
        try:
            original = await self._uploads.get(photo.original_key)
            variants = await self._processor.webp_variants(original, VARIANT_SIZES)
        except (ObjectNotFound, UnreadableImage):
            pass
        else:
            keys = {}
            for size, data in variants.items():
                key = variant_key(product.shop_id, product.id, photo.id, size)
                await self._images.put(key, data, "image/webp")
                keys[size] = key

        async with self._uow as uow:
            # Reloaded under lock: a stock reservation during the processing must not be lost.
            locked = await uow.products.list_for_update(product.shop_id, ids=[product.id])
            if not locked or locked[0].photo(photo_id).status is not PhotoStatus.PENDING:
                return
            product = locked[0]
            if keys is None:
                product.photo(photo_id).reject()
            else:
                product.mark_photo_ready(photo_id, keys, self._clock.now())
            await uow.products.save(product)
            await uow.commit()

        await self._uploads.delete(photo.original_key)
