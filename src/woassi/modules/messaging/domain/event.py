"""Events read from Meta webhooks, processed one by one in the worker.

They are pickled into arq jobs, and arq logs their repr.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class InboundMessage:
    """Message from a customer to a shop's number.

    The customer is identified by `sender_user_id`, the business-scoped user id (BSUID), which
    Meta always sends. `sender_phone` is missing when the customer uses a WhatsApp username.

    Every customer field is personal data: repr=False keeps them out of logs, including arq's,
    which logs the repr of every job's arguments.
    """

    phone_number_id: str
    message_id: str
    type: str
    sender_user_id: str | None = field(default=None, repr=False)
    sender_phone: str | None = field(default=None, repr=False)
    profile_name: str | None = field(default=None, repr=False)
    payload: Mapping[str, Any] = field(default_factory=dict, repr=False)
    # Message this one replies to, e.g. the one carrying the button that was tapped.
    context_message_id: str | None = None
    timestamp: int | None = None


@dataclass(frozen=True, slots=True)
class MessageStatusUpdate:
    """Delivery status of a message sent by a shop."""

    phone_number_id: str
    message_id: str
    status: str
    error_codes: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class AccountUpdate:
    """Change on a WhatsApp Business Account, e.g. Woassi lost access to it."""

    waba_id: str
    event: str
    time: int | None = None


@dataclass(frozen=True, slots=True)
class TemplateStatusUpdate:
    """Meta reviewed, paused or disabled a message template of an account."""

    waba_id: str
    name: str
    language: str
    status: str


type MetaEvent = InboundMessage | MessageStatusUpdate | AccountUpdate | TemplateStatusUpdate
