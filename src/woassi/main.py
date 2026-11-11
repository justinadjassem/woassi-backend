"""FastAPI application: middlewares, error handlers and routers."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from woassi.config import Settings, get_settings
from woassi.container import Container
from woassi.modules.accounts.api.router import auth_router, me_router
from woassi.modules.billing.api.router import public_router as billing_public_router
from woassi.modules.billing.api.router import router as billing_router
from woassi.modules.catalog.api.router import router as catalog_router
from woassi.modules.customers.api.router import router as customers_router
from woassi.modules.messaging.api.router import router as messaging_router
from woassi.modules.orders.api.router import router as orders_router
from woassi.modules.payments.api.router import router as payments_router
from woassi.modules.payments.api.router import webhook_router as payments_webhook_router
from woassi.modules.shops.api.router import account_router as shops_account_router
from woassi.modules.shops.api.router import router as shops_router
from woassi.shared.api.audit import router as audit_router
from woassi.shared.api.errors import register_error_handlers
from woassi.shared.api.health import router as health_router
from woassi.shared.api.idempotency import IdempotencyMiddleware
from woassi.shared.infrastructure.logging import configure_logging, configure_sentry


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Builds the `Container` at startup and releases its connections at shutdown."""
    container = await Container.build(app.state.settings)
    app.state.container = container
    try:
        yield
    finally:
        await container.close()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Builds the API. `settings` defaults to the environment; tests pass their own."""
    settings = settings or get_settings()
    configure_logging(settings.app_env)
    configure_sentry(settings.sentry_dsn, settings.app_env)

    app = FastAPI(title="Woassi API", lifespan=lifespan)
    app.state.settings = settings
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,  # the dashboard sends the refresh cookie
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(IdempotencyMiddleware)
    register_error_handlers(app)

    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(me_router)
    app.include_router(shops_account_router)
    app.include_router(shops_router)
    app.include_router(audit_router)
    app.include_router(catalog_router)
    app.include_router(orders_router)
    app.include_router(customers_router)
    app.include_router(messaging_router)
    app.include_router(payments_router)
    app.include_router(payments_webhook_router)
    app.include_router(billing_router)
    app.include_router(billing_public_router)
    return app
