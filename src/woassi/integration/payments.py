"""Orders, shops, customers and messaging seen from `payments`."""

from uuid import UUID

import structlog

from woassi.modules.customers.application.use_cases.customers import GetCustomer
from woassi.modules.customers.domain.errors import CustomerNotFound
from woassi.modules.messaging.application.use_cases.sending import SendWhatsAppMessage
from woassi.modules.messaging.domain.content import (
    ButtonsMessage,
    MessageContent,
    Recipient,
    ReplyButton,
    TemplateMessage,
    TextMessage,
)
from woassi.modules.orders.application.use_cases.orders import GetOrder
from woassi.modules.orders.domain.errors import OrderNotFound
from woassi.modules.payments.application.ports import (
    Buttons,
    Contact,
    Notification,
    NotificationNotSent,
    PayableOrder,
    ShopInfo,
    Template,
    Text,
)
from woassi.modules.shops.application.use_cases.shops import GetShopOrderProfile
from woassi.shared.domain.errors import DomainError
from woassi.shared.domain.values import PhoneNumber

log = structlog.get_logger()


class OrdersForPayments:
    """OrdersPort, for the payments module, backed by the orders module."""

    def __init__(self, use_case: GetOrder) -> None:
        self._use_case = use_case

    async def get(self, shop_id: UUID, order_id: UUID) -> PayableOrder | None:
        """The order's number, total, status and customer; None if the shop has no such order."""
        try:
            order = await self._use_case.execute(shop_id=shop_id, order_id=order_id)
        except OrderNotFound:
            return None
        return PayableOrder(
            order_id=order.id,
            shop_id=order.shop_id,
            number=order.number,
            total=order.total.value,
            status=order.status.value,
            customer_id=order.customer_id,
        )


class ShopsForPayments:
    """ShopsPort, for the payments module, backed by the shops module."""

    def __init__(self, use_case: GetShopOrderProfile) -> None:
        self._use_case = use_case

    async def info(self, shop_id: UUID) -> ShopInfo | None:
        profile = await self._use_case.execute(shop_id)
        if profile is None:
            return None
        return ShopInfo(name=profile.name, language=profile.language.value)


class CustomersForPayments:
    """CustomersPort, for the payments module, backed by the customers module."""

    def __init__(self, use_case: GetCustomer) -> None:
        self._use_case = use_case

    async def contact(self, shop_id: UUID, customer_id: UUID) -> Contact:
        try:
            customer = await self._use_case.execute(shop_id=shop_id, customer_id=customer_id)
        except CustomerNotFound:
            return Contact()
        return Contact(
            user_id=customer.user_id,
            phone=customer.phone,
            last_message_at=customer.last_message_at,
        )


def _content(notification: Notification) -> MessageContent:
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
    phone = contact.phone
    if phone and not phone.startswith("+"):
        phone = f"+{phone}"
    return Recipient(phone=PhoneNumber(phone) if phone else None, user_id=contact.user_id)


class PaymentNotifications:
    """MessagingPort, for the payments module, backed by the messaging module."""

    def __init__(self, use_case: SendWhatsAppMessage) -> None:
        self._use_case = use_case

    async def send(
        self, shop_id: UUID, to: Contact, notification: Notification, *, kind: str
    ) -> str:
        """Any refusal (outside the window, template not approved...) is NotificationNotSent."""
        try:
            return await self._use_case.execute(
                shop_id=shop_id, recipient=_recipient(to), content=_content(notification), kind=kind
            )
        except DomainError as error:
            log.info("notification_refused", shop_id=str(shop_id), kind=kind, code=error.code)
            raise NotificationNotSent(error.code) from None
