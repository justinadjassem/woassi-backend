"""SQLAlchemy implementation of the product repository."""

from collections.abc import Callable, Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from woassi.modules.catalog.domain.errors import ProductNotFound, RetailerIdAlreadyUsed
from woassi.modules.catalog.domain.product import (
    MetaSync,
    MetaSyncStatus,
    PhotoStatus,
    Product,
    ProductPhoto,
)
from woassi.modules.catalog.infrastructure.models import ProductModel, ProductPhotoModel
from woassi.shared.domain.events import Entity
from woassi.shared.domain.values import Amount


def _photo_to_domain(model: ProductPhotoModel) -> ProductPhoto:
    """Converts a photo row to a domain `ProductPhoto`."""
    return ProductPhoto(
        id=model.id,
        product_id=model.product_id,
        original_key=model.original_key,
        content_type=model.content_type,
        status=PhotoStatus(model.status),
        position=model.position,
        # JSON only has string keys
        variant_keys={int(size): key for size, key in model.variants.items()},
    )


def _to_domain(model: ProductModel) -> Product:
    """Converts a product row, with its photos, to a domain `Product`."""
    return Product(
        id=model.id,
        shop_id=model.shop_id,
        name=model.name,
        price=Amount(model.price),
        stock=model.stock,
        retailer_id=model.retailer_id,
        description=model.description,
        active=model.active,
        photos=[_photo_to_domain(photo) for photo in model.photos],
        meta_sync=MetaSync(
            status=MetaSyncStatus(model.meta_sync_status),
            version=model.meta_sync_version,
            attempts=model.meta_sync_attempts,
            next_attempt_at=model.meta_sync_next_at,
            synced_at=model.meta_synced_at,
            error_code=model.meta_sync_error,
            published=model.meta_published,
        ),
    )


def _copy_photo(photo: ProductPhoto, model: ProductPhotoModel) -> ProductPhotoModel:
    """Copies every field of `photo` onto `model`, and returns `model`."""
    model.id = photo.id
    model.product_id = photo.product_id
    model.original_key = photo.original_key
    model.content_type = photo.content_type
    model.status = photo.status.value
    model.position = photo.position
    model.variants = {str(size): key for size, key in photo.variant_keys.items()}
    return model


def _copy_product(product: Product, model: ProductModel) -> ProductModel:
    """Copies `product` onto `model`, reusing existing photo rows and adding new ones."""
    existing = {photo.id: photo for photo in model.photos}
    model.id = product.id
    model.shop_id = product.shop_id
    model.name = product.name
    model.price = product.price.value
    model.stock = product.stock
    model.retailer_id = product.retailer_id
    model.description = product.description
    model.active = product.active
    sync = product.meta_sync
    model.meta_sync_status = sync.status.value
    model.meta_sync_version = sync.version
    model.meta_sync_attempts = sync.attempts
    model.meta_sync_next_at = sync.next_attempt_at
    model.meta_synced_at = sync.synced_at
    model.meta_sync_error = sync.error_code
    model.meta_published = sync.published
    model.photos = [
        _copy_photo(photo, existing.get(photo.id) or ProductPhotoModel())
        for photo in product.photos
    ]
    return model


class SqlAlchemyProductRepository:
    """`ProductRepository` on SQLAlchemy. Saved products are tracked for their events."""

    def __init__(self, session: AsyncSession, track: Callable[[Entity], None]) -> None:
        self._session = session
        self._track = track

    async def add(self, product: Product) -> None:
        self._session.add(_copy_product(product, ProductModel()))
        try:
            await self._session.flush()
        except IntegrityError as error:
            if "uq_products_shop_id_retailer_id" in str(error.orig):
                raise RetailerIdAlreadyUsed(
                    f"retailer_id {product.retailer_id} already exists in this shop."
                ) from error
            raise
        self._track(product)

    async def get(self, shop_id: UUID, product_id: UUID) -> Product | None:
        model = await self._session.scalar(
            select(ProductModel).where(
                ProductModel.id == product_id, ProductModel.shop_id == shop_id
            )
        )
        return _to_domain(model) if model else None

    async def get_by_photo(self, photo_id: UUID) -> Product | None:
        model = await self._session.scalar(
            select(ProductModel)
            .join(ProductPhotoModel, ProductPhotoModel.product_id == ProductModel.id)
            .where(ProductPhotoModel.id == photo_id)
        )
        return _to_domain(model) if model else None

    async def list(self, shop_id: UUID, offset: int, limit: int) -> Sequence[Product]:
        models = await self._session.scalars(
            select(ProductModel)
            .where(ProductModel.shop_id == shop_id)
            .order_by(ProductModel.created_at, ProductModel.id)
            .offset(offset)
            .limit(limit)
        )
        return [_to_domain(model) for model in models]

    async def count(self, shop_id: UUID) -> int:
        total = await self._session.scalar(
            select(func.count()).select_from(ProductModel).where(ProductModel.shop_id == shop_id)
        )
        return total or 0

    async def list_for_update(
        self, shop_id: UUID, *, retailer_ids: Sequence[str] = (), ids: Sequence[UUID] = ()
    ) -> Sequence[Product]:
        # Always in id order: two orders locking the same products cannot deadlock.
        """Products matching either a retailer id or an id, locked `FOR UPDATE`."""
        models = await self._session.scalars(
            select(ProductModel)
            .where(
                ProductModel.shop_id == shop_id,
                ProductModel.retailer_id.in_(retailer_ids) | ProductModel.id.in_(ids),
            )
            .order_by(ProductModel.id)
            .with_for_update(of=ProductModel)
        )
        return [_to_domain(model) for model in models]

    async def list_shop_for_update(self, shop_id: UUID) -> Sequence[Product]:
        models = await self._session.scalars(
            select(ProductModel)
            .where(ProductModel.shop_id == shop_id)
            .order_by(ProductModel.id)
            .with_for_update(of=ProductModel)
        )
        return [_to_domain(model) for model in models]

    async def list_meta_sync_due(
        self, shop_id: UUID, now: datetime, limit: int
    ) -> Sequence[Product]:
        """PENDING products (not waiting, i.e. with a `next_attempt_at`), and FAILED ones due."""
        models = await self._session.scalars(
            select(ProductModel)
            .where(
                ProductModel.shop_id == shop_id,
                ProductModel.meta_sync_next_at.is_not(None),
                or_(
                    ProductModel.meta_sync_status == MetaSyncStatus.PENDING.value,
                    (ProductModel.meta_sync_status == MetaSyncStatus.FAILED.value)
                    & (ProductModel.meta_sync_next_at <= now),
                ),
            )
            .order_by(ProductModel.id)
            .limit(limit)
        )
        return [_to_domain(model) for model in models]

    async def shops_with_meta_sync_due(self, now: datetime, limit: int) -> Sequence[UUID]:
        shop_ids = await self._session.scalars(
            select(ProductModel.shop_id)
            .where(
                ProductModel.meta_sync_next_at <= now,
                ProductModel.meta_sync_status.in_(
                    [MetaSyncStatus.PENDING.value, MetaSyncStatus.FAILED.value]
                ),
            )
            .distinct()
            .limit(limit)
        )
        return list(shop_ids)

    async def save(self, product: Product) -> None:
        """Updates an existing product; raises ProductNotFound if it is gone."""
        model = await self._session.get(ProductModel, product.id)
        if model is None:
            raise ProductNotFound(f"Product {product.id} not found.")
        _copy_product(product, model)
        await self._session.flush()
        self._track(product)
