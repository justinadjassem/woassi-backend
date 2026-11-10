"""SQLAlchemy implementation of the order repository."""

from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from woassi.modules.orders.application.ports import DuplicateOrder
from woassi.modules.orders.domain.order import (
    ClosingReason,
    Deadlines,
    DecidedBy,
    Order,
    OrderLine,
    OrderStatus,
)
from woassi.modules.orders.infrastructure.models import OrderCounterModel, OrderModel
from woassi.shared.domain.events import Entity
from woassi.shared.domain.values import Amount


def _line_to_json(line: OrderLine) -> dict[str, Any]:
    """Serializes an order line for the `lines` JSON column."""
    return {
        "retailer_id": line.retailer_id,
        "name": line.name,
        "unit_price": line.unit_price.value,
        "requested": line.requested,
        "quantity": line.quantity,
        "product_id": str(line.product_id) if line.product_id else None,
    }


def _line_from_json(data: dict[str, Any]) -> OrderLine:
    """Parses an order line written by `_line_to_json`."""
    return OrderLine(
        retailer_id=data["retailer_id"],
        name=data["name"],
        unit_price=Amount(data["unit_price"]),
        requested=data["requested"],
        quantity=data["quantity"],
        product_id=UUID(data["product_id"]) if data["product_id"] else None,
    )


def _to_domain(model: OrderModel) -> Order:
    """Converts a row to a domain `Order`."""
    return Order(
        id=model.id,
        shop_id=model.shop_id,
        number=model.number,
        customer_id=model.customer_id,
        source_message_id=model.source_message_id,
        lines=tuple(_line_from_json(line) for line in model.lines),
        status=OrderStatus(model.status),
        created_at=model.created_at,
        expires_at=model.expires_at,
        deadlines=Deadlines(**model.deadlines),
        note=model.note,
        awaiting_merchant_since=model.awaiting_merchant_since,
        next_reminder_at=model.next_reminder_at,
        reminders_sent=model.reminders_sent,
        merchant_message_ids=list(model.merchant_message_ids),
        offer_message_id=model.offer_message_id,
        closed_at=model.closed_at,
        closing_reason=ClosingReason(model.closing_reason) if model.closing_reason else None,
        decided_by=DecidedBy(model.decided_by) if model.decided_by else None,
    )


def _copy(order: Order, model: OrderModel) -> OrderModel:
    """Copies every field of `order` onto `model`, `next_deadline` included, and returns `model`."""
    model.id = order.id
    model.shop_id = order.shop_id
    model.number = order.number
    model.customer_id = order.customer_id
    model.source_message_id = order.source_message_id
    model.lines = [_line_to_json(line) for line in order.lines]
    model.total = order.total.value
    model.status = order.status.value
    model.note = order.note
    model.deadlines = {
        "reminder_1_minutes": order.deadlines.reminder_1_minutes,
        "reminder_2_minutes": order.deadlines.reminder_2_minutes,
        "expiration_hours": order.deadlines.expiration_hours,
    }
    model.created_at = order.created_at
    model.expires_at = order.expires_at
    model.awaiting_merchant_since = order.awaiting_merchant_since
    model.next_reminder_at = order.next_reminder_at
    model.next_deadline = order.next_deadline
    model.reminders_sent = order.reminders_sent
    model.merchant_message_ids = list(order.merchant_message_ids)
    model.offer_message_id = order.offer_message_id
    model.closed_at = order.closed_at
    model.closing_reason = order.closing_reason.value if order.closing_reason else None
    model.decided_by = order.decided_by.value if order.decided_by else None
    return model


class SqlAlchemyOrderRepository:
    """`OrderRepository` on SQLAlchemy. Saved orders are tracked for their events."""

    def __init__(self, session: AsyncSession, track: Callable[[Entity], None]) -> None:
        self._session = session
        self._track = track

    async def add(self, order: Order) -> None:
        """Inserts the order; raises DuplicateOrder if its cart message already has one."""
        self._session.add(_copy(order, OrderModel()))
        try:
            await self._session.flush()
        except IntegrityError as error:
            if "uq_orders_shop_id_source_message_id" in str(error.orig):
                raise DuplicateOrder(order.source_message_id) from error
            raise
        self._track(order)

    async def get(self, shop_id: UUID, order_id: UUID, *, lock: bool = False) -> Order | None:
        query = select(OrderModel).where(OrderModel.id == order_id, OrderModel.shop_id == shop_id)
        model = await self._session.scalar(query.with_for_update() if lock else query)
        return _to_domain(model) if model else None

    async def get_by_source_message(self, shop_id: UUID, message_id: str) -> Order | None:
        model = await self._session.scalar(
            select(OrderModel).where(
                OrderModel.shop_id == shop_id, OrderModel.source_message_id == message_id
            )
        )
        return _to_domain(model) if model else None

    async def list(
        self, shop_id: UUID, status: OrderStatus | None, offset: int, limit: int
    ) -> Sequence[Order]:
        query = select(OrderModel).where(OrderModel.shop_id == shop_id)
        if status is not None:
            query = query.where(OrderModel.status == status.value)
        models = await self._session.scalars(
            query.order_by(OrderModel.created_at.desc(), OrderModel.id).offset(offset).limit(limit)
        )
        return [_to_domain(model) for model in models]

    async def count(self, shop_id: UUID, status: OrderStatus | None) -> int:
        query = select(func.count()).select_from(OrderModel).where(OrderModel.shop_id == shop_id)
        if status is not None:
            query = query.where(OrderModel.status == status.value)
        return (await self._session.scalar(query)) or 0

    async def list_due(self, now: datetime, limit: int) -> Sequence[tuple[UUID, UUID]]:
        """Open orders whose next deadline has passed, earliest first."""
        rows = await self._session.execute(
            select(OrderModel.shop_id, OrderModel.id)
            .where(OrderModel.next_deadline <= now)
            .order_by(OrderModel.next_deadline)
            .limit(limit)
        )
        return [(row.shop_id, row.id) for row in rows]

    async def next_number(self, shop_id: UUID) -> int:
        # The row stays locked until the order is committed, so numbers never repeat.
        result = await self._session.execute(
            insert(OrderCounterModel)
            .values(shop_id=shop_id, last_number=1)
            .on_conflict_do_update(
                index_elements=[OrderCounterModel.shop_id],
                set_={"last_number": OrderCounterModel.last_number + 1},
            )
            .returning(OrderCounterModel.last_number)
        )
        return int(result.scalar_one())

    async def save(self, order: Order) -> None:
        """Updates an existing order; raises LookupError if it does not exist."""
        model = await self._session.get(OrderModel, order.id)
        if model is None:
            raise LookupError(f"Order {order.id} does not exist.")
        _copy(order, model)
        await self._session.flush()
        self._track(order)
