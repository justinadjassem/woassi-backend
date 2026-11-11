"""Shops module seen from `shared`, `messaging`, `orders` and `catalog`."""

from uuid import UUID

from woassi.modules.catalog.application.ports import MetaCatalogAccess
from woassi.modules.messaging.application.ports import ShopConnection
from woassi.modules.orders.application.ports import ShopProfile
from woassi.modules.orders.domain.order import ValidationMode
from woassi.modules.shops.application.dto import ShopRouting
from woassi.modules.shops.application.use_cases.shops import (
    CheckShopOwnership,
    GetShopOrderProfile,
    ResolveShopByPhoneNumber,
    ResolveShopCatalog,
    ResolveShopConnection,
)
from woassi.modules.shops.application.use_cases.whatsapp import HandleWhatsAppAccountRemoved


class ShopsOwnership:
    """ShopOwnershipPort, for the shared api, backed by the shops module."""

    def __init__(self, use_case: CheckShopOwnership) -> None:
        self._use_case = use_case

    async def owns(self, account_id: UUID, shop_id: UUID) -> bool:
        """True if the account owns this shop and it is not archived."""
        return await self._use_case.execute(account_id=account_id, shop_id=shop_id)


def _connection(routing: ShopRouting | None) -> ShopConnection | None:
    """Converts the shops module's routing data into a messaging `ShopConnection`."""
    if routing is None:
        return None
    return ShopConnection(
        shop_id=routing.shop_id,
        waba_id=routing.waba_id,
        phone_number_id=routing.phone_number_id,
        access_token=routing.access_token,
    )


class ShopsConnections:
    """ShopConnectionsPort, for the messaging module, backed by the shops module."""

    def __init__(
        self, by_phone_number: ResolveShopByPhoneNumber, by_shop: ResolveShopConnection
    ) -> None:
        self._by_phone_number = by_phone_number
        self._by_shop = by_shop

    async def by_phone_number_id(self, phone_number_id: str) -> ShopConnection | None:
        """The connected shop behind a WhatsApp number, to route a webhook."""
        return _connection(await self._by_phone_number.execute(phone_number_id))

    async def by_shop_id(self, shop_id: UUID) -> ShopConnection | None:
        """The WhatsApp connection of a shop, to send from its number."""
        return _connection(await self._by_shop.execute(shop_id))


class ShopsWhatsAppAccounts:
    """WhatsAppAccountsPort, for the messaging module, backed by the shops module."""

    def __init__(self, use_case: HandleWhatsAppAccountRemoved) -> None:
        self._use_case = use_case

    async def account_removed(self, waba_id: str) -> int:
        """Disconnects every shop of a WhatsApp Business Account; returns how many."""
        return await self._use_case.execute(waba_id)


class ShopsOrderProfiles:
    """ShopsPort, for the orders module, backed by the shops module."""

    def __init__(self, use_case: GetShopOrderProfile) -> None:
        self._use_case = use_case

    async def profile(self, shop_id: UUID) -> ShopProfile | None:
        """What the order flow needs to know about a shop; None if it does not exist."""
        profile = await self._use_case.execute(shop_id)
        if profile is None:
            return None
        return ShopProfile(
            shop_id=profile.shop_id,
            account_id=profile.account_id,
            name=profile.name,
            language=profile.language.value,
            validation_mode=ValidationMode(profile.validation_mode.value),
            auto_validation_max_amount=profile.auto_validation_max_amount,
            notification_number=profile.notification_number,
            reminder_1_minutes=profile.reminder_1_minutes,
            reminder_2_minutes=profile.reminder_2_minutes,
            expiration_hours=profile.expiration_hours,
        )


class ShopsMetaCatalogs:
    """MetaCatalogAccessPort, for the catalog module, backed by the shops module."""

    def __init__(self, use_case: ResolveShopCatalog) -> None:
        self._use_case = use_case

    async def for_shop(self, shop_id: UUID) -> MetaCatalogAccess | None:
        """The shop's Meta catalog and token; None without a connected number and catalog."""
        routing = await self._use_case.execute(shop_id)
        if routing is None:
            return None
        return MetaCatalogAccess(
            catalog_id=routing.catalog_id, access_token=routing.access_token, brand=routing.name
        )
