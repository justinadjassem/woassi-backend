"""Errors of the messaging module."""

from woassi.shared.domain.errors import Conflict, DomainError, InvalidData, UpstreamError


class InvalidSignature(DomainError):
    """The webhook's `X-Hub-Signature-256` does not match its body."""

    code = "messaging.invalid_signature"


class VerificationRefused(DomainError):
    """Meta's subscription check has a wrong verify token or mode."""

    code = "messaging.verification_refused"


class InvalidMessage(InvalidData):
    """The message breaks one of Meta's limits (length, buttons, template name...)."""

    code = "messaging.invalid_message"


class ShopNotConnected(Conflict):
    """The shop has no usable WhatsApp number."""

    code = "messaging.shop_not_connected"


class OutsideCustomerServiceWindow(Conflict):
    """More than 24 hours since the customer last wrote: only an approved template can go."""

    code = "messaging.outside_customer_service_window"


class InvalidRecipient(InvalidData):
    """The recipient cannot receive WhatsApp messages."""

    code = "messaging.invalid_recipient"


class MessageNotSent(UpstreamError):
    """Meta did not take the message; trying again later may work."""

    code = "messaging.message_not_sent"


class TemplateNotApproved(Conflict):
    """Meta has not approved the template yet, or no longer allows it."""

    code = "messaging.template_not_approved"
