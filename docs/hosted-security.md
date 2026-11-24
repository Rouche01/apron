# Hosted security (threat model + hardening)

Companion to [hosted.md](hosted.md) (tenancy/billing) and [hosted-ops.md](hosted-ops.md) (deploy). Reporting: [SECURITY.md](../SECURITY.md).

## Trust boundaries

```text
                    public Internet
                           |
                     TLS (Caddy/LB)
                           |
         +-----------------+-----------------+
         |                                   |
   gateway :9000                      Watch (noVNC / scrcpy)
   Bearer + /v1                       view-only HITL
         |
   Compose network (computer_internal)
         |
   worker :8000  (desktop/phone control — never publish)
```

| Surface | Public? | Auth |
|---------|---------|------|
| Gateway `/v1` | Yes (via proxy) | Bearer (ops or tenant key) |
| Gateway `/health` `/ready` | Yes (probes) | None (no secrets) |
| Billing webhook | Yes | Stripe signature |
| noVNC / scrcpy Watch | Yes (via proxy) | URL knowledge; treat as sensitive |
| Worker `:8000` / phone `:8003` | **No** | Internal Compose DNS only |

## Tenant isolation (review)

Hosted mode is **soft isolation** on one gateway process:

| Layer | What you get | What you do not get |
|-------|--------------|---------------------|
| API | Per-tenant seat allowlist; cross-tenant session/recording → **404** | Separate VPC / network namespace per customer |
| Seats | Customer can only lease their `seat_ids` | Hard guarantee workers cannot talk to each other on the bridge |
| Recordings | Tenant-scoped list/get; S3 prefix `t/{tenant_id}/` | Crypto isolation between tenants on one disk |
| Billing | Key issued per Stripe customer | Automatic purge of all worker state on key rotate |

**Escape hatch:** run a dedicated Compose stack (or cluster namespace) per large customer — still the same `/v1` contract; ops wires a different `GATEWAY_PUBLIC_BASE` / inventory.

Assume a compromised worker or leaked Watch URL is a **customer-incident** for that seat, not full multi-tenant breach of API keys — but treat the Compose network as a single trust zone.

## No worker `:8000` on the public face

Desktop workers listen on `:8000` **inside** the container. Default [`docker-compose.yml`](../docker-compose.yml) publishes **only** noVNC (`6081/6082 → 6080`), not control.

Checklist:

1. Do not add `ports: ["8000:8000"]` (or any host mapping to worker control) on `worker-*`.
2. Hosted deploy: bind published ports to **loopback** (`127.0.0.1:9000`, `127.0.0.1:6081`, …) and terminate TLS at Caddy — see [hosted.md](hosted.md).
3. Phone control (`8003`) and scrcpy (`6083`) likewise loopback-only on a public host.
4. Run [`scripts/hosted-security-check.sh`](../scripts/hosted-security-check.sh) in CI / before go-live.

Inventory `url` values stay as Docker DNS (`http://worker-1:8000`). Lease `vnc_url` / `api_base` use public hostnames only.

## Token rotation

### Ops token (`COMPUTER_API_TOKEN`)

1. Generate a new secret; set it in host `.env` (and any secret store).
2. `docker compose up -d --force-recreate gateway`.
3. Update Desk / ops clients; revoke old token by not keeping it anywhere.

### Customer API keys (`tenants.json`)

1. Edit the tenant’s `api_keys` list: add a new `cwa_…` key (or replace).
2. With **rw** mount, either upsert via billing path or edit file then `reload` / recreate gateway so the registry reloads.
3. Give the customer the new key; remove the old key from `api_keys` once they cut over.
4. Stripe-provisioned tenants: rotating without Portal is a file edit (same as above). Suspend (`disabled` / webhook) clears keys.

### Stripe / Phone Harness secrets

Rotate in the vendor dashboard, update `.env`, recreate gateway. Never commit live keys.

## Abuse controls (related)

Rate limits, kill switch (`disabled` / `ABUSE_DISABLED_TENANT_IDS`), recording quotas: [hosted.md](hosted.md) Abuse limits.

## Out of scope (for this note)

- Per-tenant network policies / service mesh  
- mTLS between gateway and workers  
- Automatic key expiry / OAuth for builders  
- Hardening the Linux desktop image beyond current Compose defaults  

## GA go-live checklist (security)

- [ ] `scripts/hosted-security-check.sh` passes on the deploy compose + override  
- [ ] Published ports are `127.0.0.1:…` on the host; Caddy owns public TLS  
- [ ] Worker control not reachable from the internet (`curl` from outside fails)  
- [ ] Ops + at least one tenant key rotated once in staging  
- [ ] Watch URLs not posted in public channels; treat lease `vnc_url` as capability URLs  
- [ ] `TENANTS_PATH` set; billing webhook secret set only if open signup is on  
