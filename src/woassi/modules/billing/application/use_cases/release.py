"""Giving a document out: OTR certification when required, then the PDF, then WhatsApp.

Each step runs outside any transaction (certifier, rendering, storage, Meta), and its outcome is
written under the document's row lock, after checking again that it is still needed. Each step
is skipped once done, so a replayed job picks up where the previous one stopped.
"""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta

import structlog

from woassi.modules.billing.application.ports import (
    AccessToken,
    BillingUnitOfWork,
    Contact,
    CustomersPort,
    Document,
    DocumentRenderer,
    InvoiceCertifier,
    MessagingPort,
    Notification,
    NotificationNotSent,
    ShopsPort,
    Template,
)
from woassi.modules.billing.application.texts import (
    INVOICE_TEMPLATE,
    amount,
    language_of,
    text,
)
from woassi.modules.billing.domain.document import (
    BillingDocument,
    CertificationStatus,
    DocumentType,
)
from woassi.shared.application.ports import Clock, StoragePort

log = structlog.get_logger()

PDF = "application/pdf"
CUSTOMER_SERVICE_WINDOW = timedelta(hours=24)
WINDOW_MARGIN = timedelta(minutes=5)
# Meta downloads a document `link` when the message is sent; a day covers its retries.
DOCUMENT_LINK_TTL = timedelta(hours=24)
# How long the `/invoices/{token}` link of the template keeps working.
INVOICE_LINK_TTL = timedelta(days=30)
TO_CERTIFY = frozenset({CertificationStatus.PENDING, CertificationStatus.FAILED_RETRYABLE})


def token_hash(token: str) -> str:
    """SHA-256 (hex) of an invoice link token: the only form stored."""
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class BillingSettings:
    """`public_base_url` is the API's public URL, for `/invoices/{token}` links. Without it,
    the invoice only goes out inside the 24-hour window, as a document.
    """

    public_base_url: str | None = None


class DocumentRelease:
    """Certifies, renders, stores and sends billing documents."""

    def __init__(
        self,
        uow: BillingUnitOfWork,
        storage: StoragePort,
        renderer: DocumentRenderer,
        certifier: InvoiceCertifier,
        shops: ShopsPort,
        customers: CustomersPort,
        messaging: MessagingPort,
        clock: Clock,
        settings: BillingSettings,
    ) -> None:
        self._uow = uow
        self._storage = storage
        self._renderer = renderer
        self._certifier = certifier
        self._shops = shops
        self._customers = customers
        self._messaging = messaging
        self._clock = clock
        self._settings = settings

    async def prepare(self, document: BillingDocument) -> BillingDocument:
        """Certifies the document if it must be, then renders and stores its PDF once."""
        document = await self._certify(document)
        return await self._render(document)

    async def _certify(self, document: BillingDocument) -> BillingDocument:
        """The hook where the OTR plugs in. Nothing to do while NOT_REQUIRED."""
        if document.certification.status not in TO_CERTIFY:
            return document
        outcome = await self._certifier.certify(document)
        async with self._uow as uow:
            current = await uow.documents.get(document.shop_id, document.id, lock=True)
            if current is None or current.certification.status not in TO_CERTIFY:
                return current or document
            current.certification_changed(outcome, self._clock.now())
            await uow.documents.save(current)
            await uow.commit()
        log.info(
            "billing_document_certification",
            document_id=str(current.id),
            status=current.certification.status.value,
        )
        return current

    async def _render(self, document: BillingDocument) -> BillingDocument:
        """Renders and uploads the PDF, unless it already exists or may not be released yet."""
        if document.pdf_key is not None or not document.releasable:
            return document
        profile = await self._shops.profile(document.shop_id)
        logo = await self._shops.logo(profile) if profile else None
        pdf = await self._renderer.render(document, logo)
        key = document.expected_pdf_key
        # Same key on a replay: an upload that was never attached is simply replaced.
        await self._storage.put(key, pdf, PDF)
        async with self._uow as uow:
            current = await uow.documents.get(document.shop_id, document.id, lock=True)
            if current is None:
                return document
            if current.attach_pdf(key, hashlib.sha256(pdf).hexdigest(), self._clock.now()):
                await uow.documents.save(current)
                await uow.commit()
        return current

    async def deliver(self, document: BillingDocument) -> bool:
        """Sends the invoice to the customer. Never raises: a failure is recorded and logged."""
        if (
            document.document_type is not DocumentType.INVOICE
            or document.pdf_key is None
            or not document.releasable
        ):
            return False
        contact = (
            await self._customers.contact(document.shop_id, document.buyer.customer_id)
            if document.buyer.customer_id
            else Contact()
        )
        message_id = None
        if contact.user_id or contact.phone:
            message_id = await self._send(document, contact)
        async with self._uow as uow:
            current = await uow.documents.get(document.shop_id, document.id, lock=True)
            if current is not None:
                if message_id:
                    current.sent(message_id, self._clock.now())
                else:
                    current.not_sent(self._clock.now())
                await uow.documents.save(current)
                await uow.commit()
        if message_id is None:
            log.warning(
                "invoice_not_sent", document_id=str(document.id), shop_id=str(document.shop_id)
            )
        return message_id is not None

    async def _send(self, document: BillingDocument, contact: Contact) -> str | None:
        """The PDF itself inside the 24-hour window, else the template with a link."""
        language = language_of(document.language)
        total = amount(document.total)
        if self._inside_window(contact):
            assert document.pdf_key is not None
            link = await self._storage.presigned_get(
                document.pdf_key, int(DOCUMENT_LINK_TTL.total_seconds())
            )
            caption = text(language, "caption", number=document.number, total=total)
            sent = await self._try(
                document, contact, Document(link, f"{document.number}.pdf", caption)
            )
            if sent:
                return sent
        if not self._settings.public_base_url:
            return None
        template = Template(
            name=INVOICE_TEMPLATE,
            language=language,
            parameters={
                "invoice_number": document.number,
                "total": total,
                "link": await self._public_link(document),
            },
        )
        return await self._try(document, contact, template)

    async def _try(
        self, document: BillingDocument, contact: Contact, notification: Notification
    ) -> str | None:
        try:
            return await self._messaging.send(
                document.shop_id, contact, notification, kind="billing.invoice"
            )
        except NotificationNotSent:
            return None

    async def _public_link(self, document: BillingDocument) -> str:
        """A new `/invoices/{token}` link. Stored before sending, so it works when tapped."""
        token = secrets.token_urlsafe(32)
        now = self._clock.now()
        async with self._uow as uow:
            await uow.documents.add_access_token(
                AccessToken(
                    token_hash=token_hash(token),
                    document_id=document.id,
                    shop_id=document.shop_id,
                    created_at=now,
                    expires_at=now + INVOICE_LINK_TTL,
                )
            )
            await uow.commit()
        assert self._settings.public_base_url
        return f"{self._settings.public_base_url.rstrip('/')}/invoices/{token}"

    def _inside_window(self, contact: Contact) -> bool:
        last = contact.last_message_at
        if last is None:
            return False
        return self._clock.now() - last < CUSTOMER_SERVICE_WINDOW - WINDOW_MARGIN
