"""Session use cases: login, token refresh, logout and access token authentication.

A session is a family of refresh tokens: each refresh consumes the current token and issues the
next one in the same family, so a replayed token can revoke the whole session.
"""

from uuid import UUID, uuid4

from woassi.modules.accounts.application.dto import TokenPair
from woassi.modules.accounts.application.limits import (
    LOGIN_LIMIT,
    LOGIN_WINDOW_S,
    enforce,
    login_ip_key,
    login_phone_key,
)
from woassi.modules.accounts.application.ports import (
    AccountsUnitOfWork,
    InvalidAccessToken,
    PasswordHasher,
    TokenService,
)
from woassi.modules.accounts.application.use_cases.support import issue_tokens, parse_phone
from woassi.modules.accounts.domain.errors import InvalidCredentials, InvalidRefreshToken
from woassi.shared.application.ports import Clock, PhoneNormalizer, RateLimiter
from woassi.shared.domain.errors import Unauthorized


class Login:
    """Exchanges a phone number and password for a new session (access + refresh token).

    Failed attempts are rate limited per IP and per phone number. Failures are neutral: an
    unknown phone and a wrong password give the same error and take the same time.
    """

    def __init__(
        self,
        uow: AccountsUnitOfWork,
        hasher: PasswordHasher,
        tokens: TokenService,
        limiter: RateLimiter,
        phones: PhoneNormalizer,
        clock: Clock,
        *,
        refresh_ttl_s: int,
    ) -> None:
        self._uow = uow
        self._hasher = hasher
        self._tokens = tokens
        self._limiter = limiter
        self._phones = phones
        self._clock = clock
        self._refresh_ttl_s = refresh_ttl_s

    async def execute(self, *, phone: str, password: str, ip: str) -> TokenPair:
        """Logs the account in and returns the token pair of a brand new session.

        Raises:
            InvalidCredentials: the phone is unknown or the password is wrong.
            RateLimited: too many failed attempts from this IP or for this phone.
        """
        normalized = parse_phone(self._phones, phone)
        # An unparsable phone still gets its own counter, keyed on the raw input.
        phone_key = login_phone_key(normalized.value if normalized else phone)
        ip_key = login_ip_key(ip)
        # `hit` is atomic, so the attempt is counted before the password is even checked.
        await enforce(self._limiter, ip_key, LOGIN_LIMIT, LOGIN_WINDOW_S)
        await enforce(self._limiter, phone_key, LOGIN_LIMIT, LOGIN_WINDOW_S)

        async with self._uow as uow:
            account = await uow.accounts.get_by_phone(normalized) if normalized else None
            if account is None:
                # Hash anyway, so the response time does not reveal whether the phone exists.
                await self._hasher.dummy_verify(password)
                raise InvalidCredentials("Invalid credentials.")
            if not await self._hasher.verify(password, account.password_hash):
                raise InvalidCredentials("Invalid credentials.")

            account.record_login(self._clock.now())
            await uow.accounts.save(account)
            # Each login starts a new token family, i.e. a new session.
            pair = await issue_tokens(
                uow,
                self._tokens,
                self._clock,
                account_id=account.id,
                family_id=uuid4(),
                refresh_ttl_s=self._refresh_ttl_s,
            )
            await uow.commit()

        # Only failures count: many users can share one IP behind a mobile carrier's NAT.
        await self._limiter.forgive(ip_key)
        await self._limiter.forgive(phone_key)
        return pair


class RefreshTokens:
    """Rotates a refresh token: consumes it and issues a new pair in the same session.

    Refresh tokens are single use. Presenting one that was already consumed means it leaked,
    so the whole session (token family) is revoked.
    """

    def __init__(
        self,
        uow: AccountsUnitOfWork,
        tokens: TokenService,
        clock: Clock,
        *,
        refresh_ttl_s: int,
    ) -> None:
        self._uow = uow
        self._tokens = tokens
        self._clock = clock
        self._refresh_ttl_s = refresh_ttl_s

    async def execute(self, refresh_token: str) -> TokenPair:
        """Returns a new token pair for the session of `refresh_token`.

        Raises:
            InvalidRefreshToken: the token is unknown, expired, revoked, already used, or its
                account is gone or inactive. Every case gives the same error.
        """
        now = self._clock.now()
        # Only the SHA-256 of a refresh token is stored, so look it up by hash.
        token_hash = self._tokens.hash_refresh_token(refresh_token)
        async with self._uow as uow:
            stored = await uow.refresh_tokens.get_by_hash(token_hash)
            if stored is None or not stored.is_usable(now):
                raise InvalidRefreshToken("Invalid refresh token.")

            # `consume` is one atomic UPDATE: of two concurrent refreshes, only one wins.
            if not await uow.refresh_tokens.consume(stored.id, now):
                # Already used, so one of the two holders is a thief: end the whole session.
                # Committed before raising, otherwise the revocation would be rolled back.
                await uow.refresh_tokens.revoke_family(stored.family_id, now)
                await uow.commit()
                raise InvalidRefreshToken("Invalid refresh token.")

            account = await uow.accounts.get(stored.account_id)
            if account is None or not account.is_active:
                # The account can no longer log in: close the session for good.
                await uow.refresh_tokens.revoke_family(stored.family_id, now)
                await uow.commit()
                raise InvalidRefreshToken("Invalid refresh token.")

            # Same family: the new token continues the existing session.
            pair = await issue_tokens(
                uow,
                self._tokens,
                self._clock,
                account_id=account.id,
                family_id=stored.family_id,
                refresh_ttl_s=self._refresh_ttl_s,
            )
            await uow.commit()
        return pair


class Logout:
    """Ends the current session by revoking its refresh token.

    The access token cannot be revoked: it stays valid until it expires (15 minutes at most).
    """

    def __init__(self, uow: AccountsUnitOfWork, tokens: TokenService, clock: Clock) -> None:
        self._uow = uow
        self._tokens = tokens
        self._clock = clock

    async def execute(self, refresh_token: str) -> None:
        """Does nothing for an unknown token: the caller is logged out either way."""
        async with self._uow as uow:
            stored = await uow.refresh_tokens.get_by_hash(
                self._tokens.hash_refresh_token(refresh_token)
            )
            if stored is not None:
                await uow.refresh_tokens.revoke(stored.id, self._clock.now())
                await uow.commit()


class LogoutEverywhere:
    """Ends every session of an account by revoking all its refresh tokens.

    Password change and reset do the same in their own transaction (see `password.py`).
    """

    def __init__(self, uow: AccountsUnitOfWork, clock: Clock) -> None:
        self._uow = uow
        self._clock = clock

    async def execute(self, account_id: UUID) -> None:
        """Revokes every refresh token of `account_id`, on every device."""
        async with self._uow as uow:
            await uow.refresh_tokens.revoke_all(account_id, self._clock.now())
            await uow.commit()


class AuthenticateAccount:
    """Resolves an access token to the id of an active account.

    Backs the `CurrentAccount` API dependency (through the `AccountAuthenticator` port).
    """

    def __init__(self, uow: AccountsUnitOfWork, tokens: TokenService) -> None:
        self._uow = uow
        self._tokens = tokens

    async def execute(self, access_token: str) -> UUID:
        """Returns the account id carried by `access_token`.

        Raises:
            Unauthorized: the token is invalid or expired, or its account is gone or inactive.
        """
        try:
            # Signature and expiry are checked without touching the database.
            account_id = self._tokens.verify_access_token(access_token)
        except InvalidAccessToken as error:
            raise Unauthorized("Invalid or expired access token.") from error
        # The token alone is not enough: a deactivated account must lose access right away.
        async with self._uow as uow:
            account = await uow.accounts.get(account_id)
        if account is None or not account.is_active:
            raise Unauthorized("Invalid or expired access token.")
        return account.id
