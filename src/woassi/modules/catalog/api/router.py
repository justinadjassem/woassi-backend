"""Product routes of a shop.

Photos are uploaded in three steps: ask for a presigned upload URL, PUT the file to MinIO
directly, then confirm. The worker then turns the original into WebP variants.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Response, status

from woassi.modules.catalog.api.deps import CatalogDep
from woassi.modules.catalog.api.schemas import (
    ProductCreate,
    ProductResponse,
    UploadUrlRequest,
    UploadUrlResponse,
)
from woassi.shared.api.deps import get_authorized_shop
from woassi.shared.api.pagination import Limit, Offset, Page

router = APIRouter(
    prefix="/shops/{shop_id}/products",
    tags=["catalog"],
    dependencies=[Depends(get_authorized_shop)],
)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_product(
    shop_id: UUID, body: ProductCreate, catalog: CatalogDep
) -> ProductResponse:
    """Creates a product. Its `retailer_id` must be unique within the shop."""
    product = await catalog.create_product().execute(shop_id=shop_id, **body.model_dump())
    return ProductResponse.from_view(product)


@router.get("")
async def list_products(
    shop_id: UUID, catalog: CatalogDep, offset: Offset = 0, limit: Limit = 50
) -> Page[ProductResponse]:
    """Lists the shop's products, oldest first."""
    result = await catalog.list_products().execute(shop_id=shop_id, offset=offset, limit=limit)
    return Page[ProductResponse].from_result(result, ProductResponse.from_view)


@router.get("/{product_id}")
async def get_product(shop_id: UUID, product_id: UUID, catalog: CatalogDep) -> ProductResponse:
    """One product of the shop, with its photos."""
    product = await catalog.get_product().execute(shop_id=shop_id, product_id=product_id)
    return ProductResponse.from_view(product)


@router.post("/{product_id}/photos/upload-url")
async def request_photo_upload(
    shop_id: UUID, product_id: UUID, body: UploadUrlRequest, catalog: CatalogDep
) -> UploadUrlResponse:
    """Adds a pending photo and returns a presigned URL to PUT the file to."""
    upload = await catalog.request_photo_upload().execute(
        shop_id=shop_id,
        product_id=product_id,
        content_type=body.content_type,
        size=body.size,
    )
    return UploadUrlResponse.from_view(upload)


@router.post("/{product_id}/photos/{photo_id}/confirm", status_code=status.HTTP_202_ACCEPTED)
async def confirm_photo(
    shop_id: UUID, product_id: UUID, photo_id: UUID, catalog: CatalogDep
) -> Response:
    """Checks the uploaded file and queues its processing. 202: the photo is not ready yet."""
    await catalog.confirm_photo().execute(shop_id=shop_id, product_id=product_id, photo_id=photo_id)
    return Response(status_code=status.HTTP_202_ACCEPTED)
