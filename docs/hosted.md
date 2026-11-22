# Hosted mode (tenancy + public URLs)

Invite-only / multi-customer mode on **one** gateway process. Each customer API key may only claim and see seats in its allowlist. OSS single-token mode is unchanged when `TENANTS_PATH` is unset.

Hard isolation (separate Compose stacks per customer) remains an ops escape hatch — not required for this MVP.

Also covered here: **public `GATEWAY_PUBLIC_BASE` + desktop Watch URLs** behind LB/Caddy (phone Watch later).

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
  - ./gateway/tenants.json:/etc/computer/tenants.json:ro
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

## Capacity model (MVP vs elastic)

**This MVP = reserved seats, not elastic fleet.**

| Behavior | What happens today |
|----------|-------------------|
| Seat set | Fixed `seat_ids` in `tenants.json` (ops pre-assigns warm inventory) |
| Concurrent leases | At most one exclusive lease **per seat** in the allowlist (pool semantics unchanged) |
| On-the-fly new computers | **No** — customers cannot burst/spawn beyond their list |
| Growing a customer | Ops edits `seat_ids` (and ensures those seats exist in inventory), then reloads/recreates gateway |

Example: `"seat_ids": ["desk1"]` → that tenant can hold **one** desktop lease at a time. `"seat_ids": ["desk1","desk2"]` → up to **two** concurrent leases, still only those two machines.

That is intentional for invite-only hosted: predictable capacity and simple isolation. It is **not** “spin up N computers on demand.”

### What elastic would look like (later)

When a customer needs a variable number of leased computers:

1. **Caps, not (only) seat lists** — e.g. `max_concurrent_leases` on the tenant; claim any free seat of the requested `kind` (and/or burst), tagged with `tenant_id` (`hosted-metering`).
2. **Shared / burst pool** — relax “no burst for allowlisted customers”; enforce 429/503 when over plan.
3. **Hard stacks** (optional) — separate Compose/k3s namespace per large customer for stronger isolation at higher ops cost.

Until those land, treat `seat_ids` as **reserved capacity** you sell/assign, not an autoscaler.

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

## Not in this MVP

- Concurrent lease / seat-time caps and shared-pool elasticity (`hosted-metering`)
- Object-storage recordings / Stripe key provisioning

See [docs/v1.md](v1.md) for the shared `/v1` contract.
