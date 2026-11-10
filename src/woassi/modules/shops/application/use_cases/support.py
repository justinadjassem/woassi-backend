"""Steps shared by the shops use cases."""

from uuid import UUID

import structlog

from woassi.modules.shops.application.ports import MetaError, MetaOnboardingPort, ShopsUnitOfWork
from woassi.modules.shops.domain.shop import Shop
from woassi.shared.application.ports import SecretCipher
from woassi.shared.domain.errors import ShopNotFound

log = structlog.get_logger()


async def load_shop(uow: ShopsUnitOfWork, shop_id: UUID) -> Shop:
    """The shop; raises ShopNotFound if it does not exist or is archived."""
    shop = await uow.shops.get(shop_id)
    if shop is None:
        raise ShopNotFound(f"Shop {shop_id} not found.")
    return shop


async def unsubscribe_quietly(shop: Shop, meta: MetaOnboardingPort, cipher: SecretCipher) -> None:
    """Disconnecting must work even when Meta does not answer: the merchant has decided."""
    account = shop.whatsapp
    if account is None or account.encrypted_token is None:
        return
    try:
        await meta.unsubscribe_app(account.waba_id, cipher.decrypt(account.encrypted_token))
    except MetaError as error:
        log.warning("whatsapp_unsubscribe_failed", shop_id=str(shop.id), meta_code=error.meta_code)
