"""Application settings, read from the environment and from `.env`."""

from functools import lru_cache
from typing import Annotated, Self

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Every setting of the API and the worker. Field names match the environment variables,
    case-insensitively: `database_url` reads `DATABASE_URL`.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "dev"

    database_url: str
    redis_url: str

    minio_endpoint: str
    minio_public_endpoint: str
    minio_access_key: str
    minio_secret_key: str
    minio_secure: bool = False
    minio_region: str = "us-east-1"
    minio_bucket_uploads: str = "woassi-uploads"
    minio_bucket_products: str = "woassi-products"
    minio_bucket_documents: str = "woassi-documents"
    public_images_base_url: str

    # min_length=1: an empty secret would let anyone forge the HMAC signature.
    meta_app_secret: str = Field(min_length=1)
    meta_verify_token: str = Field(min_length=1)
    meta_app_id: str = Field(min_length=1)
    meta_config_id: str = ""  # used by the dashboard's Embedded Signup, not by this API
    meta_graph_version: str = "v27.0"
    # Meta requires a `link` per catalog item, on the business's own domain. Products link to
    # `{base}/shops/{shop_id}/products/{product_id}`.
    meta_catalog_link_base_url: str = "https://woassi.com"

    # min_length=32: HS256 is only as strong as this secret.
    jwt_secret: str = Field(min_length=32)
    jwt_access_ttl: int = 15 * 60
    jwt_refresh_ttl: int = 30 * 24 * 3600

    woassi_encryption_key: str | None = None
    woassi_encryption_keys: Annotated[list[str], NoDecode] = []

    sentry_dsn: str | None = None
    cors_origins: Annotated[list[str], NoDecode] = []

    # Public https URL of this API, e.g. https://api.woassi.com: KPrimePay sends its webhooks
    # there. Empty (as in local development): no webhooks, payments are only seen by polling.
    public_base_url: str = ""
    # Where KPrimePay sends the customer after paying. Default: {PUBLIC_BASE_URL}/payments/return.
    payment_return_url: str = ""
    kprimepay_base_url: str = "https://api.kprimepay.com/v2"
    kprimepay_connect_timeout_s: float = 5.0
    kprimepay_read_timeout_s: float = 15.0

    max_photos_per_product: int = 5
    photo_max_size_bytes: int = 8 * 1024 * 1024

    @field_validator("cors_origins", "woassi_encryption_keys", mode="before")
    @classmethod
    def _split_comma_list(cls, value: object) -> object:
        """Accepts a comma-separated string for list settings, e.g. `CORS_ORIGINS=a,b`."""
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @model_validator(mode="after")
    def _require_encryption_key(self) -> Self:
        """Refuses to start without a key to encrypt stored secrets."""
        if not self.encryption_keys:
            raise ValueError("Set WOASSI_ENCRYPTION_KEY or WOASSI_ENCRYPTION_KEYS.")
        return self

    @property
    def payment_return_url_or_default(self) -> str:
        """`payment_return_url`, else the API's own return page; KPrimePay requires one."""
        if self.payment_return_url:
            return self.payment_return_url
        base = self.public_base_url.rstrip("/") or "http://localhost:8000"
        return f"{base}/payments/return"

    @property
    def encryption_keys(self) -> list[str]:
        """Newest first. WOASSI_ENCRYPTION_KEYS takes precedence over the single key."""
        if self.woassi_encryption_keys:
            return self.woassi_encryption_keys
        return [self.woassi_encryption_key] if self.woassi_encryption_key else []


@lru_cache
def get_settings() -> Settings:
    """The settings, loaded once per process."""
    return Settings()  # values read from the environment
