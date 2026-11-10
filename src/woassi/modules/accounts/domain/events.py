"""Domain events of the accounts module."""

from dataclasses import dataclass
from uuid import UUID

from woassi.shared.domain.events import DomainEvent


@dataclass(frozen=True, kw_only=True)
class AccountCreated(DomainEvent):
    """A merchant registered."""

    account_id: UUID


@dataclass(frozen=True, kw_only=True)
class AccountLoggedIn(DomainEvent):
    """A merchant logged in successfully."""

    account_id: UUID
