"""What a shop can send, with the limits Meta enforces, checked before calling Meta.

https://developers.facebook.com/documentation/business-messaging/whatsapp/messages/send-messages
https://developers.facebook.com/documentation/business-messaging/whatsapp/messages/interactive-reply-buttons-messages.md
https://developers.facebook.com/documentation/business-messaging/whatsapp/templates/utility-templates/utility-templates.md
https://developers.facebook.com/documentation/business-messaging/whatsapp/messages/document-messages
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from woassi.modules.messaging.domain.errors import InvalidMessage
from woassi.shared.domain.values import PhoneNumber

MAX_TEXT = 4096
MAX_BUTTON_BODY = 1024
MAX_BUTTONS = 3
MAX_BUTTON_TITLE = 20
MAX_BUTTON_ID = 256
MAX_FOOTER = 60
MAX_CAPTION = 1024
MAX_FILENAME = 240
TEMPLATE_NAME = re.compile(r"[a-z0-9_]{1,512}")
LANGUAGE = re.compile(r"[a-z]{2,3}(_[A-Z]{2})?")


def _length(value: str, maximum: int, label: str) -> None:
    """Raises InvalidMessage unless `value` has 1 to `maximum` characters."""
    if not 1 <= len(value) <= maximum:
        raise InvalidMessage(f"{label} must be 1 to {maximum} characters long.")


@dataclass(frozen=True, slots=True)
class Recipient:
    """A customer, by phone number or by business-scoped user id (BSUID).

    Meta uses the phone number when both are given.
    """

    phone: PhoneNumber | None = field(default=None, repr=False)
    user_id: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.phone is None and not self.user_id:
            raise InvalidMessage("A recipient needs a phone number or a user id.")

    @property
    def address(self) -> str:
        """The phone number if known, otherwise the BSUID."""
        if self.phone is not None:
            return self.phone.value
        assert self.user_id is not None
        return self.user_id


@dataclass(frozen=True, slots=True)
class TextMessage:
    """Free-form text, only allowed within the 24-hour customer service window."""

    body: str = field(repr=False)

    def __post_init__(self) -> None:
        _length(self.body, MAX_TEXT, "A text")


@dataclass(frozen=True, slots=True)
class ReplyButton:
    """A reply button. Its `id` comes back in the webhook when the user taps it."""

    id: str
    title: str

    def __post_init__(self) -> None:
        _length(self.id, MAX_BUTTON_ID, "A button id")
        _length(self.title, MAX_BUTTON_TITLE, "A button title")


@dataclass(frozen=True, slots=True)
class ButtonsMessage:
    """Text with 1 to 3 reply buttons, only allowed within the 24-hour window."""

    body: str = field(repr=False)
    buttons: tuple[ReplyButton, ...]
    footer: str | None = None

    def __post_init__(self) -> None:
        _length(self.body, MAX_BUTTON_BODY, "A message body")
        if not 1 <= len(self.buttons) <= MAX_BUTTONS:
            raise InvalidMessage(f"A message takes 1 to {MAX_BUTTONS} buttons.")
        if len({button.title for button in self.buttons}) != len(self.buttons):
            raise InvalidMessage("Button titles must be unique.")
        if len({button.id for button in self.buttons}) != len(self.buttons):
            raise InvalidMessage("Button ids must be unique.")
        if self.footer is not None:
            _length(self.footer, MAX_FOOTER, "A footer")


@dataclass(frozen=True, slots=True)
class TemplateMessage:
    """An approved template: the only kind of message allowed outside the 24-hour window."""

    name: str
    language: str
    body_parameters: Mapping[str, str] = field(default_factory=dict, repr=False)
    # Returned in the reply webhook when the user taps the quick reply button at that index.
    button_payloads: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for value in self.body_parameters.values():
            # Meta refuses line breaks and tabs in template parameters.
            if not value or "\n" in value or "\t" in value:
                raise InvalidMessage("A template parameter must be one non-empty line.")
        for button_payload in self.button_payloads:
            _length(button_payload, MAX_BUTTON_ID, "A button payload")
        if not TEMPLATE_NAME.fullmatch(self.name):
            raise InvalidMessage("A template name takes lowercase letters, digits and _.")
        if not LANGUAGE.fullmatch(self.language):
            raise InvalidMessage("A template language looks like fr or en_US.")


@dataclass(frozen=True, slots=True)
class DocumentMessage:
    """A file Meta downloads from `link` when sending (e.g. a presigned PDF URL), only within
    the 24-hour window. `filename` keeps its extension: WhatsApp picks the icon from it.
    """

    link: str = field(repr=False)
    filename: str
    caption: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not self.link.startswith(("https://", "http://")):
            raise InvalidMessage("A document link must be an http(s) URL.")
        _length(self.filename, MAX_FILENAME, "A file name")
        if "." not in self.filename:
            raise InvalidMessage("A file name needs its extension, e.g. invoice.pdf.")
        if self.caption is not None:
            _length(self.caption, MAX_CAPTION, "A caption")


type MessageContent = TextMessage | ButtonsMessage | TemplateMessage | DocumentMessage
