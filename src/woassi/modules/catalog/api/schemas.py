"""Request and response bodies of the catalog api."""

from uuid import UUID

from pydantic import BaseModel, Field

from woassi.modules.catalog.application.dto import PhotoView, ProductView, UploadPhoto
from woassi.modules.catalog.domain.product import PhotoStatus

MAX_PRICE = 1_000_000_000_000
MAX_STOCK = 2_147_483_647  # INTEGER column


class ProductCreate(BaseModel):
    """Body of `POST /shops/{shop_id}/products`."""

    name: str = Field(min_length=1, max_length=200)
    price: int = Field(ge=0, le=MAX_PRICE, description="Amount in XOF, no cents")
    stock: int = Field(default=0, ge=0, le=MAX_STOCK)
    retailer_id: str = Field(min_length=1, max_length=100, description="SKU sent to Meta")
    description: str = ""


class PhotoResponse(BaseModel):
    """A product photo and, once processed, its public URL per width."""

    id: UUID
    status: PhotoStatus
    position: int
    urls: dict[int, str] = Field(description="Public URL per size; empty until READY")

    @classmethod
    def from_view(cls, view: PhotoView) -> "PhotoResponse":
        """Builds the response from a `PhotoView`."""
        return cls(id=view.id, status=view.status, position=view.position, urls=view.urls)


class ProductResponse(BaseModel):
    """A product as returned by the API. `price` is in XOF."""

    id: UUID
    shop_id: UUID
    name: str
    price: int
    stock: int
    retailer_id: str
    description: str
    active: bool
    photos: list[PhotoResponse]

    @classmethod
    def from_view(cls, view: ProductView) -> "ProductResponse":
        """Builds the response from a `ProductView`."""
        return cls(
            id=view.id,
            shop_id=view.shop_id,
            name=view.name,
            price=view.price,
            stock=view.stock,
            retailer_id=view.retailer_id,
            description=view.description,
            active=view.active,
            photos=[PhotoResponse.from_view(photo) for photo in view.photos],
        )


class UploadUrlRequest(BaseModel):
    """What the client is about to upload: JPEG, PNG or WebP, up to the size limit."""

    content_type: str
    size: int = Field(description="Declared size in bytes")


class UploadUrlResponse(BaseModel):
    """Presigned URL to PUT the photo to, valid `expire_s` seconds."""

    photo_id: UUID
    upload_url: str
    expire_s: int

    @classmethod
    def from_view(cls, upload: UploadPhoto) -> "UploadUrlResponse":
        """Builds the response from an `UploadPhoto`."""
        return cls(photo_id=upload.photo_id, upload_url=upload.upload_url, expire_s=upload.expire_s)
