"""Sending WhatsApp messages from a shop's number."""

from uuid import UUID

import structlog

from woassi.modules.messaging.application.ports import (
    MessagingUnitOfWork,
    SendError,
    SendFailure,
    ShopConnectionsPort,
    WhatsAppSender,
)
from woassi.modules.messaging.application.use_cases.templates import EnsureTemplateSendable
from woassi.modules.messaging.domain.content import MessageContent, Recipient, TemplateMessage
from woassi.modules.messaging.domain.errors import (
    InvalidRecipient,
    MessageNotSent,
    OutsideCustomerServiceWindow,
    ShopNotConnected,
    TemplateNotApproved,
)
from woassi.modules.messaging.domain.outbound import OutboundMessage
from woassi.shared.application.ports import Clock
from woassi.shared.domain.errors import DomainError

log = structlog.get_logger()


def _to_domain_error(error: SendError) -> DomainError:
    """Turns a `SendError` into the domain error the caller sees."""
    if error.failure is SendFailure.OUTSIDE_WINDOW:
        return OutsideCustomerServiceWindow(
            "The customer last wrote more than 24 hours ago: use an approved template."
        )
    if error.failure is SendFailure.INVALID_RECIPIENT:
        return InvalidRecipient("This recipient cannot receive WhatsApp messages.")
    return MessageNotSent("WhatsApp did not accept the message. Try again later.")


class SendWhatsAppMessage:
    """For other modules, through a port: orders, billing and payments all message customers."""

    def __init__(
        self,
        uow: MessagingUnitOfWork,
        shops: ShopConnectionsPort,
        sender: WhatsAppSender,
        templates: EnsureTemplateSendable,
        clock: Clock,
    ) -> None:
        self._uow = uow
        self._shops = shops
        self._sender = sender
        self._templates = templates
        self._clock = clock

    async def execute(
        self, *, shop_id: UUID, recipient: Recipient, content: MessageContent, kind: str
    ) -> str:
        """Return Meta's message id. `kind` says what the message is for, e.g. order.validation."""
        connection = await self._shops.by_shop_id(shop_id)
        if connection is None:
            raise ShopNotConnected("This shop has no connected WhatsApp number.")
        if isinstance(content, TemplateMessage) and not await self._templates.execute(
            connection, content.name, content.language
        ):
            raise TemplateNotApproved(
                f"Template {content.name} ({content.language}) is not approved for this shop."
            )
        try:
            message_id = await self._sender.send(connection, recipient, content)
        except SendError as error:
            log.warning(
                "whatsapp_send_failed",
                shop_id=str(shop_id),
                kind=kind,
                failure=error.failure.name,
                meta_code=error.meta_code,
            )
            raise _to_domain_error(error) from None

        # Meta may report the status before this commit: that early status is then ignored.
        async with self._uow as uow:
            await uow.outbound.add(
                OutboundMessage(
                    id=message_id,
                    shop_id=shop_id,
                    recipient=recipient.address,
                    kind=kind,
                    created_at=self._clock.now(),
                )
            )
            await uow.commit()
        return message_id
