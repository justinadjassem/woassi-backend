"""Readiness checks for the API's dependencies. Each raises if its dependency is down."""

from functools import partial

import anyio.to_thread
from arq.connections import ArqRedis
from minio import Minio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from woassi.shared.application.readiness import ReadinessCheck


def postgres_check(engine: AsyncEngine) -> ReadinessCheck:
    """Check that runs `SELECT 1` on the database."""

    async def check() -> None:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    return check


def redis_check(redis: ArqRedis) -> ReadinessCheck:
    """Check that pings Redis."""

    async def check() -> None:
        await redis.ping()

    return check


def minio_check(client: Minio, bucket: str) -> ReadinessCheck:
    """Check that `bucket` exists in MinIO, which also proves MinIO answers."""

    async def check() -> None:
        # abandon_on_cancel: otherwise the timeout would wait for the blocked SDK call to end.
        exists = await anyio.to_thread.run_sync(
            partial(client.bucket_exists, bucket), abandon_on_cancel=True
        )
        if not exists:
            raise RuntimeError(f"Bucket {bucket} does not exist.")

    return check
