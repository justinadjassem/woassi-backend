# Woassi backend

WhatsApp commerce platform for Togolese merchants. An Account owns Shops; each Shop has its own
WhatsApp number, products, customers, orders and invoice numbering. A single Meta webhook routes
events by `phone_number_id`.

Woassi never holds funds. Amounts are in XOF: integers, no cents (`Amount`).

## Commands

Everything goes through `uv` (never pip or poetry).

```bash
uv run ruff check . && uv run ruff format --check .
uv run lint-imports
uv run pytest                 # unit + e2e
uv run pytest -m integration  # needs Docker
uv run mypy src
docker compose up -d && uv run alembic upgrade head
```

The Compose Postgres is exposed on host port **5433** (5432 is taken locally).

## Architecture rules (non-negotiable)

Clean architecture + modular monolith. Each module in `src/woassi/modules/` has 4 layers:
`domain`, `application`, `infrastructure`, `api`.

1. `domain` imports nothing external: dataclasses and Python types only.
2. `application` depends on the domain and on ports (`typing.Protocol`). A use case is a class
   with `async execute(...)`; its ports come through the constructor.
3. `infrastructure` implements the ports (SQLAlchemy, MinIO, httpx, arq, Pillow).
4. `api` (FastAPI routers + Pydantic schemas) calls use cases, never repositories.
5. Modules do not import each other: they talk through ports or domain events.
6. Ports are bound to adapters only in `src/woassi/container.py`.
7. All business routes are scoped under `/shops/{shop_id}/...`.

`uv run lint-imports` enforces these rules (contracts in `pyproject.toml`).

## Talking between modules

- **Synchronous call:** the consumer declares what it needs in its own `application/ports.py`
  (e.g. `orders` declares `CatalogPort.reserve_stock(...)`). The implementation lives in
  `src/woassi/integration/`, outside the modules, and calls the provider's use cases. It is bound
  in `container.py`. Modules never import `integration`, and `integration` never imports a
  module's `api` or `infrastructure`.
- **Domain events:** entities inherit from `Entity` and call `record_event(...)`. Repositories
  track saved entities with the Unit of Work's `_track`, which publishes `pull_events()` after the
  commit, and drops them on rollback. Module UoWs inherit from `SqlAlchemyUnitOfWork`.
- **Subscribers** are registered in `Container._register_subscribers`, which runs in both the API
  and the worker: `bus.subscribe(Event, handler, name="billing.issue_invoice", deferred=True)`.
  A `deferred` subscriber runs in the worker through the `dispatch_event` job. Its `name` is stored
  in queued jobs: keep it stable. An event class must be frozen and `kw_only`, and it is pickled.
- Events are delivered at most once: a crash right after a commit loses them, a failing subscriber
  is only logged, and `dispatch_event` is not retried.

## Shared foundation

- **Secrets:** always go through the `SecretCipher` port (`Container.cipher`), never store a Meta
  token or aggregator credential in clear text.
- **Audit:** `uow.audit.record(...)` before `uow.commit()`, so that the entry shares the action's
  transaction. Actions look like `order.validated`, `invoice.issued`,
  `receiving_account.updated`. The log is append-only: read it with
  `GET /shops/{shop_id}/audit-log`.
- **Webhooks and jobs:** `async with process_once(session, source, key) as is_new:` runs the block
  once per key. The key joins the caller's transaction, so the caller must commit.
- **Rate limits:** `enforce(limiter, key, limit, window_s)`, 429 with `Retry-After`.
- **Sensitive POST routes:** `dependencies=[Depends(idempotent)]` adds the optional
  `Idempotency-Key` header. Not applied to any route yet.
- **Time:** inject `Clock`, never call `datetime.now()`. Store `now()` (UTC); `local_now()`
  (Africa/Lome) is for display. Use `FrozenClock` in tests.
- **Lists:** routes return `Page[T]` built from a use case's `PageResult[T]`, with the shared
  `Offset` and `Limit` query parameters.
- **Phone numbers:** `PhoneNumber` holds E.164 only; `PhoneNormalizer` parses free-form input,
  with Togo as the default country.

## Naming

All code is in English: identifiers, classes, files, URLs, tables, error codes, messages and
comments. Prompts may use French business terms; translate them with this glossary:

| French          | Code            | French          | Code          |
| --------------- | --------------- | --------------- | ------------- |
| Compte          | Account         | Facture         | Invoice       |
| Boutique        | Shop            | Avoir           | Credit note   |
| Client          | Customer        | Paiement        | Payment       |
| Commande        | Order           | Montant         | Amount        |
| Produit         | Product         | Messagerie      | Messaging     |

API messages are for developers: a user-facing French UI should map from the stable `code`.

## Accounts and access control

- `shared/api/deps.py` provides `CurrentAccount` and `get_authorized_shop`. Every shop route uses
  `dependencies=[Depends(get_authorized_shop)]`. A foreign shop gives a 404, never a 403. Webhooks
  stay public: they check their own signature.
- `shared` cannot import `accounts` or `shops`, so it declares `AccountAuthenticator` and
  `ShopOwnershipPort`, implemented in `woassi/integration`.
- Refresh tokens: only their SHA-256 is stored. `RefreshTokenStore.consume` is one atomic UPDATE.
  Replaying a consumed token revokes its family; that revocation is committed before raising.
  Password change and reset revoke every session of the account.
- Verification codes are hashed with the password hasher. A failed attempt is committed before
  raising, otherwise it would not count.
- Login failures are neutral (`auth.invalid_credentials`), and timing too: use `dummy_verify` when
  there is nothing to compare with. Reset requests answer 202 whether or not the phone exists.
- Rate limits count failed logins only, as many users share an IP behind a mobile carrier's NAT.
  `RateLimiter.hit` comes first, as it is atomic, and `forgive` gives the attempt back on success.
- Access tokens cannot be revoked: they stay valid for up to 15 minutes after a logout.

## Shops and WhatsApp

- `Shop` is an aggregate: settings, WhatsApp account and receiving account load and save with it.
  `ShopRepository` hides archived shops, so `DELETE` leaves every route answering 404.
- A shop is `ACTIVE` only with a `CONNECTED` number; losing the number sends it back to `DRAFT`.
- `phone_number_id` comes from the browser. It is only written once Meta has confirmed it with the
  merchant's token, otherwise anyone could squat someone else's number with a fake code.
- Graph calls live in `shops/infrastructure/meta_onboarding.py`, each with a link to its doc, and
  run outside any transaction. Check the doc again when changing `META_GRAPH_VERSION`.
- **Graph secrets travel in query strings.** Every Graph call goes through
  `shared/infrastructure/meta_graph.py` (`GraphClient`), which never lets a URL or an httpx
  exception out. `httpx` logging is silenced, Sentry's httpx integration is disabled, and
  `MetaError` and `SendError` carry no Meta text.
- `MetaError` is only a shop failure when Meta says so: the daily token check ignores outages.
- Coexistence: skip `POST /{phone}/register`, and `smb_app_data` works once per sync type, within
  24 hours. Numbers not on `platform_type=CLOUD_API` are refused, as `/register` is not implemented.
- A receiving account receives the money: changing its operator or number resets `verified`.
- No FK across modules: it would tie them together, and shops and accounts are never deleted.

## Messaging

- The worker job `process_meta_event` runs `HandleMetaEvent`. The API answers Meta 200 as soon as
  events are queued, so Meta will not resend: the worker retries by itself (`Retry`, 5 tries).
- A message key is claimed in the same transaction as its handling: if the handler raises, the
  key is rolled back and the message is retried; a concurrent duplicate waits, then skips.
  Handlers must still be idempotent on `message_id`: a crash between their commit and ours
  replays the message.
- **Customers are identified by `sender_user_id` (BSUID), always sent.** `sender_phone` is missing
  for customers with a WhatsApp username. A BSUID is scoped to the business portfolio, and changes
  with the customer's phone number (`system` message `user_changed_number`).
- Other modules send through `SendWhatsAppMessage` (via a port in `integration/`), which records
  each message in `outbound_messages`; status webhooks only move a status forward.
- Outside the 24-hour customer service window only an approved `TemplateMessage` goes through
  (`OutsideCustomerServiceWindow`). The merchant's own notification number is usually outside it.
- The sender retries only when Meta certainly sent nothing: connection refused, `130429`, `131056`.
  A timeout is never retried, as the customer could get the message twice.
- `PARTNER_REMOVED`, `ACCOUNT_DELETED` and `ACCOUNT_OFFBOARDED` disconnect the account's shops.
- Message templates are submitted to each shop's account when it connects (deferred subscriber
  `messaging.provision_templates`), in every language of `orders/application/texts.py`. Meta
  reviews them within 24 hours. `SendWhatsAppMessage` refuses a template that is not `APPROVED`
  (`TemplateNotApproved`), after asking Meta once in case a status webhook was missed.

## Orders

- `integration/orders.py` turns inbound messages into orders: a cart (`order`) goes to
  `ReceiveCart`, a tapped button (`button` for templates, `interactive` otherwise) to
  `ReplyToOrder`. Anything else only updates the customer, which also tracks the 24-hour window.
- Flow: stock is reserved on receipt. Part of the cart missing → `AWAITING_CUSTOMER` with
  "take the rest?" buttons. Then the shop's `validation_mode`: `MANUAL` (default) waits for the
  merchant; `AUTOMATIC` confirms, unless the total is above `auto_validation_max_amount`.
- **A button reply is only trusted if its `context_message_id` is the message we sent** for that
  order: anyone can type `order:accept:<id>`. Merchant buttons answer the validation template,
  customer buttons the offer.
- Closing (refused, cancelled, expired) gives the stock back. `OrderConfirmed` asks for the
  payment; a CONFIRMED or PAID order can no longer be closed.
- The cron `process_due_orders` runs every minute on `orders.next_deadline`: reminders at
  `reminder_1_minutes` and `reminder_2_minutes`, expiry after `expiration_hours`. It checks again
  under the row lock, so a merchant answering meanwhile wins.
- Messages never fail an order (`NotificationNotSent` is only logged): the dashboard routes
  (`/shops/{id}/orders`, `accept`, `refuse`) are the fallback, and the only way without a
  notification number or an approved template.
- Texts live in `orders/application/texts.py`, in French and English (`ShopSettings.language`).
  Template parameters must be a single line.
- Stock reservation locks the products in id order (`list_for_update`): no overselling, no
  deadlock. Order numbers come from `order_counters`, under a row lock.
- A cart replayed by Meta finds its order through `source_message_id` (unique per shop).

## Payments

- Woassi never holds funds: each shop has its own KPrimePay business account (one key = one
  merchant). Its key is stored only through `SecretCipher`, never returned, never logged; the
  API shows the mode (`sandbox`/`live`, from the `kpp_sbk_`/`kpp_lk_` prefix) and `updated_at`.
- KPrimePay calls live in `payments/infrastructure/kprimepay.py`, each with a link to its doc,
  run outside any transaction, and never let the key, the URL, a body or an httpx exception out.
  `map_status` is the only place KPrimePay statuses become ours.
- A `Payment` holds **attempts**: each USSD push (`ussd_push`) and each checkout is one
  KPrimePay transaction whose `transaction_id` and `Idempotency-Key` are the attempt id
  (KPrimePay binds a key to its first request). Creating one is retried after a timeout (unlike
  WhatsApp sends), by the adapter then by the poller, with the same key. A refusal fails only the
  attempt; `INVALID_KEY` fails the payment. The first PAID attempt pays the payment and cancels
  the others; a second one is only logged (`payment_received_twice`, refund by hand).
- On `OrderConfirmed`: a USSD push to the customer's WhatsApp number if it is a Togolese mobile
  (`togolese_mobile`: +228, 7x/9x; 70-73 and 90-93 are `MIXX-YAS-TG`, others `MOOV-MONEY-TG`),
  plus "confirm on your phone" with the buttons `payment:number:<id>` and `payment:link:<id>`.
  Otherwise (BSUID-only, foreign number) we ask which Mobile Money number pays
  (`awaiting_payer`). At most 3 pushes per payment, then only the link.
- Payment buttons are trusted only when `context_message_id` is in `prompt_message_ids`.
  `integration/orders.py` sends `payment:` payloads to `ReplyToPayment`, and a customer's text
  to `ReceivePayerNumber` (after `RecordContact`), which only takes it when that customer has a
  payment `awaiting_payer` in that shop and the text has digits; otherwise the text goes on.
- The payer number is personal data: `repr=False`, never logged, never in events or audit,
  masked in the API (`90 •• •• 56`).
- **KPrimePay webhooks are unsigned.** `/webhooks/kprimepay/{token}` only finds the shop by the
  token's SHA-256 (the token itself is encrypted, as it goes in every `callback_url`), claims
  `{shop_id}:{event_id}` once, and queues `verify_kprimepay_payment`. Only `debit-status`, called
  with the shop's key, can mark PAID, and only if transaction id, amount and XOF match. The
  token shows in access logs: it can only trigger a check, never a payment.
- PENDING → PAID, FAILED, EXPIRED or CANCELLED. FAILED and EXPIRED may still become PAID (late
  confirmation); PAID and CANCELLED are final. The database allows one PAID and one PENDING
  payment per order. The amount is always the order total (`with_fees=0`: the merchant pays the fee).
- `OrderConfirmed` → `payments.request_payment` (deferred; does nothing without a KPrimePay
  account). `PaymentConfirmed` → `orders.mark_paid` (CONFIRMED → PAID) and
  `billing.issue_invoice`.
- The cron `poll_pending_payments` sends again the attempts KPrimePay did not answer for,
  checks each open attempt (2, 5, 10, 30 min, then hourly), and expires the payment after
  `expires_at` (24 h, or later if a checkout lives longer). It claims each check under the row
  lock by moving `next_check_at`.
- A manual payment (`payment.recorded`) cancels the pending link. KPrimePay cannot cancel a
  checkout: if the customer pays it anyway, the worker logs `payment_received_after_cancellation`
  and the merchant refunds by hand (there is no refund API).
- Messages go as free text or buttons inside the 24-hour window, else as the templates
  `woassi_payment_push`, `woassi_payment_number` and `woassi_order_payment`
  (`payments/application/texts.py`). A message failure never fails a payment.
- `TemplateDefinition` refuses a body that starts or ends with a parameter (Meta rejects it):
  end with a few words, e.g. `{{link}} . Merci !`.

## Billing

- An invoice is issued only once paid: `PaymentConfirmed` → `billing.issue_invoice` (deferred),
  which numbers, renders (fpdf2, in the worker, never in a request), stores and sends it. One
  invoice per order (partial unique index): a replayed event only finishes what was left undone.
- One entity, `BillingDocument`, with `document_type` `INVOICE` or `CREDIT_NOTE`. Issued content
  (number, date, seller and buyer snapshots, lines, totals, payment) is immutable and sealed by
  `content_sha256`; only tracking fields move (PDF, certification, delivery, `credited_total`).
  No delete anywhere: a trigger refuses DELETE on the billing tables (10-year retention).
- Numbering: per shop and type, gapless, from `billing_counters` under a row lock in the issuing
  transaction (`FAC-2026-000001` from `invoice_prefix`, `AV-2026-000001`). The year comes from
  `Clock.local_now()` and is only a prefix: the sequence never restarts.
- Amounts are TTC (catalog prices) and the invoice total is the amount paid. Rounding, per line:
  amount before tax = total / (1 + rate), half up to the franc; tax = total − that. Totals are
  sums of lines. Not VAT-registered: `NOT_SUBJECT`, 0 %, and the PDF prints
  "NE FACTURE PAS LA TVA". The NIF is optional: a missing one never blocks an invoice.
- Corrections only through credit notes (whole invoice, lines and quantities, or an amount),
  never above what is left per line and per tax code, never negative; issued under the invoice's
  row lock, audited `credit_note.issued`. They document a refund made by hand (no refund API).
  Their PDF is rendered by `billing.render_credit_note`; they are not sent on WhatsApp.
- Delivery: inside the 24-hour window, a `document` message with a 24-hour presigned URL; outside
  it, the `woassi_invoice_ready` template with `{PUBLIC_BASE_URL}/invoices/{token}` (only the
  token's SHA-256 is stored, 30 days), which redirects to a 5-minute presigned URL. A message
  failure never fails the invoice: `delivery_status` FAILED, and `POST .../resend`.
- OTR certified e-invoice (art. 62 LPF, not live): `InvoiceCertifier` is bound to
  `NoCertificationRequired`. A shop in scope will get `PENDING` at issue, and its PDF and
  delivery wait for `CERTIFIED` (`DocumentRelease._certify`). Never invent OTR endpoints.

## Catalog sync

- Carts name products by `retailer_id`, so every product is published to the Meta catalog linked
  to the shop's number: `catalog/infrastructure/meta_catalog.py`
  (`POST /{catalog_id}/items_batch`, `UPDATE` with `allow_upsert`, `DELETE` for an inactive
  product Meta has). Doc links are there.
- A change Meta shows (create, `UpdateProduct`, a READY photo, stock to or from 0) calls
  `request_meta_sync`: `MetaSync` goes PENDING, `version` grows, one `ProductMetaSyncRequested`
  per transaction. A sale that leaves stock records nothing: Meta does not get quantities.
- Subscriber `catalog.sync_to_meta` (immediate) only queues `sync_catalog` with job id
  `sync_catalog:{shop_id}` and a 5 s delay: changes coalesce into one Graph call per shop.
- `SyncCatalogToMeta` reads the current products (order of events does not matter), calls Meta
  outside any transaction, then writes the result under `list_for_update`, only to the version
  sent: a change made meanwhile stays PENDING and goes in the next batch.
- No connected number or catalog, or no READY photo: stays PENDING with `next_attempt_at` None
  (`catalog.no_meta_catalog`, `catalog.photo_missing`), out of the cron. `WhatsAppConnected`
  (deferred `catalog.resync_on_whatsapp_connected`) and `POST /shops/{id}/products/sync` mark
  every product PENDING without events and queue one job.
- Failures: `FAILED`, our code only (`meta.unavailable`, `meta.rate_limited`,
  `meta.item_rejected`...), backoff 1 min doubling to 6 h. Cron `retry_catalog_sync` (every
  5 min) queues shops with due products, which also catches events lost after a commit
  (`META_SYNC_GRACE`, 5 min).
- Meta's catalog doc asks for JPEG or PNG images; we send the 1024 px WebP. Check it on a real
  catalog before relying on it.
- Prices go as `"<amount> XOF"`; XOF has no minor unit. Meta requires `link`: built from
  `META_CATALOG_LINK_BASE_URL`. `brand` is the shop's name.

## Pitfalls

- **A module's api never imports `woassi.container`**: it imports every module, which breaks the
  `independence` contract through an indirect import. Each module declares a `<Module>UseCases`
  Protocol in `application/facade.py`, which `Container` inherits from; the api gets it from
  `request.app.state.container`. New use case: add it to both the Protocol and the Container.
- `shared/infrastructure/db/models.py` imports every model and is reserved for `migrations/env.py`.
  Every new model must be added there, otherwise Alembic will not see it.
- After a hand-written migration, `uv run alembic check` must report no drift.
- `ProductRepository.list` shadows the `list` builtin inside its class: annotate with `Sequence`.
- The arq worker exits when Redis goes away and relies on `restart: unless-stopped`; `/health/ready`
  only covers the API, so a dead worker does not show there.
- `lint-imports` caches by mtime: use `--no-cache` when testing a contract by reverting a file.
- Never log a WhatsApp message's sender or content. arq logs the repr of job arguments:
  `MetaEvent` hides those fields (`repr=False`), so do not replace it with a dict. The same goes
  for any domain event carrying personal data and sent to a deferred subscriber.
- structlog reserves the `event` keyword for the log name: `log.info("x", event=...)` raises a
  TypeError at run time. Name it `account_event`, `meta_event`, etc.
- `AuditLog.list` also shadows the builtin; same fix as `ProductRepository.list`.
- A Protocol the api needs goes in `application/facade.py`, including for `shared`
  (`SharedUseCases`), so a new shared use case also goes in `Container`.

## MinIO storage

| Bucket             | Access      | Content                                                       |
| ------------------ | ----------- | ------------------------------------------------------------- |
| `woassi-uploads`   | private     | dashboard originals, in transit                               |
| `woassi-products`  | public read | final WebP (Meta needs a stable URL: never a presigned one)   |
| `woassi-documents` | private     | PDF invoices and credit notes, presigned URLs                 |

Object keys:

- original: `shops/{shop_id}/products/{product_id}/originals/{photo_id}`
- final: `shops/{shop_id}/products/{product_id}/{photo_id}_{400|1024}.webp`
- invoice: `shops/{shop_id}/invoices/{number}.pdf`
- credit note: `shops/{shop_id}/credit-notes/{number}.pdf`

## Current state

Every module contains code. Codes are only written to the logs (`LogVerificationCodeSender`, dev
only). Products are published to the shop's Meta catalog. Carts become orders; customers and
merchants get WhatsApp messages. Confirmed orders get a KPrimePay USSD push or link, or are paid
by hand; a
validated payment publishes `PaymentConfirmed`, the order becomes PAID and the customer gets a
PAID invoice (PDF). No OTR certification yet.
