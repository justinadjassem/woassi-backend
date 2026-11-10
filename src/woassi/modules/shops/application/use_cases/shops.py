"""Shop creation, reading, update and archiving, and lookups for other modules."""

from collections.abc import Callable
from uuid import UUID

from woassi.modules.shops.application.dto import (
    ShopOrderProfile,
    ShopRouting,
    ShopView,
    order_profile,
    shop_view,
)
from woassi.modules.shops.application.ports import MetaOnboardingPort, ShopsUnitOfWork
from woassi.modules.shops.application.use_cases.support import load_shop, unsubscribe_quietly
from woassi.modules.shops.domain.errors import SlugTaken
from woassi.modules.shops.domain.shop import Shop, ShopStatus, slugify, with_random_suffix
from woassi.shared.application.pagination import PageResult
from woassi.shared.application.ports import Clock, SecretCipher, StoragePort

SLUG_ATTEMPTS = 5


class CreateShop:
    """Creates a shop with a slug made from its name.

    Takes a Unit of Work factory, not a Unit of Work: each slug attempt needs a fresh
    transaction.
    """

    def __init__(
        self, make_uow: Callable[[], ShopsUnitOfWork], images: StoragePort, clock: Clock
    ) -> None:
        self._make_uow = make_uow
        self._images = images
        self._clock = clock

    async def execute(
        self,
        *,
        account_id: UUID,
        name: str,
        category: str = "",
        city: str = "",
        address: str = "",
    ) -> ShopView:
        """Tries the plain slug, then up to 4 random suffixes; raises SlugTaken if all are taken."""
        slug = slugify(name)
        for attempt in range(SLUG_ATTEMPTS):
            shop = Shop.create(
                account_id=account_id,
                name=name,
                slug=slug if attempt == 0 else with_random_suffix(slug),
                category=category,
                city=city,
                address=address,
                now=self._clock.now(),
            )
            try:
                # A failed INSERT ends its transaction: each attempt needs its own.
                async with self._make_uow() as uow:
                    await uow.shops.add(shop)
                    await uow.commit()
            except SlugTaken:
                continue
            return shop_view(shop, self._images)
        raise SlugTaken("Could not find a free slug for this name.")


class ListShops:
    """Lists an account's shops, one page at a time."""

    def __init__(self, uow: ShopsUnitOfWork, images: StoragePort) -> None:
        self._uow = uow
        self._images = images

    async def execute(self, *, account_id: UUID, offset: int, limit: int) -> PageResult[ShopView]:
        async with self._uow as uow:
            shops = await uow.shops.list(account_id, offset, limit)
            total = await uow.shops.count(account_id)
        return PageResult(
            items=[shop_view(shop, self._images) for shop in shops],
            total=total,
            offset=offset,
            limit=limit,
        )


class GetShop:
    """Returns one shop."""

    def __init__(self, uow: ShopsUnitOfWork, images: StoragePort) -> None:
        self._uow = uow
        self._images = images

    async def execute(self, shop_id: UUID) -> ShopView:
        async with self._uow as uow:
            shop = await load_shop(uow, shop_id)
        return shop_view(shop, self._images)


class UpdateShop:
    """Updates a shop's profile; only the arguments given change."""

    def __init__(self, uow: ShopsUnitOfWork, images: StoragePort) -> None:
        self._uow = uow
        self._images = images

    async def execute(
        self,
        *,
        shop_id: UUID,
        name: str | None = None,
        category: str | None = None,
        city: str | None = None,
        address: str | None = None,
    ) -> ShopView:
        async with self._uow as uow:
            shop = await load_shop(uow, shop_id)
            if name is not None:
                shop.rename(name)
            shop.category = shop.category if category is None else category.strip()
            shop.city = shop.city if city is None else city.strip()
            shop.address = shop.address if address is None else address.strip()
            await uow.shops.save(shop)
            await uow.commit()
        return shop_view(shop, self._images)


class ArchiveShop:
    """Keeps every row, as invoices must stay readable, but frees the WhatsApp number."""

    def __init__(
        self,
        uow: ShopsUnitOfWork,
        meta: MetaOnboardingPort,
        cipher: SecretCipher,
        clock: Clock,
    ) -> None:
        self._uow = uow
        self._meta = meta
        self._cipher = cipher
        self._clock = clock

    async def execute(self, shop_id: UUID) -> None:
        """Unsubscribes from Meta (best effort) before archiving, outside any transaction."""
        async with self._uow as uow:
            shop = await load_shop(uow, shop_id)
        await unsubscribe_quietly(shop, self._meta, self._cipher)
        async with self._uow as uow:
            shop = await load_shop(uow, shop_id)
            if shop.whatsapp is not None:
                shop.disconnect_whatsapp()
            shop.archive(self._clock.now())
            await uow.shops.save(shop)
            await uow.commit()


class CheckShopOwnership:
    """Tells whether an account owns a shop, for `get_authorized_shop`."""

    def __init__(self, uow: ShopsUnitOfWork) -> None:
        self._uow = uow

    async def execute(self, *, account_id: UUID, shop_id: UUID) -> bool:
        async with self._uow as uow:
            shop = await uow.shops.get(shop_id)
        return shop is not None and shop.account_id == account_id


class ResolveShopByPhoneNumber:
    """Finds the shop behind a WhatsApp number, to route a webhook."""

    def __init__(self, uow: ShopsUnitOfWork, cipher: SecretCipher) -> None:
        self._uow = uow
        self._cipher = cipher

    async def execute(self, phone_number_id: str) -> ShopRouting | None:
        """None for an unknown number, or a shop that may not process messages."""
        async with self._uow as uow:
            shop = await uow.shops.get_by_phone_number_id(phone_number_id)
        return _routing(shop, self._cipher)


class ResolveShopConnection:
    """Gives a shop's WhatsApp connection, to send from its number."""

    def __init__(self, uow: ShopsUnitOfWork, cipher: SecretCipher) -> None:
        self._uow = uow
        self._cipher = cipher

    async def execute(self, shop_id: UUID) -> ShopRouting | None:
        """None for an unknown shop, or one that may not send messages."""
        async with self._uow as uow:
            shop = await uow.shops.get(shop_id)
        return _routing(shop, self._cipher)


def _routing(shop: Shop | None, cipher: SecretCipher) -> ShopRouting | None:
    """Routing data of an ACTIVE shop with a CONNECTED number, token decrypted; None otherwise."""
    if shop is None or shop.status is not ShopStatus.ACTIVE or not shop.whatsapp_connected:
        return None
    whatsapp = shop.whatsapp
    assert whatsapp is not None
    assert whatsapp.encrypted_token is not None and whatsapp.phone_number_id is not None
    return ShopRouting(
        shop_id=shop.id,
        waba_id=whatsapp.waba_id,
        phone_number_id=whatsapp.phone_number_id,
        access_token=cipher.decrypt(whatsapp.encrypted_token),
    )


class GetShopOrderProfile:
    """Returns what the orders module needs to know about a shop."""

    def __init__(self, uow: ShopsUnitOfWork) -> None:
        self._uow = uow

    async def execute(self, shop_id: UUID) -> ShopOrderProfile | None:
        async with self._uow as uow:
            shop = await uow.shops.get(shop_id)
        return order_profile(shop) if shop else None
