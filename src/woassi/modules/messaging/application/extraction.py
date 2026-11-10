"""Turns a webhook payload into events.

https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/reference/messages.md
https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/reference/messages/status.md
https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/reference/account_update.md
https://developers.facebook.com/documentation/business-messaging/whatsapp/business-scoped-user-ids.md
https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/reference/message_template_status_update.md
"""

from collections.abc import Mapping, Sequence
from typing import Any

from woassi.modules.messaging.domain.event import (
    AccountUpdate,
    InboundMessage,
    MessageStatusUpdate,
    MetaEvent,
    TemplateStatusUpdate,
)


def _mappings(value: object) -> list[Mapping[str, Any]]:
    """The dict items of `value` if it is a list; an empty list for anything else."""
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return []
    return [element for element in value if isinstance(element, Mapping)]


def _text(value: object) -> str | None:
    """`value` if it is a non-empty string, else None."""
    return value if isinstance(value, str) and value else None


def _integer(value: object) -> int | None:
    """`value` as an int (Meta sends timestamps as strings), or None."""
    try:
        return int(value)  # type: ignore[call-overload, no-any-return]
    except (TypeError, ValueError):
        return None


def _profile_name(contacts: list[Mapping[str, Any]], message: Mapping[str, Any]) -> str | None:
    """WhatsApp profile name of the message's sender, found in `contacts`."""
    for contact in contacts:
        same_user = contact.get("user_id") and contact.get("user_id") == message.get("from_user_id")
        same_phone = contact.get("wa_id") and contact.get("wa_id") == message.get("from")
        if same_user or same_phone or len(contacts) == 1:
            profile = contact.get("profile")
            return _text(profile.get("name")) if isinstance(profile, Mapping) else None
    return None


def _message(
    phone_number_id: str, message: Mapping[str, Any], contacts: list[Mapping[str, Any]]
) -> InboundMessage | None:
    """An `InboundMessage`, or None if the id, type or sender is missing."""
    message_id = _text(message.get("id"))
    message_type = _text(message.get("type"))
    sender_user_id = _text(message.get("from_user_id"))
    sender_phone = _text(message.get("from"))
    if not (message_id and message_type and (sender_user_id or sender_phone)):
        return None
    content = message.get(message_type)
    context = message.get("context")
    return InboundMessage(
        phone_number_id=phone_number_id,
        message_id=message_id,
        type=message_type,
        sender_user_id=sender_user_id,
        sender_phone=sender_phone,
        profile_name=_profile_name(contacts, message),
        payload=content if isinstance(content, Mapping) else {},
        context_message_id=_text(context.get("id")) if isinstance(context, Mapping) else None,
        timestamp=_integer(message.get("timestamp")),
    )


def _status(phone_number_id: str, status: Mapping[str, Any]) -> MessageStatusUpdate | None:
    """A `MessageStatusUpdate`, or None if the id or status is missing."""
    message_id = _text(status.get("id"))
    value = _text(status.get("status"))
    if not (message_id and value):
        return None
    codes = (_integer(error.get("code")) for error in _mappings(status.get("errors")))
    return MessageStatusUpdate(
        phone_number_id=phone_number_id,
        message_id=message_id,
        status=value,
        error_codes=tuple(code for code in codes if code is not None),
    )


def _messages_change(value: Mapping[str, Any]) -> list[MetaEvent]:
    """Events of a `messages` change: inbound messages and delivery statuses."""
    metadata = value.get("metadata")
    phone_number_id = (
        _text(metadata.get("phone_number_id")) if isinstance(metadata, Mapping) else None
    )
    if not phone_number_id:
        return []
    contacts = _mappings(value.get("contacts"))
    events: list[MetaEvent] = []
    for message in _mappings(value.get("messages")):
        if (event := _message(phone_number_id, message, contacts)) is not None:
            events.append(event)
    for status in _mappings(value.get("statuses")):
        if (update := _status(phone_number_id, status)) is not None:
            events.append(update)
    return events


def _account_change(entry: Mapping[str, Any], value: Mapping[str, Any]) -> list[MetaEvent]:
    """Event of an `account_update` change, or nothing if incomplete."""
    event = _text(value.get("event"))
    # PARTNER_REMOVED names the account in waba_info, the others only in entry.id.
    waba_info = value.get("waba_info")
    waba_id = _text(waba_info.get("waba_id")) if isinstance(waba_info, Mapping) else None
    waba_id = waba_id or _text(entry.get("id"))
    if not (event and waba_id):
        return []
    return [AccountUpdate(waba_id=waba_id, event=event, time=_integer(entry.get("time")))]


def _template_change(entry: Mapping[str, Any], value: Mapping[str, Any]) -> list[MetaEvent]:
    """Event of a `message_template_status_update` change, or nothing if incomplete."""
    waba_id = _text(entry.get("id"))
    name = _text(value.get("message_template_name"))
    language = _text(value.get("message_template_language"))
    status = _text(value.get("event"))
    if not (waba_id and name and language and status):
        return []
    return [TemplateStatusUpdate(waba_id=waba_id, name=name, language=language, status=status)]


def extract_events(payload: Mapping[str, Any]) -> list[MetaEvent]:
    """Lenient: a malformed item is skipped so the rest of the batch is not lost.

    Read fields: `messages`, `account_update` and `message_template_status_update`.
    The coexistence ones (`history`, `smb_message_echoes`, `smb_app_state_sync`) are ignored.
    """
    events: list[MetaEvent] = []
    for entry in _mappings(payload.get("entry")):
        for change in _mappings(entry.get("changes")):
            value = change.get("value")
            if not isinstance(value, Mapping):
                continue
            if change.get("field") == "messages":
                events.extend(_messages_change(value))
            elif change.get("field") == "account_update":
                events.extend(_account_change(entry, value))
            elif change.get("field") == "message_template_status_update":
                events.extend(_template_change(entry, value))
    return events
