# OSS release smoke checklist

Success bar for public launch: a stranger can clone this repo, bring up the desktop stack, lease → act → recycle, and optionally revisit a recording. Unit tests run in CI without Docker.

## Automated (preferred)

### 1. CI (no Compose)

On every push/PR, [`.github/workflows/ci.yml`](../.github/workflows/ci.yml) runs gateway pytest:

- pool / burst / phone lease / register
- phone-harness provider
- recording store + API hooks

Locally:

```bash
cd gateway
pip install -r requirements-dev.txt
WORKER_INVENTORY_PATH=./workers.json REQUIRE_WORKER_INVENTORY=1 \
  COMPUTER_API_TOKEN= RECORDING_DIR=/tmp/cwa-recordings \
  python -m pytest \
    test_pool_burst.py test_pool_phone_lease.py test_pool_register.py \
    test_recording.py test_recording_api.py \
    providers/test_phone_harness.py -q
```

### 2. Compose golden path

From a clean tree (or fresh clone):

```bash
cp -n .env.example .env
docker compose up --build -d
./scripts/oss-smoke.sh
```

The script waits for `/health`, then: lease with `record:true` → screenshot → action → recycle → `GET /v1/recordings` + manifest.

Optional: open the lease `vnc_url` once during a manual run to confirm Watch.

Tear down when done: `docker compose down`.

## Manual checklist (release gate)

Use this when tagging an OSS launch (or after large gateway changes).

| Step | Expect |
|------|--------|
| Clean clone / checkout; no secrets in tree (see `github-public`) | `.env` not committed; `.env.example` has placeholders only |
| `cp -n .env.example .env` | Token defaults to `dev-token` |
| `docker compose up --build -d` | Gateway healthy; seats `desk1` / `desk2` free |
| `curl …/health` | `ok` / workers present |
| Lease desktop (`POST /v1/sessions`, optional `"record":true`) | `200`; `session_id`, `vnc_url`, `recording` as requested |
| Screenshot + one action | `200`; image base64 non-empty |
| Soft park or `?recycle=true` | `parked` or `recycled` |
| If recorded: `GET /v1/recordings?job_id=…` then `GET /v1/recordings/{id}` | Journal present after recycle; `ended_at` set; screenshot/action events |
| CI green on the release commit | Gateway unit job passed |

**Out of smoke scope:** physical/cloud phone profiles, k3s burst, hosted tenancy.

## See also

- [README.md](../README.md) — quick start  
- [BINDING.md](../BINDING.md) — bind loop + recording curl  
- [docs/v1.md](v1.md) — public contract  
- [CONTRIBUTING.md](../CONTRIBUTING.md) — PR expectations  
