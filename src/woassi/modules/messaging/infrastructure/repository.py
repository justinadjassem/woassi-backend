"""SQLAlchemy implementations of the messaging repositories."""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from woassi.modules.messaging.domain.outbound import OutboundMessage, OutboundStatus
from woassi.modules.messaging.domain.templates import MessageTemplate, TemplateStatus
from woassi.modules.messaging.infrastructure.models import (
    MessageTemplateModel,
    OutboundMessageModel,
)
from woassi.shared.infrastructure.idempotency import claim_key


class SqlAlchemyInbox:
    """`Inbox` on the shared `processed_keys` table."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim(self, source: str, key: str) -> bool:
        """Claims `key` in the current transaction; False if it was already processed."""
        return await claim_key(self._session, source, key)


def _copy(message: OutboundMessage, model: OutboundMessageModel) -> OutboundMessageModel:
    """Copies every field of `message` onto `model`, and returns `model`."""
    model.id = message.id
    model.shop_id = message.shop_id
    model.recipient = message.recipient
    model.kind = message.kind
    model.status = message.status.value
    model.error_code = message.error_code
    model.created_at = message.created_at
    model.updated_at = message.updated_at
    return model


class SqlAlchemyOutboundMessages:
    """`OutboundMessageRepository` on SQLAlchemy."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, message: OutboundMessage) -> None:
        self._session.add(_copy(message, OutboundMessageModel()))
        await self._session.flush()

    async def get(self, message_id: str) -> OutboundMessage | None:
        # Locked: two status webhooks for one message are applied one after the other.
        model = await self._session.get(OutboundMessageModel, message_id, with_for_update=True)
        if model is None:
            return None
        return OutboundMessage(
            id=model.id,
            shop_id=model.shop_id,
            recipient=model.recipient,
            kind=model.kind,
            created_at=model.created_at,
            status=OutboundStatus(model.status),
            error_code=model.error_code,
            updated_at=model.updated_at,
        )

    async def save(self, message: OutboundMessage) -> None:
        """Updates an existing message; raises LookupError if it does not exist."""
        model = await self._session.get(OutboundMessageModel, message.id)
        if model is None:
            raise LookupError(f"Outbound message {message.id} does not exist.")
        _copy(message, model)
        await self._session.flush()


def _template(model: MessageTemplateModel) -> MessageTemplate:
    """Converts a row to a domain `MessageTemplate`."""
    return MessageTemplate(
        shop_id=model.shop_id,
        waba_id=model.waba_id,
        name=model.name,
        language=model.language,
        status=TemplateStatus(model.status),
        updated_at=model.updated_at,
        meta_template_id=model.meta_template_id,
    )


class SqlAlchemyMessageTemplates:
    """`MessageTemplateRepository` on SQLAlchemy."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, shop_id: UUID, name: str, language: str) -> MessageTemplate | None:
        model = await self._session.get(MessageTemplateModel, (shop_id, name, language))
        return _template(model) if model else None

    async def list_by_waba(self, waba_id: str, name: str) -> Sequence[MessageTemplate]:
        models = await self._session.scalars(
            select(MessageTemplateModel).where(
                MessageTemplateModel.waba_id == waba_id, MessageTemplateModel.name == name
            )
        )
        return [_template(model) for model in models]

    async def save(self, template: MessageTemplate) -> None:
        key = (template.shop_id, template.name, template.language)
        model = await self._session.get(MessageTemplateModel, key)
        if model is None:
            model = MessageTemplateModel(
                shop_id=template.shop_id, name=template.name, language=template.language
            )
            self._session.add(model)
        model.waba_id = template.waba_id
        model.status = template.status.value
        model.meta_template_id = template.meta_template_id
        model.updated_at = template.updated_at
        await self._session.flush()
