import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.fakes import NOW
from tests.orders_fakes import FakeShops, RecordingMessaging, profile
from woassi.integration.catalog import CatalogStock
from woassi.integration.customers import CustomerContacts
from woassi.modules.catalog.application.use_cases.stock import ReleaseStock, ReserveStock
from woassi.modules.catalog.domain.product import Product
from woassi.modules.catalog.infrastructure.uow import SqlAlchemyCatalogUoW
from woassi.modules.customers.application.use_cases.customers import GetCustomer, RecordContact
from woassi.modules.customers.infrastructure.uow import SqlAlchemyCustomersUoW
from woassi.modules.orders.application.use_cases.orders import (
    Cart,
    OrderFlow,
    ProcessDueOrders,
    ReceiveCart,
    ReplyToOrder,
)
from woassi.modules.orders.domain.order import OrderStatus
from woassi.modules.orders.infrastructure.uow import SqlAlchemyOrdersUoW
from woassi.shared.domain.events import DomainEvent
from woassi.shared.domain.values import Amount, PhoneNumber
from woassi.shared.infrastructure.clock import FrozenClock

pytestmark = pytest.mark.integration


class NullBus:
    async def publish(self, event: DomainEvent) -> None:
        return None


class Real:
    """Postgres for the catalog, customers and orders; fakes for the shop and WhatsApp."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions
        self.clock = FrozenClock(datetime(2026, 1, 1, 9, tzinfo=UTC))
        self.shop_id = uuid4()
        self.shops = FakeShops(profile(self.shop_id))
        self.messaging = RecordingMessaging()

    def catalog_uow(self) -> SqlAlchemyCatalogUoW:
        return SqlAlchemyCatalogUoW(self.sessions, NullBus(), self.clock)

    def customers_uow(self) -> SqlAlchemyCustomersUoW:
        return SqlAlchemyCustomersUoW(self.sessions, NullBus(), self.clock)

    def stock(self) -> CatalogStock:
        return CatalogStock(
            ReserveStock(self.catalog_uow(), self.clock),
            ReleaseStock(self.catalog_uow(), self.clock),
        )

    def flow[F: OrderFlow](self, use_case: type[F]) -> F:
        return use_case(
            SqlAlchemyOrdersUoW(self.sessions, NullBus(), self.clock),
            self.shops,
            self.stock(),
            CustomerContacts(GetCustomer(self.customers_uow())),
            self.messaging,
            self.clock,
        )

    async def product(self, stock: int, retailer_id: str = "SKU-1") -> Product:
        product = Product.create(
            now=NOW,
            shop_id=self.shop_id,
            name="Pagne",
            price=Amount(5000),
            stock=stock,
            retailer_id=retailer_id,
        )
        async with self.catalog_uow() as uow:
            await uow.products.add(product)
            await uow.commit()
        return product

    async def stock_of(self, product: Product) -> int:
        async with self.catalog_uow() as uow:
            current = await uow.products.get(self.shop_id, product.id)
        assert current is not None
        return current.stock

    async def customer(self) -> UUID:
        return await RecordContact(self.customers_uow()).execute(
            shop_id=self.shop_id,
            user_id="TG.1",
            phone=PhoneNumber("+22891234567"),
            name="Ama",
            at=self.clock.now(),
        )

    async def cart(self, customer_id: UUID, quantity: int, message_id: str) -> UUID | None:
        return await self.flow(ReceiveCart).execute(
            shop_id=self.shop_id,
            cart=Cart(message_id=message_id, customer_id=customer_id, items=(("SKU-1", quantity),)),
        )


@pytest.fixture
def real(sessions: async_sessionmaker[AsyncSession]) -> Real:
    return Real(sessions)


async def test_the_last_item_is_sold_once(real: Real) -> None:
    """Ten customers want the last pagne at the same moment."""
    product = await real.product(stock=1)
    reserve = [ReserveStock(real.catalog_uow(), real.clock) for _ in range(10)]

    results = await asyncio.gather(
        *(use_case.execute(shop_id=real.shop_id, requested={"SKU-1": 1}) for use_case in reserve)
    )

    assert sorted(items[0].reserved for items in results) == [0] * 9 + [1]
    assert await real.stock_of(product) == 0


async def test_concurrent_reservations_never_go_below_zero(real: Real) -> None:
    product = await real.product(stock=7)
    results = await asyncio.gather(
        *(
            ReserveStock(real.catalog_uow(), real.clock).execute(
                shop_id=real.shop_id, requested={"SKU-1": 2}
            )
            for _ in range(6)
        )
    )
    assert sum(items[0].reserved for items in results) == 7
    assert await real.stock_of(product) == 0


async def test_order_numbers_never_repeat(real: Real) -> None:
    await real.product(stock=100)
    customer = await real.customer()

    ids = await asyncio.gather(*(real.cart(customer, 1, f"wamid.{n}") for n in range(15)))

    async with SqlAlchemyOrdersUoW(real.sessions, NullBus(), real.clock) as uow:
        orders = [await uow.orders.get(real.shop_id, order_id) for order_id in ids if order_id]
    assert sorted(o.number for o in orders if o) == list(range(1, 16))


async def test_a_cart_replayed_concurrently_gives_one_order_and_one_reservation(real: Real) -> None:
    product = await real.product(stock=10)
    customer = await real.customer()

    ids = await asyncio.gather(*(real.cart(customer, 2, "wamid.SAME") for _ in range(5)))

    assert len(set(ids)) == 1
    assert await real.stock_of(product) == 8


async def test_whole_flow_on_postgres(real: Real) -> None:
    product = await real.product(stock=5)
    customer = await real.customer()

    order_id = await real.cart(customer, 2, "wamid.CART")
    assert order_id is not None
    [(template, message_id)] = real.messaging.to_merchant()
    assert template.parameters["customer_name"] == "Ama"

    accepted = await real.flow(ReplyToOrder).execute(
        shop_id=real.shop_id, payload=template.button_payloads[0], context_message_id=message_id
    )

    assert accepted
    async with SqlAlchemyOrdersUoW(real.sessions, NullBus(), real.clock) as uow:
        order = await uow.orders.get(real.shop_id, order_id)
        [entry] = await uow.audit.list(real.shop_id, 0, 10)
    assert order is not None and order.status is OrderStatus.CONFIRMED
    assert entry.action == "order.validated"
    assert await real.stock_of(product) == 3


async def test_due_orders_are_found_by_the_scheduler(real: Real) -> None:
    product = await real.product(stock=5)
    customer = await real.customer()
    order_id = await real.cart(customer, 2, "wamid.CART")

    real.clock.advance(timedelta(minutes=15))
    assert await real.flow(ProcessDueOrders).execute() == 1
    assert await real.flow(ProcessDueOrders).execute() == 0

    real.clock.advance(timedelta(hours=24))
    assert await real.flow(ProcessDueOrders).execute() == 1
    async with SqlAlchemyOrdersUoW(real.sessions, NullBus(), real.clock) as uow:
        order = await uow.orders.get(real.shop_id, order_id)  # type: ignore[arg-type]
    assert order is not None and order.status is OrderStatus.EXPIRED
    assert await real.stock_of(product) == 5


async def test_a_customer_is_found_again_by_phone_or_user_id(real: Real) -> None:
    record = RecordContact(real.customers_uow())
    first = await record.execute(
        shop_id=real.shop_id,
        user_id=None,
        phone=PhoneNumber("+22891234567"),
        name=None,
        at=real.clock.now(),
    )
    second = await record.execute(
        shop_id=real.shop_id,
        user_id="TG.1",
        phone=PhoneNumber("+22891234567"),
        name="Ama",
        at=real.clock.now(),
    )
    third = await record.execute(
        shop_id=real.shop_id, user_id="TG.1", phone=None, name=None, at=real.clock.now()
    )
    assert first == second == third
    customer = await GetCustomer(real.customers_uow()).execute(
        shop_id=real.shop_id, customer_id=first
    )
    assert (customer.user_id, customer.phone, customer.name) == ("TG.1", "+22891234567", "Ama")
