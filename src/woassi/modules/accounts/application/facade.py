"""Use cases the accounts api needs, provided by the composition root (`Container`)."""

from typing import Protocol

from woassi.modules.accounts.application.use_cases.password import (
    ChangePassword,
    RequestPasswordReset,
    ResetPassword,
)
from woassi.modules.accounts.application.use_cases.profile import GetMyAccount, UpdateMyAccount
from woassi.modules.accounts.application.use_cases.registration import Register, VerifyPhone
from woassi.modules.accounts.application.use_cases.session import (
    AuthenticateAccount,
    Login,
    Logout,
    LogoutEverywhere,
    RefreshTokens,
)


class AccountsUseCases(Protocol):
    """What this module expects from the composition root (see catalog.application.facade)."""

    def register(self) -> Register: ...

    def verify_phone(self) -> VerifyPhone: ...

    def login(self) -> Login: ...

    def refresh_tokens(self) -> RefreshTokens: ...

    def logout(self) -> Logout: ...

    def logout_everywhere(self) -> LogoutEverywhere: ...

    def request_password_reset(self) -> RequestPasswordReset: ...

    def reset_password(self) -> ResetPassword: ...

    def get_my_account(self) -> GetMyAccount: ...

    def update_my_account(self) -> UpdateMyAccount: ...

    def change_password(self) -> ChangePassword: ...

    def authenticate_account(self) -> AuthenticateAccount: ...
