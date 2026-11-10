"""SQLAlchemy implementations of the accounts repositories."""

from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from woassi.modules.accounts.domain.account import Account, AccountStatus
from woassi.modules.accounts.domain.errors import PhoneAlreadyUsed
from woassi.modules.accounts.domain.refresh_token import RefreshToken
from woassi.modules.accounts.domain.verification_code import CodePurpose, VerificationCode
from woassi.modules.accounts.infrastructure.models import (
    AccountModel,
    RefreshTokenModel,
    VerificationCodeModel,
)
from woassi.shared.domain.events import Entity
from woassi.shared.domain.values import PhoneNumber


def _account_to_domain(model: AccountModel) -> Account:
    """Converts a row to a domain `Account`."""
    return Account(
        id=model.id,
        full_name=model.full_name,
        phone=PhoneNumber(model.phone),
        password_hash=model.password_hash,
        created_at=model.created_at,
        email=model.email,
        status=AccountStatus(model.status),
        phone_verified=model.phone_verified,
        last_login_at=model.last_login_at,
    )


def _copy_account(account: Account, model: AccountModel) -> AccountModel:
    """Copies every field of `account` onto `model`, and returns `model`."""
    model.id = account.id
    model.full_name = account.full_name
    model.phone = account.phone.value
    model.email = account.email
    model.password_hash = account.password_hash
    model.status = account.status.value
    model.phone_verified = account.phone_verified
    model.created_at = account.created_at
    model.last_login_at = account.last_login_at
    return model


class SqlAlchemyAccountRepository:
    """`AccountRepository` on SQLAlchemy. Saved accounts are tracked for their events."""

    def __init__(self, session: AsyncSession, track: Callable[[Entity], None]) -> None:
        self._session = session
        self._track = track

    async def add(self, account: Account) -> None:
        """Inserts the account; raises PhoneAlreadyUsed on the phone's unique constraint."""
        self._session.add(_copy_account(account, AccountModel()))
        try:
            await self._session.flush()
        except IntegrityError as error:
            if "uq_accounts_phone" in str(error.orig):
                raise PhoneAlreadyUsed("This phone number is already registered.") from error
            raise
        self._track(account)

    async def get(self, account_id: UUID) -> Account | None:
        model = await self._session.get(AccountModel, account_id)
        return _account_to_domain(model) if model else None

    async def get_by_phone(self, phone: PhoneNumber) -> Account | None:
        model = await self._session.scalar(
            select(AccountModel).where(AccountModel.phone == phone.value)
        )
        return _account_to_domain(model) if model else None

    async def save(self, account: Account) -> None:
        """Updates an existing account; raises LookupError if it does not exist."""
        model = await self._session.get(AccountModel, account.id)
        if model is None:
            raise LookupError(f"Account {account.id} does not exist.")
        _copy_account(account, model)
        await self._session.flush()
        self._track(account)


class SqlAlchemyRefreshTokenStore:
    """`RefreshTokenStore` on SQLAlchemy."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, token: RefreshToken) -> None:
        self._session.add(
            RefreshTokenModel(
                id=token.id,
                account_id=token.account_id,
                family_id=token.family_id,
                token_hash=token.token_hash,
                expires_at=token.expires_at,
            )
        )
        await self._session.flush()

    async def get_by_hash(self, token_hash: str) -> RefreshToken | None:
        model = await self._session.scalar(
            select(RefreshTokenModel).where(RefreshTokenModel.token_hash == token_hash)
        )
        if model is None:
            return None
        return RefreshToken(
            id=model.id,
            account_id=model.account_id,
            family_id=model.family_id,
            token_hash=model.token_hash,
            expires_at=model.expires_at,
            consumed_at=model.consumed_at,
            revoked_at=model.revoked_at,
        )

    async def consume(self, token_id: UUID, now: datetime) -> bool:
        # A single UPDATE: of two concurrent requests, the second one blocks, then matches nothing.
        result = await self._session.execute(
            update(RefreshTokenModel)
            .where(
                RefreshTokenModel.id == token_id,
                RefreshTokenModel.consumed_at.is_(None),
                RefreshTokenModel.revoked_at.is_(None),
            )
            .values(consumed_at=now)
            .returning(RefreshTokenModel.id)
        )
        return result.first() is not None

    async def revoke(self, token_id: UUID, now: datetime) -> None:
        await self._revoke(now, RefreshTokenModel.id == token_id)

    async def revoke_family(self, family_id: UUID, now: datetime) -> None:
        await self._revoke(now, RefreshTokenModel.family_id == family_id)

    async def revoke_all(self, account_id: UUID, now: datetime) -> None:
        await self._revoke(now, RefreshTokenModel.account_id == account_id)

    async def _revoke(self, now: datetime, condition: object) -> None:
        """Revokes every token matching `condition` that is not revoked yet."""
        await self._session.execute(
            update(RefreshTokenModel)
            .where(condition, RefreshTokenModel.revoked_at.is_(None))  # type: ignore[arg-type]
            .values(revoked_at=now)
        )


def _code_to_domain(model: VerificationCodeModel) -> VerificationCode:
    """Converts a row to a domain `VerificationCode`."""
    return VerificationCode(
        id=model.id,
        account_id=model.account_id,
        purpose=CodePurpose(model.purpose),
        code_hash=model.code_hash,
        expires_at=model.expires_at,
        created_at=model.created_at,
        attempts=model.attempts,
        consumed_at=model.consumed_at,
    )


class SqlAlchemyVerificationCodeStore:
    """`VerificationCodeStore` on SQLAlchemy."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, code: VerificationCode) -> None:
        self._session.add(
            VerificationCodeModel(
                id=code.id,
                account_id=code.account_id,
                purpose=code.purpose.value,
                code_hash=code.code_hash,
                expires_at=code.expires_at,
                attempts=code.attempts,
                created_at=code.created_at,
                consumed_at=code.consumed_at,
            )
        )
        await self._session.flush()

    async def get_latest(self, account_id: UUID, purpose: CodePurpose) -> VerificationCode | None:
        """Latest code of this purpose, locked (`FOR UPDATE`) until the transaction ends."""
        model = await self._session.scalar(
            select(VerificationCodeModel)
            .where(
                VerificationCodeModel.account_id == account_id,
                VerificationCodeModel.purpose == purpose.value,
            )
            .order_by(VerificationCodeModel.created_at.desc(), VerificationCodeModel.id)
            .limit(1)
            .with_for_update()
        )
        return _code_to_domain(model) if model else None

    async def save(self, code: VerificationCode) -> None:
        await self._session.execute(
            update(VerificationCodeModel)
            .where(VerificationCodeModel.id == code.id)
            .values(attempts=code.attempts, consumed_at=code.consumed_at)
        )

    async def invalidate(self, account_id: UUID, purpose: CodePurpose, now: datetime) -> None:
        await self._session.execute(
            update(VerificationCodeModel)
            .where(
                VerificationCodeModel.account_id == account_id,
                VerificationCodeModel.purpose == purpose.value,
                VerificationCodeModel.consumed_at.is_(None),
            )
            .values(consumed_at=now)
        )
