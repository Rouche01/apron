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
| `docker-compose.yml` | `gateway` + `worker-1` + `worker-2`; optional `phone-1` + `scrcpy-web` via profile `phone` |
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

- Gateway: `http://localhost:9000` (`/docs`, `/openapi.json`)
- noVNC: `http://localhost:6081/vnc.html?…` and `:6082` (lease `vnc_url` includes `autoconnect=1&resize=scale`)
- Phone HITL: Compose `scrcpy-web` on `:6083` (`/watch.html?udid=` from bind/lease; `/` is the device picker)

## Agent-facing API (gateway)

Base: `/v1` · Auth: `Authorization: Bearer $COMPUTER_API_TOKEN` (optional if token unset — dev only).

| Method | Path | Notes |
|--------|------|--------|
| `POST` | `/v1/sessions` | Lease a free worker (`kind`: `desktop` default, or `phone`) |
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
| `POST` | `/v1/seats/{id}/bind` | Phone: runtime bind (`serial` and/or wireless `connect`) — refused while leased |
| `POST` | `/v1/seats/{id}/unbind` | Phone: clear runtime bind (back to `ADB_SERIAL` env) |
| `GET` | `/health` · `/ready` | Ops aggregates |

Worker (internal): `GET /health`, `GET /screenshot`, `POST /action`, `POST /session/reset`.

Phone worker env: `ADB_SERIAL` (required if multiple devices), `ADB_CONNECT` (optional `host:port`), `RESET_FORCE_STOP_PACKAGES` / `RECYCLE_CLEAR_PACKAGES` (comma package lists). Soft park → force-stop; recycle / idle wipe → `pm clear`.

Compose `phone-1` + `scrcpy-web` share the bridge network and host `~/.android`. **First-time Allow:** on the phone, enable Wireless debugging (or USB), then from the Compose host run `adb connect ip:5555` and tap **Allow** / **Authorize this computer** when prompted — the same `~/.android` keys are mounted into both containers. After that you can rebind without recreating services: `POST /v1/seats/{id}/bind` with `{"connect":"ip:5555"}` (or `{"serial":"…"}` for USB). Boot env `ADB_SERIAL`/`ADB_CONNECT` is the default bind; runtime bind updates Watch `vnc_url` with `?udid=`. Inventory `vnc_url` points at scrcpy-web (`http://localhost:6083/watch.html`). Watch is **read-only**; drive the device via gateway `POST …/action` or the physical phone.

Pool states: **free** → **leased** → **parked** (idle TTL) → free after reset. Capacity is **fixed** (Compose workers), not on-demand.

## Conventions

- **Do not** put LLM calls in the gateway or worker. Bind models via `BINDING.md` / `schemas/`.
- Prefer **warm pool + park/reset** over spinning containers or K8s unless capacity/isolation forces it.
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
| `COMPOSE_PROFILES` | Set `phone` to start `phone-1` + `scrcpy-web` (use with `workers.with-phone.json`) |
| `ADB_SERIAL` / `ADB_CONNECT` | Default phone + scrcpy-web bind at boot (host must see the device; Allow once) |
| `SCRCPY_ADB_API_URL` | Gateway → scrcpy-web internal bind API (default `http://scrcpy-web:8001`) |
| `PHONE_HITL_URL` | Optional override mirrored into phone-1 health detail (default `http://localhost:6083/watch.html`) |
| `REQUIRE_WORKER_INVENTORY` | Compose/`Dockerfile.gateway` set to `1` — refuse legacy URL lists |
| `SESSION_TTL_SEC` / `PARKED_TTL_SEC` | Lease and parked idle TTLs |

**Per-environment inventory:** do not reuse one `vnc_url` across hosts. Point `.env` at the right file:

- Local desktop-only: `WORKER_INVENTORY_FILE=./gateway/workers.json`
- Local + phone: `workers.with-phone.json` + `COMPOSE_PROFILES=phone`
- ThinkPad/Caddy: copy `gateway/workers.hsv.example.json` → e.g. `gateway/workers.hsv.json`, edit hostnames (includes optional `phone1`)

On boot the gateway **logs each seat** (`id`, `kind`, `url`, `vnc_url`) and **exits** if the mount is missing/invalid (Docker empty-dir footgun). Confirm with `GET /v1/seats` after up.

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
| `@relay/engines-computer` | Pass `kind: "phone"` on `POST /v1/sessions`. Forward phone action bodies (`swipe`, `long_press`, `launch`, `key` with `back`/`home`/`recents`, optional `ref`). Prefer `GET …/observe` when the loop is vision+tree; fall back to screenshot pixels for custom views (Reels, etc.). |
| `@relay/browse-policy` (or phone system prompt) | Phone priors: swipe-heavy navigation, avoid desktop chords (`ctrl+l`), use `launch`/`uri` to open apps, `home`/`back` for system chrome. HITL **Watch** via lease `vnc_url` (scrcpy-web / `cu-phone.hsv`). |
| Desk / Settings | Inventory already exposes `kind` on `/v1/seats`; show phone seats separately from desktop noVNC. Open `vnc_url` for read-only Watch. Use `GET …/seats/{id}/devices` + `POST …/bind` so users can attach their own phone (document the one-time Allow prompt). |
| Desk take-over (intervene) | **relay-os Desk** — when `need_human`, drive via gateway `POST …/action` (same control plane as the agent), not by injecting clicks into scrcpy-web. Physical phone OK for local dogfood. |
| ThinkPad Caddy / `cu-phone.hsv` | **Deploy + Desk docs** (`relay-os/apps/relay-desk/LAPTOP.md`) — live Caddyfile Host route → `127.0.0.1:6083`. This repo documents the snippet and ships `workers.hsv.example.json`; applying Caddy on the host is not a computer-with-agent code change. |
| CommunityEngager | Stay **desktop evidence-only** unless an explicit phone transport is designed; do not silently lease `kind=phone` for Reddit. |

Park/recycle semantics: phone park → soft force-stop (packages stay logged in); recycle → `pm clear` on configured packages — map Relay “job done” to recycle only when a clean app state is intended.

Phone HITL Watch contract (this runtime): Compose `scrcpy-web` on `:6083` is **read-only**; inventory `vnc_url` is `http://localhost:6083/watch.html` or `https://cu-phone.hsv/watch.html`. See `.cursor/plans/phone_scrcpy_web_hitl.plan.md`.

## Out of scope (for now)

- On-demand container/Pod per lease
- Gateway-hosted vision/LLM loops
- Auto CAPTCHA solve
- Reliable Cmd+V into noVNC canvas (use `type` action or noVNC clipboard UI)

## Quick bind loop

```
POST /v1/sessions → screenshot → model → POST .../action → … → DELETE /v1/sessions/{id}
```

See `BINDING.md` for curl / Python / fetch examples and tool JSON.
