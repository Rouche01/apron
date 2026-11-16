# Binding any agent / model to the Computer Runtime

Inference stays **outside** this project. Your agent leases a seat, loops (screenshot → model → action), then soft-releases (or recycles when the job is done).

Default display (desktop): **1280×800**. Gateway: `http://localhost:9000` (Compose).

Seat **kind** (inventory JSON via `WORKER_INVENTORY_PATH`): `desktop` (default) or `phone`. Optional inventory `provider` (e.g. `phone-harness`) marks a cloud phone seat — same `kind: phone` lease for agents; gateway provisions ADB + `watch_url`. `POST /v1/sessions` body field `kind` selects the pool; omit it for desktop. Lease response includes `kind` and `seat_id`.

**Phone selection:** `{"kind":"phone"}` claims **cloud** seats only (inventory `provider` set), then phone **lease-burst** if `PHONE_BURST_MAX>0`. Physical glass is opt-in: register/bind via ops, then pin with `"seat_id"`. `job_id` still reattaches a parked seat of either flavor. Exact pin errors: `404` / `400` / `409` (`seat_not_found` / `seat_kind_mismatch` / `seat_busy`). Full model: [`.cursor/plans/phone_seat_flows_8a1a7577.plan.md`](.cursor/plans/phone_seat_flows_8a1a7577.plan.md).

**Register physical (ops):** `POST /v1/devices/register` with `{"connect":"ip:5555"}` (or `serial`) binds the first free unbound physical seat and returns `{seat_id, bound_serial, vnc_url}` for the agent pin. Optional `"seat_id"` rebinds that seat. If none are free and `PHONE_WARM_SPAWN_MAX>0` with `SEAT_BURST_BACKEND=k3s`, spawns a **warm reserved** phone worker (`warm_spawned`, survives recycle) then binds. Tear down with `POST /v1/devices/unregister` `{"seat_id":"phone-warm-…"}` (inventory seats: `/v1/seats/{id}/unbind` only).

**Relay OS:** phase 1 needs no Relay package changes — bind with HTTP as below. Later work in `@relay/engines-computer` / browse-policy is sketched under **Relay OS → Downstream bind notes** in [`AGENTS.md`](AGENTS.md).

## Lease vs park vs recycle

| Concept | What it means |
|---------|----------------|
| **Lease** | Exclusive input claim on a worker (`POST /v1/sessions`, optional `kind`). |
| **Park** | Soft `DELETE` — drop the claim, **keep Firefox warm** for the same `session_id` / `job_id`. Worker is not free for other jobs. |
| **Acquire / reattach** | Take the claim again: `POST …/acquire` (by `session_id`) or `POST /v1/sessions` with the same `job_id`. |
| **Recycle** | Wipe Firefox and free the worker: `DELETE …?recycle=true` or `POST …/recycle`. Also happens after parked idle (`PARKED_TTL_SEC`, default 1800s). |

Lease TTL expiry **parks** (does not wipe). Use recycle when the job is truly finished so the next unrelated lease gets a clean browser.

Workers use a dedicated `relay-agent` Firefox profile; recycle / `/session/reset` clears session-restore artifacts so “Restore Session” does not appear.

## 1. Auth

```bash
export COMPUTER_API_TOKEN=dev-token
export GW=http://localhost:9000
export AUTH="Authorization: Bearer $COMPUTER_API_TOKEN"
```

## 2. Lease → act → soft release

```bash
# Lease (kind defaults to desktop; use "phone" when a phone seat exists)
SESSION=$(curl -s -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"agent_id":"demo","job_id":"demo-job","ttl_sec":600,"kind":"desktop"}' \
  "$GW/v1/sessions")
echo "$SESSION" | jq .
SID=$(echo "$SESSION" | jq -r .session_id)
VNC=$(echo "$SESSION" | jq -r .vnc_url)
echo "Watch: $VNC kind=$(echo "$SESSION" | jq -r .kind) seat=$(echo "$SESSION" | jq -r .seat_id)"

# Screenshot (base64 PNG in .image)
curl -s -H "$AUTH" "$GW/v1/sessions/$SID/screenshot" | jq -r .image | head -c 64
echo …

# Focus Firefox address bar and open a URL
curl -s -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"type":"key","text":"ctrl+l"}' \
  "$GW/v1/sessions/$SID/action"
curl -s -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"type":"type","text":"https://example.com"}' \
  "$GW/v1/sessions/$SID/action"
curl -s -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"type":"key","text":"Return"}' \
  "$GW/v1/sessions/$SID/action"

# Heartbeat if the job is long
curl -s -H "$AUTH" -X POST "$GW/v1/sessions/$SID/heartbeat"

# Soft release — parks the warm desktop (tabs/cookies kept)
curl -s -H "$AUTH" -X DELETE "$GW/v1/sessions/$SID"
# → { "ok": true, "session_id": "...", "parked": true }
```

When the pool has no **free** workers of the requested `kind` (parked seats do not count as free), `POST /v1/sessions` returns **503** — retry, acquire an existing park, or wait for recycle / parked TTL.

## 3. Serial leases (same job)

Keep the desktop across claim gaps (HITL pause, staged steps):

```bash
# … after soft DELETE above …

# Option A — same session_id
SESSION=$(curl -s -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"ttl_sec":600}' \
  -X POST "$GW/v1/sessions/$SID/acquire")
echo "$SESSION" | jq .

# Option B — same job_id (reattaches the parked desktop)
SESSION=$(curl -s -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"agent_id":"demo","job_id":"demo-job","ttl_sec":600}' \
  "$GW/v1/sessions")
SID=$(echo "$SESSION" | jq -r .session_id)

# … more act/screenshot …

# Job finished — wipe Firefox and free the worker
curl -s -H "$AUTH" -X DELETE "$GW/v1/sessions/$SID?recycle=true"
# or: curl -s -H "$AUTH" -X POST "$GW/v1/sessions/$SID/recycle"
```

Mid-lease wipe without releasing: `POST /v1/sessions/$SID/reset`.

## 4. Agent loop (pseudocode)

```
session = POST /v1/sessions   # optional job_id for reattach
try:
  while not done:
    if need_to_yield_claim:          # HITL / pause
      DELETE /v1/sessions/{id}       # park
      # later: POST …/acquire  or  POST /v1/sessions { job_id }
    shot = GET  .../screenshot
    decision = your_model(shot, tools)
    if decision is tool call:
      map tool → POST .../action
    POST .../heartbeat periodically
finally:
  DELETE /v1/sessions/{id}?recycle=true   # job done
```

OpenAPI live docs: `$GW/docs` · schema: `$GW/openapi.json`

## 5. Tool schemas (drop into your host)

| File | Use when |
|------|----------|
| [`schemas/openai-tools.json`](schemas/openai-tools.json) | OpenAI / compatible `tools` function calling |
| [`schemas/anthropic-computer-use.json`](schemas/anthropic-computer-use.json) | Map Anthropic-style `action` names → gateway bodies |

OpenAI-shaped tools expose only the `function` objects to the model; use each tool’s `http` hint in your executor.

Example executor mapping (desktop + shared):

| Tool / Anthropic action | Gateway |
|-------------------------|---------|
| `computer_screenshot` / `screenshot` | `GET .../screenshot` |
| `computer_click` / `left_click` | `{ "type":"click", "x", "y" }` |
| `computer_type` / `type` | `{ "type":"type", "text" }` |
| `computer_key` / `key` | `{ "type":"key", "text" }` |
| `computer_scroll` / `scroll` | `{ "type":"scroll", "direction", "amount" }` |

### Phone actions (lease `kind: "phone"`)

Coordinates are **screenshot pixels** (device-reported size, e.g. 1080×2340). Prefer phone tools when the seat is a phone; desktop chords like `ctrl+l` do not apply.

| Tool / action | Gateway body |
|---------------|--------------|
| `computer_click` / `tap` | `{ "type":"tap", "x", "y" }` (alias: `click`) |
| `computer_long_press` | `{ "type":"long_press", "x", "y", "duration_ms"? }` |
| `computer_swipe` | `{ "type":"swipe", "x1", "y1", "x2", "y2", "duration_ms"? }` |
| `computer_scroll` | `{ "type":"scroll", "direction", "amount"? }` (mid-screen swipe) |
| `computer_type` | `{ "type":"type", "text" }` (ADB `input text`; limited special chars) |
| `computer_key` | `{ "type":"key", "text":"back"\|"home"\|"recents"\|… }` |
| `computer_launch` | `{ "type":"launch", "package"? , "uri"? }` |
| observe (HTTP) | `GET .../observe` → `{ image, elements[{ref,text,content_desc,bounds,center,clickable,…}] }` |
| tap by ref | `{ "type":"tap", "ref":"e3" }` (re-dumps tree; prefers `resource-id` refs when unique) |

### Bind your own phone to a seat (physical)

Compose mounts host `~/.android` into `phone-1` and `scrcpy-web`. The human taps **Allow** / **Authorize this computer** once when that adb key first appears (USB or Wireless debugging). After that, Desk or curl can rebind without recreating containers. Bind is refused while the seat is **leased**, and refused entirely on **cloud** seats (`provider` set).

```bash
# Optional: authorize on the host first (shows the Allow prompt on the phone)
# adb connect 192.168.1.20:5555

SEAT=phone1   # id from GET /v1/seats
curl -s -H "$AUTH" "$GW/v1/seats/$SEAT/devices" | jq .

# Wireless — connect + select (serial becomes ip:port)
curl -s -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"connect":"192.168.1.20:5555"}' \
  "$GW/v1/seats/$SEAT/bind" | jq '{ok, bound_serial, vnc_url, hint}'

# Or USB serial already visible to adb
# curl -s -H "$AUTH" -H 'Content-Type: application/json' \
#   -d '{"serial":"R5CY53CAB6N"}' "$GW/v1/seats/$SEAT/bind" | jq .

# If ok=false / unauthorized: tap Allow on the phone, then retry bind.
# Lease vnc_url includes ?udid=… so Watch opens that device.
```

### Cloud phone (Phone Harness) dogfood

Inventory seat with `"provider": "phone-harness"` (see `gateway/workers.with-phone-cloud.json`). Agents still lease `kind: "phone"` — no agent API change. Soft park keeps billing; recycle tears down the harness session.

```bash
# .env: COMPOSE_PROFILES=phone-cloud
#       WORKER_INVENTORY_FILE=./gateway/workers.with-phone-cloud.json
#       PHONE_HARNESS_API_KEY=pck_…
# docker compose up --build -d

curl -s -H "$AUTH" "$GW/v1/seats" | jq '.seats[] | {id, kind, provider, state, vnc_url, ephemeral, warm_spawned, bound_serial}'

# kind=phone without seat_id → cloud only (skips free physical seats).
SESSION=$(curl -s -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"kind":"phone","agent_id":"cloud-demo","ttl_sec":600}' \
  "$GW/v1/sessions")
echo "$SESSION" | jq '{session_id, seat_id, kind, vnc_url}'
SID=$(echo "$SESSION" | jq -r .session_id)
# expect seat_id=phone-cloud1 (or phone-burst-* when PHONE_BURST_MAX>0 and reserved cloud is full)
# Open .vnc_url in a browser — provider watch_url (view-only). No Allow prompt.

curl -s -H "$AUTH" "$GW/v1/sessions/$SID/observe" | jq '{geometry, element_count}'
curl -s -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"type":"key","text":"home"}' \
  "$GW/v1/sessions/$SID/action"

# Soft park (cloud session kept):
# curl -s -H "$AUTH" -X DELETE "$GW/v1/sessions/$SID"
# Job done — teardown cloud + free seat (stops billing):
curl -s -H "$AUTH" -X DELETE "$GW/v1/sessions/$SID?recycle=true"
```

Adding another ADB+watch cloud vendor: implement `CloudPhoneProvider` in `gateway/providers/`, register it, extend `KNOWN_CLOUD_PROVIDERS`, set inventory `provider`.

### Physical phone — register then pin

```bash
# Compose profile phone (+ optional phone-cloud). Allow once on the device first.
REG=$(curl -s -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"connect":"192.168.178.65:5555"}' \
  "$GW/v1/devices/register")
echo "$REG" | jq '{seat_id, bound_serial, vnc_url, warm_spawned}'
SEAT=$(echo "$REG" | jq -r .seat_id)

# Agent / pipeline: pin the prepared glass (cloud default will not pick it otherwise)
SESSION=$(curl -s -H "$AUTH" -H 'Content-Type: application/json' \
  -d "{\"kind\":\"phone\",\"seat_id\":\"$SEAT\",\"agent_id\":\"demo\",\"ttl_sec\":600}" \
  "$GW/v1/sessions")
SID=$(echo "$SESSION" | jq -r .session_id)
# Watch: open .vnc_url (scrcpy-web /watch.html?udid=… on :6083) — read-only HITL
echo "$SESSION" | jq '{session_id, kind, seat_id, vnc_url}'

# Warm-spawned seats only — destroy worker when glass leaves the fleet:
# curl -s -H "$AUTH" -H 'Content-Type: application/json' \
#   -d "{\"seat_id\":\"$SEAT\"}" "$GW/v1/devices/unregister"
```

```bash
# Legacy: bind a known seat id, then lease with the same seat_id
# POST /v1/seats/phone1/bind  {"connect":"…"}
SESSION=$(curl -s -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"kind":"phone","seat_id":"phone1","agent_id":"demo","ttl_sec":600}' \
  "$GW/v1/sessions")
SID=$(echo "$SESSION" | jq -r .session_id)
echo "$SESSION" | jq '{session_id, kind, seat_id, vnc_url}'

# Vision + tree
OBS=$(curl -s -H "$AUTH" "$GW/v1/sessions/$SID/observe")
echo "$OBS" | jq '{geometry, element_count, sample: .elements[:3]}'

curl -s -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"type":"swipe","x1":540,"y1":1800,"x2":540,"y2":600,"duration_ms":350}' \
  "$GW/v1/sessions/$SID/action"

curl -s -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"type":"key","text":"home"}' \
  "$GW/v1/sessions/$SID/action"

curl -s -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"type":"launch","package":"com.android.settings"}' \
  "$GW/v1/sessions/$SID/action"
```

## 6. Minimal Python bind

```python
import os, httpx

GW = os.environ.get("GW", "http://localhost:9000")
TOKEN = os.environ["COMPUTER_API_TOKEN"]
H = {"Authorization": f"Bearer {TOKEN}"}

with httpx.Client(base_url=GW, headers=H, timeout=60) as c:
    lease = c.post(
        "/v1/sessions",
        json={"agent_id": "py", "job_id": "py-job"},
    ).raise_for_status().json()
    sid = lease["session_id"]
    try:
        img = c.get(f"/v1/sessions/{sid}/screenshot").json()["image"]
        # … pass img to your model …
        c.post(
            f"/v1/sessions/{sid}/action",
            json={"type": "key", "text": "ctrl+l"},
        ).raise_for_status()
    finally:
        # Soft park: c.delete(f"/v1/sessions/{sid}")
        # Job done — wipe:
        c.delete(f"/v1/sessions/{sid}", params={"recycle": "true"})
```

## 7. Minimal fetch bind

```js
const GW = process.env.GW ?? "http://localhost:9000";
const auth = { Authorization: `Bearer ${process.env.COMPUTER_API_TOKEN}` };

const lease = await fetch(`${GW}/v1/sessions`, {
  method: "POST",
  headers: { ...auth, "Content-Type": "application/json" },
  body: JSON.stringify({ agent_id: "js", job_id: "js-job" }),
}).then((r) => r.json());

const sid = lease.session_id;
try {
  const { image } = await fetch(`${GW}/v1/sessions/${sid}/screenshot`, {
    headers: auth,
  }).then((r) => r.json());
  // … model …
  await fetch(`${GW}/v1/sessions/${sid}/action`, {
    method: "POST",
    headers: { ...auth, "Content-Type": "application/json" },
    body: JSON.stringify({ type: "click", x: 100, y: 200 }),
  });
} finally {
  await fetch(`${GW}/v1/sessions/${sid}?recycle=true`, {
    method: "DELETE",
    headers: auth,
  });
}
```

## 8. Ops tips

- Prefer `lease.api_base` from the lease response as the session prefix.
- Send `X-Job-Id` (or body `job_id`) so serial steps can reattach and logs correlate.
- Open `vnc_url` for human watch / HITL; do not expose worker `:8000` publicly.
- `/health` and `/ready` report `free` / `leased` / `parked` — only **free** seats accept unrelated new leases.
- `GET /v1/seats` (auth) lists each worker with `id`, `kind`, `state`, `session_id`, `vnc_url`, `job_id` so operators can Watch / Acquire / Recycle abandoned parks without guessing ids. After deploy, check `vnc_url`s match this environment (localhost vs `.hsv`).
- Inventory is selected by `WORKER_INVENTORY_FILE` in `.env`; gateway logs every seat at startup and refuses to start if the file mount is missing.
- Compose: `docker compose up --build` then bind against port **9000**.
