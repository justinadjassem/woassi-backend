"""Account registration and phone number verification."""

from woassi.modules.accounts.application.dto import AccountView, account_view
from woassi.modules.accounts.application.limits import CODE_LIMIT, CODE_WINDOW_S, code_key, enforce
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
from woassi.modules.accounts.domain.account import Account, validate_password
from woassi.modules.accounts.domain.errors import InvalidCode
from woassi.modules.accounts.domain.verification_code import CodePurpose
from woassi.shared.application.ports import Clock, PhoneNormalizer, RateLimiter


class Register:
    """Creates an account and sends a code to verify its phone number."""

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

    async def execute(
        self, *, full_name: str, phone: str, password: str, email: str | None = None
    ) -> AccountView:
        """Creates the account, unverified, and sends the verification code once committed.

        Raises:
            InvalidData: the phone number is not valid.
            WeakPassword: the password breaks the length rules.
            RateLimited: too many codes sent to this phone number.
            PhoneAlreadyUsed: an account already uses this phone number.
        """
        normalized = self._phones.normalize(phone)
        validate_password(password)
        await enforce(self._limiter, code_key(normalized.value), CODE_LIMIT, CODE_WINDOW_S)

        account = Account.register(
            full_name=full_name,
            phone=normalized,
            password_hash=await self._hasher.hash(password),
            now=self._clock.now(),
            email=email,
        )
        async with self._uow as uow:
            await uow.accounts.add(account)
            code = await issue_code(
                uow,
                self._hasher,
                self._clock,
                account_id=account.id,
                purpose=CodePurpose.PHONE_VERIFICATION,
            )
            await uow.commit()
        await self._sender.send(normalized, code, CodePurpose.PHONE_VERIFICATION)
        return account_view(account)


class VerifyPhone:
    """Marks an account's phone number as verified, with the code sent to it."""

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

    async def execute(self, *, phone: str, code: str) -> None:
        """Raises InvalidCode for an unknown phone or a wrong, expired or exhausted code.

        Every case takes the same time, so the error does not reveal whether the phone exists.
        """
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
                purpose=CodePurpose.PHONE_VERIFICATION,
                code=code,
            )
            account.verify_phone()
            await uow.accounts.save(account)
            await uow.commit()
