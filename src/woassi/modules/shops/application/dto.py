"""Data returned by the shops use cases."""

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from woassi.modules.shops.domain.shop import (
    InvoiceDetails,
    Language,
    Operator,
    PaymentMode,
    ReceivingAccount,
    Shop,
    ShopSettings,
    ShopStatus,
    TaxRegime,
    ValidationMode,
    WhatsAppAccount,
    WhatsAppStatus,
)
from woassi.shared.application.ports import SecretCipher, StoragePort


@dataclass(frozen=True, slots=True)
class ShopView:
    """Read-only view of a shop."""

    id: UUID
    name: str
    slug: str
    category: str
    city: str
    address: str
    status: ShopStatus
    logo_url: str | None
    created_at: datetime


def shop_view(shop: Shop, images: StoragePort) -> ShopView:
    """Builds the view of `shop`, with its logo's public URL."""
    return ShopView(
        id=shop.id,
        name=shop.name,
        slug=shop.slug,
        category=shop.category,
        city=shop.city,
        address=shop.address,
        status=shop.status,
        logo_url=images.public_url(shop.logo_key) if shop.logo_key else None,
        created_at=shop.created_at,
    )


@dataclass(frozen=True, slots=True)
class SettingsView:
    """Read-only view of a shop's settings."""

    notification_number: str | None
    payment_modes: tuple[PaymentMode, ...]
    reminder_1_minutes: int
    reminder_2_minutes: int
    expiration_hours: int
    welcome_message: str
    invoice_details: InvoiceDetails
    invoice_prefix: str
    language: Language
    validation_mode: ValidationMode
    auto_validation_max_amount: int | None


def settings_view(settings: ShopSettings) -> SettingsView:
    """Builds the view of `settings`."""
    number = settings.notification_number
    return SettingsView(
        notification_number=number.value if number else None,
        payment_modes=settings.payment_modes,
        reminder_1_minutes=settings.reminder_1_minutes,
        reminder_2_minutes=settings.reminder_2_minutes,
        expiration_hours=settings.expiration_hours,
        welcome_message=settings.welcome_message,
        invoice_details=settings.invoice_details,
        invoice_prefix=settings.invoice_prefix,
        language=settings.language,
        validation_mode=settings.validation_mode,
        auto_validation_max_amount=settings.auto_validation_max_amount,
    )


@dataclass(frozen=True, slots=True)
class WhatsAppView:
    """No token here, encrypted or not."""

    status: WhatsAppStatus
    waba_id: str
    phone_number_id: str | None
    display_phone_number: str | None
    verified_name: str | None
    catalog_id: str | None
    connected_at: datetime | None
    error_message: str | None


def whatsapp_view(account: WhatsAppAccount) -> WhatsAppView:
    """Builds the view of a WhatsApp connection."""
    return WhatsAppView(
        status=account.status,
        waba_id=account.waba_id,
        phone_number_id=account.phone_number_id,
        display_phone_number=account.display_phone_number,
        verified_name=account.verified_name,
        catalog_id=account.catalog_id,
        connected_at=account.connected_at,
        error_message=account.error_message,
    )


@dataclass(frozen=True, slots=True)
class ReceivingAccountView:
    """Read-only view of a receiving account, merchant id decrypted."""

    operator: Operator
    number: str
    holder_name: str
    aggregator_merchant_id: str | None
    verified: bool


def receiving_account_view(account: ReceivingAccount, cipher: SecretCipher) -> ReceivingAccountView:
    """Builds the view of `account`, decrypting the aggregator merchant id."""
    encrypted = account.encrypted_merchant_id
    return ReceivingAccountView(
        operator=account.operator,
        number=account.number.value,
        holder_name=account.holder_name,
        aggregator_merchant_id=cipher.decrypt(encrypted) if encrypted else None,
        verified=account.verified,
    )


@dataclass(frozen=True, slots=True)
class LogoUpload:
    """Where to upload a new logo, and for how long the URL is valid."""

    logo_id: UUID
    upload_url: str
    expire_s: int


@dataclass(frozen=True, slots=True)
class ShopRouting:
    """A connected shop's WhatsApp identifiers and decrypted token. Never log it."""

    shop_id: UUID
    waba_id: str
    phone_number_id: str
    access_token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class ShopCatalogRouting:
    """The Meta catalog of a connected shop, with its decrypted token. Never log it."""

    shop_id: UUID
    name: str
    catalog_id: str
    access_token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class ShopOrderProfile:
    """What the orders module needs to know about a shop."""

    shop_id: UUID
    account_id: UUID
    name: str
    language: Language
    validation_mode: ValidationMode
    auto_validation_max_amount: int | None
    notification_number: str | None = field(repr=False)
    reminder_1_minutes: int
    reminder_2_minutes: int
    expiration_hours: int


def order_profile(shop: Shop) -> ShopOrderProfile:
    """Builds the order profile of `shop`."""
    settings = shop.settings
    number = settings.notification_number
    return ShopOrderProfile(
        shop_id=shop.id,
        account_id=shop.account_id,
        name=shop.name,
        language=settings.language,
        validation_mode=settings.validation_mode,
        auto_validation_max_amount=settings.auto_validation_max_amount,
        notification_number=number.value if number else None,
        reminder_1_minutes=settings.reminder_1_minutes,
        reminder_2_minutes=settings.reminder_2_minutes,
        expiration_hours=settings.expiration_hours,
    )


@dataclass(frozen=True, slots=True)
class ShopBillingProfile:
    """What the billing module prints on a shop's invoices. Empty strings when unknown."""

    shop_id: UUID
    account_id: UUID
    name: str
    language: Language
    invoice_prefix: str
    legal_name: str
    address: str
    phone: str
    tax_id: str
    trade_register: str
    tax_regime: TaxRegime | None
    vat_registered: bool
    logo_key: str | None


def billing_profile(shop: Shop) -> ShopBillingProfile:
    """Builds the billing profile of `shop`: the legal address, else the shop's own address."""
    details = shop.settings.invoice_details
    address = details.address.strip() or ", ".join(
        part for part in (shop.address.strip(), shop.city.strip()) if part
    )
    return ShopBillingProfile(
        shop_id=shop.id,
        account_id=shop.account_id,
        name=shop.name,
        language=shop.settings.language,
        invoice_prefix=shop.settings.invoice_prefix,
        legal_name=details.company_name.strip(),
        address=address,
        phone=details.phone.strip(),
        tax_id=details.tax_id.strip(),
        trade_register=details.trade_register.strip(),
        tax_regime=details.tax_regime,
        vat_registered=details.vat_registered,
        logo_key=shop.logo_key,
    )
