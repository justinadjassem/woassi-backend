# woassi-backend
Platforme B2B2C via whatsapp

Woassi backend: WhatsApp commerce for Togolese merchants. FastAPI, PostgreSQL, Redis (arq) and
MinIO, as a modular monolith. The architecture rules are in [CLAUDE.md](CLAUDE.md).

## Getting started

Requirements: [uv](https://docs.astral.sh/uv/) and Docker.

```bash
cp .env.example .env
uv sync
# Generate the encryption key and paste it into WOASSI_ENCRYPTION_KEY in .env
uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# Set META_APP_ID and META_APP_SECRET from the Meta App Dashboard
# Generate the JWT secret and paste it into JWT_SECRET in .env
uv run python -c "import secrets; print(secrets.token_urlsafe(48))"
docker compose up -d          # api, worker, postgres, redis, minio, minio-init
uv run alembic upgrade head
curl localhost:8000/health
```

`curl localhost:8000/health/ready` also checks Postgres, Redis and MinIO, and answers 503 if one
of them is down.

API docs: <http://localhost:8000/docs>. MinIO console: <http://localhost:9001>.

The Compose Postgres is exposed on host port `5433`, to avoid clashing with a local PostgreSQL.

## Running the API and worker outside Docker

```bash
docker compose up -d postgres redis minio minio-init
uv run uvicorn woassi.main:create_app --factory --reload
uv run arq woassi.worker.WorkerSettings
```

## Checks

```bash
uv run ruff check . && uv run ruff format --check .
uv run lint-imports
uv run pytest                 # unit and e2e
uv run pytest -m integration  # real MinIO, needs Docker
uv run mypy src
```

To run them on every commit: `uv run pre-commit install`.

## Authentication

```bash
curl -X POST localhost:8000/auth/register -H 'Content-Type: application/json' \
  -d '{"full_name": "Ama", "phone": "90 12 34 56", "password": "correct-horse"}'
docker compose logs api | grep verification_code    # in dev, the code goes to the logs
curl -X POST localhost:8000/auth/verify -H 'Content-Type: application/json' \
  -d '{"phone": "90 12 34 56", "code": "<code>"}'
curl -X POST localhost:8000/auth/login -H 'Content-Type: application/json' \
  -d '{"phone": "90 12 34 56", "password": "correct-horse"}'
```

The access token lasts 15 minutes and goes in `Authorization: Bearer`. The refresh token lasts 30
days, works once, and is returned both in the body and in an httpOnly cookie on `/auth`. A browser
client must not send two refreshes at once: replaying a consumed token ends the session.

Outside `APP_ENV=dev` the application refuses to start until a real code sender exists.

Behind a reverse proxy, configure uvicorn's `--forwarded-allow-ips`. Otherwise every client appears
under the proxy's IP and they all share one login rate limit.

## Connecting a WhatsApp number

The dashboard runs Meta's Embedded Signup, then sends the result to
`POST /shops/{shop_id}/whatsapp/connect`. It needs:

| Variable             | Used by   | Purpose                                                       |
| -------------------- | --------- | ------------------------------------------------------------- |
| `META_APP_ID`        | API       | exchanges the signup code, checks tokens                      |
| `META_APP_SECRET`    | API       | same, and signs webhooks                                      |
| `META_CONFIG_ID`     | dashboard | Facebook Login for Business configuration passed to `FB.login` |
| `META_GRAPH_VERSION` | API       | Graph API version, `v27.0`                                    |
| `META_CATALOG_LINK_BASE_URL` | worker | base of the product `link` required by Meta's catalog |

On the dashboard side:

- use Embedded Signup v4 or later, as v2 and v3 are deprecated. On v4, set
  `featureType: "whatsapp_business_app_onboarding"` in `extras`; it is the default on v5;
- send `code`, `waba_id` and `phone_number_id`, plus `catalog_id` if the event has `catalog_ids`.

In the Meta App Dashboard, under **WhatsApp > Configuration**:

- callback URL: `https://<public host>/webhooks/meta`, verify token: `META_VERIFY_TOKEN`;
- subscribe to `messages`, `account_update`, `message_template_status_update`, `history`,
  `smb_app_state_sync` and `smb_message_echoes`.

Meta only calls public HTTPS URLs. In development, expose the API through a tunnel first, for
instance `cloudflared tunnel --url http://localhost:8000`, and use the URL it prints.

A shop goes `ACTIVE` once its number is `CONNECTED`. A worker job checks every token daily at 03:00
UTC.

## Product catalog on WhatsApp

Customers order from the Meta catalog linked to the shop's number, and carts name products by
`retailer_id`. The worker publishes the products there (`items_batch`): on creation, on
`PATCH /shops/{shop_id}/products/{product_id}`, when a photo is ready, and when the stock reaches
zero or comes back. Meta gets the 1024 px photo, the price as `5000 XOF`, `in stock` or
`out of stock`; `active: false` deletes the item there. Each product shows `meta_sync`
(`PENDING`, `SYNCED`, `FAILED` and an `error_code`); failures are retried every 5 minutes, with
backoff. Connecting a number sends the whole catalog, and `POST /shops/{shop_id}/products/sync`
sends it again on demand.

## Orders

A WhatsApp cart becomes an order, and its stock is reserved. The shop's settings decide what
happens next:

- `validation_mode`: `MANUAL` (default) asks the merchant on their `notification_number` with the
  `woassi_order_validation` template; `AUTOMATIC` confirms at once, unless the total is above
  `auto_validation_max_amount`;
- `language`: `fr` or `en`, for the merchant and the customers;
- reminders at 15 and 60 minutes, expiry after 24 hours (`reminder_*`, `expiration_hours`).

If part of the cart is out of stock, the customer is offered the rest. The merchant can also
decide from the dashboard: `POST /shops/{shop_id}/orders/{order_id}/accept` or `/refuse`.

The template is submitted when a shop connects its number; until Meta approves it, which can
take 24 hours, orders only wait in the dashboard. A paid order moves from `CONFIRMED` to `PAID`.

## Payments

Woassi never holds funds: each shop opens its own KPrimePay business account and gives Woassi a
v2 API key scoped to `read` and `payments:write` (never `payouts:write`). The money lands in the
merchant's KPrimePay balance.

| Route                                                  | Purpose                                         |
| ------------------------------------------------------ | ----------------------------------------------- |
| `PUT /shops/{shop_id}/payment-provider`                | checks the key with KPrimePay, stores it encrypted |
| `GET`, `DELETE /shops/{shop_id}/payment-provider`      | configured, `sandbox` or `live`; never the key  |
| `GET /shops/{shop_id}/payments[/{payment_id}]`         | filters: `status`, `order_id`                   |
| `POST /shops/{shop_id}/orders/{order_id}/payments/request` | `{"channel": "checkout"}` (default): the usable link, or a new one; `"ussd_push"`: push again to the last number |
| `POST /shops/{shop_id}/orders/{order_id}/payments/manual`  | cash or mobile money received directly       |
| `POST /webhooks/kprimepay/{token}`                     | public, per-shop secret token                   |
| `GET /payments/return`                                 | where KPrimePay sends the customer back         |

Once an order is confirmed, a customer whose WhatsApp number is a Togolese mobile gets a USSD
push (Flooz or Mixx by Yas) to approve with their PIN, and a WhatsApp message with two buttons:
"Another number" (they then type the Mobile Money number that pays, e.g. `90 12 34 56`) and
"Payment link" (card or foreign payers, KPrimePay's hosted checkout). Other customers are asked
which number will pay. At most 3 pushes per payment. Outside the 24-hour window the messages go
as the `woassi_payment_push`, `woassi_payment_number` and `woassi_order_payment` templates.
KPrimePay does not sign its webhooks: they only queue a check, and the worker asks
`debit-status` itself. The cron
`poll_pending_payments` also checks pending payments every minute, with backoff, and expires
them.

| Variable                 | Purpose                                                          |
| ------------------------ | ---------------------------------------------------------------- |
| `PUBLIC_BASE_URL`        | public https URL of the API, for webhooks; empty: polling only  |
| `PAYMENT_RETURN_URL`     | defaults to `{PUBLIC_BASE_URL}/payments/return`                  |
| `KPRIMEPAY_BASE_URL`     | `https://api.kprimepay.com/v2`                                   |
| `KPRIMEPAY_*_TIMEOUT_S`  | connect 5 s, read 15 s                                           |

Test with a sandbox key (`kpp_sbk_...`): it never moves real money.

## Billing

Once a payment is confirmed, the worker issues the order's invoice, marked "PAYÉE" / "PAID",
stores the PDF in the private `woassi-documents` bucket and sends it to the customer on WhatsApp
(the PDF inside the 24-hour window, the `woassi_invoice_ready` template with a link outside it).
Corrections go through credit notes, which document a refund the merchant made by hand.

| Route                                                      | Purpose                                    |
| ---------------------------------------------------------- | ------------------------------------------ |
| `GET /shops/{shop_id}/invoices[/{invoice_id}]`             | filters: `order_id`, `issued_from`, `issued_to` |
| `GET /shops/{shop_id}/invoices/{invoice_id}/pdf`           | presigned URL (`?redirect=true`: 302)      |
| `POST /shops/{shop_id}/invoices/{invoice_id}/resend`       | sends it on WhatsApp again                 |
| `POST /shops/{shop_id}/invoices/{invoice_id}/credit-notes` | whole invoice, some lines, or an amount    |
| `GET /shops/{shop_id}/credit-notes[/{id}][/pdf]`           | filters: `invoice_id`, `order_id`, dates   |
| `GET /shops/{shop_id}/billing/summary`                     | turnover before tax, VAT per rate, credit notes, net |
| `GET /invoices/{token}`                                    | public link of the template, 302 to the PDF |

Tax details live in the shop settings, under `invoice_details`: `tax_id` (NIF), `trade_register`
(RCCM), `address`, `tax_regime` (`TPU` or `REAL`) and `vat_registered`. Only a VAT-registered
shop charges 18 % VAT; the others print "NE FACTURE PAS LA TVA". All are optional.
`PUBLIC_BASE_URL` is needed for the template's link, and MinIO's public endpoint must be reachable
by Meta for the PDF to go out as a document. Togo's certified e-invoice (OTR) is not live: the
`InvoiceCertifier` port is ready for it.

## Encryption keys

Shop secrets (Meta tokens, aggregator credentials) are encrypted with Fernet. The API and the
worker refuse to start without a key.

To rotate, generate a new key and list it first, as the first key encrypts and all of them decrypt:

```bash
WOASSI_ENCRYPTION_KEYS=<new key>,<old key>
```

Once stored values are re-encrypted (`FernetSecretCipher.rotate`), remove the old key. Losing all
the keys means losing the secrets: store them in a secrets manager, never in the repository.

## Troubleshooting

**The Docker build hangs on `apt-get update`.** Containers have no DNS, which happens behind a
local resolver (`systemd-resolved`, Cloudflare WARP). To check:
`docker run --rm alpine nslookup deb.debian.org`.

- Lasting fix: set `{"dns": ["1.1.1.1", "8.8.8.8"]}` in `/etc/docker/daemon.json`, then run
  `sudo systemctl restart docker`.
- Workaround without `sudo`: `docker build --network host -t woassi-api:dev .`, then
  `docker compose up -d`, which reuses the image.
