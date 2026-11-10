"""SQLAlchemy implementation of the customer repository."""

from collections.abc import Callable, Sequence
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from woassi.modules.customers.domain.customer import Customer
from woassi.modules.customers.infrastructure.models import CustomerModel
from woassi.shared.domain.events import Entity
from woassi.shared.domain.values import PhoneNumber


def _to_domain(model: CustomerModel) -> Customer:
    """Converts a row to a domain `Customer`."""
    return Customer(
        id=model.id,
        shop_id=model.shop_id,
        created_at=model.created_at,
        user_id=model.user_id,
        phone=PhoneNumber(model.phone) if model.phone else None,
        name=model.name,
        last_message_at=model.last_message_at,
    )


def _copy(customer: Customer, model: CustomerModel) -> CustomerModel:
    """Copies every field of `customer` onto `model`, and returns `model`."""
    model.id = customer.id
    model.shop_id = customer.shop_id
    model.user_id = customer.user_id
    model.phone = customer.phone.value if customer.phone else None
    model.name = customer.name
    model.created_at = customer.created_at
    model.last_message_at = customer.last_message_at
    return model


class SqlAlchemyCustomerRepository:
    """`CustomerRepository` on SQLAlchemy. Saved customers are tracked for their events."""

    def __init__(self, session: AsyncSession, track: Callable[[Entity], None]) -> None:
        self._session = session
        self._track = track

    async def add(self, customer: Customer) -> None:
        self._session.add(_copy(customer, CustomerModel()))
        await self._session.flush()
        self._track(customer)

    async def get(self, shop_id: UUID, customer_id: UUID) -> Customer | None:
        model = await self._session.scalar(
            select(CustomerModel).where(
                CustomerModel.id == customer_id, CustomerModel.shop_id == shop_id
            )
        )
        return _to_domain(model) if model else None

    async def find(
        self, shop_id: UUID, *, user_id: str | None, phone: PhoneNumber | None
    ) -> Customer | None:
        for column, value in ((CustomerModel.user_id, user_id), (CustomerModel.phone, phone)):
            if not value:
                continue
            model = await self._session.scalar(
                select(CustomerModel)
                .where(CustomerModel.shop_id == shop_id, column == str(value))
                .order_by(CustomerModel.created_at)
                .limit(1)
                .with_for_update()
            )
            if model is not None:
                return _to_domain(model)
        return None

    async def list(self, shop_id: UUID, offset: int, limit: int) -> Sequence[Customer]:
        models = await self._session.scalars(
            select(CustomerModel)
            .where(CustomerModel.shop_id == shop_id)
            .order_by(CustomerModel.last_message_at.desc().nulls_last(), CustomerModel.id)
            .offset(offset)
            .limit(limit)
        )
        return [_to_domain(model) for model in models]

    async def count(self, shop_id: UUID) -> int:
        total = await self._session.scalar(
            select(func.count()).select_from(CustomerModel).where(CustomerModel.shop_id == shop_id)
        )
        return total or 0

    async def save(self, customer: Customer) -> None:
        """Updates an existing customer; raises LookupError if it does not exist."""
        model = await self._session.get(CustomerModel, customer.id)
        if model is None:
            raise LookupError(f"Customer {customer.id} does not exist.")
        _copy(customer, model)
        await self._session.flush()
        self._track(customer)
