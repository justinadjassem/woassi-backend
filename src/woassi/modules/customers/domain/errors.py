"""Errors of the customers module."""

from woassi.shared.domain.errors import InvalidData, NotFound


class InvalidCustomer(InvalidData):
    """A customer needs at least a user id or a phone number."""

    code = "customers.invalid_customer"


class CustomerNotFound(NotFound):
    """The customer does not exist in this shop."""

    code = "customers.customer_not_found"
