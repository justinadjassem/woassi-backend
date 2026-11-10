"""Customers module seen from `orders`: how to reach a customer."""

from uuid import UUID

from woassi.modules.customers.application.use_cases.customers import GetCustomer
from woassi.modules.customers.domain.errors import CustomerNotFound
from woassi.modules.orders.application.ports import Contact


class CustomerContacts:
    """CustomersPort, for the orders module, backed by the customers module."""

    def __init__(self, use_case: GetCustomer) -> None:
        self._use_case = use_case

    async def contact(self, shop_id: UUID, customer_id: UUID) -> Contact:
        """WhatsApp contact of a customer; an empty contact if it no longer exists."""
        try:
            customer = await self._use_case.execute(shop_id=shop_id, customer_id=customer_id)
        except CustomerNotFound:
            return Contact()
        return Contact(user_id=customer.user_id, phone=customer.phone, name=customer.name)
