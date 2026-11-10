"""Rate limits of the accounts module and their keys."""

from woassi.shared.application.ports import RateLimiter
from woassi.shared.domain.errors import RateLimited

LOGIN_LIMIT = 5
LOGIN_WINDOW_S = 15 * 60
CODE_LIMIT = 3
CODE_WINDOW_S = 3600


def login_phone_key(phone: str) -> str:
    """Failed logins for one phone number."""
    return f"login:phone:{phone}"


def login_ip_key(ip: str) -> str:
    """Failed logins from one IP."""
    return f"login:ip:{ip}"


def code_key(phone: str) -> str:
    """Verification codes sent to one phone number (SMS cost and spam)."""
    return f"code:{phone}"


def password_change_key(account_id: object) -> str:
    """Failed password changes of one account."""
    return f"password-change:{account_id}"


async def enforce(limiter: RateLimiter, key: str, limit: int, window_s: int) -> None:
    """Counts one attempt on `key`; raises RateLimited once over `limit` in the window."""
    retry_after_s = await limiter.hit(key, limit, window_s)
    if retry_after_s is not None:
        raise RateLimited("Too many attempts. Try again later.", retry_after_s=retry_after_s)
