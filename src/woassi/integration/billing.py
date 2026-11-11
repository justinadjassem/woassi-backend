"""Orders, shops, customers and messaging seen from `billing`."""

from uuid import UUID

import structlog

from woassi.modules.billing.application.ports import (
    BillableLine,
    BillableOrder,
    Contact,
    Document,
    Notification,
    NotificationNotSent,
    SellerProfile,
    Template,
)
from woassi.modules.customers.application.use_cases.customers import GetCustomer
from woassi.modules.customers.domain.errors import CustomerNotFound
from woassi.modules.messaging.application.use_cases.sending import SendWhatsAppMessage
from woassi.modules.messaging.domain.content import (
    DocumentMessage,
    MessageContent,
    Recipient,
    TemplateMessage,
)
from woassi.modules.orders.application.use_cases.orders import GetOrder
from woassi.modules.orders.domain.errors import OrderNotFound
from woassi.modules.shops.application.use_cases.shops import GetShopBillingProfile
from woassi.shared.application.ports import StoragePort
from woassi.shared.domain.errors import DomainError
from woassi.shared.domain.values import PhoneNumber

log = structlog.get_logger()

MAX_LOGO_BYTES = 2 * 1024 * 1024


class OrdersForBilling:
    """OrdersPort, for the billing module, backed by the orders module."""

    def __init__(self, use_case: GetOrder) -> None:
        self._use_case = use_case

    async def get(self, shop_id: UUID, order_id: UUID) -> BillableOrder | None:
        """The order's reserved lines at their order-time prices; None if there is no such order."""
        try:
            order = await self._use_case.execute(shop_id=shop_id, order_id=order_id)
        except OrderNotFound:
            return None
        return BillableOrder(
            order_id=order.id,
            shop_id=order.shop_id,
            number=order.number,
            customer_id=order.customer_id,
            total=order.total.value,
            lines=tuple(
                BillableLine(
                    name=line.name,
                    quantity=line.quantity,
                    unit_price=line.unit_price.value,
                    product_id=line.product_id,
                    retailer_id=line.retailer_id,
                )
                for line in order.lines
            ),
        )


class ShopsForBilling:
    """ShopsPort, for the billing module, backed by the shops module and the logo bucket."""

    def __init__(self, use_case: GetShopBillingProfile, logos: StoragePort) -> None:
        self._use_case = use_case
        self._logos = logos

    async def profile(self, shop_id: UUID) -> SellerProfile | None:
        profile = await self._use_case.execute(shop_id)
        if profile is None:
            return None
        return SellerProfile(
            shop_id=profile.shop_id,
            account_id=profile.account_id,
            name=profile.name,
            language=profile.language.value,
            invoice_prefix=profile.invoice_prefix,
            legal_name=profile.legal_name,
            address=profile.address,
            phone=profile.phone,
            tax_id=profile.tax_id,
            trade_register=profile.trade_register,
            tax_regime=profile.tax_regime.value if profile.tax_regime else None,
            vat_registered=profile.vat_registered,
            logo_key=profile.logo_key,
        )

    async def logo(self, profile: SellerProfile) -> bytes | None:
        """The shop's WebP logo from the public bucket. Best effort: None on any failure."""
        if not profile.logo_key:
            return None
        try:
            info = await self._logos.stat(profile.logo_key)
            if info is None or info.size > MAX_LOGO_BYTES:
                return None
            return await self._logos.get(profile.logo_key)
        except Exception:
            log.warning("shop_logo_unavailable", shop_id=str(profile.shop_id))
            return None


class CustomersForBilling:
    """CustomersPort, for the billing module, backed by the customers module."""

    def __init__(self, use_case: GetCustomer) -> None:
        self._use_case = use_case

    async def contact(self, shop_id: UUID, customer_id: UUID) -> Contact:
        try:
            customer = await self._use_case.execute(shop_id=shop_id, customer_id=customer_id)
        except CustomerNotFound:
            return Contact()
        return Contact(
            name=customer.name,
            user_id=customer.user_id,
            phone=customer.phone,
            last_message_at=customer.last_message_at,
        )


def _content(notification: Notification) -> MessageContent:
    match notification:
        case Document():
            return DocumentMessage(
                link=notification.link, filename=notification.filename, caption=notification.caption
            )
        case Template():
            return TemplateMessage(
                notification.name, notification.language, notification.parameters
            )


def _recipient(contact: Contact) -> Recipient:
    phone = contact.phone
    if phone and not phone.startswith("+"):
        phone = f"+{phone}"
    return Recipient(phone=PhoneNumber(phone) if phone else None, user_id=contact.user_id)


class BillingNotifications:
    """MessagingPort, for the billing module, backed by the messaging module."""

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
