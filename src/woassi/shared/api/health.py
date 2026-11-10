"""Health probes: liveness (`/health`) and readiness (`/health/ready`)."""

from typing import Literal

from fastapi import APIRouter, Response, status
from pydantic import BaseModel

from woassi.shared.api.deps import SharedDep

router = APIRouter(prefix="/health", tags=["system"])


class ReadinessResponse(BaseModel):
    """Overall status, and the result of each dependency check."""

    status: Literal["ready", "unavailable"]
    checks: dict[str, Literal["ok", "failed"]]


@router.get("")
async def health() -> dict[str, str]:
    """Liveness: answers as long as the process serves requests."""
    return {"status": "ok"}


@router.get("/ready", responses={503: {"model": ReadinessResponse}})
async def ready(shared: SharedDep, response: Response) -> ReadinessResponse:
    """Readiness: 200 if every dependency answers, 503 otherwise.

    Covers the API only: a dead worker does not show here.
    """
    report = await shared.check_readiness().execute()
    if not report.ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadinessResponse(
        status="ready" if report.ready else "unavailable",
        checks={name: "ok" if ok else "failed" for name, ok in report.checks.items()},
    )
