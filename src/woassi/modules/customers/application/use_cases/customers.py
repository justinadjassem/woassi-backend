"""Customer records: kept up to date by inbound messages, read by the dashboard."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from woassi.modules.customers.application.ports import CustomersUnitOfWork
from woassi.modules.customers.domain.customer import Customer
from woassi.modules.customers.domain.errors import CustomerNotFound
from woassi.shared.application.pagination import PageResult
from woassi.shared.domain.values import PhoneNumber


@dataclass(frozen=True, slots=True)
class CustomerView:
    """Read-only view of a customer."""

    id: UUID
    user_id: str | None
    phone: str | None
    name: str | None
    created_at: datetime
    last_message_at: datetime | None


def customer_view(customer: Customer) -> CustomerView:
    """Builds the view of `customer`."""
    return CustomerView(
        id=customer.id,
        user_id=customer.user_id,
        phone=customer.phone.value if customer.phone else None,
        name=customer.name,
        created_at=customer.created_at,
        last_message_at=customer.last_message_at,
    )


class RecordContact:
    """A customer wrote to a shop: create the record, or bring it up to date."""

    def __init__(self, uow: CustomersUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        *,
        shop_id: UUID,
        user_id: str | None,
        phone: PhoneNumber | None,
        name: str | None,
        at: datetime,
    ) -> UUID:
        """Creates or updates the customer, and returns its id."""
        async with self._uow as uow:
            customer = await uow.customers.find(shop_id, user_id=user_id, phone=phone)
            if customer is None:
                customer = Customer.first_contact(
                    shop_id=shop_id, user_id=user_id, phone=phone, name=name, now=at
                )
                await uow.customers.add(customer)
            else:
                customer.wrote(user_id=user_id, phone=phone, name=name, now=at)
                await uow.customers.save(customer)
            await uow.commit()
        return customer.id


class ChangeCustomerUserId:
    """Meta gives a new BSUID when a user changes phone number (`user_changed_number`)."""

    def __init__(self, uow: CustomersUnitOfWork) -> None:
        self._uow = uow

    async def execute(self, *, shop_id: UUID, previous_user_id: str, user_id: str) -> bool:
        """Moves the customer to `user_id`. False if no customer had `previous_user_id`."""
        async with self._uow as uow:
            customer = await uow.customers.find(shop_id, user_id=previous_user_id, phone=None)
            if customer is None or customer.user_id != previous_user_id:
                return False
            customer.user_id = user_id
            await uow.customers.save(customer)
            await uow.commit()
        return True


class ListCustomers:
    """Lists a shop's customers, one page at a time."""

    def __init__(self, uow: CustomersUnitOfWork) -> None:
        self._uow = uow

    async def execute(self, *, shop_id: UUID, offset: int, limit: int) -> PageResult[CustomerView]:
        async with self._uow as uow:
            customers = await uow.customers.list(shop_id, offset, limit)
            total = await uow.customers.count(shop_id)
        return PageResult(
            items=[customer_view(c) for c in customers], total=total, offset=offset, limit=limit
        )


class GetCustomer:
    """Returns one customer of a shop."""

    def __init__(self, uow: CustomersUnitOfWork) -> None:
        self._uow = uow

    async def execute(self, *, shop_id: UUID, customer_id: UUID) -> CustomerView:
        """Raises CustomerNotFound if the shop has no such customer."""
        async with self._uow as uow:
            customer = await uow.customers.get(shop_id, customer_id)
        if customer is None:
            raise CustomerNotFound(f"Customer {customer_id} not found.")
        return customer_view(customer)
