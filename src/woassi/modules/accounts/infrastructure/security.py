"""Password hashing (Argon2) and tokens (JWT access tokens, random refresh tokens)."""

import hashlib
import secrets
from functools import partial
from uuid import UUID, uuid4

import anyio.to_thread
import jwt
from pwdlib import PasswordHash
from pwdlib.exceptions import PwdlibError
from pwdlib.hashers.argon2 import Argon2Hasher

from woassi.modules.accounts.application.ports import AccessToken, InvalidAccessToken
from woassi.shared.application.ports import Clock

ALGORITHM = "HS256"
ACCESS_TYPE = "access"


class Argon2PasswordHasher:
    """Argon2 is slow on purpose: it runs in a thread so that it does not block the event loop."""

    def __init__(self) -> None:
        self._context = PasswordHash((Argon2Hasher(),))
        self._dummy_hash = self._context.hash(secrets.token_urlsafe())

    async def hash(self, password: str) -> str:
        """Argon2 hash of `password`, with a random salt."""
        return await anyio.to_thread.run_sync(self._context.hash, password)

    async def verify(self, password: str, password_hash: str) -> bool:
        """True if `password` matches; False for a wrong password or a malformed hash."""
        try:
            return await anyio.to_thread.run_sync(
                partial(self._context.verify, password, password_hash)
            )
        except PwdlibError:
            return False

    async def dummy_verify(self, password: str) -> None:
        """Verifies against a random hash, only to spend the same time as `verify`."""
        await self.verify(password, self._dummy_hash)


class JwtTokenService:
    """Access tokens are HS256 JWTs; refresh tokens are opaque random strings."""

    def __init__(self, secret: str, access_ttl_s: int, clock: Clock) -> None:
        self._secret = secret
        self._access_ttl_s = access_ttl_s
        self._clock = clock

    def issue_access_token(self, account_id: UUID) -> AccessToken:
        """JWT for `account_id`, valid `access_ttl_s` seconds from the clock's now."""
        issued_at = int(self._clock.now().timestamp())
        token = jwt.encode(
            {
                "sub": str(account_id),
                "type": ACCESS_TYPE,
                "jti": str(uuid4()),
                "iat": issued_at,
                "exp": issued_at + self._access_ttl_s,
            },
            self._secret,
            algorithm=ALGORITHM,
        )
        return AccessToken(token=token, expires_in=self._access_ttl_s)

    def verify_access_token(self, token: str) -> UUID:
        """Returns the account id; raises InvalidAccessToken for any problem."""
        try:
            # Expiry and issue date are checked below, against the injected clock.
            claims = jwt.decode(
                token,
                self._secret,
                algorithms=[ALGORITHM],
                options={
                    "require": ["sub", "type", "jti", "iat", "exp"],
                    "verify_exp": False,
                    "verify_iat": False,
                },
            )
            if claims["type"] != ACCESS_TYPE:
                raise InvalidAccessToken
            if claims["exp"] <= self._clock.now().timestamp():
                raise InvalidAccessToken
            return UUID(claims["sub"])
        except (jwt.InvalidTokenError, ValueError, TypeError) as error:
            raise InvalidAccessToken from error

    def new_refresh_token(self) -> str:
        """256 random bits, URL-safe."""
        return secrets.token_urlsafe(32)

    def hash_refresh_token(self, token: str) -> str:
        # The token is 256 random bits: unlike a password, a fast hash is safe here.
        """SHA-256 of the token, in hex."""
        return hashlib.sha256(token.encode()).hexdigest()
