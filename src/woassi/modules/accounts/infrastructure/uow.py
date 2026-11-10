"""Unit of Work of the accounts module."""

from woassi.modules.accounts.application.ports import (
    AccountRepository,
    RefreshTokenStore,
    VerificationCodeStore,
)
from woassi.modules.accounts.infrastructure.repository import (
    SqlAlchemyAccountRepository,
    SqlAlchemyRefreshTokenStore,
    SqlAlchemyVerificationCodeStore,
)
from woassi.shared.infrastructure.db.uow import SqlAlchemyUnitOfWork


class SqlAlchemyAccountsUoW(SqlAlchemyUnitOfWork):
    """Gives the accounts repositories, all on this Unit of Work's session."""

    @property
    def accounts(self) -> AccountRepository:
        return SqlAlchemyAccountRepository(self._active_session(), self._track)

    @property
    def refresh_tokens(self) -> RefreshTokenStore:
        return SqlAlchemyRefreshTokenStore(self._active_session())

    @property
    def verification_codes(self) -> VerificationCodeStore:
        return SqlAlchemyVerificationCodeStore(self._active_session())
