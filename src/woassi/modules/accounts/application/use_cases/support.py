"""Steps shared by several use cases."""

from datetime import timedelta
from uuid import UUID, uuid4

from woassi.modules.accounts.application.dto import TokenPair
from woassi.modules.accounts.application.ports import (
    AccountsUnitOfWork,
    PasswordHasher,
    TokenService,
)
from woassi.modules.accounts.domain.errors import InvalidCode
from woassi.modules.accounts.domain.refresh_token import RefreshToken
from woassi.modules.accounts.domain.verification_code import (
    CodePurpose,
    VerificationCode,
    generate_code,
)
from woassi.shared.application.ports import Clock, PhoneNormalizer
from woassi.shared.domain.errors import InvalidData
from woassi.shared.domain.values import PhoneNumber


def parse_phone(phones: PhoneNormalizer, raw: str) -> PhoneNumber | None:
    """For neutral flows: an unusable number is treated like an unknown one."""
    try:
        return phones.normalize(raw)
    except InvalidData:
        return None


async def issue_tokens(
    uow: AccountsUnitOfWork,
    tokens: TokenService,
    clock: Clock,
    *,
    account_id: UUID,
    family_id: UUID,
    refresh_ttl_s: int,
) -> TokenPair:
    """Creates a token pair: a new access token, and a refresh token stored in `family_id`.

    The caller commits. Pass a new family id for a login, the current one for a rotation.
    """
    access = tokens.issue_access_token(account_id)
    refresh = tokens.new_refresh_token()
    await uow.refresh_tokens.add(
        RefreshToken(
            id=uuid4(),
            account_id=account_id,
            family_id=family_id,
            token_hash=tokens.hash_refresh_token(refresh),
            expires_at=clock.now() + timedelta(seconds=refresh_ttl_s),
        )
    )
    return TokenPair(
        access_token=access.token,
        expires_in=access.expires_in,
        refresh_token=refresh,
        refresh_expires_in=refresh_ttl_s,
    )


async def issue_code(
    uow: AccountsUnitOfWork,
    hasher: PasswordHasher,
    clock: Clock,
    *,
    account_id: UUID,
    purpose: CodePurpose,
) -> str:
    """Replace any previous code. The caller sends it once its transaction is committed."""
    now = clock.now()
    code = generate_code()
    await uow.verification_codes.invalidate(account_id, purpose, now)
    await uow.verification_codes.add(
        VerificationCode.issue(
            account_id=account_id,
            purpose=purpose,
            code_hash=await hasher.hash(code),
            now=now,
        )
    )
    return code


async def consume_code(
    uow: AccountsUnitOfWork,
    hasher: PasswordHasher,
    clock: Clock,
    *,
    account_id: UUID,
    purpose: CodePurpose,
    code: str,
) -> None:
    """Raise InvalidCode otherwise. A failed attempt is committed, or it would not count."""
    now = clock.now()
    stored = await uow.verification_codes.get_latest(account_id, purpose)
    if stored is None or not stored.is_usable(now):
        await hasher.dummy_verify(code)
        raise InvalidCode("Invalid or expired code.")
    if not await hasher.verify(code, stored.code_hash):
        stored.record_failure()
        await uow.verification_codes.save(stored)
        await uow.commit()
        raise InvalidCode("Invalid or expired code.")
    stored.consume(now)
    await uow.verification_codes.save(stored)
