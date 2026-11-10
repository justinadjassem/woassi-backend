"""Message templates: the only messages allowed outside the 24-hour window.

https://developers.facebook.com/documentation/business-messaging/whatsapp/templates/overview.md
https://developers.facebook.com/documentation/business-messaging/whatsapp/templates/template-management.md
https://developers.facebook.com/documentation/business-messaging/whatsapp/templates/components.md
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from woassi.modules.messaging.domain.content import LANGUAGE, TEMPLATE_NAME
from woassi.modules.messaging.domain.errors import InvalidMessage

MAX_BODY = 1024
MAX_QUICK_REPLY = 25
MAX_QUICK_REPLIES = 10
PARAMETER = re.compile(r"\{\{([a-z_][a-z0-9_]*)\}\}")


@dataclass(frozen=True, slots=True)
class TemplateDefinition:
    """A utility template with named parameters, as Woassi submits it to Meta."""

    name: str
    language: str
    body: str
    examples: Mapping[str, str] = field(default_factory=dict)
    quick_replies: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not TEMPLATE_NAME.fullmatch(self.name) or not LANGUAGE.fullmatch(self.language):
            raise InvalidMessage("Invalid template name or language.")
        if not 1 <= len(self.body) <= MAX_BODY:
            raise InvalidMessage(f"A template body takes 1 to {MAX_BODY} characters.")
        if set(PARAMETER.findall(self.body)) != set(self.examples):
            raise InvalidMessage("Every template parameter needs an example, and only those.")
        if len(self.quick_replies) > MAX_QUICK_REPLIES or any(
            not 1 <= len(reply) <= MAX_QUICK_REPLY for reply in self.quick_replies
        ):
            raise InvalidMessage("Invalid quick reply buttons.")


class TemplateStatus(StrEnum):
    """Review status of a template. Only APPROVED can be sent."""

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    PAUSED = "PAUSED"
    DISABLED = "DISABLED"
    UNKNOWN = "UNKNOWN"


# Webhook events and API statuses that change whether a template can be sent.
_FROM_META = {
    "APPROVED": TemplateStatus.APPROVED,
    "REINSTATED": TemplateStatus.APPROVED,
    "PENDING": TemplateStatus.PENDING,
    "IN_APPEAL": TemplateStatus.PENDING,
    "REJECTED": TemplateStatus.REJECTED,
    "PAUSED": TemplateStatus.PAUSED,
    "DISABLED": TemplateStatus.DISABLED,
    "DELETED": TemplateStatus.DISABLED,
    "PENDING_DELETION": TemplateStatus.DISABLED,
    "ARCHIVED": TemplateStatus.DISABLED,
}


def status_from_meta(value: str) -> TemplateStatus | None:
    """None for events that change nothing for sending, such as FLAGGED or LOCKED."""
    return _FROM_META.get(value.upper())


def same_language(meta_language: str, language: str) -> bool:
    """Meta writes `en_US` or `en-US` depending on the endpoint."""
    return meta_language.replace("-", "_").lower() == language.replace("-", "_").lower()


@dataclass(slots=True)
class MessageTemplate:
    """A template submitted to a shop's account, in one language, and its review status."""

    shop_id: UUID
    waba_id: str
    name: str
    language: str
    status: TemplateStatus
    updated_at: datetime
    meta_template_id: str | None = None

    @property
    def sendable(self) -> bool:
        """True if Meta approved the template."""
        return self.status is TemplateStatus.APPROVED
