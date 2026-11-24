# Hosted mode (tenancy + public URLs)

Invite-only / multi-customer mode on **one** gateway process. Each customer API key may only claim and see seats in its allowlist. OSS single-token mode is unchanged when `TENANTS_PATH` is unset.

Hard isolation (separate Compose stacks per customer) remains an ops escape hatch — not required for this MVP.

Also covered here: **public `GATEWAY_PUBLIC_BASE` + desktop Watch URLs** behind LB/Caddy (phone Watch later).

**Ops (deploy, alerts, capacity, recording backup):** [hosted-ops.md](hosted-ops.md).  
**Security (threat model, isolation, token rotation, port check):** [hosted-security.md](hosted-security.md).

## Enable

1. Copy [`gateway/tenants.example.json`](../gateway/tenants.example.json) → e.g. `gateway/tenants.json` and replace placeholder keys / `seat_ids`.
2. Point the gateway at it:

```bash
# .env / Compose
TENANTS_PATH=/etc/computer/tenants.json
COMPUTER_API_TOKEN=ops-secret   # ops principal: all seats (Desk / bind / register)
```

Compose mount example:

```yaml
environment:
  TENANTS_PATH: /etc/computer/tenants.json
volumes:
  # Invite-only (ops edits tenants.json by hand): :ro is fine
  - ./gateway/tenants.json:/etc/computer/tenants.json:ro
  # Stripe signup provision (see Billing below): mount rw — drop :ro
  # - ./gateway/tenants.json:/etc/computer/tenants.json
```

3. Recreate gateway: `docker compose up -d --force-recreate gateway`

## Auth

| Bearer | Access |
|--------|--------|
| Customer key from `tenants.json` | Only that tenant’s `seat_ids`; own sessions + recordings |
| `COMPUTER_API_TOKEN` | Ops — all seats / sessions / recordings |
| Missing / wrong | **401** |

When `TENANTS_PATH` is unset: legacy OSS behavior (`COMPUTER_API_TOKEN` optional in dev).

## Isolation rules

- Lease / pin only within allowlist (foreign pin → **404**)
- Cross-tenant session id on screenshot/action/release → **404**
- `GET /v1/seats` and recordings list are filtered per tenant
- Lease-burst is **not** used for allowlisted customers (no spawn outside their seats)

## Capacity model + metering

**Reserved seats + plan caps** (still not a full autoscaler).

| Field | Meaning |
|-------|---------|
| `seat_ids` | Fixed allowlist of inventory seats this tenant may claim |
| `max_concurrent_leases` | Max **leased + parked** sessions owned by the tenant (default = `len(seat_ids)`) |
| `max_lease_ttl_sec` | Optional clamp on lease / heartbeat / acquire TTL (min 30) |

| Behavior | What happens |
|----------|----------------|
| New lease while at cap | **429** `tenant_lease_cap` (plan overage — not the same as pool empty) |
| Reattach (`job_id` / parked pin) | Does **not** consume an extra concurrent slot |
| No free seat in allowlist | **503** `no free … workers` (capacity) |
| On-the-fly new computers | **No** — customers still cannot burst outside `seat_ids` |
| Growing a customer | Ops adds seats to inventory + `seat_ids`, and/or raises `max_concurrent_leases` (cannot exceed seats they can actually claim) |

Example: `"seat_ids": ["desk1","desk2"], "max_concurrent_leases": 1` → two possible machines, but only **one** held session at a time. `"max_lease_ttl_sec": 3600` → requests for longer TTLs are clamped to 1h.

### What fuller elasticity would look like (later)

1. Shared pool / optional empty allowlist + burst under the same concurrent cap  
2. Cumulative seat-time billing / quotas (beyond per-lease TTL clamp)  
3. Hard stacks per large customer  

Until then, treat `seat_ids` as reserved inventory and caps as the plan envelope.

## Public URLs (desktop first)

Agents and browsers must reach the **same** origins you bake into lease responses:

| Surface | Env / inventory | Example |
|---------|-----------------|--------|
| API (`api_base`) | `GATEWAY_PUBLIC_BASE` | `https://api.example.com` |
| Desktop Watch (`vnc_url`) | inventory `vnc_url` per seat | `https://desk1.example.com/vnc.html?…` |

Worker **control** URLs (`url`: `http://worker-1:8000`) stay on the Compose network — never publish worker `:8000` on the public face.

### Checklist

1. Copy [`gateway/workers.hosted.example.json`](../gateway/workers.hosted.example.json) → e.g. `gateway/workers.hosted.json`; replace `example.com` with your DNS names (desktop seats only in this template).
2. Set:

```bash
GATEWAY_PUBLIC_BASE=https://api.example.com
WORKER_INVENTORY_FILE=./gateway/workers.hosted.json
```

3. On the host, bind Compose published ports to **loopback** so only the reverse proxy is reachable:

```yaml
ports:
  - "127.0.0.1:9000:9000"
  - "127.0.0.1:6081:6080"
  - "127.0.0.1:6082:6080"
```

(Prefer a local `docker-compose.override.yml` — do not commit ThinkPad-only binds into the shared compose file.)

4. Terminate TLS at Caddy (or LB) with Host routes → those loopback ports. Starter file: [`deploy/hosted-caddy.example.caddyfile`](../deploy/hosted-caddy.example.caddyfile).

5. Smoke from a laptop (not the host):

```bash
export GW=https://api.example.com
export AUTH="Authorization: Bearer <tenant-or-ops-token>"
curl -s -H "$AUTH" "$GW/health"
# Lease → confirm api_base and vnc_url are https://…example.com (not localhost)
# Open vnc_url in a browser; Watch should connect over WSS via Caddy
```

Gateway already injects `autoconnect=1&resize=scale` (and `port=443` on HTTPS noVNC URLs when needed).

### Phone Watch

Physical scrcpy (`:6083`) / cloud vendor `watch_url` follow the same pattern (public hostname → loopback) but are **out of this desktop-first pack**. See [AGENTS.md](../AGENTS.md) ThinkPad / Caddy notes when you add them.

### Localhost warning

If `TENANTS_PATH` is set and `GATEWAY_PUBLIC_BASE` still looks like `localhost` / `127.0.0.1`, the gateway logs a **warning** at boot — fine for dogfood, wrong for real customers.

## Hosted recordings (object storage)

Same `/v1/recordings` API as OSS. Storage backend is selectable:

| Env | Meaning |
|-----|---------|
| `RECORDING_BACKEND` | `local` (default) or `s3` (S3/R2/MinIO) |
| `RECORDING_DIR` | Local root when `local` |
| `RECORDING_S3_BUCKET` | Bucket name (required for `s3`) |
| `RECORDING_S3_PREFIX` | Key prefix (default `recordings`) |
| `RECORDING_S3_ENDPOINT` | Optional (R2/MinIO endpoint URL) |
| `RECORDING_S3_REGION` | Optional region |
| `RECORDING_S3_ACCESS_KEY` / `RECORDING_S3_SECRET_KEY` | Credentials (or ambient IAM) |

Object key layout (per-tenant prefix):

```text
{prefix}/t/{tenant_id}/…/meta.json
{prefix}/t/{tenant_id}/…/events.jsonl
{prefix}/t/{tenant_id}/…/frames/000001.png
{prefix}/_sessions/{session_id}.json   # lookup index
```

Anonymous / OSS sessions use tenant segment `anon`.

### Quota

Optional tenant field `max_recording_bytes`. When set:

- `start` refuses new journals over quota (lease still succeeds; `recording: false`)
- frame appends over quota are omitted with event note `frame_omitted_quota`

TTL remains `RECORDING_TTL_SEC` (default 7d) via the existing sweeper.

## Billing (Stripe Checkout → API key)

Open signup for the paid hosted fleet. Same `/v1` lease API; keys land in `tenants.json` via webhook. Off when `STRIPE_SECRET_KEY` is unset.

### Env

| Variable | Role |
|----------|------|
| `STRIPE_SECRET_KEY` | Stripe secret; billing disabled if empty |
| `STRIPE_WEBHOOK_SECRET` | Verify `Stripe-Signature` on `/v1/billing/webhook` |
| `STRIPE_PRICE_STARTER` / `STRIPE_PRICE_PRO` | Recurring Price IDs (`starter` = 1 seat, `pro` = 2) |
| `BILLING_SUCCESS_URL` | Checkout return (include `{CHECKOUT_SESSION_ID}`) |
| `BILLING_CANCEL_URL` | Checkout cancel |
| `BILLING_PORTAL_RETURN_URL` | Customer Portal return (invoices / cancel) |
| `BILLING_DEFAULT_RECORDING_BYTES` | Recording fair-use quota on provision (default **500 MiB**; `0` = unlimited) |
| `TENANTS_PATH` | **Required** for billing — registry the webhook upserts |

Mount `tenants.json` **read-write** (no `:ro`) so Checkout provision can persist tenants without recreating the gateway.

### Flow

1. `GET /v1/billing/plans` — public plan matrix (seats; whether a price id is configured).
2. `POST /v1/billing/checkout` `{"email","plan"}` — returns Stripe Checkout `url` + `checkout_session_id` (**404** if billing off).
3. Stripe → `POST /v1/billing/webhook` (`checkout.session.completed`) — gateway:
   - Claims **N unclaimed non-ephemeral desktop** inventory seats (prefers `state=free`; skips phone / burst)
   - Upserts tenant (`id` = Stripe `customer`; `cwa_…` API key; plan caps)
   - Idempotent if that customer already has seats + keys
4. Success page: `GET /v1/billing/checkout/{CHECKOUT_SESSION_ID}` — returns `api_key` **once**, then `revealed: true` / `api_key: null`.
5. Invoices / cancel: `POST /v1/billing/portal` with the customer Bearer → Stripe Customer Portal URL.
6. `customer.subscription.deleted` (or unpaid/canceled update) → suspend (clear keys + seats). Plan change → grow/shrink allowlist.

Undersupply (not enough free desktop seats for the plan) → webhook **500** so Stripe retries; **no** partial tenant.

### Capacity rule

Unclaimed desktop seats in inventory must cover open signups:  
`Σ plan.seats for active subscriptions ≤ desktop inventory − seats already in any tenant allowlist`.  
Grow workers + inventory before marketing a plan tier you cannot fulfill. Phone seats are **not** auto-claimed.

### Not in this pack

- Shared-pool / burst elasticity beyond the seat allowlist  
- Metered seat-hour Stripe usage records (caps remain `max_concurrent_leases`)  
- Phone seats in auto-claim  

## Abuse limits

Active when **tenancy is on** (`TENANTS_PATH` set). OSS single-token mode is unchanged.

| Control | Behavior |
|---------|----------|
| Rate limit | Per-tenant sliding window on authenticated `/v1/*` (**429** `rate_limited`). Screenshot / observe / action use a stricter bucket. |
| Billing IP limit | Public `GET/POST /v1/billing/plans` + checkout + key reveal (not webhook). |
| Kill switch | Tenant `"disabled": true` in `tenants.json`, or `ABUSE_DISABLED_TENANT_IDS` — **403** `tenant_disabled`. Ops Bearer (`COMPUTER_API_TOKEN`) is exempt. |
| Recording fair-use | Stripe provision sets `max_recording_bytes` (see Billing); existing quota already refuses overage. |

| Variable | Default | Role |
|----------|---------|------|
| `ABUSE_RATE_LIMIT_RPM` | `120` | General `/v1` per tenant (`0` = off) |
| `ABUSE_RATE_LIMIT_OBSERVE_RPM` | `60` | Screenshot / observe / action |
| `ABUSE_RATE_LIMIT_BILLING_RPM` | `20` | Public billing per client IP |
| `ABUSE_DISABLED_TENANT_IDS` | (empty) | Comma-separated tenant ids to deny immediately |

**Watch (noVNC / scrcpy):** the gateway does **not** proxy Watch bytes. Fair-use there is infra (Caddy/LB). Agent load is controlled via the observe/action rate limits above.

See [docs/v1.md](v1.md) for the shared `/v1` contract and [hosted-ops.md](hosted-ops.md) for deploy / alert / backup / webhook / kill-switch ops.
