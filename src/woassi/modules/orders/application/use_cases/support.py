"""Helpers of the order flow: policies, message contents, and sending."""

from uuid import UUID

import structlog

from woassi.modules.orders.application.ports import (
    Buttons,
    CatalogPort,
    Contact,
    MessagingPort,
    Notification,
    NotificationNotSent,
    ShopProfile,
    Template,
    Text,
)
from woassi.modules.orders.application.texts import VALIDATION_TEMPLATE, amount, summary, text
from woassi.modules.orders.domain.order import (
    ClosingReason,
    Deadlines,
    Order,
    OrderStatus,
    ValidationPolicy,
)
from woassi.shared.domain.values import Amount

log = structlog.get_logger()

# Button ids and payloads come back in the reply webhook: `order:<action>:<order id>`.
ACCEPT, REFUSE, OFFER_YES, OFFER_NO = "accept", "refuse", "offer_yes", "offer_no"


def payload(action: str, order_id: UUID) -> str:
    """Button payload for `action` on `order_id`."""
    return f"order:{action}:{order_id}"


def policy(profile: ShopProfile) -> ValidationPolicy:
    """The shop's validation policy. A 0 or missing limit means no limit."""
    limit = profile.auto_validation_max_amount
    return ValidationPolicy(profile.validation_mode, Amount(limit) if limit else None)


def deadlines(profile: ShopProfile) -> Deadlines:
    """The shop's reminder and expiry delays."""
    return Deadlines(
        reminder_1_minutes=profile.reminder_1_minutes,
        reminder_2_minutes=profile.reminder_2_minutes,
        expiration_hours=profile.expiration_hours,
    )


def customer_update(order: Order, language: str) -> Notification | None:
    """What the customer is told about the order's current state."""
    values = {"number": order.number, "total": amount(order.total)}
    match order.status:
        case OrderStatus.AWAITING_MERCHANT:
            return Text(text(language, "pending", **values))
        case OrderStatus.CONFIRMED:
            return Text(text(language, "confirmed", **values))
        case OrderStatus.AWAITING_CUSTOMER:
            body = text(
                language,
                "offer",
                missing=summary(order.missing_lines, requested=True),
                available=summary(order.available_lines),
                total=amount(order.total),
            )
            return Buttons(
                body[:1024],
                (
                    (payload(OFFER_YES, order.id), text(language, "offer_yes")),
                    (payload(OFFER_NO, order.id), text(language, "offer_no")),
                ),
            )
        case OrderStatus.REJECTED if order.closing_reason is ClosingReason.OUT_OF_STOCK:
            return Text(text(language, "out_of_stock"))
        case OrderStatus.REJECTED:
            return Text(text(language, "refused", **values))
        case OrderStatus.CANCELLED:
            return Text(text(language, "cancelled", **values))
        case OrderStatus.EXPIRED:
            return Text(text(language, "expired", **values))
    return None


def merchant_request(order: Order, profile: ShopProfile, customer_name: str | None) -> Template:
    """The validation template for the merchant, with Accept / Refuse buttons."""
    return Template(
        name=VALIDATION_TEMPLATE,
        language=profile.language,
        parameters={
            "order_number": str(order.number),
            "customer_name": (customer_name or text(profile.language, "customer"))[:100],
            "items": summary(order.lines),
            "total": amount(order.total),
        },
        button_payloads=(payload(ACCEPT, order.id), payload(REFUSE, order.id)),
    )


async def notify(
    messaging: MessagingPort,
    order: Order,
    to: Contact,
    notification: Notification | None,
    *,
    kind: str,
) -> str | None:
    """Never fails the order: a message that cannot go out is only logged."""
    if notification is None or not (to.user_id or to.phone):
        return None
    try:
        return await messaging.send(order.shop_id, to, notification, kind=kind)
    except NotificationNotSent:
        log.warning(
            "order_notification_not_sent",
            order_id=str(order.id),
            shop_id=str(order.shop_id),
            kind=kind,
        )
        return None


async def release(catalog: CatalogPort, order: Order) -> None:
    """Gives back the stock the order holds, if any."""
    if order.reserved:
        await catalog.release(order.shop_id, order.reserved)
