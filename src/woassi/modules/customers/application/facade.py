"""Use cases the customers api needs, provided by the composition root."""

from typing import Protocol

from woassi.modules.customers.application.use_cases.customers import GetCustomer, ListCustomers


class CustomersUseCases(Protocol):
    """What this module expects from the composition root (see catalog.application.facade)."""

    def list_customers(self) -> ListCustomers: ...

    def get_customer(self) -> GetCustomer: ...
