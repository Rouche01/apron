# Hosted tenancy (soft seat isolation)

Invite-only / multi-customer mode on **one** gateway process. Each customer API key may only claim and see seats in its allowlist. OSS single-token mode is unchanged when `TENANTS_PATH` is unset.

Hard isolation (separate Compose stacks per customer) remains an ops escape hatch — not required for this MVP.

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

## Not in this MVP

- Concurrent lease / seat-time caps and shared-pool elasticity (`hosted-metering`)
- Public LB URL packaging (`hosted-public-urls`)
- Object-storage recordings / Stripe key provisioning

See [docs/v1.md](v1.md) for the shared `/v1` contract.
