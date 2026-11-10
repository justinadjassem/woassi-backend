"""Errors of the orders module."""

from woassi.shared.domain.errors import Conflict, NotFound


class OrderNotFound(NotFound):
    """The order (or its shop) does not exist."""

    code = "orders.order_not_found"


class OrderNotOpen(Conflict):
    """The order was already confirmed, refused, cancelled or has expired."""

    code = "orders.order_not_open"
