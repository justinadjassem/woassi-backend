"""arq worker: background jobs and crons.

Run with `arq woassi.worker.WorkerSettings`. Each job gets its use cases from the `Container`
built at startup.
"""

from typing import Any
from uuid import UUID

import structlog
from arq import Retry, cron
from arq.connections import RedisSettings

from woassi.config import get_settings
from woassi.container import Container
from woassi.modules.messaging.domain.event import MetaEvent
from woassi.shared.domain.events import DomainEvent
from woassi.shared.infrastructure.logging import configure_logging, configure_sentry

log = structlog.get_logger()

MAX_TRIES = 5
RETRY_DELAY_S = 30


async def on_startup(ctx: dict[str, Any]) -> None:
    """Sets up logging and Sentry, and builds the `Container` shared by every job."""
    settings = get_settings()
    configure_logging(settings.app_env)
    configure_sentry(settings.sentry_dsn, settings.app_env)
    ctx["container"] = await Container.build(settings)


async def on_shutdown(ctx: dict[str, Any]) -> None:
    """Releases the container's connections."""
    await ctx["container"].close()


async def process_photo(ctx: dict[str, Any], photo_id: str) -> None:
    """Turns an uploaded product photo into its WebP variants."""
    await ctx["container"].process_photo().execute(UUID(photo_id))


async def process_meta_event(ctx: dict[str, Any], event: MetaEvent) -> None:
    """Meta does not resend once the API answered 200: the worker has to retry by itself."""
    try:
        await ctx["container"].handle_meta_event().execute(event)
    except Exception as error:
        if ctx["job_try"] >= MAX_TRIES:
            log.exception("meta_event_dropped", job_try=ctx["job_try"], meta_event=repr(event))
            raise
        raise Retry(defer=RETRY_DELAY_S * ctx["job_try"]) from error


async def verify_kprimepay_payment(ctx: dict[str, Any], shop_id: str, payment_id: str) -> None:
    """After a KPrimePay webhook: asks KPrimePay itself, as webhooks are not signed."""
    try:
        verify = ctx["container"].verify_payment()
        await verify.execute(shop_id=UUID(shop_id), payment_id=UUID(payment_id))
    except Exception as error:
        if ctx["job_try"] >= MAX_TRIES:
            # The poller still checks the payment later.
            log.exception("payment_check_dropped", job_try=ctx["job_try"], payment_id=payment_id)
            raise
        raise Retry(defer=RETRY_DELAY_S * ctx["job_try"]) from error


async def poll_pending_payments(ctx: dict[str, Any]) -> None:
    """Cron, every minute: checks pending payments with KPrimePay, and expires them."""
    handled = await ctx["container"].poll_pending_payments().execute()
    if handled:
        log.info("pending_payments_polled", payments=handled)


async def dispatch_event(ctx: dict[str, Any], subscriber: str, event: DomainEvent) -> None:
    """Runs a deferred event subscriber, queued by `ArqDeferredDispatcher`."""
    await ctx["container"].event_bus.dispatch(subscriber, event)


async def sync_catalog(ctx: dict[str, Any], shop_id: str) -> None:
    """Sends a shop's pending products to its Meta catalog, queued by `ArqCatalogSyncQueue`."""
    sent = await ctx["container"].sync_catalog_to_meta().execute(UUID(shop_id))
    log.info("meta_catalog_synced", shop_id=shop_id, products=sent)


async def retry_catalog_sync(ctx: dict[str, Any]) -> None:
    """Cron, every 5 minutes: queues the shops whose products are due again for Meta."""
    shops = await ctx["container"].retry_catalog_sync().execute()
    if shops:
        log.info("meta_catalog_retries_queued", shops=shops)


async def check_whatsapp_tokens(ctx: dict[str, Any]) -> None:
    """Daily cron, 3:00 UTC: flags the WhatsApp connections whose token Meta reports invalid."""
    invalid = await ctx["container"].check_whatsapp_tokens().execute()
    log.info("whatsapp_tokens_checked", invalid=invalid)


async def process_due_orders(ctx: dict[str, Any]) -> None:
    """Cron, every minute: sends order reminders and expires unanswered orders."""
    handled = await ctx["container"].process_due_orders().execute()
    if handled:
        log.info("due_orders_processed", orders=handled)


class WorkerSettings:
    """arq configuration: jobs, crons, Redis connection and retries."""

    functions = (
        process_photo,
        process_meta_event,
        verify_kprimepay_payment,
        dispatch_event,
        sync_catalog,
    )
    cron_jobs = (
        cron(check_whatsapp_tokens, hour=3, minute=0, timeout=3600),
        # Every minute: reminders at 15 and 60 minutes, expiry after 24 hours.
        cron(process_due_orders, second=0, timeout=50, unique=True),
        # Every minute, offset from the orders cron: backoff of 2, 5, 10, 30 min, then hourly.
        cron(poll_pending_payments, second=30, timeout=50, unique=True),
        cron(retry_catalog_sync, minute=set(range(0, 60, 5)), second=30, timeout=60, unique=True),
    )
    on_startup = on_startup
    on_shutdown = on_shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    # With no stored result, the process_photo job id is freed as soon as the job ends.
    keep_result = 0
    max_tries = MAX_TRIES
