# Hosted beta ops

Runbook for an invite-only hosted fleet on this repo’s Compose gateway + desktop workers. Product surface stays `/v1`; this doc is for the operator who deploys and pages on it.

Pair with [hosted.md](hosted.md) (tenancy, public URLs, metering, S3 recordings), [hosted-security.md](hosted-security.md) (threat model / ports / token rotation), and [v1.md](v1.md) (API contract).

## Deploy runbook

### Preconditions

| Item | Notes |
|------|--------|
| Host | Linux with Docker Compose; loopback-bound published ports |
| DNS / TLS | `GATEWAY_PUBLIC_BASE` + inventory `vnc_url` hostnames → Caddy/LB |
| Secrets | `.env` with real `COMPUTER_API_TOKEN`, tenant keys, optional `RECORDING_S3_*`, Stripe billing vars if open signup |
| Inventory | Hosted desktop JSON (not localhost Watch URLs) |
| Tenants | `gateway/tenants.json` mounted; ops token ≠ customer keys |
| Billing (optional) | Public HTTPS to `/v1/billing/webhook`; **rw** tenants mount; Stripe Price IDs + webhook secret |

### First bring-up

```bash
# On the deploy host
cp -n .env.example .env
# Edit: COMPUTER_API_TOKEN, GATEWAY_PUBLIC_BASE=https://api.example.com,
#       WORKER_INVENTORY_FILE=./gateway/workers.hosted.json,
#       TENANTS_FILE=./gateway/tenants.json, RECORDING_* as needed

cp gateway/workers.hosted.example.json gateway/workers.hosted.json
cp gateway/tenants.example.json gateway/tenants.json
# Replace example.com / placeholder API keys / seat_ids

# Loopback binds (local override — do not commit host-specific compose)
cat > docker-compose.override.yml <<'EOF'
services:
  gateway:
    ports: ["127.0.0.1:9000:9000"]
    environment:
      TENANTS_PATH: /etc/computer/tenants.json
      # Optional Stripe open signup — see docs/hosted.md Billing
      # STRIPE_SECRET_KEY: ${STRIPE_SECRET_KEY:-}
      # STRIPE_WEBHOOK_SECRET: ${STRIPE_WEBHOOK_SECRET:-}
      # STRIPE_PRICE_STARTER: ${STRIPE_PRICE_STARTER:-}
      # STRIPE_PRICE_PRO: ${STRIPE_PRICE_PRO:-}
      # BILLING_SUCCESS_URL: ${BILLING_SUCCESS_URL:-}
      # BILLING_CANCEL_URL: ${BILLING_CANCEL_URL:-}
      # BILLING_PORTAL_RETURN_URL: ${BILLING_PORTAL_RETURN_URL:-}
    volumes:
      # Invite-only hand-edited keys: :ro is fine. Stripe provision needs rw (no :ro).
      - ${TENANTS_FILE:-./gateway/tenants.json}:/etc/computer/tenants.json:ro
  worker-1:
    ports: ["127.0.0.1:6081:6080"]
  worker-2:
    ports: ["127.0.0.1:6082:6080"]
EOF

docker compose up --build -d
curl -sf http://127.0.0.1:9000/health | python3 -m json.tool
curl -sf -H "Authorization: Bearer $COMPUTER_API_TOKEN" \
  http://127.0.0.1:9000/v1/seats | python3 -m json.tool
```

Apply Caddy from [`deploy/hosted-caddy.example.caddyfile`](../deploy/hosted-caddy.example.caddyfile). External smoke:

```bash
export GW=https://api.example.com
export AUTH="Authorization: Bearer <tenant-key>"
curl -sf -H "$AUTH" "$GW/health"
curl -sf -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"kind":"desktop"}' "$GW/v1/sessions"
# Confirm api_base + vnc_url use public https hostnames; open Watch; DELETE session
```

### Upgrade

1. `git fetch && git checkout <tag>` (prefer annotated release tags).
2. Diff `.env.example` / inventory / tenants schema; merge new env keys.
3. `docker compose pull` (if using registry images) or `docker compose up --build -d`.
4. Probe `/health` then `/ready`; spot-check one lease + Watch.
5. If tenants or inventory changed: recreate gateway so mounts reload  
   (`docker compose up -d --force-recreate gateway`).

### Rollback

1. `git checkout <previous-tag>`.
2. `docker compose up --build -d` (same override / secrets).
3. Recording volume / S3 objects are **not** wiped by a gateway recreate — only by TTL sweep or explicit delete.
4. If a bad tenants.json shipped: restore previous file, recreate gateway.

### Safe vs unsafe ops

| Safe | Avoid without plan |
|------|---------------------|
| Recreate gateway | `docker volume rm` of `gateway_recordings` |
| Soft release / park | Bulk recycle of customer seats mid-job |
| Raise `max_concurrent_leases` within `seat_ids` | Publishing worker `:8000` |
| Add seats to inventory + tenant allowlist | Sharing one customer API key across orgs |

## Health / ready alerts

Unauthenticated probes (safe to scrape from the host or a private checker):

| Path | Expect | Meaning |
|------|--------|---------|
| `GET /health` | **200**, `"ok": true` | Process up; includes `free` / `leased` / `parked` / `workers` |
| `GET /ready` | **200**, `"ok": true` | ≥1 free seat **and** ≥1 healthy worker — will accept new leases |
| `GET /ready` | **503** | Full pool and/or no healthy worker — page capacity / workers |

Suggested check (also [`scripts/hosted-health-probe.sh`](../scripts/hosted-health-probe.sh)):

```bash
# Critical — gateway down
curl -sf --max-time 5 "$GW/health" >/dev/null || exit 2

# Warning — cannot accept new leases (full or unhealthy)
code=$(curl -s -o /tmp/ready.json -w '%{http_code}' --max-time 5 "$GW/ready")
# 200 = ok; 503 = alert (capacity or workers); else unexpected
```

### Alert matrix (beta)

| Signal | Severity | First action |
|--------|----------|--------------|
| `/health` fail ≥2m | **P1** | `docker compose ps`; gateway logs; recreate gateway if crashed |
| `/ready` 503, `healthy_workers: false` | **P1** | Worker healthchecks; `docker compose logs worker-1`; restart unhealthy worker |
| `/ready` 503, `free: 0`, workers healthy | **P2** | Capacity playbook — pool full (expected under load) |
| Sustained `leased` ≈ `workers` while customers 429 | **P2** | Grow inventory / rebalance tenant `seat_ids` |
| Tenant 429 `tenant_lease_cap` spikes | **P3** | Plan/cap — not infra failure; raise cap or seat allowlist |
| Caddy / TLS errors on Watch | **P2** | Confirm loopback ports + Host routes; lease `vnc_url` hostnames |

Do **not** alert on `/ready` 503 alone during planned full utilization without a free-seat SLO. For beta, treat “health down” and “zero healthy workers” as pages; treat “free=0” as a capacity ticket unless it lasts beyond your advertised seat count.

Compose already healthchecks gateway `:9000/health` and each worker. Prefer probing the **public** `GATEWAY_PUBLIC_BASE` from outside the host so Caddy is in the path for API alerts; probe loopback for worker-only issues.

## Stripe webhook + billing capacity

When open signup is on ([hosted.md](hosted.md) Billing):

1. In Stripe Dashboard → Webhooks, endpoint  
   `https://<GATEWAY_PUBLIC_BASE host>/v1/billing/webhook`  
   Events: `checkout.session.completed`, `customer.subscription.updated`, `customer.subscription.deleted`.
2. Copy the signing secret into `STRIPE_WEBHOOK_SECRET`. Endpoint must be reachable on **public HTTPS** (same Caddy path as the API).
3. Mount `tenants.json` **rw** so provision/suspend persist without gateway recreate.
4. Capacity: unclaimed **desktop** inventory seats ≥ sum of seats for plans you sell. Webhook returns **500** on undersupply (Stripe retries); no partial tenant. Watch signup 500s in gateway logs as a capacity page.

Smoke (after a test Checkout in Stripe test mode):

```bash
curl -sf "$GW/v1/billing/plans" | python3 -m json.tool
# Complete Checkout in browser → success URL should call:
# GET $GW/v1/billing/checkout/{CHECKOUT_SESSION_ID}  → api_key once
```

## Kill a tenant (abuse)

Instant deny without waiting on Stripe:

1. Set `"disabled": true` on the tenant in `tenants.json` (rw mount — gateway hot-reloads on next upsert/`reload`, or recreate gateway after a hand edit), **or**
2. Set `ABUSE_DISABLED_TENANT_IDS=cus_…,other_id` and recreate gateway.

Customer Bearer → **403** `tenant_disabled`. Ops token still works. Subscription cancel / webhook suspend also clears keys + seats and sets `disabled`.

Rate-limit spikes: lower `ABUSE_RATE_LIMIT_*` or investigate; Watch bandwidth is Caddy/LB, not the gateway.

## Capacity playbook

Hosted beta capacity = **reserved inventory** + **per-tenant caps**. Customers do not get lease-burst outside `seat_ids`. With Stripe billing, new signups **auto-claim** unused desktop seats into the new tenant’s allowlist — keep spare inventory ahead of demand.

### Read the room

```bash
# Ops token — all seats
curl -sf -H "Authorization: Bearer $COMPUTER_API_TOKEN" "$GW/v1/seats" | python3 -m json.tool
curl -sf "$GW/ready" | python3 -m json.tool
```

| Symptom | Likely cause | Action |
|---------|--------------|--------|
| Agent **503** `no free … workers` | Allowlist seats all leased/parked or unhealthy | Wait park TTL, recycle idle parked, fix worker, or add seats |
| Agent **429** `tenant_lease_cap` | Hit `max_concurrent_leases` | Raise cap (≤ claimable seats) or free their sessions |
| `/ready` 503, `free: 0` | Fleet saturated | Add desktop workers + inventory + Caddy routes; update tenant `seat_ids` |
| One tenant starves others | Uneven allowlists | Rebalance `seat_ids`; keep ops token for Desk intervention |

### Grow a desktop seat (checklist)

1. Add Compose worker service (or scale pattern) with loopback noVNC port.
2. Append seat to inventory JSON (`id`, `url` internal, public `vnc_url`).
3. Caddy Host route → new loopback port.
4. Add `id` to the customer’s `seat_ids` (and bump `max_concurrent_leases` if the plan allows).
5. Recreate gateway; confirm `GET /v1/seats`.

### Park vs recycle under pressure

- **Park** (soft `DELETE`): keeps warm desktop; still counts toward tenant concurrent cap.
- **Recycle**: wipe + free — use for stuck/idle seats when you need capacity back now (`POST …/recycle` or `DELETE ?recycle=true`).

Parked-idle sweeper (`PARKED_TTL_SEC`) returns reserved seats to **free** after reset — prefer waiting when load is bursty and customers will reacquire.

## Recordings backup / restore

Journals are **not** video: `meta.json`, `events.jsonl`, `frames/*.png`, plus `_sessions/{session_id}.json` index. TTL (`RECORDING_TTL_SEC`, default 7d) deletes expired objects via the gateway sweeper — backups are for operator error / disk loss inside that window, not long-term archive.

### What to back up

| Backend | Critical | Optional |
|---------|----------|----------|
| `local` | Volume `gateway_recordings` (or `RECORDING_DIR`) — especially `**/meta.json`, `**/events.jsonl`, `_sessions/` | `frames/` (large; skip if meta+events enough for audit) |
| `s3` | Bucket versioning / provider snapshot on `{RECORDING_S3_PREFIX}/` | Same; frames dominate size |

Also back up **config**: `tenants.json`, inventory JSON, `.env` (secrets store — not git).

### Backup (local volume)

[`scripts/recording-backup.sh`](../scripts/recording-backup.sh) copies metadata (+ optional frames) from the Compose volume or a host path:

```bash
# Metadata-only tarball (default)
./scripts/recording-backup.sh --out /var/backups/cwa-recordings

# Include PNG frames
./scripts/recording-backup.sh --out /var/backups/cwa-recordings --frames
```

Schedule daily via cron/systemd while TTL is 7d (retention of tarballs can be shorter than product TTL).

### Backup (S3 / R2)

Prefer bucket **versioning** + lifecycle aligned with `RECORDING_TTL_SEC`. Cross-account/region replication is optional for beta. Do not rely on gateway process backups for object storage — protect the bucket.

### Restore

**Local**

1. Stop gateway writes: `docker compose stop gateway` (or scale to 0).
2. Extract tarball into the recordings root (Compose volume mount target or `RECORDING_DIR`).
3. Ensure `_sessions/{id}.json` and `t/{tenant}/{id}/meta.json` are both present for each session you care about — list/get resolve via the index.
4. `docker compose start gateway`; `GET /v1/recordings` with ops or tenant key.

**S3**

1. Restore objects under the same `RECORDING_S3_PREFIX` (including `_sessions/`).
2. Point gateway at the same bucket/prefix; no local volume required.
3. Confirm one known `session_id` via `GET /v1/recordings/{id}`.

### Integrity notes

- Restoring **meta + events** without frames still lists the recording; frame GETs 404 for missing PNGs.
- Restoring frames without `_sessions` index leaves orphans until you rebuild index entries (`{"tenant_seg":"…","session_id":"…"}`) or re-list only finds legacy flat layouts.
- TTL sweep will delete restored journals whose `expires_at` / age is past TTL — bump TTL or rewrite meta expiry before start if you need a longer hold after restore.

## Beta exit checklist

Before calling hosted beta “ops-ready”:

- [ ] Public `/health` + `/ready` probed on a schedule (script or uptime checker)
- [ ] Deploy + upgrade + rollback exercised once on the real host
- [ ] Capacity playbook understood (503 vs 429)
- [ ] Recording backup job exists (local cron or S3 versioning)
- [ ] Worker `:8000` not on the public face; only gateway + Watch via proxy  
- [ ] `scripts/hosted-security-check.sh` passes (compose + override)  
- [ ] Ops / tenant token rotation exercised once — [hosted-security.md](hosted-security.md)
