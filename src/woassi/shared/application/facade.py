"""Use cases the shared api needs, provided by the composition root (`Container`)."""

from typing import Protocol

from woassi.shared.application.audit import ListAuditEntries
from woassi.shared.application.ports import (
    AccountAuthenticator,
    IdempotencyStore,
    ShopOwnershipPort,
)
from woassi.shared.application.readiness import CheckReadiness


class SharedUseCases(Protocol):
    """What the shared api expects from the composition root."""

    def list_audit_entries(self) -> ListAuditEntries: ...

    def check_readiness(self) -> CheckReadiness: ...

    def idempotency_store(self) -> IdempotencyStore: ...

    def account_authenticator(self) -> AccountAuthenticator: ...

    def shop_ownership(self) -> ShopOwnershipPort: ...
