"""Errors of the shops module."""

from woassi.shared.domain.errors import Conflict, InvalidData, UpstreamError


class InvalidShop(InvalidData):
    """The shop data breaks a business rule (e.g. a blank name)."""

    code = "shops.invalid_shop"


class InvalidSettings(InvalidData):
    """The settings are inconsistent (reminder order, invoice prefix...)."""

    code = "shops.invalid_settings"


class InvalidReceivingAccount(InvalidData):
    """The receiving account data breaks a business rule."""

    code = "shops.invalid_receiving_account"


class InvalidLogo(InvalidData):
    """The logo's type or size is not accepted, or the file is missing or unreadable."""

    code = "shops.invalid_logo"


class ShopNotActivatable(Conflict):
    """The shop cannot go ACTIVE: no connected number, or suspended."""

    code = "shops.not_activatable"


class SlugTaken(Conflict):
    """Another shop already uses this slug."""

    code = "shops.slug_taken"


class WhatsAppAlreadyConnected(Conflict):
    """The shop already has a connected number."""

    code = "shops.whatsapp_already_connected"


class PhoneNumberAlreadyConnected(Conflict):
    """Another shop holds this WhatsApp number."""

    code = "shops.phone_number_already_connected"


class WhatsAppNotFound(InvalidData):
    """The shop has no WhatsApp connection."""

    code = "shops.whatsapp_not_connected"


class InvalidSignupCode(InvalidData):
    """The Embedded Signup code expired or was already used."""

    code = "shops.invalid_signup_code"


class PhoneNumberNotUsable(InvalidData):
    """The number cannot be used through the Cloud API."""

    code = "shops.phone_number_not_usable"


class WhatsAppConnectionFailed(UpstreamError):
    """Meta refused the connection or did not answer."""

    code = "shops.whatsapp_connection_failed"
