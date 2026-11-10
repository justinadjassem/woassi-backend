"""Messaging module seen from `orders`: sending WhatsApp notifications."""

from uuid import UUID

import structlog

from woassi.modules.messaging.application.use_cases.sending import SendWhatsAppMessage
from woassi.modules.messaging.domain.content import (
    ButtonsMessage,
    MessageContent,
    Recipient,
    ReplyButton,
    TemplateMessage,
    TextMessage,
)
from woassi.modules.messaging.domain.templates import TemplateDefinition
from woassi.modules.orders.application.ports import (
    Buttons,
    Contact,
    Notification,
    NotificationNotSent,
    Template,
    Text,
)
from woassi.modules.orders.application.texts import TEMPLATES
from woassi.shared.domain.errors import DomainError
from woassi.shared.domain.values import PhoneNumber

log = structlog.get_logger()


def template_definitions() -> list[TemplateDefinition]:
    """The templates every shop needs, as the orders module wrote them."""
    return [
        TemplateDefinition(
            name=template.name,
            language=template.language,
            body=template.body,
            examples=template.examples,
            quick_replies=template.buttons,
        )
        for template in TEMPLATES
    ]


def _content(notification: Notification) -> MessageContent:
    """Converts an orders notification into a messaging content."""
    match notification:
        case Text():
            return TextMessage(notification.body)
        case Buttons():
            return ButtonsMessage(
                notification.body,
                tuple(ReplyButton(id=id_, title=title) for id_, title in notification.buttons),
            )
        case Template():
            return TemplateMessage(
                notification.name,
                notification.language,
                notification.parameters,
                notification.button_payloads,
            )


def _recipient(contact: Contact) -> Recipient:
    """Converts an orders contact into a messaging recipient."""
    phone = contact.phone
    if phone and not phone.startswith("+"):
        # Meta writes `from` without the plus sign.
        phone = f"+{phone}"
    return Recipient(phone=PhoneNumber(phone) if phone else None, user_id=contact.user_id)


class WhatsAppNotifications:
    """MessagingPort, for the orders module, backed by the messaging module."""

    def __init__(self, use_case: SendWhatsAppMessage) -> None:
        self._use_case = use_case

    async def send(
        self, shop_id: UUID, to: Contact, notification: Notification, *, kind: str
    ) -> str:
        """Sends `notification` and returns the WhatsApp message id.

        `kind` labels the message in `outbound_messages`. Any refusal (no connected number, outside
        the 24-hour window, template not approved...) becomes NotificationNotSent.
        """
        try:
            return await self._use_case.execute(
                shop_id=shop_id, recipient=_recipient(to), content=_content(notification), kind=kind
            )
        except DomainError as error:
            log.info("notification_refused", shop_id=str(shop_id), kind=kind, code=error.code)
            raise NotificationNotSent(error.code) from None
