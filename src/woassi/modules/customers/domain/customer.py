"""Customers: the WhatsApp users who write to a shop."""

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID, uuid4

from woassi.modules.customers.domain.errors import InvalidCustomer
from woassi.shared.domain.events import Entity
from woassi.shared.domain.values import PhoneNumber


@dataclass(slots=True)
class Customer(Entity):
    """A WhatsApp user who wrote to a shop.

    `user_id` is Meta's business-scoped user id (BSUID). The phone number is optional: Meta
    leaves it out for users with a WhatsApp username.
    """

    id: UUID
    shop_id: UUID
    created_at: datetime
    user_id: str | None = field(default=None, repr=False)
    phone: PhoneNumber | None = field(default=None, repr=False)
    name: str | None = field(default=None, repr=False)
    last_message_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.user_id and self.phone is None:
            raise InvalidCustomer("A customer needs a user id or a phone number.")

    @classmethod
    def first_contact(
        cls,
        *,
        shop_id: UUID,
        user_id: str | None,
        phone: PhoneNumber | None,
        name: str | None,
        now: datetime,
    ) -> "Customer":
        """A new customer, first seen at `now`."""
        return cls(
            id=uuid4(),
            shop_id=shop_id,
            created_at=now,
            user_id=user_id,
            phone=phone,
            name=name,
            last_message_at=now,
        )

    def wrote(
        self, *, user_id: str | None, phone: PhoneNumber | None, name: str | None, now: datetime
    ) -> None:
        """Fill in what Meta sends, without forgetting what it leaves out this time."""
        self.user_id = user_id or self.user_id
        self.phone = phone or self.phone
        self.name = name or self.name
        if self.last_message_at is None or now > self.last_message_at:
            self.last_message_at = now
