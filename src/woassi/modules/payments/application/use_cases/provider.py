"""A shop's KPrimePay account: Woassi stores its API key, encrypted, and never shows it again.

The key is checked with a read-only KPrimePay call, outside any transaction.
"""

import secrets
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import structlog

from woassi.modules.payments.application.ports import (
    PaymentGateway,
    PaymentsUnitOfWork,
    ProviderError,
    ProviderFailure,
)
from woassi.modules.payments.application.use_cases.flow import token_hash
from woassi.modules.payments.domain.errors import InvalidProviderKey, ProviderUnavailable
from woassi.modules.payments.domain.provider_account import ProviderAccount, ProviderMode, mode_of
from woassi.shared.application.ports import Clock, SecretCipher

log = structlog.get_logger()

TARGET_TYPE = "payment_provider"


@dataclass(frozen=True, slots=True)
class ProviderAccountView:
    """What the dashboard may see: never the key, nor the callback token."""

    configured: bool
    provider: str = "kprimepay"
    mode: ProviderMode | None = None
    updated_at: datetime | None = None


def _view(account: ProviderAccount | None) -> ProviderAccountView:
    if account is None:
        return ProviderAccountView(configured=False)
    return ProviderAccountView(configured=True, mode=account.mode, updated_at=account.updated_at)


class ConfigurePaymentProvider:
    """Sets or replaces the shop's KPrimePay key."""

    def __init__(
        self,
        uow: PaymentsUnitOfWork,
        gateway: PaymentGateway,
        cipher: SecretCipher,
        clock: Clock,
    ) -> None:
        self._uow = uow
        self._gateway = gateway
        self._cipher = cipher
        self._clock = clock

    async def execute(
        self, *, shop_id: UUID, account_id: UUID, api_key: str, ip: str | None = None
    ) -> ProviderAccountView:
        """Raises InvalidProviderKey (malformed or refused) or ProviderUnavailable."""
        api_key = api_key.strip()
        mode = mode_of(api_key)
        try:
            await self._gateway.verify_key(api_key)
        except ProviderError as error:
            log.warning(
                "payment_provider_check_failed",
                shop_id=str(shop_id),
                failure=error.failure.name,
                provider_code=error.code,
            )
            if error.failure is ProviderFailure.UNAVAILABLE:
                raise ProviderUnavailable(
                    "KPrimePay is not responding right now. Please try again in a few minutes."
                ) from None
            raise InvalidProviderKey(
                "KPrimePay refused this key. Check that it is active and has the read and "
                "payments:write scopes."
            ) from None

        now = self._clock.now()
        async with self._uow as uow:
            previous = await uow.provider_accounts.get(shop_id)
            if previous is None:
                token = secrets.token_urlsafe(32)
                encrypted_token, digest, created_at = (
                    self._cipher.encrypt(token),
                    token_hash(token),
                    now,
                )
            else:
                # Same callback URL: checkouts already sent keep reaching us.
                encrypted_token = previous.encrypted_callback_token
                digest, created_at = previous.callback_token_hash, previous.created_at
            account = ProviderAccount(
                shop_id=shop_id,
                mode=mode,
                encrypted_api_key=self._cipher.encrypt(api_key),
                encrypted_callback_token=encrypted_token,
                callback_token_hash=digest,
                created_at=created_at,
                updated_at=now,
            )
            await uow.provider_accounts.save(account)
            await uow.audit.record(
                account_id=account_id,
                action="payment_provider.updated",
                target_type=TARGET_TYPE,
                target_id=str(shop_id),
                shop_id=shop_id,
                details={"provider": "kprimepay", "mode": mode.value},
                ip=ip,
            )
            await uow.commit()
        return _view(account)


class GetPaymentProvider:
    """Whether the shop has a KPrimePay account, and in which mode."""

    def __init__(self, uow: PaymentsUnitOfWork) -> None:
        self._uow = uow

    async def execute(self, shop_id: UUID) -> ProviderAccountView:
        async with self._uow as uow:
            return _view(await uow.provider_accounts.get(shop_id))


class RemovePaymentProvider:
    """Forgets the shop's key. Pending checkouts are no longer followed and will expire."""

    def __init__(self, uow: PaymentsUnitOfWork) -> None:
        self._uow = uow

    async def execute(self, *, shop_id: UUID, account_id: UUID, ip: str | None = None) -> None:
        """Does nothing if there is no account."""
        async with self._uow as uow:
            if await uow.provider_accounts.get(shop_id) is None:
                return
            await uow.provider_accounts.delete(shop_id)
            await uow.audit.record(
                account_id=account_id,
                action="payment_provider.removed",
                target_type=TARGET_TYPE,
                target_id=str(shop_id),
                shop_id=shop_id,
                details={"provider": "kprimepay"},
                ip=ip,
            )
            await uow.commit()
