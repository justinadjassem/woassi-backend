"""Errors of the accounts module."""

from woassi.shared.domain.errors import Conflict, Forbidden, InvalidData, Unauthorized


class InvalidCredentials(Unauthorized):
    """Deliberately vague: it must not reveal whether the phone number exists."""

    code = "auth.invalid_credentials"


class InvalidRefreshToken(Unauthorized):
    """The refresh token is unknown, expired, revoked or reused. Always the same error."""

    code = "auth.invalid_refresh_token"


class AccountSuspended(Forbidden):
    """The account is suspended."""

    code = "accounts.suspended"


class PhoneAlreadyUsed(Conflict):
    """An account already uses this phone number."""

    code = "accounts.phone_already_used"


class InvalidAccount(InvalidData):
    """The account data breaks a business rule (e.g. a blank name)."""

    code = "accounts.invalid_account"


class WeakPassword(InvalidData):
    """The password does not have 8 to 128 characters."""

    code = "accounts.weak_password"


class WrongPassword(InvalidData):
    """The current password given to change it is wrong."""

    code = "accounts.wrong_password"


class InvalidCode(InvalidData):
    """Same error for a wrong, expired or exhausted code, or an unknown phone number."""

    code = "accounts.invalid_code"
