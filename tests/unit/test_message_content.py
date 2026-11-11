from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from woassi.modules.messaging.domain.content import (
    ButtonsMessage,
    DocumentMessage,
    Recipient,
    ReplyButton,
    TemplateMessage,
    TextMessage,
)
from woassi.modules.messaging.domain.errors import InvalidMessage
from woassi.modules.messaging.domain.outbound import OutboundMessage, OutboundStatus
from woassi.shared.domain.values import PhoneNumber

NOW = datetime(2026, 1, 1, tzinfo=UTC)
YES, NO = ReplyButton("accept", "Valider"), ReplyButton("refuse", "Indisponible")


def test_recipient_needs_an_address() -> None:
    with pytest.raises(InvalidMessage):
        Recipient()
    assert Recipient(user_id="TG.1").address == "TG.1"
    assert Recipient(phone=PhoneNumber("+22890123456"), user_id="TG.1").address == "+22890123456"


def test_recipient_stays_out_of_repr() -> None:
    assert "22890123456" not in repr(Recipient(phone=PhoneNumber("+22890123456")))


@pytest.mark.parametrize("body", ["", "x" * 4097])
def test_text_limits(body: str) -> None:
    with pytest.raises(InvalidMessage):
        TextMessage(body)


def test_buttons_message() -> None:
    message = ButtonsMessage("Nouvelle commande", (YES, NO), footer="Woassi")
    assert len(message.buttons) == 2


@pytest.mark.parametrize(
    "build",
    [
        lambda: ButtonsMessage("x", ()),
        lambda: ButtonsMessage("x", (YES, NO, ReplyButton("a", "A"), ReplyButton("b", "B"))),
        lambda: ButtonsMessage("x", (YES, ReplyButton("other", "Valider"))),
        lambda: ButtonsMessage("x", (YES, ReplyButton("accept", "Autre"))),
        lambda: ButtonsMessage("", (YES,)),
        lambda: ButtonsMessage("x" * 1025, (YES,)),
        lambda: ButtonsMessage("x", (YES,), footer="x" * 61),
        lambda: ReplyButton("id", "x" * 21),
        lambda: ReplyButton("x" * 257, "ok"),
    ],
    ids=[
        "none",
        "four",
        "same-title",
        "same-id",
        "empty-body",
        "long-body",
        "long-footer",
        "long-title",
        "long-id",
    ],
)
def test_button_limits(build: object) -> None:
    with pytest.raises(InvalidMessage):
        build()  # type: ignore[operator]


@pytest.mark.parametrize(
    ("name", "language"), [("Order Request", "fr"), ("order", "french"), ("order", "")]
)
def test_template_limits(name: str, language: str) -> None:
    with pytest.raises(InvalidMessage):
        TemplateMessage(name, language)


def outbound() -> OutboundMessage:
    return OutboundMessage(
        id="wamid.1", shop_id=uuid4(), recipient="TG.1", kind="test", created_at=NOW
    )


def test_statuses_only_move_forward() -> None:
    message = outbound()
    assert message.apply(OutboundStatus.DELIVERED, None, NOW)
    # The "sent" webhook arrives late: it must not hide the delivery.
    assert not message.apply(OutboundStatus.SENT, None, NOW)
    assert message.apply(OutboundStatus.READ, None, NOW + timedelta(seconds=1))
    assert message.status is OutboundStatus.READ
    assert message.updated_at == NOW + timedelta(seconds=1)


def test_failure_is_final() -> None:
    message = outbound()
    assert message.apply(OutboundStatus.FAILED, 131047, NOW)
    assert message.error_code == 131047
    assert not message.apply(OutboundStatus.DELIVERED, None, NOW)
    assert message.status is OutboundStatus.FAILED


@pytest.mark.parametrize(
    ("link", "filename", "caption"),
    [
        ("ftp://files/a.pdf", "a.pdf", None),
        ("https://files/a.pdf", "", None),
        ("https://files/a.pdf", "invoice", None),
        ("https://files/a.pdf", "a.pdf", ""),
        ("https://files/a.pdf", "a.pdf", "x" * 1025),
    ],
)
def test_document_limits(link: str, filename: str, caption: str | None) -> None:
    with pytest.raises(InvalidMessage):
        DocumentMessage(link, filename, caption)


def test_document_link_stays_out_of_repr() -> None:
    message = DocumentMessage("https://files/a.pdf?X-Amz-Signature=secret", "a.pdf", "Ama")
    assert "secret" not in repr(message)
    assert "Ama" not in repr(message)
