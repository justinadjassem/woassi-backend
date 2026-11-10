"""Domain events of the catalog module."""

from dataclasses import dataclass
from uuid import UUID

from woassi.shared.domain.events import DomainEvent


@dataclass(frozen=True, kw_only=True)
class ProductCreated(DomainEvent):
    """A product was added to a shop's catalog."""

    product_id: UUID
    shop_id: UUID
    retailer_id: str
