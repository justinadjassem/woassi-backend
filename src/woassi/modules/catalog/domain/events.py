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


@dataclass(frozen=True, kw_only=True)
class ProductMetaSyncRequested(DomainEvent):
    """Something Meta's catalog shows changed: name, price, photos, availability...

    Carries ids only, no personal data. The subscriber reads the product's current state, so
    events arriving late or out of order are harmless.
    """

    product_id: UUID
    shop_id: UUID
