"""Readiness probe: checks the API's dependencies (database, Redis, MinIO)."""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

import structlog

log = structlog.get_logger()

type ReadinessCheck = Callable[[], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    """Result of each check, by name."""

    checks: dict[str, bool]

    @property
    def ready(self) -> bool:
        """True if every check passed."""
        return all(self.checks.values())


class CheckReadiness:
    """Runs every readiness check concurrently, each with its own timeout."""

    def __init__(self, checks: Mapping[str, ReadinessCheck], *, timeout_s: float = 2.0) -> None:
        self._checks = checks
        self._timeout_s = timeout_s

    async def execute(self) -> ReadinessReport:
        """Runs all checks. A failing or slow check is reported, never raised."""
        results = await asyncio.gather(*(self._run(name) for name in self._checks))
        return ReadinessReport(checks=dict(zip(self._checks, results, strict=True)))

    async def _run(self, name: str) -> bool:
        """Runs one check; any error or timeout counts as not ready."""
        try:
            async with asyncio.timeout(self._timeout_s):
                await self._checks[name]()
        except Exception as error:
            log.warning("readiness_check_failed", check=name, error=type(error).__name__)
            return False
        return True
