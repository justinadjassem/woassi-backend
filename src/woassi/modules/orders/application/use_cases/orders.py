"""The order flow: from a WhatsApp cart to a confirmed or closed order.

Each step commits first, then sends messages outside the transaction: a failed message never
undoes a step.
"""

from dataclasses import dataclass, field
from uuid import UUID

import structlog

from woassi.modules.orders.application.ports import (
    CatalogPort,
    Contact,
    CustomersPort,
    DuplicateOrder,
    MessagingPort,
    OrdersUnitOfWork,
    ShopProfile,
    ShopsPort,
)
from woassi.modules.orders.application.use_cases.support import (
    ACCEPT,
    OFFER_NO,
    OFFER_YES,
    REFUSE,
    customer_update,
    deadlines,
    merchant_request,
    notify,
    policy,
    release,
)
from woassi.modules.orders.domain.errors import OrderNotFound
from woassi.modules.orders.domain.order import (
    DueAction,
    Order,
    OrderLine,
    OrderStatus,
)
from woassi.shared.application.ports import Clock
from woassi.shared.domain.values import Amount

log = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class Cart:
    """A cart sent on WhatsApp: (retailer id, quantity) pairs, as the customer chose them."""

    message_id: str
    customer_id: UUID
    items: tuple[tuple[str, int], ...]  # (retailer_id, quantity)
    note: str = field(default="", repr=False)


class OrderFlow:
    """Shared by the use cases that move an order: load, save, and tell the people involved."""

    def __init__(
        self,
        uow: OrdersUnitOfWork,
        shops: ShopsPort,
        catalog: CatalogPort,
        customers: CustomersPort,
        messaging: MessagingPort,
        clock: Clock,
    ) -> None:
        self._uow = uow
        self._shops = shops
        self._catalog = catalog
        self._customers = customers
        self._messaging = messaging
        self._clock = clock

    async def _profile(self, shop_id: UUID) -> ShopProfile:
        """The shop's profile; raises OrderNotFound if the shop is gone."""
        profile = await self._shops.profile(shop_id)
        if profile is None:
            raise OrderNotFound(f"Shop {shop_id} not found.")
        return profile

    async def _after_change(self, order: Order, profile: ShopProfile, *, kind: str) -> None:
        """Outside any transaction: give stock back, tell the customer, ask the merchant."""
        if order.status in (OrderStatus.REJECTED, OrderStatus.CANCELLED, OrderStatus.EXPIRED):
            await release(self._catalog, order)

        customer = await self._customers.contact(order.shop_id, order.customer_id)
        recorded: list[tuple[str, str]] = []
        sent = await notify(
            self._messaging, order, customer, customer_update(order, profile.language), kind=kind
        )
        if sent and order.status is OrderStatus.AWAITING_CUSTOMER:
            recorded.append(("offer", sent))
        if order.status is OrderStatus.AWAITING_MERCHANT and not order.merchant_message_ids:
            sent = await self._ask_merchant(
                order, profile, customer, kind="order.validation_request"
            )
            if sent:
                recorded.append(("merchant", sent))
        if recorded:
            await self._record_messages(order.shop_id, order.id, recorded)

    async def _ask_merchant(
        self, order: Order, profile: ShopProfile, customer: Contact, *, kind: str
    ) -> str | None:
        """Sends the validation template to the merchant's notification number, if any."""
        if profile.notification_number is None:
            # No number to ask on WhatsApp: the order waits in the dashboard.
            return None
        return await notify(
            self._messaging,
            order,
            Contact(phone=profile.notification_number),
            merchant_request(order, profile, customer.name),
            kind=kind,
        )

    async def _record_messages(
        self, shop_id: UUID, order_id: UUID, messages: list[tuple[str, str]]
    ) -> None:
        """Reply webhooks are only trusted when they answer one of these messages."""
        async with self._uow as uow:
            order = await uow.orders.get(shop_id, order_id, lock=True)
            if order is None:
                return
            for role, message_id in messages:
                if role == "offer":
                    order.record_offer_message(message_id)
                else:
                    order.record_merchant_message(message_id)
            await uow.orders.save(order)
            await uow.commit()


class ReceiveCart(OrderFlow):
    """Turns a cart into an order: reserves stock, then confirms it, sends it to the merchant,
    or offers the customer what is left.
    """

    async def execute(self, *, shop_id: UUID, cart: Cart) -> UUID | None:
        """Return the order id, or None if this shop takes no orders."""
        profile = await self._shops.profile(shop_id)
        if profile is None:
            return None

        async with self._uow as uow:
            existing = await uow.orders.get_by_source_message(shop_id, cart.message_id)
        if existing is not None:
            # Replayed webhook: the stock is already held and the messages already sent.
            return existing.id

        requested: dict[str, int] = {}
        for retailer_id, quantity in cart.items:
            requested[retailer_id] = requested.get(retailer_id, 0) + max(quantity, 0)
        items = await self._catalog.reserve(shop_id, requested)
        lines = tuple(
            OrderLine(
                retailer_id=item.retailer_id,
                name=item.name,
                unit_price=Amount(item.unit_price),
                requested=item.requested,
                quantity=item.reserved,
                product_id=item.product_id,
            )
            for item in items
        )

        try:
            async with self._uow as uow:
                order = Order.receive(
                    shop_id=shop_id,
                    number=await uow.orders.next_number(shop_id),
                    customer_id=cart.customer_id,
                    source_message_id=cart.message_id,
                    lines=lines,
                    note=cart.note,
                    policy=policy(profile),
                    deadlines=deadlines(profile),
                    now=self._clock.now(),
                )
                await uow.orders.add(order)
                await uow.commit()
        except BaseException as error:
            # No order holds this stock: give it back. On a duplicate, the run that won holds
            # its own reservation, so this one has to go too.
            held = {i.product_id: i.reserved for i in items if i.product_id and i.reserved}
            if held:
                await self._catalog.release(shop_id, held)
            if not isinstance(error, DuplicateOrder):
                raise
            async with self._uow as uow:
                winner = await uow.orders.get_by_source_message(shop_id, cart.message_id)
            return winner.id if winner else None

        await self._after_change(order, profile, kind="order.received")
        return order.id


class ReplyToOrder(OrderFlow):
    """A button tapped on WhatsApp, by the merchant or by the customer."""

    async def execute(self, *, shop_id: UUID, payload: str, context_message_id: str | None) -> bool:
        """False for a reply that does not apply: unknown, stale, or not an answer to us."""
        parts = payload.split(":")
        if len(parts) != 3 or parts[0] != "order" or context_message_id is None:
            return False
        action = parts[1]
        try:
            order_id = UUID(parts[2])
        except ValueError:
            return False
        profile = await self._profile(shop_id)

        async with self._uow as uow:
            order = await uow.orders.get(shop_id, order_id, lock=True)
            if order is None or not order.is_open:
                return False
            # Only a reply to the very message we sent counts: anyone can type a payload.
            to_merchant = context_message_id in order.merchant_message_ids
            to_customer = context_message_id == order.offer_message_id
            awaiting_merchant = order.status is OrderStatus.AWAITING_MERCHANT
            now = self._clock.now()
            audit_action = None
            if action == ACCEPT and to_merchant and awaiting_merchant:
                order.confirm(now)
                audit_action = "order.validated"
            elif action == REFUSE and to_merchant and awaiting_merchant:
                order.refuse(now)
                audit_action = "order.refused"
            elif action == OFFER_YES and to_customer:
                order.accept_offer(policy(profile), now)
            elif action == OFFER_NO and to_customer:
                order.decline_offer(now)
            else:
                return False
            if audit_action:
                # The button reached the merchant's own number: the shop owner decided.
                await uow.audit.record(
                    account_id=profile.account_id,
                    action=audit_action,
                    target_type="order",
                    target_id=str(order.id),
                    shop_id=shop_id,
                    details={"number": order.number, "channel": "whatsapp"},
                )
            await uow.orders.save(order)
            await uow.commit()

        await self._after_change(order, profile, kind="order.update")
        return True


class DecideOrder(OrderFlow):
    """The merchant accepts or refuses from the dashboard."""

    async def execute(
        self,
        *,
        shop_id: UUID,
        order_id: UUID,
        accept: bool,
        account_id: UUID,
        ip: str | None = None,
    ) -> Order:
        """Raises OrderNotFound, or OrderNotOpen if the order no longer awaits the merchant."""
        profile = await self._profile(shop_id)
        async with self._uow as uow:
            order = await uow.orders.get(shop_id, order_id, lock=True)
            if order is None:
                raise OrderNotFound(f"Order {order_id} not found.")
            if accept:
                order.confirm(self._clock.now())
            else:
                order.refuse(self._clock.now())
            await uow.audit.record(
                account_id=account_id,
                action="order.validated" if accept else "order.refused",
                target_type="order",
                target_id=str(order.id),
                shop_id=shop_id,
                details={"number": order.number, "channel": "dashboard"},
                ip=ip,
            )
            await uow.orders.save(order)
            await uow.commit()
        await self._after_change(order, profile, kind="order.update")
        return order


class ProcessDueOrders(OrderFlow):
    """Cron job: reminds the merchant, then expires. Safe to run on several workers at once."""

    async def execute(self, *, limit: int = 200) -> int:
        """Handles up to `limit` due orders; returns how many it moved. One failure skips that
        order only.
        """
        async with self._uow as uow:
            due = await uow.orders.list_due(self._clock.now(), limit)
        handled = 0
        for shop_id, order_id in due:
            try:
                handled += await self._one(shop_id, order_id)
            except Exception:
                log.exception("due_order_failed", order_id=str(order_id))
        return handled

    async def _one(self, shop_id: UUID, order_id: UUID) -> int:
        """Reminds or expires one order; returns 1 if it did something, 0 otherwise."""
        profile = await self._shops.profile(shop_id)
        async with self._uow as uow:
            order = await uow.orders.get(shop_id, order_id, lock=True)
            # Checked again under the lock: the merchant may have answered meanwhile.
            action = order.due_action(self._clock.now()) if order else None
            if order is None or action is None:
                return 0
            if action is DueAction.EXPIRE:
                order.expire(self._clock.now())
            else:
                order.reminded()
            await uow.orders.save(order)
            await uow.commit()

        if profile is None:
            # The shop is gone: still give the stock back.
            if action is DueAction.EXPIRE:
                await release(self._catalog, order)
            return 1
        if action is DueAction.EXPIRE:
            await self._after_change(order, profile, kind="order.expired")
        else:
            customer = await self._customers.contact(shop_id, order.customer_id)
            sent = await self._ask_merchant(
                order, profile, customer, kind="order.validation_reminder"
            )
            if sent:
                await self._record_messages(shop_id, order.id, [("merchant", sent)])
        return 1


@dataclass(frozen=True, slots=True)
class OrderPage:
    """One page of orders, and the total number of matching orders."""

    items: list[Order]
    total: int
    offset: int
    limit: int


class ListOrders:
    """Lists a shop's orders, one page at a time."""

    def __init__(self, uow: OrdersUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self, *, shop_id: UUID, status: OrderStatus | None, offset: int, limit: int
    ) -> OrderPage:
        async with self._uow as uow:
            orders = await uow.orders.list(shop_id, status, offset, limit)
            total = await uow.orders.count(shop_id, status)
        return OrderPage(items=list(orders), total=total, offset=offset, limit=limit)


class GetOrder:
    """Returns one order of a shop."""

    def __init__(self, uow: OrdersUnitOfWork) -> None:
        self._uow = uow

    async def execute(self, *, shop_id: UUID, order_id: UUID) -> Order:
        """Raises OrderNotFound if the shop has no such order."""
        async with self._uow as uow:
            order = await uow.orders.get(shop_id, order_id)
        if order is None:
            raise OrderNotFound(f"Order {order_id} not found.")
        return order
