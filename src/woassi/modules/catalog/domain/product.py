"""Products, their stock and their photos."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID, uuid4

from woassi.modules.catalog.domain.errors import (
    InsufficientStock,
    InvalidProduct,
    PhotoNotFound,
    TooManyPhotos,
)
from woassi.modules.catalog.domain.events import ProductCreated, ProductMetaSyncRequested
from woassi.shared.domain.events import Entity
from woassi.shared.domain.values import Amount

DEFAULT_MAX_PHOTOS = 5
# Width of the photo variant shown in Meta's catalog.
META_IMAGE_SIZE = 1024
# If the event is lost, the retry cron picks a pending product up after this delay.
META_SYNC_GRACE = timedelta(minutes=5)
META_SYNC_FIRST_RETRY = timedelta(minutes=1)
META_SYNC_MAX_RETRY = timedelta(hours=6)


def _required_name(name: str) -> str:
    """Strips `name`; raises InvalidProduct if nothing is left."""
    name = name.strip()
    if not name:
        raise InvalidProduct("Product name is required.")
    return name


def original_key(shop_id: UUID, product_id: UUID, photo_id: UUID) -> str:
    """Object key of an uploaded original, in the private uploads bucket."""
    return f"shops/{shop_id}/products/{product_id}/originals/{photo_id}"


def variant_key(shop_id: UUID, product_id: UUID, photo_id: UUID, size: int) -> str:
    """Object key of a WebP variant `size` pixels wide, in the public products bucket."""
    return f"shops/{shop_id}/products/{product_id}/{photo_id}_{size}.webp"


class PhotoStatus(StrEnum):
    """PENDING until processed, then READY, or REJECTED if the file was unusable."""

    PENDING = "PENDING"
    READY = "READY"
    REJECTED = "REJECTED"


@dataclass(slots=True)
class ProductPhoto:
    """A photo of a product. `variant_keys` maps each width to its WebP object key."""

    id: UUID
    product_id: UUID
    original_key: str
    content_type: str
    status: PhotoStatus
    position: int
    variant_keys: dict[int, str] = field(default_factory=dict)

    def mark_ready(self, variant_keys: dict[int, str]) -> None:
        """The variants are stored: the photo can be shown."""
        self.status = PhotoStatus.READY
        self.variant_keys = dict(variant_keys)

    def reject(self) -> None:
        """The file was missing, too large or unreadable."""
        self.status = PhotoStatus.REJECTED


class MetaSyncStatus(StrEnum):
    """Where a product stands with Meta's catalog.

    PENDING: a change waits to be sent (or the shop has no connected catalog yet).
    SYNCED: Meta accepted the product's current version. FAILED: retried with backoff.
    """

    PENDING = "PENDING"
    SYNCED = "SYNCED"
    FAILED = "FAILED"


@dataclass(slots=True)
class MetaSync:
    """Sync state of a product with Meta's catalog.

    `version` grows with every change to send: a result only marks the product SYNCED if no
    change came in while Meta was being called. `next_attempt_at` is when the retry cron may
    pick it up; None while waiting for something else (a connected catalog, a photo).
    `error_code` is ours, never Meta's text. `published` tells whether Meta has the item, so
    that a deactivated product is only deleted there if it was sent.
    """

    status: MetaSyncStatus = MetaSyncStatus.PENDING
    version: int = 0
    attempts: int = 0
    next_attempt_at: datetime | None = None
    synced_at: datetime | None = None
    error_code: str | None = None
    published: bool = False


def meta_retry_delay(attempts: int) -> timedelta:
    """Backoff after `attempts` failures: 1, 2, 4... minutes, capped at 6 hours."""
    delay = META_SYNC_FIRST_RETRY * (1 << min(max(attempts - 1, 0), 20))
    return min(delay, META_SYNC_MAX_RETRY)


@dataclass(slots=True)
class Product(Entity):
    """A product of a shop's catalog.

    `retailer_id` is the SKU shared with Meta's catalog: carts refer to products by it.
    """

    id: UUID
    shop_id: UUID
    name: str
    price: Amount
    stock: int
    retailer_id: str
    description: str = ""
    active: bool = True
    photos: list[ProductPhoto] = field(default_factory=list)
    meta_sync: MetaSync = field(default_factory=MetaSync)

    @classmethod
    def create(
        cls,
        *,
        shop_id: UUID,
        name: str,
        price: Amount,
        stock: int,
        retailer_id: str,
        now: datetime,
        description: str = "",
    ) -> "Product":
        """Creates a product, to be sent to Meta's catalog.

        Records `ProductCreated` and `ProductMetaSyncRequested`. Raises InvalidProduct for bad
        data.
        """
        name = _required_name(name)
        retailer_id = retailer_id.strip()
        if not retailer_id:
            raise InvalidProduct("Product retailer_id is required.")
        if stock < 0:
            raise InvalidProduct("Stock cannot be negative.")
        product = cls(
            id=uuid4(),
            shop_id=shop_id,
            name=name,
            price=price,
            stock=stock,
            retailer_id=retailer_id,
            description=description,
        )
        product.record_event(
            ProductCreated(product_id=product.id, shop_id=shop_id, retailer_id=retailer_id)
        )
        product.request_meta_sync(now)
        return product

    @property
    def in_stock(self) -> bool:
        """What Meta shows as availability: `in stock` or `out of stock`."""
        return self.stock > 0

    def update(
        self,
        *,
        now: datetime,
        name: str | None = None,
        price: Amount | None = None,
        description: str | None = None,
        active: bool | None = None,
    ) -> None:
        """Changes the given fields; asks for a Meta sync if any of them actually changed.

        Deactivating a product removes it from Meta's catalog. Raises InvalidProduct for a
        blank name.
        """
        before = (self.name, self.price, self.description, self.active)
        if name is not None:
            self.name = _required_name(name)
        if price is not None:
            self.price = price
        if description is not None:
            self.description = description
        if active is not None:
            self.active = active
        if (self.name, self.price, self.description, self.active) != before:
            self.request_meta_sync(now)

    def adjust_stock(self, delta: int, *, now: datetime) -> None:
        """Adds `delta` (negative to remove). Raises InsufficientStock below zero.

        Only a change of availability (to or from zero) asks for a Meta sync: Meta does not get
        the quantity, so an order that leaves stock does not cost a Graph call.
        """
        if self.stock + delta < 0:
            raise InsufficientStock(f"Insufficient stock for {self.name}: {self.stock} available.")
        self.set_stock(self.stock + delta, now=now)

    def set_stock(self, stock: int, *, now: datetime) -> None:
        """Replaces the stock count. Raises InvalidProduct below zero."""
        if stock < 0:
            raise InvalidProduct("Stock cannot be negative.")
        was_in_stock = self.in_stock
        self.stock = stock
        if self.in_stock != was_in_stock:
            self.request_meta_sync(now)

    def mark_photo_ready(self, photo_id: UUID, variant_keys: dict[int, str], now: datetime) -> None:
        """The photo's variants are stored: Meta's catalog must get the new image."""
        self.photo(photo_id).mark_ready(variant_keys)
        self.request_meta_sync(now)

    @property
    def meta_image_keys(self) -> list[str]:
        """Object keys of the images sent to Meta, main image first."""
        ready = sorted(
            (p for p in self.photos if p.status is PhotoStatus.READY), key=lambda p: p.position
        )
        return [p.variant_keys[META_IMAGE_SIZE] for p in ready if META_IMAGE_SIZE in p.variant_keys]

    def request_meta_sync(self, now: datetime) -> None:
        """Marks the product as to be sent, and records one `ProductMetaSyncRequested`."""
        self.mark_meta_sync_pending(now)
        if not any(isinstance(e, ProductMetaSyncRequested) for e in self._events):
            self.record_event(ProductMetaSyncRequested(product_id=self.id, shop_id=self.shop_id))

    def mark_meta_sync_pending(self, now: datetime) -> None:
        """Marks the product as to be sent, without an event: for a whole-shop resync, which
        queues a single job instead of one per product.
        """
        sync = self.meta_sync
        sync.status = MetaSyncStatus.PENDING
        sync.version += 1
        sync.attempts = 0
        sync.error_code = None
        sync.next_attempt_at = now + META_SYNC_GRACE

    def wait_for_meta_sync(self, version: int, reason: str) -> None:
        """Nothing can be sent yet (no connected catalog, no photo): stays PENDING, out of the
        retry cron, until a new change or a resync. `reason` is an error code.
        """
        if self.meta_sync.version != version:
            return
        self.meta_sync.status = MetaSyncStatus.PENDING
        self.meta_sync.next_attempt_at = None
        self.meta_sync.error_code = reason

    def mark_meta_synced(self, version: int, *, published: bool, now: datetime) -> None:
        """Meta accepted `version` (an upsert if `published`, a deletion otherwise).

        A product changed meanwhile stays PENDING: its newer version still has to go.
        """
        self.meta_sync.published = published
        if self.meta_sync.version != version:
            return
        self.meta_sync.status = MetaSyncStatus.SYNCED
        self.meta_sync.attempts = 0
        self.meta_sync.next_attempt_at = None
        self.meta_sync.synced_at = now
        self.meta_sync.error_code = None

    def mark_meta_sync_failed(self, version: int, error_code: str, now: datetime) -> None:
        """Meta refused `version` or could not be reached: retried later, with backoff."""
        if self.meta_sync.version != version:
            return
        sync = self.meta_sync
        sync.status = MetaSyncStatus.FAILED
        sync.attempts += 1
        sync.error_code = error_code
        sync.next_attempt_at = now + meta_retry_delay(sync.attempts)

    def add_photo(self, *, content_type: str, max_photos: int = DEFAULT_MAX_PHOTOS) -> ProductPhoto:
        # A rejected photo must not use up a slot for good.
        """Adds a pending photo at the next position. Raises TooManyPhotos at the limit."""
        active_photos = [p for p in self.photos if p.status is not PhotoStatus.REJECTED]
        if len(active_photos) >= max_photos:
            raise TooManyPhotos(f"A product cannot have more than {max_photos} photos.")
        photo_id = uuid4()
        photo = ProductPhoto(
            id=photo_id,
            product_id=self.id,
            original_key=original_key(self.shop_id, self.id, photo_id),
            content_type=content_type,
            status=PhotoStatus.PENDING,
            position=max((p.position for p in self.photos), default=-1) + 1,
        )
        self.photos.append(photo)
        return photo

    def photo(self, photo_id: UUID) -> ProductPhoto:
        """The photo with this id; raises PhotoNotFound otherwise."""
        for photo in self.photos:
            if photo.id == photo_id:
                return photo
        raise PhotoNotFound(f"Photo {photo_id} not found.")
