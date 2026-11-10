"""Message templates: submission when a shop connects, and approval checks before sending."""

from collections.abc import Sequence
from uuid import UUID

import structlog

from woassi.modules.messaging.application.ports import (
    MessagingUnitOfWork,
    SendError,
    ShopConnection,
    ShopConnectionsPort,
    TemplateRegistry,
)
from woassi.modules.messaging.domain.event import TemplateStatusUpdate
from woassi.modules.messaging.domain.templates import (
    MessageTemplate,
    TemplateDefinition,
    TemplateStatus,
    same_language,
    status_from_meta,
)
from woassi.shared.application.ports import Clock

log = structlog.get_logger()


class ProvisionTemplates:
    """Submit Woassi's templates to a shop's account, after it connects its number.

    Idempotent: an existing template is only read back. Meta reviews new ones within 24 hours.
    """

    def __init__(
        self,
        uow: MessagingUnitOfWork,
        shops: ShopConnectionsPort,
        registry: TemplateRegistry,
        definitions: Sequence[TemplateDefinition],
        clock: Clock,
    ) -> None:
        self._uow = uow
        self._shops = shops
        self._registry = registry
        self._definitions = definitions
        self._clock = clock

    async def execute(self, shop_id: UUID) -> int:
        """Return how many templates are approved already."""
        connection = await self._shops.by_shop_id(shop_id)
        if connection is None:
            return 0
        approved = 0
        for definition in self._definitions:
            try:
                submitted = await self._registry.submit(connection, definition)
            except SendError as error:
                # One failure must not stop the others; the next send checks again.
                log.warning(
                    "template_submission_failed",
                    shop_id=str(shop_id),
                    template=definition.name,
                    language=definition.language,
                    meta_code=error.meta_code,
                )
                continue
            status = status_from_meta(submitted.status) or TemplateStatus.UNKNOWN
            await self._record(connection, definition.name, definition.language, status, submitted)
            approved += status is TemplateStatus.APPROVED
        return approved

    async def _record(
        self,
        connection: ShopConnection,
        name: str,
        language: str,
        status: TemplateStatus,
        submitted: object,
    ) -> None:
        """Stores the status Meta gave for one template."""
        async with self._uow as uow:
            await uow.templates.save(
                MessageTemplate(
                    shop_id=connection.shop_id,
                    waba_id=connection.waba_id,
                    name=name,
                    language=language,
                    status=status,
                    updated_at=self._clock.now(),
                    meta_template_id=getattr(submitted, "meta_template_id", None),
                )
            )
            await uow.commit()


class EnsureTemplateSendable:
    """Before a template goes out: checks the stored status, and asks Meta when it is not good.

    Asking again covers a missed status webhook, at the cost of one call per pending send.
    """

    def __init__(self, uow: MessagingUnitOfWork, registry: TemplateRegistry, clock: Clock) -> None:
        self._uow = uow
        self._registry = registry
        self._clock = clock

    async def execute(self, connection: ShopConnection, name: str, language: str) -> bool:
        """True if the template can be sent now.

        False if Meta has not approved it, does not know it, or cannot be reached.
        """
        async with self._uow as uow:
            template = await uow.templates.get(connection.shop_id, name, language)
        if template is not None and template.sendable:
            return True
        try:
            current = await self._registry.status(connection, name, language)
        except SendError:
            return False
        if current is None:
            return False
        status = status_from_meta(current.status) or TemplateStatus.UNKNOWN
        async with self._uow as uow:
            await uow.templates.save(
                MessageTemplate(
                    shop_id=connection.shop_id,
                    waba_id=connection.waba_id,
                    name=name,
                    language=language,
                    status=status,
                    updated_at=self._clock.now(),
                    meta_template_id=current.meta_template_id,
                )
            )
            await uow.commit()
        return status is TemplateStatus.APPROVED


async def apply_template_status(
    uow: MessagingUnitOfWork, update: TemplateStatusUpdate, clock: Clock
) -> int:
    """From the `message_template_status_update` webhook. Return how many records moved."""
    status = status_from_meta(update.status)
    if status is None:
        return 0
    async with uow:
        templates = [
            template
            for template in await uow.templates.list_by_waba(update.waba_id, update.name)
            if same_language(update.language, template.language)
        ]
        for template in templates:
            template.status = status
            template.updated_at = clock.now()
            await uow.templates.save(template)
        await uow.commit()
    return len(templates)
