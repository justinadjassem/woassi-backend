"""Clock implementations: the real one, and a frozen one for tests."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

DISPLAY_TIMEZONE = ZoneInfo("Africa/Lome")


class SystemClock:
    """Real time."""

    def now(self) -> datetime:
        """Current time in UTC."""
        return datetime.now(UTC)

    def local_now(self) -> datetime:
        """Current time in Africa/Lome, for display."""
        return self.now().astimezone(DISPLAY_TIMEZONE)


class FrozenClock:
    """For tests: time only moves when `advance` is called."""

    def __init__(self, current: datetime) -> None:
        self._current = current

    def now(self) -> datetime:
        """The frozen time, in the timezone it was given in."""
        return self._current

    def local_now(self) -> datetime:
        """The frozen time in Africa/Lome."""
        return self._current.astimezone(DISPLAY_TIMEZONE)

    def advance(self, delta: timedelta) -> None:
        """Moves the clock forward by `delta`."""
        self._current += delta
