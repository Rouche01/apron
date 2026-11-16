# AGENTS.md — Computer Runtime

Guidance for agents working in this repo. Inference stays **outside**; this project is a model-agnostic desktop control plane.

## What this is

Self-hosted **computer-use** stack: warm Linux desktops (XFCE + Firefox) and optional **Android phone** seats behind a **gateway** that leases exclusive sessions. Agents call HTTP (screenshot / input); humans watch via noVNC or scrcpy-web.

Sibling consumer: `~/Projects/relay-os` (`@relay/engines-computer`, CommunityEngager `REDDIT_TRANSPORT=computer`).

## Layout

| Path | Role |
|------|------|
| `Dockerfile` + `entrypoint.sh` + `server/app.py` | **Desktop worker** — Xvfb, desktop, noVNC `:6080`, control API `:8000` |
| `Dockerfile.phone` + `phone/` | **Phone worker** — ADB screenshot/tap/swipe/key/launch, soft/hard reset `:8000` |
| `Dockerfile.scrcpy` + `scrcpy-web/` | **Phone HITL** — Mel0nFish/ws-scrcpy-enhanced (scrcpy-server 3.3.4) on host `:6083` (profile `phone`; Android 16 OK) |
| `Dockerfile.gateway` + `gateway/` | **Gateway** — lease/park/release, proxy, Bearer auth, OpenAPI `:9000` |
| `gateway/providers/` | **Cloud phone providers** — `CloudPhoneProvider` registry; `phone-harness` first impl |
| `gateway/workers/` | **Burst WorkerFactory** — disabled by default; k3s backend for on-demand seats |
| `deploy/k3s/` | **k3s manifests** — namespace + desktop burst Job template |
| `docker-compose.yml` | `gateway` + `worker-1` + `worker-2`; optional `phone` / `phone-cloud` profiles |
| `schemas/` + `BINDING.md` | OpenAI / Anthropic-shaped tool maps for any agent host |
| `profile/` + `scripts/` | Firefox profile / launch helpers |

## Run (local)

```bash
cp -n .env.example .env
docker compose up --build -d
curl -s http://localhost:9000/health
curl -s -H "Authorization: Bearer $COMPUTER_API_TOKEN" -X POST http://localhost:9000/v1/sessions
```

Desktop + phone (third seat; ADB on the Compose host — USB or wireless):

```bash
# .env: WORKER_INVENTORY_FILE=./gateway/workers.with-phone.json
#       COMPOSE_PROFILES=phone
#       ADB_SERIAL=…   # and/or ADB_CONNECT=host:port
# Authorize wireless on the host first: adb connect ip:5555 (Allow on phone)
docker compose up --build -d
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:6083/   # expect 200
curl -s -H "Authorization: Bearer $COMPUTER_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"kind":"phone"}' http://localhost:9000/v1/sessions
# Open lease vnc_url (…/watch.html) — live stream; intervene via gateway action / physical device
```

Desktop + **cloud** phone (Phone Harness; no physical phone / no scrcpy):

```bash
# .env: WORKER_INVENTORY_FILE=./gateway/workers.with-phone-cloud.json
#       COMPOSE_PROFILES=phone-cloud   # or phone,phone-cloud with physical too
#       PHONE_HARNESS_API_KEY=pck_…
docker compose up --build -d
curl -s -H "Authorization: Bearer $COMPUTER_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"kind":"phone"}' http://localhost:9000/v1/sessions
# Prefer leasing until seat phone-cloud1 is claimed (or free physical first).
# Open lease vnc_url — vendor watch_url (view-only). Recycle to stop billing.
```

- Gateway: `http://localhost:9000` (`/docs`, `/openapi.json`)
- noVNC: `http://localhost:6081/vnc.html?…` and `:6082` (lease `vnc_url` includes `autoconnect=1&resize=scale`)
- Phone HITL (physical): Compose `scrcpy-web` on `:6083` (`/watch.html?udid=` from bind/lease; `/` is the device picker)
- Phone HITL (cloud): lease `vnc_url` is the provider `watch_url` (not scrcpy)

## Agent-facing API (gateway)

Base: `/v1` · Auth: `Authorization: Bearer $COMPUTER_API_TOKEN` (optional if token unset — dev only).

| Method | Path | Notes |
|--------|------|--------|
| `POST` | `/v1/sessions` | Lease a free worker (`kind`: `desktop` default, or `phone`). Phone without `seat_id` → cloud only; pass `seat_id` to pin physical (or any) seat. |
| `POST` | `/v1/sessions/{id}/heartbeat` | Extend lease TTL |
| `POST` | `/v1/sessions/{id}/acquire` | Re-lease a **parked** desktop |
| `DELETE` | `/v1/sessions/{id}` | Soft release → **park** (warm); `?recycle=true` wipes + frees |
| `POST` | `/v1/sessions/{id}/recycle` | Hard wipe + free |
| `GET` | `/v1/sessions/{id}/screenshot` | Base64 PNG |
| `GET` | `/v1/sessions/{id}/observe` | Phone: PNG + trimmed UIAutomator `elements[]` (desktop: 404) |
| `POST` | `/v1/sessions/{id}/action` | Forward to worker (`click`/`tap`, `swipe`, `type`, `key`, `launch`, `ref`, …) |
| `GET` | `/v1/seats` | Per-worker inventory (`kind` + `free`/`leased`/`parked` + `session_id`/`vnc_url`/`job_id`) |
| `GET` | `/v1/seats/{id}/devices` | Phone: adb devices visible to the seat |
| `GET` | `/v1/seats/{id}/device` | Phone: current bind + selected device |
| `POST` | `/v1/devices/register` | Phone (physical): pick free unbound seat (or `seat_id`) + bind; if none and `PHONE_WARM_SPAWN_MAX>0`, spawn warm reserved worker; returns `seat_id` for lease pin |
| `POST` | `/v1/devices/unregister` | Phone (physical): destroy a **warm-spawned** seat (unbind + factory destroy); inventory seats use `/unbind` only |
| `POST` | `/v1/seats/{id}/bind` | Phone (physical): runtime bind (`serial` / `connect` / optional `unlock`) — refused while leased or on cloud seats |
| `POST` | `/v1/seats/{id}/unbind` | Phone (physical): clear runtime bind — refused on cloud seats |
| `GET` | `/health` · `/ready` | Ops aggregates |

Worker (internal): `GET /health`, `GET /screenshot`, `POST /action`, `POST /session/reset`.

Phone worker env: `ADB_SERIAL` (required if multiple devices), `ADB_CONNECT` (optional `host:port`), `ALLOW_EMPTY_DEVICE` (cloud worker: healthy before bind), `RESET_FORCE_STOP_PACKAGES` / `RECYCLE_CLEAR_PACKAGES` (comma package lists). Soft park → force-stop; recycle / idle wipe → `pm clear`.

Compose `phone-1` + `scrcpy-web` share the bridge network and host `~/.android`. **First-time Allow:** on the phone, enable Wireless debugging (or USB), then from the Compose host run `adb connect ip:5555` and tap **Allow** / **Authorize this computer** when prompted — the same `~/.android` keys are mounted into both containers. After that you can rebind without recreating services: `POST /v1/seats/{id}/bind` with `{"connect":"ip:5555"}` (or `{"serial":"…"}` for USB). Boot env `ADB_SERIAL`/`ADB_CONNECT` is the default bind; runtime bind updates Watch `vnc_url` with `?udid=`. Inventory `vnc_url` points at scrcpy-web (`http://localhost:6083/watch.html`). Watch is **read-only**; drive the device via gateway `POST …/action` or the physical phone.

### Cloud phone seats (`CloudPhoneProvider`)

Inventory seats may set `"provider": "phone-harness"` (kind still `phone`). On lease the gateway:

1. Calls the registered provider (`gateway/providers/`) — provision until ADB-ready
2. Binds `phone-cloud-1` with `connect` + vendor `unlock`
3. Sets lease `vnc_url` to the provider **watch_url** (view-only HITL)

Soft **park** keeps the cloud session (billing continues). **Recycle** / parked-idle free calls provider **teardown** (stops billing). Manual `/v1/seats/{id}/bind` is refused on cloud seats.

Add another ADB+watch vendor later: new module under `gateway/providers/`, `register()`, add id to `KNOWN_CLOUD_PROVIDERS` in `gateway/inventory.py`, inventory `provider` string. Pool never imports vendor SDKs.

Pool states: **free** → **leased** → **parked** (idle TTL) → free after reset (ephemeral **lease-burst** seats are **destroyed**, not returned to free; **warm-spawned** physical seats survive recycle until `POST /v1/devices/unregister`).

Capacity:

- **Reserved** Compose inventory (desktop + optional physical/cloud phone)
- **Lease burst** (opt-in): when claimable seats of a `kind` are full → k3s `WorkerFactory` (`DESKTOP_BURST_MAX` / `PHONE_BURST_MAX`, default `0`). Phone burst = phone worker Pod + `CloudPhoneProvider` (not a physical phone).
- **Physical register** (ops): `POST /v1/devices/register` binds a physical phone; optional warm spawn when collection full (`PHONE_WARM_SPAWN_MAX`, default `0`). Agents pin with `seat_id`. Default `kind=phone` lease is **cloud-only**.

Plans: [`.cursor/plans/elastic_seats.plan.md`](.cursor/plans/elastic_seats.plan.md) (desktop + burst factory) · [`.cursor/plans/phone_seat_flows_8a1a7577.plan.md`](.cursor/plans/phone_seat_flows_8a1a7577.plan.md) (phone cloud default, pin, register, phone burst). Constraints: one desktop worker per seat; one ADB bind per phone worker.

## Conventions

- **Do not** put LLM calls in the gateway or worker. Bind models via `BINDING.md` / `schemas/`.
- Prefer **warm pool + park/reset** for reserved seats. Use **k3s burst** (not ad-hoc Docker socket) when you need elastic capacity beyond inventory.
- Keep worker `:8000` off the public/tailnet face; publish noVNC + gateway via Caddy (or localhost only).
- Display: set **`ASPECT_RATIO`** (e.g. `16:9`, `16:10`) + **`WIDTH`**; `entrypoint.sh` derives `HEIGHT` (default `16:10` → 1280×800). Explicit `WIDTH`+`HEIGHT` with empty `ASPECT_RATIO` still works. VNC uses **local** `resize=scale`, not remote desktop resize.
- Gateway injects `autoconnect=1&resize=scale` on lease `vnc_url` if missing.

### Env that matters

| Variable | Meaning |
|----------|---------|
| `COMPUTER_API_TOKEN` | Bearer for gateway |
| `GATEWAY_PUBLIC_BASE` | Origin baked into `api_base` (e.g. `http://desk.hsv` on ThinkPad) |
| `WORKER_INVENTORY_PATH` | Path to JSON array of seats (`url`, `vnc_url`, `kind`, optional `id`) |
| `WORKER_INVENTORY_FILE` | Host path Compose mounts to that file (default `./gateway/workers.json`) |
| `COMPOSE_PROFILES` | `phone` → `phone-1` + `scrcpy-web`; `phone-cloud` → `phone-cloud-1` (comma-combine both) |
| `ADB_SERIAL` / `ADB_CONNECT` | Default phone + scrcpy-web bind at boot (host must see the device; Allow once) |
| `SCRCPY_ADB_API_URL` | Gateway → scrcpy-web internal bind API (default `http://scrcpy-web:8001`) |
| `PHONE_HITL_URL` | Optional override mirrored into phone-1 health detail (default `http://localhost:6083/watch.html`) |
| `PHONE_HARNESS_API_KEY` | Bearer for Phone Harness (`pck_…`); required to lease cloud seats |
| `PHONE_HARNESS_API_BASE` | Default `https://api.phone-harness.com` |
| `PHONE_HARNESS_TIMEOUT_SEC` | Cloud session lifetime floor (default max(SESSION+PARKED, 900)) |
| `PHONE_HARNESS_PROFILE_ID` | Optional persistent harness profile |
| `REQUIRE_WORKER_INVENTORY` | Compose/`Dockerfile.gateway` set to `1` — refuse legacy URL lists |
| `SESSION_TTL_SEC` / `PARKED_TTL_SEC` | Lease and parked idle TTLs |
| `SEAT_BURST_BACKEND` | `disabled` (default) or `k3s` — on-demand worker factory |
| `DESKTOP_BURST_MAX` / `PHONE_BURST_MAX` | Max ephemeral seats beyond reserved (default `0`) |
| `PHONE_BURST_PROVIDER` | Cloud provider id for phone burst seats (default `phone-harness`) |
| `SEAT_BURST_PHONE_IMAGE` | k3s phone worker image (default `computer-with-agent-phone:local`) |
| `PHONE_WARM_SPAWN_MAX` | Max register-spawned reserved physical seats (default `0` = bind existing only) |
| `KUBECONFIG` | kubeconfig for k3s burst (when backend is `k3s`) |

**Per-environment inventory:** do not reuse one `vnc_url` across hosts. Point `.env` at the right file:

- Local desktop-only: `WORKER_INVENTORY_FILE=./gateway/workers.json`
- Local + physical phone: `workers.with-phone.json` + `COMPOSE_PROFILES=phone`
- Local + cloud (and optional physical): `workers.with-phone-cloud.json` + `COMPOSE_PROFILES=phone-cloud` (add `phone` if both)
- ThinkPad/Caddy: copy `gateway/workers.hsv.example.json` → e.g. `gateway/workers.hsv.json`, edit hostnames (includes optional `phone1`)

On boot the gateway **logs each seat** (`id`, `kind`, `provider`, `url`, `vnc_url`) and **exits** if the mount is missing/invalid (Docker empty-dir footgun) or an inventory `provider` is unregistered. Confirm with `GET /v1/seats` after up.

Legacy (only if `WORKER_INVENTORY_PATH` is unset and `REQUIRE_WORKER_INVENTORY` is off): `WORKER_URLS` + `WORKER_VNC_URLS` → all `desktop`.

## ThinkPad / Caddy (`.hsv`)

Match relay-os deploy style: backends on `127.0.0.1`, Caddy Host routing, laptop `/etc/hosts` → Tailscale IP.

Suggested names: **`desk.hsv`** / **`desk1.hsv`** / **`desk2.hsv`**, or the live Desk deploy pair **`cu-gateway.hsv`** / **`cu1.hsv`** / **`cu2.hsv`** (phone HITL e.g. **`cu-phone.hsv`**). Laptop install + Settings: `relay-os/apps/relay-desk/LAPTOP.md`.

Phone ADB must reach the device **from the ThinkPad** (wireless ADB preferred; USB into ThinkPad OK). Laptop-only USB does not attach to a ThinkPad-hosted `phone-1`.

On deploy hosts, bind Compose ports to loopback:

```yaml
ports:
  - "127.0.0.1:9000:9000"
  - "127.0.0.1:6081:6080"
  - "127.0.0.1:6082:6080"
  - "127.0.0.1:6083:8000"   # scrcpy-web (profile phone)
```

Caddy Host route for phone Watch (apply on the deploy host; see also `relay-os/apps/relay-desk/LAPTOP.md`):

```caddy
cu-phone.hsv {
  reverse_proxy 127.0.0.1:6083
}
```

Set `GATEWAY_PUBLIC_BASE` and edit the inventory JSON `vnc_url`s to those hostnames (see `gateway/workers.hsv.example.json` — phone uses `https://cu-phone.hsv/watch.html`). Keep desktop worker `url`s as Docker DNS; phone control URL is `http://phone-1:8003` when using the Compose `phone` profile (bridge).

## Relay OS

- Package: `@relay/engines-computer` (`ExecutionEngine` type `"computer"`)
- CommunityEngager: `REDDIT_TRANSPORT=computer` is **evidence-only** (lease → open thread URL → screenshot → release; **never posts**). Needs `COMMUNITY_POST_MODE=auto` to reach execute; `COMPUTER_GATEWAY_URL` + matching token.

### Downstream bind notes (phone) — no relay-os changes required for phase 1

Agents can curl this gateway today. When Relay grows phone seats, keep inference/persona in relay-os and treat this repo as the device control plane only.

| Relay surface | Suggested change (later) |
|---------------|--------------------------|
| `@relay/engines-computer` | Pass `kind: "phone"` on `POST /v1/sessions` (cloud default). Optional `seat_id` when Desk pre-registered a physical phone. Forward phone action bodies (`swipe`, `long_press`, `launch`, `key` with `back`/`home`/`recents`, optional `ref`). Prefer `GET …/observe` when the loop is vision+tree; fall back to screenshot pixels for custom views (Reels, etc.). |
| `@relay/browse-policy` (or phone system prompt) | Phone priors: swipe-heavy navigation, avoid desktop chords (`ctrl+l`), use `launch`/`uri` to open apps, `home`/`back` for system chrome. HITL **Watch** via lease `vnc_url` (scrcpy-web / `cu-phone.hsv`). |
| Desk / Settings | `/v1/seats` exposes `kind`, `provider`, `ephemeral`, `warm_spawned`, `bound_serial`. Physical prep: `POST /v1/devices/register` → copy `seat_id` into the run; Watch via `vnc_url`. Cloud: omit `seat_id` on lease (no manual bind). |
| Desk take-over (intervene) | **relay-os Desk** — when `need_human`, drive via gateway `POST …/action` (same control plane as the agent), not by injecting clicks into scrcpy-web. Physical phone OK for local dogfood. |
| ThinkPad Caddy / `cu-phone.hsv` | **Deploy + Desk docs** (`relay-os/apps/relay-desk/LAPTOP.md`) — live Caddyfile Host route → `127.0.0.1:6083`. This repo documents the snippet and ships `workers.hsv.example.json`; applying Caddy on the host is not a computer-with-agent code change. |
| CommunityEngager | Stay **desktop evidence-only** unless an explicit phone transport is designed; do not silently lease `kind=phone` for Reddit. |

Park/recycle semantics: phone park → soft force-stop (packages stay logged in); recycle → `pm clear` on configured packages — map Relay “job done” to recycle only when a clean app state is intended.

Phone HITL Watch contract (this runtime): Compose `scrcpy-web` on `:6083` is **read-only**; inventory `vnc_url` is `http://localhost:6083/watch.html` or `https://cu-phone.hsv/watch.html`. Cloud seats use the provider `watch_url` instead. See [`.cursor/plans/phone_seat_flows_8a1a7577.plan.md`](.cursor/plans/phone_seat_flows_8a1a7577.plan.md) (selection + register + burst) and older HITL notes under `.cursor/plans/phone_scrcpy_web_hitl.plan.md` if present.

## Out of scope (for now)

- Multi-cluster / HPA-based scaling (lease-demand burst is opt-in via `*_BURST_MAX`; see elastic + phone_seat_flows plans)
- Gateway-hosted vision/LLM loops
- Auto CAPTCHA solve
- Reliable Cmd+V into noVNC canvas (use `type` action or noVNC clipboard UI)
- Non-ADB cloud tooling inside `CloudPhoneProvider` (different worker/kind)
- Lease-time spawn of **physical** workers (physical phones grow via register / warm spawn only)

## Quick bind loop

```
POST /v1/sessions → screenshot → model → POST .../action → … → DELETE /v1/sessions/{id}
```

See `BINDING.md` for curl / Python / fetch examples and tool JSON.
