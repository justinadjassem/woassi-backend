"""Ports of the shops module: its repository, Meta onboarding, and logo processing."""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum, auto
from typing import Protocol
from uuid import UUID

from woassi.modules.shops.domain.shop import Shop
from woassi.shared.application.ports import UnitOfWork


class ShopRepository(Protocol):
    """Archived shops are invisible here, except through `slug_exists`."""

    async def add(self, shop: Shop) -> None:
        """Raise SlugTaken if the slug is taken."""
        ...

    async def get(self, shop_id: UUID) -> Shop | None:
        """The shop, unless archived."""
        ...

    async def get_by_phone_number_id(self, phone_number_id: str) -> Shop | None:
        """The shop holding this WhatsApp number, unless archived."""
        ...

    async def list(self, account_id: UUID, offset: int, limit: int) -> Sequence[Shop]:
        """A page of the account's shops, oldest first."""
        ...

    async def count(self, account_id: UUID) -> int:
        """Number of shops of the account."""
        ...

    async def list_connected(self) -> Sequence[Shop]:
        """Every shop with a CONNECTED number."""
        ...

    async def list_by_waba_id(self, waba_id: str) -> Sequence[Shop]:
        """Every shop using this WhatsApp Business Account."""
        ...

    async def save(self, shop: Shop) -> None:
        """Raise PhoneNumberAlreadyConnected if another shop holds the number."""
        ...


class ShopsUnitOfWork(UnitOfWork, Protocol):
    """Unit of Work of the shops module."""

    @property
    def shops(self) -> ShopRepository: ...


class MetaFailure(Enum):
    """Why a Meta call failed. UNAVAILABLE is Meta's fault, not the shop's."""

    INVALID_CODE = auto()
    INVALID_TOKEN = auto()
    REJECTED = auto()
    UNAVAILABLE = auto()


class MetaError(Exception):
    """Carries no text from Meta on purpose: it is safe to log."""

    def __init__(self, failure: MetaFailure, *, meta_code: int | None = None) -> None:
        super().__init__(f"{failure.name} (Meta code {meta_code})")
        self.failure = failure
        self.meta_code = meta_code


@dataclass(frozen=True, slots=True)
class MetaPhoneNumber:
    """A WhatsApp number as Meta describes it."""

    display_phone_number: str
    verified_name: str
    on_business_app: bool
    on_cloud_api: bool


class MetaOnboardingPort(Protocol):
    """Every method raises MetaError on failure."""

    async def exchange_code(self, code: str) -> str:
        """Exchanges the Embedded Signup code for the merchant's access token."""
        ...

    async def subscribe_app(self, waba_id: str, token: str) -> None:
        """Subscribes Woassi's app to the account's webhooks."""
        ...

    async def get_phone_number(self, phone_number_id: str, token: str) -> MetaPhoneNumber:
        """Reads the number, which also proves the token gives access to it."""
        ...

    async def link_catalog(
        self, waba_id: str, phone_number_id: str, token: str, catalog_id: str | None
    ) -> str | None:
        """Return the catalog of the account, linking `catalog_id` if it has none."""
        ...

    async def sync_business_app_data(self, phone_number_id: str, token: str) -> None:
        """Asks Meta to sync contacts and history from the WhatsApp Business app."""
        ...

    async def unsubscribe_app(self, waba_id: str, token: str) -> None:
        """Stops the account's webhooks to Woassi."""
        ...

    async def token_is_valid(self, token: str) -> bool:
        """False if Meta says the token is no longer valid."""
        ...


class LogoProcessor(Protocol):
    """Converts an uploaded logo to WebP."""

    async def to_webp(self, data: bytes) -> bytes:
        """Raise InvalidLogo if `data` is not a usable image."""
        ...
