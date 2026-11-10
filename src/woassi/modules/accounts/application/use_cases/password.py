"""Password use cases: reset by code, and change while logged in."""

from uuid import UUID

from woassi.modules.accounts.application.limits import (
    CODE_LIMIT,
    CODE_WINDOW_S,
    LOGIN_LIMIT,
    LOGIN_WINDOW_S,
    code_key,
    enforce,
    password_change_key,
)
from woassi.modules.accounts.application.ports import (
    AccountsUnitOfWork,
    PasswordHasher,
    VerificationCodeSender,
)
from woassi.modules.accounts.application.use_cases.support import (
    consume_code,
    issue_code,
    parse_phone,
)
from woassi.modules.accounts.domain.account import validate_password
from woassi.modules.accounts.domain.errors import InvalidCode, WrongPassword
from woassi.modules.accounts.domain.verification_code import CodePurpose
from woassi.shared.application.ports import Clock, PhoneNormalizer, RateLimiter
from woassi.shared.domain.errors import Unauthorized


class RequestPasswordReset:
    """Sends a password reset code to a phone number.

    Rate limited per phone, and neutral: the response never tells whether the phone exists.
    """

    def __init__(
        self,
        uow: AccountsUnitOfWork,
        hasher: PasswordHasher,
        sender: VerificationCodeSender,
        limiter: RateLimiter,
        phones: PhoneNormalizer,
        clock: Clock,
    ) -> None:
        self._uow = uow
        self._hasher = hasher
        self._sender = sender
        self._limiter = limiter
        self._phones = phones
        self._clock = clock

    async def execute(self, phone: str) -> None:
        """Same outcome whether or not the number exists, 429 included."""
        normalized = parse_phone(self._phones, phone)
        await enforce(
            self._limiter,
            code_key(normalized.value if normalized else phone),
            CODE_LIMIT,
            CODE_WINDOW_S,
        )
        # Checked after the rate limit, so an invalid number costs an attempt too.
        if normalized is None:
            return
        async with self._uow as uow:
            account = await uow.accounts.get_by_phone(normalized)
            if account is None:
                return
            code = await issue_code(
                uow,
                self._hasher,
                self._clock,
                account_id=account.id,
                purpose=CodePurpose.PASSWORD_RESET,
            )
            await uow.commit()
        await self._sender.send(normalized, code, CodePurpose.PASSWORD_RESET)


class ResetPassword:
    """Sets a new password with a reset code, then ends every session of the account."""

    def __init__(
        self,
        uow: AccountsUnitOfWork,
        hasher: PasswordHasher,
        phones: PhoneNormalizer,
        clock: Clock,
    ) -> None:
        self._uow = uow
        self._hasher = hasher
        self._phones = phones
        self._clock = clock

    async def execute(self, *, phone: str, code: str, new_password: str) -> None:
        """Checks `code` and sets `new_password`.

        Raises:
            WeakPassword: `new_password` breaks the length rules.
            InvalidCode: unknown phone, or wrong, expired or exhausted code (always the same
            error).
        """
        validate_password(new_password)
        normalized = parse_phone(self._phones, phone)
        async with self._uow as uow:
            account = await uow.accounts.get_by_phone(normalized) if normalized else None
            if account is None:
                await self._hasher.dummy_verify(code)
                raise InvalidCode("Invalid or expired code.")
            await consume_code(
                uow,
                self._hasher,
                self._clock,
                account_id=account.id,
                purpose=CodePurpose.PASSWORD_RESET,
                code=code,
            )
            account.change_password(await self._hasher.hash(new_password))
            # The code reached this phone, which proves that the owner controls it.
            account.verify_phone()
            await uow.accounts.save(account)
            await uow.refresh_tokens.revoke_all(account.id, self._clock.now())
            await uow.commit()


class ChangePassword:
    """Changes the password of a logged-in account, which must give its current password."""

    def __init__(
        self,
        uow: AccountsUnitOfWork,
        hasher: PasswordHasher,
        limiter: RateLimiter,
        clock: Clock,
    ) -> None:
        self._uow = uow
        self._hasher = hasher
        self._limiter = limiter
        self._clock = clock

    async def execute(self, *, account_id: UUID, current_password: str, new_password: str) -> None:
        """Checks `current_password`, then sets `new_password` and ends every session.

        Raises:
            WeakPassword: `new_password` breaks the length rules.
            RateLimited: too many wrong current passwords.
            WrongPassword: `current_password` is wrong.
        """
        validate_password(new_password)
        key = password_change_key(account_id)
        await enforce(self._limiter, key, LOGIN_LIMIT, LOGIN_WINDOW_S)
        async with self._uow as uow:
            account = await uow.accounts.get(account_id)
            if account is None:
                raise Unauthorized("Authentication required.")
            if not await self._hasher.verify(current_password, account.password_hash):
                raise WrongPassword("The current password is wrong.")
            account.change_password(await self._hasher.hash(new_password))
            await uow.accounts.save(account)
            # Every session ends, including one that a thief may have opened.
            await uow.refresh_tokens.revoke_all(account_id, self._clock.now())
            await uow.commit()
        # Only failures count toward the limit.
        await self._limiter.forgive(key)
