# Compose profiles & inventories

**Default (no profile):** gateway + `worker-1` + `worker-2` — two desktop seats. Inventory: [`gateway/workers.json`](../gateway/workers.json).

Set inventory with `WORKER_INVENTORY_FILE` in `.env` (Compose mounts that host file read-only). The file **must exist** before `up` or Docker may mount an empty directory and the gateway will exit.

| Goal | `.env` / env | Inventory file |
|------|----------------|----------------|
| Desktop only (default) | *(leave defaults)* | `./gateway/workers.json` |
| + physical Android | `COMPOSE_PROFILES=phone` | `./gateway/workers.with-phone.json` |
| + cloud Android | `COMPOSE_PROFILES=phone-cloud` + `PHONE_HARNESS_API_KEY` | `./gateway/workers.with-phone-cloud.json` |
| Physical + cloud | `COMPOSE_PROFILES=phone,phone-cloud` | usually `./gateway/workers.with-phone-cloud.json` (includes both seats) |
| k3s burst | `SEAT_BURST_BACKEND=k3s`, `DESKTOP_BURST_MAX` / `PHONE_BURST_MAX` | reserved inventory unchanged; see [`deploy/k3s/README.md`](../deploy/k3s/README.md) |

## Physical phone

1. Device reachable via ADB from the **Compose host** (wireless preferred; USB works when the daemon sees the device).
2. Authorize once: `adb connect HOST:5555` (or plug USB) → tap **Allow** on the phone. Host `~/.android` is mounted into `phone-1` and `scrcpy-web`.
3. `COMPOSE_PROFILES=phone` + inventory with-phone → `docker compose up --build -d`
4. Watch: `http://localhost:6083/watch.html` (lease `vnc_url` adds `?udid=`).
5. Agents pin with `"seat_id"` after `POST /v1/devices/register` (or bind). Unpinned `kind=phone` leases **cloud** only.

## Cloud phone

1. `PHONE_HARNESS_API_KEY` (or your registered `CloudPhoneProvider`) in `.env`.
2. Profile `phone-cloud` + cloud inventory → up.
3. `POST /v1/sessions` with `{"kind":"phone"}` provisions + binds; `vnc_url` is the vendor watch URL.
4. **Recycle** (or parked idle) tears down the vendor session — soft park keeps billing running.

## Burst (k3s)

Reserved Compose seats stay warm. When claimable seats of a `kind` are full and `*_BURST_MAX > 0`, the gateway spawns ephemeral workers via `WorkerFactory`. Default is off (`SEAT_BURST_BACKEND=disabled`, burst max `0`). Full steps: [`deploy/k3s/README.md`](../deploy/k3s/README.md).

## Custom Watch hostnames

If noVNC / phone Watch should use LAN or reverse-proxy hostnames (not `localhost`), copy an inventory example, edit `vnc_url`s, and point `WORKER_INVENTORY_FILE` at it. Keep worker `url`s as Docker DNS names (`http://worker-1:8000`, etc.) on the Compose network. Worked Tailscale/Caddy notes live in [AGENTS.md](../AGENTS.md).
