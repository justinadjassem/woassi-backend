"""Redis rate limiter, using Lua scripts so each step is atomic."""

from typing import Any, cast

from arq.connections import ArqRedis

# INCR and EXPIRE in one script: if they were separate, a crash in between would leave a counter
# that never expires, and block its key for good.
_HIT = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return {count, redis.call('TTL', KEYS[1])}
"""

# Skips a key that has expired, which DECR would recreate at -1 with no expiry.
_FORGIVE = """
if redis.call('EXISTS', KEYS[1]) == 1 then
    redis.call('DECR', KEYS[1])
end
return 0
"""


class RedisRateLimiter:
    """Fixed window: the first attempt starts it, and it does not move afterwards."""

    def __init__(self, redis: ArqRedis) -> None:
        self._redis = redis

    async def hit(self, key: str, limit: int, window_s: int) -> int | None:
        """Counts one attempt. Returns None if allowed, else the seconds before the window ends."""
        result = await cast(Any, self._redis.eval(_HIT, 1, self._name(key), str(window_s)))
        count, ttl = int(result[0]), int(result[1])
        return None if count <= limit else max(ttl, 1)

    async def forgive(self, key: str) -> None:
        """Takes one attempt back, if the window has not expired."""
        await cast(Any, self._redis.eval(_FORGIVE, 1, self._name(key)))

    @staticmethod
    def _name(key: str) -> str:
        """Redis key of a rate limit key."""
        return f"rate:{key}"
