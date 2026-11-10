"""Use cases the orders api needs, provided by the composition root."""

from typing import Protocol

from woassi.modules.orders.application.use_cases.orders import DecideOrder, GetOrder, ListOrders


class OrdersUseCases(Protocol):
    """What this module expects from the composition root (see catalog.application.facade)."""

    def list_orders(self) -> ListOrders: ...

    def get_order(self) -> GetOrder: ...

    def decide_order(self) -> DecideOrder: ...
