"""Logging and error reporting setup."""

import logging

import sentry_sdk
import structlog
from sentry_sdk.integrations.httpx import HttpxIntegration


def silence_url_logging() -> None:
    """httpx logs every URL at INFO, query string included: Graph API secrets travel there."""
    logging.getLogger("httpx").setLevel(logging.WARNING)


def configure_logging(app_env: str) -> None:
    """Structlog: console output in dev, JSON in other environments."""
    silence_url_logging()
    renderer: structlog.typing.Processor = (
        structlog.dev.ConsoleRenderer() if app_env == "dev" else structlog.processors.JSONRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        cache_logger_on_first_use=True,
    )


def configure_sentry(dsn: str | None, app_env: str) -> None:
    """Enables Sentry when a DSN is configured; does nothing otherwise."""
    if dsn:
        # Graph API secrets travel in query strings: keep those URLs out of Sentry breadcrumbs.
        sentry_sdk.init(dsn=dsn, environment=app_env, disabled_integrations=[HttpxIntegration()])
