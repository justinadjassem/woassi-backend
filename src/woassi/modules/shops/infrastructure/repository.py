"""SQLAlchemy implementation of the shop repository."""

from collections.abc import Callable, Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from woassi.modules.shops.domain.errors import PhoneNumberAlreadyConnected, SlugTaken
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
from woassi.modules.shops.infrastructure.models import (
    ReceivingAccountModel,
    ShopModel,
    ShopSettingsModel,
    WhatsAppAccountModel,
)
from woassi.shared.domain.events import Entity
from woassi.shared.domain.values import PhoneNumber


def _invoice_details(data: dict[str, Any]) -> InvoiceDetails:
    """Rows written before the tax fields existed read as not VAT-registered, no regime."""
    regime = data.get("tax_regime")
    return InvoiceDetails(
        company_name=data.get("company_name", ""),
        tax_id=data.get("tax_id", ""),
        trade_register=data.get("trade_register", ""),
        address=data.get("address", ""),
        phone=data.get("phone", ""),
        tax_regime=TaxRegime(regime) if regime else None,
        vat_registered=bool(data.get("vat_registered", False)),
    )


def _settings_to_domain(model: ShopSettingsModel) -> ShopSettings:
    """Converts a settings row to domain `ShopSettings`."""
    number = model.notification_number
    return ShopSettings(
        notification_number=PhoneNumber(number) if number else None,
        payment_modes=tuple(PaymentMode(mode) for mode in model.payment_modes),
        reminder_1_minutes=model.reminder_1_minutes,
        reminder_2_minutes=model.reminder_2_minutes,
        expiration_hours=model.expiration_hours,
        welcome_message=model.welcome_message,
        invoice_details=_invoice_details(model.invoice_details),
        invoice_prefix=model.invoice_prefix,
        language=Language(model.language),
        validation_mode=ValidationMode(model.validation_mode),
        auto_validation_max_amount=model.auto_validation_max_amount,
    )


def _whatsapp_to_domain(model: WhatsAppAccountModel) -> WhatsAppAccount:
    """Converts a WhatsApp row to a domain `WhatsAppAccount`."""
    return WhatsAppAccount(
        waba_id=model.waba_id,
        status=WhatsAppStatus(model.status),
        phone_number_id=model.phone_number_id,
        display_phone_number=model.display_phone_number,
        verified_name=model.verified_name,
        catalog_id=model.catalog_id,
        encrypted_token=model.encrypted_token,
        connected_at=model.connected_at,
        error_message=model.error_message,
    )


def _receiving_to_domain(model: ReceivingAccountModel) -> ReceivingAccount:
    """Converts a receiving account row to a domain `ReceivingAccount`."""
    return ReceivingAccount(
        operator=Operator(model.operator),
        number=PhoneNumber(model.number),
        holder_name=model.holder_name,
        encrypted_merchant_id=model.encrypted_merchant_id,
        verified=model.verified,
    )


def _to_domain(model: ShopModel) -> Shop:
    """Converts a shop row, with its parts, to a domain `Shop`."""
    return Shop(
        id=model.id,
        account_id=model.account_id,
        name=model.name,
        slug=model.slug,
        category=model.category,
        city=model.city,
        address=model.address,
        created_at=model.created_at,
        status=ShopStatus(model.status),
        logo_key=model.logo_key,
        archived_at=model.archived_at,
        settings=_settings_to_domain(model.settings),
        whatsapp=_whatsapp_to_domain(model.whatsapp) if model.whatsapp else None,
        receiving_account=(
            _receiving_to_domain(model.receiving_account) if model.receiving_account else None
        ),
    )


def _copy_settings(settings: ShopSettings, model: ShopSettingsModel) -> ShopSettingsModel:
    """Copies `settings` onto `model`, and returns `model`."""
    number = settings.notification_number
    details = settings.invoice_details
    model.notification_number = number.value if number else None
    model.payment_modes = [mode.value for mode in settings.payment_modes]
    model.reminder_1_minutes = settings.reminder_1_minutes
    model.reminder_2_minutes = settings.reminder_2_minutes
    model.expiration_hours = settings.expiration_hours
    model.welcome_message = settings.welcome_message
    model.invoice_details = {
        "company_name": details.company_name,
        "tax_id": details.tax_id,
        "trade_register": details.trade_register,
        "address": details.address,
        "phone": details.phone,
        "tax_regime": details.tax_regime.value if details.tax_regime else None,
        "vat_registered": details.vat_registered,
    }
    model.invoice_prefix = settings.invoice_prefix
    model.language = settings.language.value
    model.validation_mode = settings.validation_mode.value
    model.auto_validation_max_amount = settings.auto_validation_max_amount
    return model


def _copy_whatsapp(account: WhatsAppAccount, model: WhatsAppAccountModel) -> WhatsAppAccountModel:
    """Copies `account` onto `model`, and returns `model`."""
    model.waba_id = account.waba_id
    model.status = account.status.value
    model.phone_number_id = account.phone_number_id
    model.display_phone_number = account.display_phone_number
    model.verified_name = account.verified_name
    model.catalog_id = account.catalog_id
    model.encrypted_token = account.encrypted_token
    model.connected_at = account.connected_at
    model.error_message = account.error_message
    return model


def _copy_receiving(
    account: ReceivingAccount, model: ReceivingAccountModel
) -> ReceivingAccountModel:
    """Copies `account` onto `model`, and returns `model`."""
    model.operator = account.operator.value
    model.number = account.number.value
    model.holder_name = account.holder_name
    model.encrypted_merchant_id = account.encrypted_merchant_id
    model.verified = account.verified
    return model


def _copy_shop(shop: Shop, model: ShopModel) -> ShopModel:
    """Copies the shop's own fields (not its parts) onto `model`, and returns `model`."""
    model.id = shop.id
    model.account_id = shop.account_id
    model.name = shop.name
    model.slug = shop.slug
    model.category = shop.category
    model.city = shop.city
    model.address = shop.address
    model.logo_key = shop.logo_key
    model.status = shop.status.value
    model.created_at = shop.created_at
    model.archived_at = shop.archived_at
    return model


class SqlAlchemyShopRepository:
    """`ShopRepository` on SQLAlchemy. Archived shops are filtered out of every read."""

    def __init__(self, session: AsyncSession, track: Callable[[Entity], None]) -> None:
        self._session = session
        self._track = track

    async def add(self, shop: Shop) -> None:
        """Inserts the shop with its settings; raises SlugTaken on the slug's unique constraint."""
        model = _copy_shop(shop, ShopModel())
        model.settings = _copy_settings(shop.settings, ShopSettingsModel())
        self._session.add(model)
        try:
            await self._session.flush()
        except IntegrityError as error:
            if "uq_shops_slug" in str(error.orig):
                raise SlugTaken(f"Slug {shop.slug} is taken.") from error
            raise
        self._track(shop)

    async def get(self, shop_id: UUID) -> Shop | None:
        model = await self._session.scalar(
            select(ShopModel).where(ShopModel.id == shop_id, ShopModel.archived_at.is_(None))
        )
        return _to_domain(model) if model else None

    async def get_by_phone_number_id(self, phone_number_id: str) -> Shop | None:
        model = await self._session.scalar(
            select(ShopModel)
            .join(WhatsAppAccountModel)
            .where(
                WhatsAppAccountModel.phone_number_id == phone_number_id,
                ShopModel.archived_at.is_(None),
            )
        )
        return _to_domain(model) if model else None

    async def list(self, account_id: UUID, offset: int, limit: int) -> Sequence[Shop]:
        models = await self._session.scalars(
            select(ShopModel)
            .where(ShopModel.account_id == account_id, ShopModel.archived_at.is_(None))
            .order_by(ShopModel.created_at, ShopModel.id)
            .offset(offset)
            .limit(limit)
        )
        return [_to_domain(model) for model in models]

    async def count(self, account_id: UUID) -> int:
        total = await self._session.scalar(
            select(func.count())
            .select_from(ShopModel)
            .where(ShopModel.account_id == account_id, ShopModel.archived_at.is_(None))
        )
        return total or 0

    async def list_connected(self) -> Sequence[Shop]:
        models = await self._session.scalars(
            select(ShopModel)
            .join(WhatsAppAccountModel)
            .where(WhatsAppAccountModel.status == WhatsAppStatus.CONNECTED.value)
            .where(ShopModel.archived_at.is_(None))
            .order_by(ShopModel.id)
        )
        return [_to_domain(model) for model in models]

    async def list_by_waba_id(self, waba_id: str) -> Sequence[Shop]:
        models = await self._session.scalars(
            select(ShopModel)
            .join(WhatsAppAccountModel)
            .where(WhatsAppAccountModel.waba_id == waba_id, ShopModel.archived_at.is_(None))
            .order_by(ShopModel.id)
        )
        return [_to_domain(model) for model in models]

    async def save(self, shop: Shop) -> None:
        """Updates the shop and its parts; a removed part deletes its row.

        Raises PhoneNumberAlreadyConnected on the number's unique constraint, LookupError if
        the shop
        does not exist.
        """
        model = await self._session.get(ShopModel, shop.id)
        if model is None:
            raise LookupError(f"Shop {shop.id} does not exist.")
        _copy_shop(shop, model)
        _copy_settings(shop.settings, model.settings)
        model.whatsapp = (
            _copy_whatsapp(shop.whatsapp, model.whatsapp or WhatsAppAccountModel())
            if shop.whatsapp
            else None
        )
        model.receiving_account = (
            _copy_receiving(
                shop.receiving_account, model.receiving_account or ReceivingAccountModel()
            )
            if shop.receiving_account
            else None
        )
        try:
            await self._session.flush()
        except IntegrityError as error:
            if "uq_whatsapp_accounts_phone_number_id" in str(error.orig):
                raise PhoneNumberAlreadyConnected(
                    "This number is connected to another shop."
                ) from error
            raise
        self._track(shop)
