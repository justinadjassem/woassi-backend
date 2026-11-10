"""Domain events of the shops module."""

from dataclasses import dataclass
from uuid import UUID

from woassi.shared.domain.events import DomainEvent


@dataclass(frozen=True, kw_only=True)
class ShopCreated(DomainEvent):
    """A merchant created a shop."""

    shop_id: UUID
    account_id: UUID


@dataclass(frozen=True, kw_only=True)
class WhatsAppConnected(DomainEvent):
    """A shop's number is connected; messaging submits its templates."""

    shop_id: UUID
    phone_number_id: str


@dataclass(frozen=True, kw_only=True)
class ReceivingAccountChanged(DomainEvent):
    """The account receiving the shop's payments was set, changed or removed."""

    shop_id: UUID
