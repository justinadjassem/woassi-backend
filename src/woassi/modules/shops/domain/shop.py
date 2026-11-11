"""The shop aggregate: profile, settings, WhatsApp connection and receiving account."""

import re
import secrets
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from woassi.modules.shops.domain.errors import (
    InvalidReceivingAccount,
    InvalidSettings,
    InvalidShop,
    ShopNotActivatable,
    WhatsAppAlreadyConnected,
    WhatsAppNotFound,
)
from woassi.modules.shops.domain.events import (
    ReceivingAccountChanged,
    ShopCreated,
    WhatsAppConnected,
)
from woassi.shared.domain.events import Entity
from woassi.shared.domain.values import PhoneNumber

INVOICE_PREFIX = re.compile(r"[A-Z0-9]{1,10}")
MAX_SLUG_LENGTH = 60


def slugify(name: str) -> str:
    """URL-friendly slug of `name`: ASCII, lowercase, dashes, at most 60 characters."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")
    return slug[:MAX_SLUG_LENGTH].strip("-") or "shop"


def with_random_suffix(slug: str) -> str:
    """`slug` with 6 random hex characters, to retry after a collision."""
    return f"{slug}-{secrets.token_hex(3)}"


def logo_key(shop_id: UUID, logo_id: UUID) -> str:
    """Object key of a shop logo, in the public bucket."""
    return f"shops/{shop_id}/logo/{logo_id}.webp"


def original_logo_key(shop_id: UUID, logo_id: UUID) -> str:
    """Object key of an uploaded logo, in the private uploads bucket."""
    return f"shops/{shop_id}/logo/originals/{logo_id}"


class ShopStatus(StrEnum):
    """DRAFT until a number is connected, then ACTIVE. Only ACTIVE shops send and receive."""

    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"


class PaymentMode(StrEnum):
    """How a shop's customers can pay."""

    CASH = "CASH"
    MIXX = "MIXX"
    MOOV = "MOOV"


class WhatsAppStatus(StrEnum):
    """State of a shop's WhatsApp connection."""

    IN_PROGRESS = "IN_PROGRESS"
    CONNECTED = "CONNECTED"
    ERROR = "ERROR"
    DISCONNECTED = "DISCONNECTED"


class Language(StrEnum):
    """Language of the messages sent to the shop's customers and merchant."""

    FR = "fr"
    EN = "en"


class ValidationMode(StrEnum):
    """MANUAL: every order waits for the merchant. AUTOMATIC: only the doubtful ones do."""

    MANUAL = "MANUAL"
    AUTOMATIC = "AUTOMATIC"


class Operator(StrEnum):
    """Mobile money operator of a receiving account."""

    MIXX = "MIXX"
    MOOV = "MOOV"


class TaxRegime(StrEnum):
    """Togolese tax regime. TPU: single professional tax, individuals up to 60 M XOF of
    turnover, which covers VAT. REAL: the real regime, above 60 M XOF.
    """

    TPU = "TPU"
    REAL = "REAL"


@dataclass(frozen=True, slots=True)
class InvoiceDetails:
    """Legal details printed on the shop's invoices. All optional: a small informal merchant
    has no NIF, and still gets invoices.
    """

    company_name: str = ""
    tax_id: str = ""  # NIF
    trade_register: str = ""  # RCCM
    address: str = ""  # legal address; the shop's address is printed when empty
    phone: str = ""
    tax_regime: TaxRegime | None = None
    # Only a VAT-registered seller charges VAT (18 %); the others print "NE FACTURE PAS LA TVA".
    vat_registered: bool = False


@dataclass(frozen=True, slots=True)
class ShopSettings:
    """Settings of a shop. Validated as a whole, as some fields depend on each other."""

    notification_number: PhoneNumber | None = None
    payment_modes: tuple[PaymentMode, ...] = (PaymentMode.CASH,)
    reminder_1_minutes: int = 15
    reminder_2_minutes: int = 60
    expiration_hours: int = 24
    welcome_message: str = ""
    invoice_details: InvoiceDetails = field(default_factory=InvoiceDetails)
    invoice_prefix: str = "FAC"
    language: Language = Language.FR
    # Manual by default: a new shop's stock is rarely accurate yet.
    validation_mode: ValidationMode = ValidationMode.MANUAL
    # In automatic mode, orders above this amount still go to the merchant.
    auto_validation_max_amount: int | None = None

    def __post_init__(self) -> None:
        if self.auto_validation_max_amount is not None and self.auto_validation_max_amount <= 0:
            raise InvalidSettings("The automatic validation limit must be positive.")
        if not self.payment_modes:
            raise InvalidSettings("At least one payment mode is required.")
        expiration_minutes = self.expiration_hours * 60
        if not 0 < self.reminder_1_minutes < self.reminder_2_minutes < expiration_minutes:
            raise InvalidSettings("Reminders must come in order, before the expiration.")
        if not INVOICE_PREFIX.fullmatch(self.invoice_prefix):
            raise InvalidSettings("The invoice prefix must be 1 to 10 capitals or digits.")


@dataclass(slots=True)
class WhatsAppAccount:
    """A shop's WhatsApp connection. The access token is stored encrypted."""

    waba_id: str
    status: WhatsAppStatus
    # Only set once Meta has confirmed that the merchant's token gives access to this number.
    phone_number_id: str | None = None
    display_phone_number: str | None = None
    verified_name: str | None = None
    catalog_id: str | None = None
    encrypted_token: str | None = field(default=None, repr=False)
    connected_at: datetime | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class ReceivingAccount:
    """The mobile money account that receives the shop's payments.

    `verified` is reset whenever the operator or the number changes.
    """

    operator: Operator
    number: PhoneNumber
    holder_name: str
    encrypted_merchant_id: str | None = field(default=None, repr=False)
    verified: bool = False

    def __post_init__(self) -> None:
        if not self.holder_name.strip():
            raise InvalidReceivingAccount("The holder name is required.")


def _required(value: str, label: str) -> str:
    """Strips `value`; raises InvalidShop if nothing is left."""
    value = value.strip()
    if not value:
        raise InvalidShop(f"{label} is required.")
    return value


@dataclass(slots=True)
class Shop(Entity):
    """A shop of an account. Settings, WhatsApp and receiving account load and save with it."""

    id: UUID
    account_id: UUID
    name: str
    slug: str
    category: str
    city: str
    address: str
    created_at: datetime
    status: ShopStatus = ShopStatus.DRAFT
    logo_key: str | None = None
    archived_at: datetime | None = None
    settings: ShopSettings = field(default_factory=ShopSettings)
    whatsapp: WhatsAppAccount | None = None
    receiving_account: ReceivingAccount | None = None

    @classmethod
    def create(
        cls,
        *,
        account_id: UUID,
        name: str,
        slug: str,
        category: str,
        city: str,
        address: str,
        now: datetime,
    ) -> "Shop":
        """Creates a DRAFT shop with default settings, and records `ShopCreated`."""
        shop = cls(
            id=uuid4(),
            account_id=account_id,
            name=_required(name, "Name"),
            slug=slug,
            category=category.strip(),
            city=city.strip(),
            address=address.strip(),
            created_at=now,
        )
        shop.record_event(ShopCreated(shop_id=shop.id, account_id=account_id))
        return shop

    def rename(self, name: str) -> None:
        """The slug does not follow: it has to stay stable once it is shared."""
        self.name = _required(name, "Name")

    @property
    def whatsapp_connected(self) -> bool:
        """True if the shop's number is CONNECTED."""
        return self.whatsapp is not None and self.whatsapp.status is WhatsAppStatus.CONNECTED

    def activate(self) -> None:
        """Raises ShopNotActivatable without a connected number, or if suspended."""
        if not self.whatsapp_connected:
            raise ShopNotActivatable("A shop needs a connected WhatsApp number to go active.")
        if self.status is ShopStatus.SUSPENDED:
            raise ShopNotActivatable("A suspended shop cannot go active.")
        self.status = ShopStatus.ACTIVE

    def _back_to_draft(self) -> None:
        """An ACTIVE shop that loses its number goes back to DRAFT."""
        if self.status is ShopStatus.ACTIVE:
            self.status = ShopStatus.DRAFT

    def start_whatsapp_connection(self, waba_id: str) -> None:
        """Raises WhatsAppAlreadyConnected if a number is already connected."""
        if self.whatsapp_connected:
            raise WhatsAppAlreadyConnected("This shop already has a connected number.")
        self.whatsapp = WhatsAppAccount(waba_id=waba_id, status=WhatsAppStatus.IN_PROGRESS)

    def connect_whatsapp(
        self,
        *,
        waba_id: str,
        phone_number_id: str,
        display_phone_number: str,
        verified_name: str,
        catalog_id: str | None,
        encrypted_token: str,
        now: datetime,
    ) -> None:
        """Stores the confirmed connection, activates a DRAFT shop, and records
        `WhatsAppConnected`.
        """
        self.whatsapp = WhatsAppAccount(
            waba_id=waba_id,
            status=WhatsAppStatus.CONNECTED,
            phone_number_id=phone_number_id,
            display_phone_number=display_phone_number,
            verified_name=verified_name,
            catalog_id=catalog_id,
            encrypted_token=encrypted_token,
            connected_at=now,
        )
        if self.status is ShopStatus.DRAFT:
            self.activate()
        self.record_event(WhatsAppConnected(shop_id=self.id, phone_number_id=phone_number_id))

    def fail_whatsapp(self, message: str) -> None:
        """Without a working number, the shop can no longer stay active."""
        if self.whatsapp is None:
            raise WhatsAppNotFound("This shop has no WhatsApp number.")
        self.whatsapp.status = WhatsAppStatus.ERROR
        self.whatsapp.error_message = message
        self._back_to_draft()

    def disconnect_whatsapp(self, reason: str | None = None) -> None:
        """Frees the number, which another shop may then connect."""
        if self.whatsapp is None:
            raise WhatsAppNotFound("This shop has no WhatsApp number.")
        self.whatsapp = WhatsAppAccount(
            waba_id=self.whatsapp.waba_id, status=WhatsAppStatus.DISCONNECTED, error_message=reason
        )
        self._back_to_draft()

    def set_receiving_account(self, account: ReceivingAccount | None) -> None:
        """Sets or removes (None) the receiving account, and records `ReceivingAccountChanged`."""
        self.receiving_account = account
        self.record_event(ReceivingAccountChanged(shop_id=self.id))

    def archive(self, now: datetime) -> None:
        """Hides the shop everywhere. Rows are kept, so invoices stay readable."""
        self.archived_at = now
