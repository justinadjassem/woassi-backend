"""Rate limits, for any module: `enforce` turns an exceeded limit into a 429."""

from woassi.shared.application.ports import RateLimiter
from woassi.shared.domain.errors import RateLimited


async def enforce(limiter: RateLimiter, key: str, limit: int, window_s: int) -> None:
    """Counts one attempt on `key`; raises RateLimited once over `limit` in the window."""
    retry_after_s = await limiter.hit(key, limit, window_s)
    if retry_after_s is not None:
        raise RateLimited("Too many attempts. Try again later.", retry_after_s=retry_after_s)
