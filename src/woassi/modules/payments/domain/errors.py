"""Errors of the payments module."""

from woassi.shared.domain.errors import Conflict, InvalidData, NotFound, UpstreamError


class PaymentNotFound(NotFound):
    """The payment (or its order) does not exist in this shop."""

    code = "payments.payment_not_found"


class OrderNotPayable(Conflict):
    """Only a CONFIRMED order can be paid."""

    code = "payments.order_not_payable"


class OrderAlreadyPaid(Conflict):
    """The order already has a PAID payment."""

    code = "payments.order_already_paid"


class InvalidPaymentTransition(Conflict):
    """The payment's status cannot move that way: statuses only move forward."""

    code = "payments.invalid_transition"


class AmountOutOfRange(InvalidData):
    """KPrimePay collects 10 to 2,000,000 XOF."""

    code = "payments.amount_out_of_range"


class InvalidProviderKey(InvalidData):
    """The KPrimePay key is malformed, or KPrimePay refused it."""

    code = "payments.invalid_provider_key"


class ProviderNotConfigured(Conflict):
    """The shop has no KPrimePay account: only manual payments are possible."""

    code = "payments.provider_not_configured"


class ProviderUnavailable(UpstreamError):
    """KPrimePay did not answer usefully. Trying again later may work."""

    code = "payments.provider_unavailable"


class UnknownCallback(NotFound):
    """No shop has this callback token."""

    code = "payments.unknown_callback"


class OrderNotFound(NotFound):
    """The shop has no such order."""

    code = "payments.order_not_found"


class PushLimitReached(Conflict):
    """A payment takes at most 3 USSD pushes; then only a payment link."""

    code = "payments.push_limit_reached"


class NoPayerNumber(Conflict):
    """No Togolese Mobile Money number is known for this payment."""

    code = "payments.no_payer_number"
