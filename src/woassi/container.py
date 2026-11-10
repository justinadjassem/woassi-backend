"""Composition root: the only place where ports are bound to their adapters."""

from dataclasses import dataclass

import httpx
from arq import create_pool
from arq.connections import ArqRedis, RedisSettings
from minio import Minio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from woassi.config import Settings
from woassi.integration.accounts import AccountsAuthenticator
from woassi.integration.catalog import CatalogStock
from woassi.integration.customers import CustomerContacts
from woassi.integration.images import CatalogLogoProcessor
from woassi.integration.messaging import WhatsAppNotifications, template_definitions
from woassi.integration.orders import OrdersInboundHandler
from woassi.integration.shops import (
    ShopsConnections,
    ShopsOrderProfiles,
    ShopsOwnership,
    ShopsWhatsAppAccounts,
)
from woassi.modules.accounts.application.facade import AccountsUseCases
from woassi.modules.accounts.application.ports import (
    AccountsUnitOfWork,
    PasswordHasher,
    TokenService,
    VerificationCodeSender,
)
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
from woassi.modules.accounts.infrastructure.code_sender import LogVerificationCodeSender
from woassi.modules.accounts.infrastructure.security import Argon2PasswordHasher, JwtTokenService
from woassi.modules.accounts.infrastructure.uow import SqlAlchemyAccountsUoW
from woassi.modules.catalog.application.facade import CatalogUseCases
from woassi.modules.catalog.application.ports import (
    CatalogUnitOfWork,
    ImageProcessor,
    PhotoJobQueue,
)
from woassi.modules.catalog.application.use_cases.photos import (
    ConfirmPhoto,
    ProcessPhoto,
    RequestPhotoUpload,
)
from woassi.modules.catalog.application.use_cases.products import (
    CreateProduct,
    GetProduct,
    ListProducts,
)
from woassi.modules.catalog.application.use_cases.stock import ReleaseStock, ReserveStock
from woassi.modules.catalog.infrastructure.image_processor import PillowImageProcessor
from woassi.modules.catalog.infrastructure.photo_queue import ArqPhotoJobQueue
from woassi.modules.catalog.infrastructure.uow import SqlAlchemyCatalogUoW
from woassi.modules.customers.application.facade import CustomersUseCases
from woassi.modules.customers.application.ports import CustomersUnitOfWork
from woassi.modules.customers.application.use_cases.customers import (
    ChangeCustomerUserId,
    GetCustomer,
    ListCustomers,
    RecordContact,
)
from woassi.modules.customers.infrastructure.uow import SqlAlchemyCustomersUoW
from woassi.modules.messaging.application.facade import MessagingUseCases
from woassi.modules.messaging.application.ports import (
    EventQueue,
    InboundMessageHandler,
    MessagingUnitOfWork,
    ShopConnectionsPort,
    WhatsAppAccountsPort,
    WhatsAppSender,
)
from woassi.modules.messaging.application.use_cases.processing import HandleMetaEvent
from woassi.modules.messaging.application.use_cases.sending import SendWhatsAppMessage
from woassi.modules.messaging.application.use_cases.templates import (
    EnsureTemplateSendable,
    ProvisionTemplates,
)
from woassi.modules.messaging.application.use_cases.webhook import (
    ReceiveMetaWebhook,
    VerifyMetaSubscription,
)
from woassi.modules.messaging.infrastructure.event_queue import ArqEventQueue
from woassi.modules.messaging.infrastructure.template_registry import GraphTemplateRegistry
from woassi.modules.messaging.infrastructure.uow import SqlAlchemyMessagingUoW
from woassi.modules.messaging.infrastructure.whatsapp_sender import HttpxWhatsAppSender
from woassi.modules.orders.application.facade import OrdersUseCases
from woassi.modules.orders.application.ports import (
    CatalogPort,
    CustomersPort,
    MessagingPort,
    OrdersUnitOfWork,
    ShopsPort,
)
from woassi.modules.orders.application.use_cases.orders import (
    DecideOrder,
    GetOrder,
    ListOrders,
    OrderFlow,
    ProcessDueOrders,
    ReceiveCart,
    ReplyToOrder,
)
from woassi.modules.orders.infrastructure.uow import SqlAlchemyOrdersUoW
from woassi.modules.shops.application.facade import ShopsUseCases
from woassi.modules.shops.application.ports import (
    LogoProcessor,
    MetaOnboardingPort,
    ShopsUnitOfWork,
)
from woassi.modules.shops.application.use_cases.logo import ConfirmLogo, RequestLogoUpload
from woassi.modules.shops.application.use_cases.receiving_account import (
    DeleteReceivingAccount,
    GetReceivingAccount,
    SetReceivingAccount,
)
from woassi.modules.shops.application.use_cases.settings import GetSettings, ReplaceSettings
from woassi.modules.shops.application.use_cases.shops import (
    ArchiveShop,
    CheckShopOwnership,
    CreateShop,
    GetShop,
    GetShopOrderProfile,
    ListShops,
    ResolveShopByPhoneNumber,
    ResolveShopConnection,
    UpdateShop,
)
from woassi.modules.shops.application.use_cases.whatsapp import (
    CheckWhatsAppTokens,
    ConnectWhatsApp,
    DisconnectWhatsApp,
    GetWhatsApp,
    HandleWhatsAppAccountRemoved,
)
from woassi.modules.shops.domain.events import WhatsAppConnected
from woassi.modules.shops.infrastructure.meta_onboarding import HttpxMetaOnboarding
from woassi.modules.shops.infrastructure.uow import SqlAlchemyShopsUoW
from woassi.shared.application.audit import ListAuditEntries
from woassi.shared.application.facade import SharedUseCases
from woassi.shared.application.ports import (
    AccountAuthenticator,
    Clock,
    IdempotencyStore,
    PhoneNormalizer,
    RateLimiter,
    SecretCipher,
    ShopOwnershipPort,
    StoragePort,
    UnitOfWork,
)
from woassi.shared.application.readiness import CheckReadiness
from woassi.shared.infrastructure.clock import SystemClock
from woassi.shared.infrastructure.crypto import FernetSecretCipher
from woassi.shared.infrastructure.db.session import create_engine, create_session_factory
from woassi.shared.infrastructure.db.uow import SqlAlchemyUnitOfWork
from woassi.shared.infrastructure.events.arq_dispatcher import ArqDeferredDispatcher
from woassi.shared.infrastructure.events.bus import InProcessEventBus
from woassi.shared.infrastructure.health import minio_check, postgres_check, redis_check
from woassi.shared.infrastructure.idempotency import RedisIdempotencyStore
from woassi.shared.infrastructure.meta_graph import GraphClient
from woassi.shared.infrastructure.phone import PhonenumbersNormalizer
from woassi.shared.infrastructure.rate_limiter import RedisRateLimiter
from woassi.shared.infrastructure.storage.minio_storage import MinioStorage


def _minio_client(endpoint: str, settings: Settings) -> Minio:
    """MinIO client for `endpoint`: the internal one to transfer, the public one to sign."""
    return Minio(
        endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_secure,
        region=settings.minio_region,
    )


def _code_sender(settings: Settings) -> VerificationCodeSender:
    # The log sender writes codes in clear text: it must never run outside development.
    """Verification code sender for the environment. Refuses to start outside dev for now."""
    if settings.app_env == "dev":
        return LogVerificationCodeSender()
    raise RuntimeError(f"No verification code sender for environment {settings.app_env}.")


@dataclass
class Container(
    AccountsUseCases,
    CatalogUseCases,
    CustomersUseCases,
    MessagingUseCases,
    OrdersUseCases,
    SharedUseCases,
    ShopsUseCases,
):
    """Builds every adapter and use case, and implements each module's `<Module>UseCases`.

    One instance per process (API or worker). Use cases and Units of Work are created per
    call.
    """

    settings: Settings
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    arq: ArqRedis
    minio: Minio
    clock: Clock
    event_bus: InProcessEventBus
    cipher: SecretCipher
    hasher: PasswordHasher
    code_sender: VerificationCodeSender
    http: httpx.AsyncClient
    uploads: StoragePort
    product_images: StoragePort
    documents: StoragePort

    @classmethod
    async def build(cls, settings: Settings) -> "Container":
        """Opens the connections (Postgres, Redis, MinIO, HTTP) and registers the event
        subscribers.
        """
        engine = create_engine(settings.database_url)
        client = _minio_client(settings.minio_endpoint, settings)
        signer = _minio_client(settings.minio_public_endpoint, settings)
        arq = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        container = cls(
            settings=settings,
            engine=engine,
            sessions=create_session_factory(engine),
            arq=arq,
            minio=client,
            clock=SystemClock(),
            event_bus=InProcessEventBus(ArqDeferredDispatcher(arq)),
            cipher=FernetSecretCipher(settings.encryption_keys),
            hasher=Argon2PasswordHasher(),
            code_sender=_code_sender(settings),
            http=httpx.AsyncClient(timeout=15),
            uploads=MinioStorage(client, signer, settings.minio_bucket_uploads),
            product_images=MinioStorage(
                client,
                signer,
                settings.minio_bucket_products,
                public_base_url=settings.public_images_base_url,
            ),
            documents=MinioStorage(client, signer, settings.minio_bucket_documents),
        )
        container._register_subscribers()
        return container

    def _register_subscribers(self) -> None:
        """Bind domain events to their subscribers, in the API and in the worker alike."""

        async def provision_templates(event: WhatsAppConnected) -> None:
            await self.provision_templates().execute(event.shop_id)

        # Deferred: several Graph calls, which must not slow down the connection request.
        self.event_bus.subscribe(
            WhatsAppConnected,
            provision_templates,
            name="messaging.provision_templates",
            deferred=True,
        )

    async def close(self) -> None:
        """Closes the HTTP client, the Redis pool and the database engine."""
        await self.http.aclose()
        await self.arq.aclose()
        await self.engine.dispose()

    # Factories
    def unit_of_work(self) -> UnitOfWork:
        return SqlAlchemyUnitOfWork(self.sessions, self.event_bus, self.clock)

    def accounts_uow(self) -> AccountsUnitOfWork:
        return SqlAlchemyAccountsUoW(self.sessions, self.event_bus, self.clock)

    def rate_limiter(self) -> RateLimiter:
        return RedisRateLimiter(self.arq)

    def token_service(self) -> TokenService:
        return JwtTokenService(self.settings.jwt_secret, self.settings.jwt_access_ttl, self.clock)

    def account_authenticator(self) -> AccountAuthenticator:
        return AccountsAuthenticator(self.authenticate_account())

    def shop_ownership(self) -> ShopOwnershipPort:
        return ShopsOwnership(CheckShopOwnership(self.shops_uow()))

    def shops_uow(self) -> ShopsUnitOfWork:
        return SqlAlchemyShopsUoW(self.sessions, self.event_bus, self.clock)

    def meta_onboarding(self) -> MetaOnboardingPort:
        return HttpxMetaOnboarding(
            self.graph(),
            app_id=self.settings.meta_app_id,
            app_secret=self.settings.meta_app_secret,
        )

    def graph(self) -> GraphClient:
        return GraphClient(self.http, graph_version=self.settings.meta_graph_version)

    def logo_processor(self) -> LogoProcessor:
        return CatalogLogoProcessor(self.image_processor())

    def shop_connections(self) -> ShopConnectionsPort:
        return ShopsConnections(
            ResolveShopByPhoneNumber(self.shops_uow(), self.cipher),
            ResolveShopConnection(self.shops_uow(), self.cipher),
        )

    def whatsapp_accounts(self) -> WhatsAppAccountsPort:
        return ShopsWhatsAppAccounts(HandleWhatsAppAccountRemoved(self.shops_uow()))

    def inbound_message_handler(self) -> InboundMessageHandler:
        return OrdersInboundHandler(
            RecordContact(self.customers_uow()),
            ChangeCustomerUserId(self.customers_uow()),
            self.receive_cart(),
            self.reply_to_order(),
            self.clock,
        )

    def customers_uow(self) -> CustomersUnitOfWork:
        return SqlAlchemyCustomersUoW(self.sessions, self.event_bus, self.clock)

    def orders_uow(self) -> OrdersUnitOfWork:
        return SqlAlchemyOrdersUoW(self.sessions, self.event_bus, self.clock)

    def order_shops(self) -> ShopsPort:
        return ShopsOrderProfiles(GetShopOrderProfile(self.shops_uow()))

    def order_catalog(self) -> CatalogPort:
        return CatalogStock(ReserveStock(self.catalog_uow()), ReleaseStock(self.catalog_uow()))

    def order_customers(self) -> CustomersPort:
        return CustomerContacts(GetCustomer(self.customers_uow()))

    def order_messaging(self) -> MessagingPort:
        return WhatsAppNotifications(self.send_whatsapp_message())

    def template_registry(self) -> GraphTemplateRegistry:
        return GraphTemplateRegistry(self.graph())

    def _order_flow[F: OrderFlow](self, use_case: type[F]) -> F:
        """Builds an order flow use case with all its ports."""
        return use_case(
            self.orders_uow(),
            self.order_shops(),
            self.order_catalog(),
            self.order_customers(),
            self.order_messaging(),
            self.clock,
        )

    # Use cases: orders
    def receive_cart(self) -> ReceiveCart:
        return self._order_flow(ReceiveCart)

    def reply_to_order(self) -> ReplyToOrder:
        return self._order_flow(ReplyToOrder)

    def decide_order(self) -> DecideOrder:
        return self._order_flow(DecideOrder)

    def process_due_orders(self) -> ProcessDueOrders:
        return self._order_flow(ProcessDueOrders)

    def list_orders(self) -> ListOrders:
        return ListOrders(self.orders_uow())

    def get_order(self) -> GetOrder:
        return GetOrder(self.orders_uow())

    # Use cases: customers
    def list_customers(self) -> ListCustomers:
        return ListCustomers(self.customers_uow())

    def get_customer(self) -> GetCustomer:
        return GetCustomer(self.customers_uow())

    def provision_templates(self) -> ProvisionTemplates:
        return ProvisionTemplates(
            self.messaging_uow(),
            self.shop_connections(),
            self.template_registry(),
            template_definitions(),
            self.clock,
        )

    def messaging_uow(self) -> MessagingUnitOfWork:
        return SqlAlchemyMessagingUoW(self.sessions, self.event_bus, self.clock)

    def whatsapp_sender(self) -> WhatsAppSender:
        return HttpxWhatsAppSender(self.graph())

    # Use cases: messaging, worker side and for other modules
    def handle_meta_event(self) -> HandleMetaEvent:
        return HandleMetaEvent(
            self.messaging_uow(),
            self.shop_connections(),
            self.whatsapp_accounts(),
            self.inbound_message_handler(),
            self.clock,
        )

    def send_whatsapp_message(self) -> SendWhatsAppMessage:
        return SendWhatsAppMessage(
            self.messaging_uow(),
            self.shop_connections(),
            self.whatsapp_sender(),
            EnsureTemplateSendable(self.messaging_uow(), self.template_registry(), self.clock),
            self.clock,
        )

    def catalog_uow(self) -> CatalogUnitOfWork:
        return SqlAlchemyCatalogUoW(self.sessions, self.event_bus, self.clock)

    def phone_normalizer(self) -> PhoneNormalizer:
        return PhonenumbersNormalizer()

    def idempotency_store(self) -> IdempotencyStore:
        return RedisIdempotencyStore(self.arq)

    def photo_jobs(self) -> PhotoJobQueue:
        return ArqPhotoJobQueue(self.arq)

    def image_processor(self) -> ImageProcessor:
        return PillowImageProcessor()

    def meta_events(self) -> EventQueue:
        return ArqEventQueue(self.arq)

    # Use cases: accounts
    def register(self) -> Register:
        return Register(
            self.accounts_uow(),
            self.hasher,
            self.code_sender,
            self.rate_limiter(),
            self.phone_normalizer(),
            self.clock,
        )

    def verify_phone(self) -> VerifyPhone:
        return VerifyPhone(self.accounts_uow(), self.hasher, self.phone_normalizer(), self.clock)

    def login(self) -> Login:
        return Login(
            self.accounts_uow(),
            self.hasher,
            self.token_service(),
            self.rate_limiter(),
            self.phone_normalizer(),
            self.clock,
            refresh_ttl_s=self.settings.jwt_refresh_ttl,
        )

    def refresh_tokens(self) -> RefreshTokens:
        return RefreshTokens(
            self.accounts_uow(),
            self.token_service(),
            self.clock,
            refresh_ttl_s=self.settings.jwt_refresh_ttl,
        )

    def logout(self) -> Logout:
        return Logout(self.accounts_uow(), self.token_service(), self.clock)

    def logout_everywhere(self) -> LogoutEverywhere:
        return LogoutEverywhere(self.accounts_uow(), self.clock)

    def request_password_reset(self) -> RequestPasswordReset:
        return RequestPasswordReset(
            self.accounts_uow(),
            self.hasher,
            self.code_sender,
            self.rate_limiter(),
            self.phone_normalizer(),
            self.clock,
        )

    def reset_password(self) -> ResetPassword:
        return ResetPassword(self.accounts_uow(), self.hasher, self.phone_normalizer(), self.clock)

    def get_my_account(self) -> GetMyAccount:
        return GetMyAccount(self.accounts_uow())

    def update_my_account(self) -> UpdateMyAccount:
        return UpdateMyAccount(self.accounts_uow())

    def change_password(self) -> ChangePassword:
        return ChangePassword(self.accounts_uow(), self.hasher, self.rate_limiter(), self.clock)

    def authenticate_account(self) -> AuthenticateAccount:
        return AuthenticateAccount(self.accounts_uow(), self.token_service())

    # Use cases: shops
    def create_shop(self) -> CreateShop:
        """Passes the Unit of Work factory itself: each slug attempt needs a new transaction."""
        return CreateShop(self.shops_uow, self.product_images, self.clock)

    def list_shops(self) -> ListShops:
        return ListShops(self.shops_uow(), self.product_images)

    def get_shop(self) -> GetShop:
        return GetShop(self.shops_uow(), self.product_images)

    def update_shop(self) -> UpdateShop:
        return UpdateShop(self.shops_uow(), self.product_images)

    def archive_shop(self) -> ArchiveShop:
        return ArchiveShop(self.shops_uow(), self.meta_onboarding(), self.cipher, self.clock)

    def get_settings(self) -> GetSettings:
        return GetSettings(self.shops_uow())

    def replace_settings(self) -> ReplaceSettings:
        return ReplaceSettings(self.shops_uow(), self.phone_normalizer())

    def connect_whatsapp(self) -> ConnectWhatsApp:
        return ConnectWhatsApp(self.shops_uow(), self.meta_onboarding(), self.cipher, self.clock)

    def get_whatsapp(self) -> GetWhatsApp:
        return GetWhatsApp(self.shops_uow())

    def disconnect_whatsapp(self) -> DisconnectWhatsApp:
        return DisconnectWhatsApp(self.shops_uow(), self.meta_onboarding(), self.cipher)

    def check_whatsapp_tokens(self) -> CheckWhatsAppTokens:
        return CheckWhatsAppTokens(self.shops_uow(), self.meta_onboarding(), self.cipher)

    def get_receiving_account(self) -> GetReceivingAccount:
        return GetReceivingAccount(self.shops_uow(), self.cipher)

    def set_receiving_account(self) -> SetReceivingAccount:
        return SetReceivingAccount(self.shops_uow(), self.phone_normalizer(), self.cipher)

    def delete_receiving_account(self) -> DeleteReceivingAccount:
        return DeleteReceivingAccount(self.shops_uow())

    def request_logo_upload(self) -> RequestLogoUpload:
        return RequestLogoUpload(self.shops_uow(), self.uploads)

    def confirm_logo(self) -> ConfirmLogo:
        return ConfirmLogo(
            self.shops_uow(), self.uploads, self.product_images, self.logo_processor()
        )

    # Use cases: shared
    def list_audit_entries(self) -> ListAuditEntries:
        return ListAuditEntries(self.unit_of_work())

    def check_readiness(self) -> CheckReadiness:
        return CheckReadiness(
            {
                "postgres": postgres_check(self.engine),
                "redis": redis_check(self.arq),
                "minio": minio_check(self.minio, self.settings.minio_bucket_uploads),
            }
        )

    # Use cases: catalog
    def create_product(self) -> CreateProduct:
        return CreateProduct(self.catalog_uow(), self.product_images)

    def list_products(self) -> ListProducts:
        return ListProducts(self.catalog_uow(), self.product_images)

    def get_product(self) -> GetProduct:
        return GetProduct(self.catalog_uow(), self.product_images)

    def request_photo_upload(self) -> RequestPhotoUpload:
        return RequestPhotoUpload(
            self.catalog_uow(),
            self.uploads,
            max_photos=self.settings.max_photos_per_product,
            max_size_bytes=self.settings.photo_max_size_bytes,
        )

    def confirm_photo(self) -> ConfirmPhoto:
        return ConfirmPhoto(
            self.catalog_uow(),
            self.uploads,
            self.photo_jobs(),
            max_size_bytes=self.settings.photo_max_size_bytes,
        )

    def process_photo(self) -> ProcessPhoto:
        return ProcessPhoto(
            self.catalog_uow(), self.uploads, self.product_images, self.image_processor()
        )

    # Use cases: messaging
    def verify_meta_subscription(self) -> VerifyMetaSubscription:
        return VerifyMetaSubscription(self.settings.meta_verify_token)

    def receive_meta_webhook(self) -> ReceiveMetaWebhook:
        return ReceiveMetaWebhook(self.settings.meta_app_secret, self.meta_events())
