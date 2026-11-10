"""Ports of the customers module."""

from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from woassi.modules.customers.domain.customer import Customer
from woassi.shared.application.ports import UnitOfWork
from woassi.shared.domain.values import PhoneNumber


class CustomerRepository(Protocol):
    """Stores the customers of every shop."""

    async def add(self, customer: Customer) -> None:
        """Inserts a new customer."""
        ...

    async def get(self, shop_id: UUID, customer_id: UUID) -> Customer | None:
        """The customer, only if it belongs to `shop_id`."""
        ...

    async def find(
        self, shop_id: UUID, *, user_id: str | None, phone: PhoneNumber | None
    ) -> Customer | None:
        """By user id first, then by phone number. Locks the row it returns."""
        ...

    async def list(self, shop_id: UUID, offset: int, limit: int) -> Sequence[Customer]:
        """A page of the shop's customers, most recently active first."""
        ...

    async def count(self, shop_id: UUID) -> int:
        """Number of customers of the shop."""
        ...

    async def save(self, customer: Customer) -> None:
        """Writes back a customer loaded from this repository."""
        ...


class CustomersUnitOfWork(UnitOfWork, Protocol):
    """Unit of Work of the customers module."""

    @property
    def customers(self) -> CustomerRepository: ...
