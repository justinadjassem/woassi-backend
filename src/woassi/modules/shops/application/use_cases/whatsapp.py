"""WhatsApp number connection (Embedded Signup), disconnection, and health checks.

Graph calls run outside any transaction; the shop is reloaded before each write.
"""

from uuid import UUID

import structlog

from woassi.modules.shops.application.dto import WhatsAppView, whatsapp_view
from woassi.modules.shops.application.ports import (
    MetaError,
    MetaFailure,
    MetaOnboardingPort,
    MetaPhoneNumber,
    ShopsUnitOfWork,
)
from woassi.modules.shops.application.use_cases.support import load_shop, unsubscribe_quietly
from woassi.modules.shops.domain.errors import (
    InvalidSignupCode,
    PhoneNumberAlreadyConnected,
    PhoneNumberNotUsable,
    WhatsAppConnectionFailed,
    WhatsAppNotFound,
)
from woassi.modules.shops.domain.shop import WhatsAppStatus
from woassi.shared.application.ports import Clock, SecretCipher
from woassi.shared.domain.errors import DomainError

log = structlog.get_logger()

_INVALID_CODE = "The WhatsApp signup expired or was already used. Please connect again."
_NOT_ON_CLOUD_API = (
    "This number is not set up for the WhatsApp Business Platform yet. "
    "Connect a number used with the WhatsApp Business app."
)
_MESSAGES = {
    MetaFailure.INVALID_TOKEN: "Meta refused access to this account. Please connect again.",
    MetaFailure.REJECTED: "Meta refused this WhatsApp account. Check it in WhatsApp Manager.",
    MetaFailure.UNAVAILABLE: "Meta is not responding right now. Please try again in a few minutes.",
}
_TOKEN_EXPIRED = "Access to this WhatsApp account has expired. Please connect it again."


def _to_domain_error(error: MetaError) -> DomainError:
    """Turns a `MetaError` into the domain error the merchant sees."""
    if error.failure is MetaFailure.INVALID_CODE:
        return InvalidSignupCode(_INVALID_CODE)
    return WhatsAppConnectionFailed(_MESSAGES[error.failure])


class ConnectWhatsApp:
    """Connects a shop's WhatsApp number from the Embedded Signup result."""

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

    async def execute(
        self,
        *,
        shop_id: UUID,
        code: str,
        waba_id: str,
        phone_number_id: str,
        catalog_id: str | None = None,
    ) -> WhatsAppView:
        """Exchanges the code, subscribes to webhooks, checks the number, links the catalog,
        then stores the encrypted token. The shop goes ACTIVE.

        Every failure is recorded on the shop (status ERROR) before raising.

        Raises:
            PhoneNumberAlreadyConnected: another shop holds this number.
            WhatsAppAlreadyConnected: this shop already has a connected number.
            InvalidSignupCode: the code expired or was already used.
            PhoneNumberNotUsable: the number is not on the Cloud API.
            WhatsAppConnectionFailed: Meta refused or did not answer.
        """
        async with self._uow as uow:
            shop = await load_shop(uow, shop_id)
            holder = await uow.shops.get_by_phone_number_id(phone_number_id)
            if holder is not None and holder.id != shop_id:
                raise PhoneNumberAlreadyConnected("This number is connected to another shop.")
            shop.start_whatsapp_connection(waba_id)
            await uow.shops.save(shop)
            await uow.commit()

        # No transaction is open during these calls: they can take several seconds.
        try:
            token = await self._meta.exchange_code(code)
            await self._meta.subscribe_app(waba_id, token)
            phone = await self._meta.get_phone_number(phone_number_id, token)
            if not phone.on_cloud_api:
                raise PhoneNumberNotUsable(_NOT_ON_CLOUD_API)
            catalog = await self._meta.link_catalog(waba_id, phone_number_id, token, catalog_id)
        except MetaError as error:
            log.warning(
                "whatsapp_connection_failed",
                shop_id=str(shop_id),
                failure=error.failure.name,
                meta_code=error.meta_code,
            )
            domain_error = _to_domain_error(error)
            await self._record_failure(shop_id, domain_error.message)
            raise domain_error from None
        except PhoneNumberNotUsable as error:
            await self._record_failure(shop_id, error.message)
            raise

        if phone.on_business_app:
            await self._sync_business_app(shop_id, phone_number_id, token)
        return await self._finish(
            shop_id,
            waba_id=waba_id,
            phone_number_id=phone_number_id,
            phone=phone,
            catalog_id=catalog,
            token=token,
        )

    async def _sync_business_app(self, shop_id: UUID, phone_number_id: str, token: str) -> None:
        """Best effort: Meta accepts each sync once, so a retry of this flow would always fail."""
        try:
            await self._meta.sync_business_app_data(phone_number_id, token)
        except MetaError as error:
            log.warning(
                "whatsapp_history_sync_failed", shop_id=str(shop_id), meta_code=error.meta_code
            )

    async def _finish(
        self,
        shop_id: UUID,
        *,
        waba_id: str,
        phone_number_id: str,
        phone: MetaPhoneNumber,
        catalog_id: str | None,
        token: str,
    ) -> WhatsAppView:
        """Stores the connection; the unique number constraint settles a race with another shop."""
        try:
            async with self._uow as uow:
                shop = await load_shop(uow, shop_id)
                shop.connect_whatsapp(
                    waba_id=waba_id,
                    phone_number_id=phone_number_id,
                    display_phone_number=phone.display_phone_number,
                    verified_name=phone.verified_name,
                    catalog_id=catalog_id,
                    encrypted_token=self._cipher.encrypt(token),
                    now=self._clock.now(),
                )
                await uow.shops.save(shop)
                await uow.commit()
        except PhoneNumberAlreadyConnected as error:
            # Another shop got there during the Meta calls.
            await self._record_failure(shop_id, error.message)
            raise
        assert shop.whatsapp is not None
        return whatsapp_view(shop.whatsapp)

    async def _record_failure(self, shop_id: UUID, message: str) -> None:
        """Marks the connection as failed, with a message for the merchant."""
        async with self._uow as uow:
            shop = await load_shop(uow, shop_id)
            shop.fail_whatsapp(message)
            await uow.shops.save(shop)
            await uow.commit()


class GetWhatsApp:
    """Returns a shop's WhatsApp connection."""

    def __init__(self, uow: ShopsUnitOfWork) -> None:
        self._uow = uow

    async def execute(self, shop_id: UUID) -> WhatsAppView:
        """Raises WhatsAppNotFound if the shop never started a connection."""
        async with self._uow as uow:
            shop = await load_shop(uow, shop_id)
        if shop.whatsapp is None:
            raise WhatsAppNotFound("This shop has no WhatsApp number.")
        return whatsapp_view(shop.whatsapp)


class DisconnectWhatsApp:
    """Disconnects a shop's number at the merchant's request."""

    def __init__(
        self, uow: ShopsUnitOfWork, meta: MetaOnboardingPort, cipher: SecretCipher
    ) -> None:
        self._uow = uow
        self._meta = meta
        self._cipher = cipher

    async def execute(self, shop_id: UUID) -> None:
        """Unsubscribes from Meta (best effort), then frees the number."""
        async with self._uow as uow:
            shop = await load_shop(uow, shop_id)
        await unsubscribe_quietly(shop, self._meta, self._cipher)
        async with self._uow as uow:
            shop = await load_shop(uow, shop_id)
            shop.disconnect_whatsapp()
            await uow.shops.save(shop)
            await uow.commit()


class CheckWhatsAppTokens:
    """Daily job. Only an explicit "invalid" counts: a Meta outage must not disconnect shops."""

    def __init__(
        self, uow: ShopsUnitOfWork, meta: MetaOnboardingPort, cipher: SecretCipher
    ) -> None:
        self._uow = uow
        self._meta = meta
        self._cipher = cipher

    async def execute(self) -> int:
        """Return how many tokens turned out to be invalid."""
        async with self._uow as uow:
            shops = await uow.shops.list_connected()

        invalid = 0
        for shop in shops:
            assert shop.whatsapp is not None and shop.whatsapp.encrypted_token is not None
            try:
                token = self._cipher.decrypt(shop.whatsapp.encrypted_token)
                if await self._meta.token_is_valid(token):
                    continue
            except MetaError as error:
                log.warning(
                    "whatsapp_token_check_failed", shop_id=str(shop.id), meta_code=error.meta_code
                )
                continue
            invalid += 1
            log.warning("whatsapp_token_invalid", shop_id=str(shop.id))
            async with self._uow as uow:
                fresh = await load_shop(uow, shop.id)
                fresh.fail_whatsapp(_TOKEN_EXPIRED)
                await uow.shops.save(fresh)
                await uow.commit()
        return invalid


_REMOVED_IN_META = (
    "This WhatsApp account was disconnected from Woassi in Meta. Please connect again."
)


class HandleWhatsAppAccountRemoved:
    """Meta says that Woassi lost access to a WhatsApp account: release its shops' numbers.

    No call to Meta: the token is useless now. Idempotent, as Meta may send the event again.
    """

    def __init__(self, uow: ShopsUnitOfWork) -> None:
        self._uow = uow

    async def execute(self, waba_id: str) -> int:
        """Return how many shops lost their number."""
        async with self._uow as uow:
            shops = [
                shop
                for shop in await uow.shops.list_by_waba_id(waba_id)
                if shop.whatsapp is not None
                and shop.whatsapp.status is not WhatsAppStatus.DISCONNECTED
            ]
            for shop in shops:
                shop.disconnect_whatsapp(_REMOVED_IN_META)
                await uow.shops.save(shop)
            await uow.commit()
        return len(shops)
