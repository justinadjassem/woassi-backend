"""Customer routes of a shop (read-only: customers are created by their messages)."""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from woassi.modules.customers.api.deps import CustomersDep
from woassi.modules.customers.application.use_cases.customers import CustomerView
from woassi.shared.api.deps import get_authorized_shop
from woassi.shared.api.pagination import Limit, Offset, Page

router = APIRouter(
    prefix="/shops/{shop_id}/customers",
    tags=["customers"],
    dependencies=[Depends(get_authorized_shop)],
)


class CustomerResponse(BaseModel):
    """A customer as returned by the API. `user_id` is Meta's BSUID."""

    id: UUID
    user_id: str | None
    phone: str | None
    name: str | None
    created_at: datetime
    last_message_at: datetime | None

    @classmethod
    def from_view(cls, view: CustomerView) -> "CustomerResponse":
        """Builds the response from a `CustomerView`."""
        return cls(
            id=view.id,
            user_id=view.user_id,
            phone=view.phone,
            name=view.name,
            created_at=view.created_at,
            last_message_at=view.last_message_at,
        )


@router.get("")
async def list_customers(
    shop_id: UUID, customers: CustomersDep, offset: Offset = 0, limit: Limit = 50
) -> Page[CustomerResponse]:
    """Lists the shop's customers, most recently active first."""
    result = await customers.list_customers().execute(shop_id=shop_id, offset=offset, limit=limit)
    return Page[CustomerResponse].from_result(result, CustomerResponse.from_view)


@router.get("/{customer_id}")
async def get_customer(
    shop_id: UUID, customer_id: UUID, customers: CustomersDep
) -> CustomerResponse:
    """One customer of the shop."""
    view = await customers.get_customer().execute(shop_id=shop_id, customer_id=customer_id)
    return CustomerResponse.from_view(view)
