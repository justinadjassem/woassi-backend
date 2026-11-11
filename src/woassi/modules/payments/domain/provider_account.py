"""A shop's own KPrimePay business account. Woassi only holds its API key, encrypted."""

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from woassi.modules.payments.domain.errors import InvalidProviderKey

# https://developers.kprimepay.com/index?v=v2#authentication
SANDBOX_PREFIX = "kpp_sbk_"
LIVE_PREFIX = "kpp_lk_"
_KEY = re.compile(r"[A-Za-z0-9_\-]{8,200}")


class ProviderMode(StrEnum):
    """Sandbox keys never move real money."""

    SANDBOX = "sandbox"
    LIVE = "live"


def mode_of(api_key: str) -> ProviderMode:
    """The mode a key belongs to, from its prefix. Raises InvalidProviderKey otherwise.

    The message never quotes the key.
    """
    if _KEY.fullmatch(api_key):
        if api_key.startswith(SANDBOX_PREFIX) and len(api_key) > len(SANDBOX_PREFIX):
            return ProviderMode.SANDBOX
        if api_key.startswith(LIVE_PREFIX) and len(api_key) > len(LIVE_PREFIX):
            return ProviderMode.LIVE
    raise InvalidProviderKey(
        f"A KPrimePay key starts with {SANDBOX_PREFIX} (sandbox) or {LIVE_PREFIX} (live)."
    )


@dataclass(slots=True)
class ProviderAccount:
    """The KPrimePay account of one shop.

    The key and the callback token are encrypted with SecretCipher. The token goes in the
    callback URL of every checkout, so it has to be readable; its SHA-256 finds the shop when a
    webhook comes in.
    """

    shop_id: UUID
    mode: ProviderMode
    encrypted_api_key: str = field(repr=False)
    encrypted_callback_token: str = field(repr=False)
    callback_token_hash: str = field(repr=False)
    created_at: datetime
    updated_at: datetime
