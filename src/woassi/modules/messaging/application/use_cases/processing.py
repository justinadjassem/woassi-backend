"""Handling of webhook events, in the worker."""

import structlog

from woassi.modules.messaging.application.ports import (
    InboundMessageHandler,
    MessagingUnitOfWork,
    ShopConnectionsPort,
    WhatsAppAccountsPort,
)
from woassi.modules.messaging.application.use_cases.templates import apply_template_status
from woassi.modules.messaging.domain.event import (
    AccountUpdate,
    InboundMessage,
    MessageStatusUpdate,
    MetaEvent,
    TemplateStatusUpdate,
)
from woassi.modules.messaging.domain.outbound import status_from_meta
from woassi.shared.application.ports import Clock

log = structlog.get_logger()

MESSAGE_SOURCE = "meta.message"
# Events after which Woassi can no longer use the account.
# https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/reference/account_update.md
ACCESS_LOST = frozenset({"PARTNER_REMOVED", "ACCOUNT_DELETED", "ACCOUNT_OFFBOARDED"})


class HandleMetaEvent:
    """Runs in the worker, for every event of a webhook.

    Meta resends a webhook until it gets a 200, which the API answers as soon as the events are
    queued: from then on, retrying is the worker's job, and duplicates are filtered here.
    """

    def __init__(
        self,
        uow: MessagingUnitOfWork,
        shops: ShopConnectionsPort,
        accounts: WhatsAppAccountsPort,
        handler: InboundMessageHandler,
        clock: Clock,
    ) -> None:
        self._uow = uow
        self._shops = shops
        self._accounts = accounts
        self._handler = handler
        self._clock = clock

    async def execute(self, event: MetaEvent) -> None:
        """Dispatches `event` by type."""
        match event:
            case InboundMessage():
                await self._message(event)
            case MessageStatusUpdate():
                await self._status(event)
            case AccountUpdate():
                await self._account(event)
            case TemplateStatusUpdate():
                moved = await apply_template_status(self._uow, event, self._clock)
                log.info(
                    "template_status_updated",
                    waba_id=event.waba_id,
                    template=event.name,
                    language=event.language,
                    status=event.status,
                    records=moved,
                )

    async def _message(self, message: InboundMessage) -> None:
        """Hands an inbound message to the handler, once.

        A message for a number no shop owns is dropped (its key stays claimed).
        """
        async with self._uow as uow:
            # The key stays reserved until the commit: a concurrent duplicate waits, then skips.
            if not await uow.inbox.claim(MESSAGE_SOURCE, message.message_id):
                log.info("meta_message_duplicate", message_id=message.message_id)
                return
            shop = await self._shops.by_phone_number_id(message.phone_number_id)
            if shop is None:
                log.warning(
                    "meta_message_unrouted",
                    phone_number_id=message.phone_number_id,
                    message_id=message.message_id,
                )
                await uow.commit()
                return
            # If the handler raises, the key is rolled back with the transaction.
            await self._handler.handle(shop.shop_id, message)
            await uow.commit()

    async def _status(self, update: MessageStatusUpdate) -> None:
        """Moves an outbound message's status forward; unknown statuses are ignored."""
        status = status_from_meta(update.status)
        if status is None:
            return
        async with self._uow as uow:
            message = await uow.outbound.get(update.message_id)
            # Unknown: sent from the WhatsApp Business app, or from before Woassi.
            if message is None:
                return
            error_code = update.error_codes[0] if update.error_codes else None
            if message.apply(status, error_code, self._clock.now()):
                await uow.outbound.save(message)
                await uow.commit()

    async def _account(self, update: AccountUpdate) -> None:
        """Disconnects the account's shops if Woassi lost access to it; logs other events."""
        if update.event not in ACCESS_LOST:
            log.info("meta_account_update", waba_id=update.waba_id, account_event=update.event)
            return
        affected = await self._accounts.account_removed(update.waba_id)
        log.warning(
            "meta_account_access_lost",
            waba_id=update.waba_id,
            account_event=update.event,
            shops=affected,
        )
