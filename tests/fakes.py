"""In-memory fake adapters for testing use cases without I/O."""

from collections import Counter
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Self
from uuid import UUID, uuid4

from woassi.modules.accounts.domain.account import Account
from woassi.modules.accounts.domain.errors import PhoneAlreadyUsed
from woassi.modules.accounts.domain.refresh_token import RefreshToken
from woassi.modules.accounts.domain.verification_code import CodePurpose, VerificationCode
from woassi.modules.catalog.domain.errors import UnreadableImage
from woassi.modules.catalog.domain.product import MetaSyncStatus, Product
from woassi.modules.messaging.application.ports import ShopConnection, SubmittedTemplate
from woassi.modules.messaging.domain.event import InboundMessage, MetaEvent
from woassi.modules.messaging.domain.outbound import OutboundMessage
from woassi.modules.messaging.domain.templates import MessageTemplate, TemplateDefinition
from woassi.modules.shops.domain.errors import PhoneNumberAlreadyConnected, SlugTaken
from woassi.modules.shops.domain.shop import Shop, WhatsAppStatus
from woassi.shared.application.ports import IdempotencyRecord, ObjectInfo, StoredResponse
from woassi.shared.domain.audit import AuditEntry
from woassi.shared.domain.errors import ObjectNotFound
from woassi.shared.domain.events import DomainEvent, Entity
from woassi.shared.domain.values import PhoneNumber

NOW = datetime(2026, 1, 1, 9, tzinfo=UTC)


class FakeStorage:
    def __init__(self, public_base: str | None = None) -> None:
        self.objects: dict[str, bytes] = {}
        self.public_base = public_base

    async def presigned_put(self, key: str, content_type: str, expire_s: int = 600) -> str:
        return f"http://signed/put/{key}?expire={expire_s}"

    async def presigned_get(self, key: str, expire_s: int = 3600) -> str:
        return f"http://signed/get/{key}?expire={expire_s}"

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        self.objects[key] = data

    async def get(self, key: str) -> bytes:
        if key not in self.objects:
            raise ObjectNotFound(key)
        return self.objects[key]

    async def stat(self, key: str) -> ObjectInfo | None:
        if key not in self.objects:
            return None
        return ObjectInfo(key=key, size=len(self.objects[key]), content_type=None)

    async def delete(self, key: str) -> None:
        self.objects.pop(key, None)

    def public_url(self, key: str) -> str:
        assert self.public_base is not None
        return f"{self.public_base}/{key}"


class FakeProductRepository:
    def __init__(self) -> None:
        self.products: dict[UUID, Product] = {}
        self.pending: dict[UUID, Product] = {}
        self.tracked: list[Product] = []

    def _stage(self, product: Product) -> None:
        stored = deepcopy(product)
        stored.pull_events()
        self.pending[product.id] = stored
        self.tracked.append(product)

    async def add(self, product: Product) -> None:
        self._stage(product)

    async def get(self, shop_id: UUID, product_id: UUID) -> Product | None:
        product = self.products.get(product_id)
        return deepcopy(product) if product and product.shop_id == shop_id else None

    async def get_by_photo(self, photo_id: UUID) -> Product | None:
        for product in self.products.values():
            if any(photo.id == photo_id for photo in product.photos):
                return deepcopy(product)
        return None

    async def list(self, shop_id: UUID, offset: int, limit: int) -> Sequence[Product]:
        products = [p for p in self.products.values() if p.shop_id == shop_id]
        return deepcopy(products[offset : offset + limit])

    async def count(self, shop_id: UUID) -> int:
        return len([p for p in self.products.values() if p.shop_id == shop_id])

    async def list_for_update(
        self, shop_id: UUID, *, retailer_ids: Sequence[str] = (), ids: Sequence[UUID] = ()
    ) -> Sequence[Product]:
        products = [
            p
            for p in self.products.values()
            if p.shop_id == shop_id and (p.retailer_id in retailer_ids or p.id in ids)
        ]
        return deepcopy(sorted(products, key=lambda p: p.id))

    async def list_shop_for_update(self, shop_id: UUID) -> Sequence[Product]:
        products = [p for p in self.products.values() if p.shop_id == shop_id]
        return deepcopy(sorted(products, key=lambda p: p.id))

    async def list_meta_sync_due(
        self, shop_id: UUID, now: datetime, limit: int
    ) -> Sequence[Product]:
        due = [
            p
            for p in sorted(self.products.values(), key=lambda p: p.id)
            if p.shop_id == shop_id
            and p.meta_sync.next_attempt_at is not None
            and (
                p.meta_sync.status is MetaSyncStatus.PENDING
                or (
                    p.meta_sync.status is MetaSyncStatus.FAILED
                    and p.meta_sync.next_attempt_at <= now
                )
            )
        ]
        return deepcopy(due[:limit])

    async def shops_with_meta_sync_due(self, now: datetime, limit: int) -> Sequence[UUID]:
        shop_ids = {
            p.shop_id
            for p in self.products.values()
            if p.meta_sync.status is not MetaSyncStatus.SYNCED
            and p.meta_sync.next_attempt_at is not None
            and p.meta_sync.next_attempt_at <= now
        }
        return sorted(shop_ids)[:limit]

    async def save(self, product: Product) -> None:
        self._stage(product)


class FakeAuditLog:
    def __init__(self) -> None:
        self.entries: list[AuditEntry] = []

    async def record(
        self,
        *,
        account_id: UUID,
        action: str,
        target_type: str,
        target_id: str,
        shop_id: UUID | None = None,
        details: Mapping[str, Any] | None = None,
        ip: str | None = None,
    ) -> None:
        self.entries.append(
            AuditEntry(
                id=uuid4(),
                account_id=account_id,
                shop_id=shop_id,
                action=action,
                target_type=target_type,
                target_id=target_id,
                details=details or {},
                ip=ip,
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )

    async def list(self, shop_id: UUID, offset: int, limit: int) -> Sequence[AuditEntry]:
        entries = [e for e in self.entries if e.shop_id == shop_id]
        return entries[offset : offset + limit]

    async def count(self, shop_id: UUID) -> int:
        return len([e for e in self.entries if e.shop_id == shop_id])


class FakeIdempotencyStore:
    def __init__(self) -> None:
        self.records: dict[str, IdempotencyRecord] = {}

    async def begin(self, key: str, fingerprint: str) -> IdempotencyRecord | None:
        if key in self.records:
            return self.records[key]
        self.records[key] = IdempotencyRecord(fingerprint, None)
        return None

    async def complete(self, key: str, fingerprint: str, response: StoredResponse) -> None:
        self.records[key] = IdempotencyRecord(fingerprint, response)

    async def release(self, key: str) -> None:
        self.records.pop(key, None)


class FakeUoW:
    """Like a real transaction: nothing is persisted without a commit."""

    def __init__(self) -> None:
        self.repository = FakeProductRepository()
        self.audit_log = FakeAuditLog()
        self.events: list[DomainEvent] = []

    @property
    def products(self) -> FakeProductRepository:
        return self.repository

    @property
    def audit(self) -> FakeAuditLog:
        return self.audit_log

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.rollback()

    async def commit(self) -> None:
        self.repository.products.update(self.repository.pending)
        self.repository.pending.clear()
        for product in self.repository.tracked:
            self.events.extend(product.pull_events())
        self.repository.tracked.clear()

    async def rollback(self) -> None:
        self.repository.pending.clear()
        self.repository.tracked.clear()


class FakeJobQueue:
    def __init__(self) -> None:
        self.photos: list[UUID] = []

    async def enqueue_processing(self, photo_id: UUID) -> None:
        self.photos.append(photo_id)


class FakeCatalogSyncQueue:
    def __init__(self) -> None:
        self.shops: list[UUID] = []

    async def enqueue_sync(self, shop_id: UUID) -> None:
        self.shops.append(shop_id)


class FakeImageProcessor:
    def __init__(self, *, unreadable: bool = False) -> None:
        self.unreadable = unreadable

    async def webp_variants(self, data: bytes, sizes: Sequence[int]) -> dict[int, bytes]:
        if self.unreadable:
            raise UnreadableImage("unreadable")
        return {size: f"webp-{size}".encode() for size in sizes}


class FakeEventQueue:
    def __init__(self) -> None:
        self.events: list[MetaEvent] = []

    async def enqueue(self, event: MetaEvent) -> None:
        self.events.append(event)


@dataclass
class AccountsState:
    accounts: dict[UUID, Account] = field(default_factory=dict)
    tokens: dict[UUID, RefreshToken] = field(default_factory=dict)
    codes: dict[UUID, VerificationCode] = field(default_factory=dict)


class FakeAccountRepository:
    def __init__(self, uow: "FakeAccountsUoW") -> None:
        self._uow = uow

    async def add(self, account: Account) -> None:
        if await self.get_by_phone(account.phone) is not None:
            raise PhoneAlreadyUsed("This phone number is already registered.")
        self._store(account)

    def _store(self, account: Account) -> None:
        # Like a database row, the stored copy carries no pending event.
        stored = deepcopy(account)
        stored.pull_events()
        self._uow.state.accounts[account.id] = stored
        self._uow.track(account)

    async def get(self, account_id: UUID) -> Account | None:
        return deepcopy(self._uow.state.accounts.get(account_id))

    async def get_by_phone(self, phone: PhoneNumber) -> Account | None:
        for account in self._uow.state.accounts.values():
            if account.phone == phone:
                return deepcopy(account)
        return None

    async def save(self, account: Account) -> None:
        self._store(account)


class FakeRefreshTokenStore:
    def __init__(self, uow: "FakeAccountsUoW") -> None:
        self._uow = uow

    async def add(self, token: RefreshToken) -> None:
        self._uow.state.tokens[token.id] = token

    async def get_by_hash(self, token_hash: str) -> RefreshToken | None:
        for token in self._uow.state.tokens.values():
            if token.token_hash == token_hash:
                return token
        return None

    async def consume(self, token_id: UUID, now: datetime) -> bool:
        token = self._uow.state.tokens[token_id]
        if token.consumed_at or token.revoked_at:
            return False
        self._uow.state.tokens[token_id] = replace(token, consumed_at=now)
        return True

    def _revoke(self, now: datetime, *ids: UUID) -> None:
        for token_id in ids:
            token = self._uow.state.tokens[token_id]
            if token.revoked_at is None:
                self._uow.state.tokens[token_id] = replace(token, revoked_at=now)

    async def revoke(self, token_id: UUID, now: datetime) -> None:
        self._revoke(now, token_id)

    async def revoke_family(self, family_id: UUID, now: datetime) -> None:
        tokens = self._uow.state.tokens.values()
        self._revoke(now, *[t.id for t in tokens if t.family_id == family_id])

    async def revoke_all(self, account_id: UUID, now: datetime) -> None:
        tokens = self._uow.state.tokens.values()
        self._revoke(now, *[t.id for t in tokens if t.account_id == account_id])


class FakeVerificationCodeStore:
    def __init__(self, uow: "FakeAccountsUoW") -> None:
        self._uow = uow

    async def add(self, code: VerificationCode) -> None:
        self._uow.state.codes[code.id] = deepcopy(code)

    async def get_latest(self, account_id: UUID, purpose: CodePurpose) -> VerificationCode | None:
        codes = [
            code
            for code in self._uow.state.codes.values()
            if code.account_id == account_id and code.purpose is purpose
        ]
        return deepcopy(codes[-1]) if codes else None

    async def save(self, code: VerificationCode) -> None:
        self._uow.state.codes[code.id] = deepcopy(code)

    async def invalidate(self, account_id: UUID, purpose: CodePurpose, now: datetime) -> None:
        for code in self._uow.state.codes.values():
            if code.account_id == account_id and code.purpose is purpose and not code.consumed_at:
                code.consume(now)


class FakeAccountsUoW:
    """Works on a copy of the database: what is not committed disappears, like a rollback."""

    def __init__(self, database: AccountsState, events: list[DomainEvent]) -> None:
        self.database = database
        self.state = deepcopy(database)
        self.events = events
        self.tracked: list[Entity] = []
        self.audit_log = FakeAuditLog()

    @property
    def accounts(self) -> FakeAccountRepository:
        return FakeAccountRepository(self)

    @property
    def refresh_tokens(self) -> FakeRefreshTokenStore:
        return FakeRefreshTokenStore(self)

    @property
    def verification_codes(self) -> FakeVerificationCodeStore:
        return FakeVerificationCodeStore(self)

    @property
    def audit(self) -> FakeAuditLog:
        return self.audit_log

    def track(self, entity: Entity) -> None:
        self.tracked.append(entity)

    async def __aenter__(self) -> Self:
        self.state = deepcopy(self.database)
        self.tracked = []
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.rollback()

    async def commit(self) -> None:
        self.database.accounts = deepcopy(self.state.accounts)
        self.database.tokens = deepcopy(self.state.tokens)
        self.database.codes = deepcopy(self.state.codes)
        for entity in self.tracked:
            self.events.extend(entity.pull_events())
        self.tracked = []

    async def rollback(self) -> None:
        self.state = deepcopy(self.database)
        self.tracked = []


class FakePasswordHasher:
    """Instant and reversible: the real one takes 80 ms per call."""

    async def hash(self, password: str) -> str:
        return f"hashed:{password}"

    async def verify(self, password: str, password_hash: str) -> bool:
        return password_hash == f"hashed:{password}"

    async def dummy_verify(self, password: str) -> None:
        return None


class FakeCodeSender:
    def __init__(self) -> None:
        self.sent: list[tuple[PhoneNumber, str, CodePurpose]] = []

    async def send(self, phone: PhoneNumber, code: str, purpose: CodePurpose) -> None:
        self.sent.append((phone, code, purpose))

    @property
    def last_code(self) -> str:
        return self.sent[-1][1]


class FakeRateLimiter:
    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()

    async def hit(self, key: str, limit: int, window_s: int) -> int | None:
        self.counts[key] += 1
        return None if self.counts[key] <= limit else window_s

    async def forgive(self, key: str) -> None:
        self.counts[key] -= 1


class FakeShopRepository:
    def __init__(self, uow: "FakeShopsUoW") -> None:
        self._uow = uow

    def _visible(self) -> list[Shop]:
        return [s for s in self._uow.state.values() if s.archived_at is None]

    def _store(self, shop: Shop) -> None:
        stored = deepcopy(shop)
        stored.pull_events()
        self._uow.state[shop.id] = stored
        self._uow.tracked.append(shop)

    async def add(self, shop: Shop) -> None:
        if any(other.slug == shop.slug for other in self._uow.state.values()):
            raise SlugTaken(f"Slug {shop.slug} is taken.")
        self._store(shop)

    async def get(self, shop_id: UUID) -> Shop | None:
        shop = self._uow.state.get(shop_id)
        return deepcopy(shop) if shop and shop.archived_at is None else None

    async def get_by_phone_number_id(self, phone_number_id: str) -> Shop | None:
        for shop in self._visible():
            if shop.whatsapp and shop.whatsapp.phone_number_id == phone_number_id:
                return deepcopy(shop)
        return None

    async def list(self, account_id: UUID, offset: int, limit: int) -> Sequence[Shop]:
        shops = [s for s in self._visible() if s.account_id == account_id]
        return deepcopy(shops[offset : offset + limit])

    async def count(self, account_id: UUID) -> int:
        return len([s for s in self._visible() if s.account_id == account_id])

    async def list_connected(self) -> Sequence[Shop]:
        return deepcopy(
            [
                shop
                for shop in self._visible()
                if shop.whatsapp and shop.whatsapp.status is WhatsAppStatus.CONNECTED
            ]
        )

    async def list_by_waba_id(self, waba_id: str) -> Sequence[Shop]:
        return deepcopy(
            [s for s in self._visible() if s.whatsapp and s.whatsapp.waba_id == waba_id]
        )

    async def save(self, shop: Shop) -> None:
        number = shop.whatsapp.phone_number_id if shop.whatsapp else None
        for other in self._uow.state.values():
            other_number = other.whatsapp.phone_number_id if other.whatsapp else None
            if other.id != shop.id and number and other_number == number:
                raise PhoneNumberAlreadyConnected("Taken.")
        self._store(shop)


class FakeShopsUoW:
    """Works on a copy of the database: what is not committed disappears, like a rollback."""

    def __init__(
        self, database: dict[UUID, Shop], events: list[DomainEvent], audit_log: FakeAuditLog
    ) -> None:
        self.database = database
        self.state = deepcopy(database)
        self.events = events
        self.audit_log = audit_log
        self.tracked: list[Entity] = []

    @property
    def shops(self) -> FakeShopRepository:
        return FakeShopRepository(self)

    @property
    def audit(self) -> FakeAuditLog:
        return self.audit_log

    async def __aenter__(self) -> Self:
        self.state = deepcopy(self.database)
        self.tracked = []
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.rollback()

    async def commit(self) -> None:
        self.database.clear()
        self.database.update(deepcopy(self.state))
        for entity in self.tracked:
            self.events.extend(entity.pull_events())
        self.tracked = []

    async def rollback(self) -> None:
        self.state = deepcopy(self.database)
        self.tracked = []


@dataclass
class MessagingState:
    claimed: set[tuple[str, str]] = field(default_factory=set)
    outbound: dict[str, OutboundMessage] = field(default_factory=dict)
    templates: dict[tuple[UUID, str, str], MessageTemplate] = field(default_factory=dict)


class FakeMessageTemplates:
    def __init__(self, uow: "FakeMessagingUoW") -> None:
        self._uow = uow

    async def get(self, shop_id: UUID, name: str, language: str) -> MessageTemplate | None:
        return deepcopy(self._uow.state.templates.get((shop_id, name, language)))

    async def list_by_waba(self, waba_id: str, name: str) -> Sequence[MessageTemplate]:
        return [
            deepcopy(t)
            for t in self._uow.state.templates.values()
            if t.waba_id == waba_id and t.name == name
        ]

    async def save(self, template: MessageTemplate) -> None:
        key = (template.shop_id, template.name, template.language)
        self._uow.state.templates[key] = deepcopy(template)


class FakeTemplateRegistry:
    """Meta's side: what was submitted, and the status Meta gives back."""

    def __init__(self, status: str = "APPROVED") -> None:
        self.status_value = status
        self.submitted: list[TemplateDefinition] = []
        self.lookups = 0

    async def submit(
        self, connection: ShopConnection, definition: TemplateDefinition
    ) -> SubmittedTemplate:
        self.submitted.append(definition)
        return SubmittedTemplate(meta_template_id=str(len(self.submitted)), status="PENDING")

    async def status(
        self, connection: ShopConnection, name: str, language: str
    ) -> SubmittedTemplate | None:
        self.lookups += 1
        return SubmittedTemplate(meta_template_id="1", status=self.status_value)


class FakeInbox:
    def __init__(self, uow: "FakeMessagingUoW") -> None:
        self._uow = uow

    async def claim(self, source: str, key: str) -> bool:
        if (source, key) in self._uow.state.claimed:
            return False
        self._uow.state.claimed.add((source, key))
        return True


class FakeOutboundMessages:
    def __init__(self, uow: "FakeMessagingUoW") -> None:
        self._uow = uow

    async def add(self, message: OutboundMessage) -> None:
        self._uow.state.outbound[message.id] = deepcopy(message)

    async def get(self, message_id: str) -> OutboundMessage | None:
        return deepcopy(self._uow.state.outbound.get(message_id))

    async def save(self, message: OutboundMessage) -> None:
        self._uow.state.outbound[message.id] = deepcopy(message)


class FakeMessagingUoW:
    """Works on a copy of the database: what is not committed disappears, like a rollback."""

    def __init__(self, database: MessagingState) -> None:
        self.database = database
        self.state = deepcopy(database)
        self.audit_log = FakeAuditLog()

    @property
    def inbox(self) -> FakeInbox:
        return FakeInbox(self)

    @property
    def outbound(self) -> FakeOutboundMessages:
        return FakeOutboundMessages(self)

    @property
    def templates(self) -> FakeMessageTemplates:
        return FakeMessageTemplates(self)

    @property
    def audit(self) -> FakeAuditLog:
        return self.audit_log

    async def __aenter__(self) -> Self:
        self.state = deepcopy(self.database)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.rollback()

    async def commit(self) -> None:
        self.database.claimed = set(self.state.claimed)
        self.database.outbound = deepcopy(self.state.outbound)
        self.database.templates = deepcopy(self.state.templates)

    async def rollback(self) -> None:
        self.state = deepcopy(self.database)


class FakeShopConnections:
    def __init__(self, *connections: ShopConnection) -> None:
        self.connections = list(connections)

    async def by_phone_number_id(self, phone_number_id: str) -> ShopConnection | None:
        return next((c for c in self.connections if c.phone_number_id == phone_number_id), None)

    async def by_shop_id(self, shop_id: UUID) -> ShopConnection | None:
        return next((c for c in self.connections if c.shop_id == shop_id), None)


class FakeWhatsAppAccounts:
    def __init__(self) -> None:
        self.removed: list[str] = []

    async def account_removed(self, waba_id: str) -> int:
        self.removed.append(waba_id)
        return 1


class RecordingInboundHandler:
    def __init__(self, *, failures: int = 0) -> None:
        self.handled: list[tuple[UUID, InboundMessage]] = []
        self.failures = failures

    async def handle(self, shop_id: UUID, message: InboundMessage) -> None:
        if self.failures:
            self.failures -= 1
            raise ConnectionError("orders is down")
        self.handled.append((shop_id, message))
