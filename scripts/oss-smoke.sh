#!/usr/bin/env bash
# Desktop golden-path smoke against a running Compose stack.
# Usage (from repo root, after `docker compose up --build -d`):
#   ./scripts/oss-smoke.sh
# Env: GW (default http://localhost:9000), COMPUTER_API_TOKEN (default from .env or dev-token)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

GW="${GW:-http://localhost:9000}"
TOKEN="${COMPUTER_API_TOKEN:-}"
if [[ -z "$TOKEN" && -f .env ]]; then
  # shellcheck disable=SC1091
  set -a
  # Only pull the token line if present; ignore other .env side effects carefully
  TOKEN="$(grep -E '^COMPUTER_API_TOKEN=' .env | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'")"
  set +a
fi
TOKEN="${TOKEN:-dev-token}"
AUTH="Authorization: Bearer ${TOKEN}"

need() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "missing dependency: $1" >&2
    exit 1
  }
}
need curl
need python3

echo "==> health"
for i in $(seq 1 60); do
  if curl -sf "$GW/health" >/dev/null; then
    break
  fi
  if [[ "$i" -eq 60 ]]; then
    echo "gateway not healthy at $GW/health" >&2
    exit 1
  fi
  sleep 2
done
curl -sf "$GW/health" | python3 -m json.tool

echo "==> lease (record=true)"
LEASE="$(curl -sf -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"agent_id":"oss-smoke","job_id":"oss-smoke-job","record":true,"ttl_sec":600}' \
  "$GW/v1/sessions")"
echo "$LEASE" | python3 -m json.tool
SID="$(echo "$LEASE" | python3 -c 'import json,sys; print(json.load(sys.stdin)["session_id"])')"
REC="$(echo "$LEASE" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("recording"))')"
if [[ "$REC" != "True" && "$REC" != "true" ]]; then
  echo "expected recording=true on lease, got: $REC" >&2
  exit 1
fi

echo "==> screenshot"
SHOT="$(curl -sf -H "$AUTH" "$GW/v1/sessions/$SID/screenshot")"
python3 -c 'import json,sys; d=json.load(sys.stdin); assert d.get("image"), d; print("image_b64_len", len(d["image"]))' <<<"$SHOT"

echo "==> action (key)"
curl -sf -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"type":"key","text":"ctrl+l"}' \
  "$GW/v1/sessions/$SID/action" | python3 -m json.tool

echo "==> recycle"
curl -sf -H "$AUTH" -X DELETE "$GW/v1/sessions/$SID?recycle=true" | python3 -m json.tool

echo "==> recordings list"
LIST="$(curl -sf -H "$AUTH" "$GW/v1/recordings?job_id=oss-smoke-job")"
echo "$LIST" | python3 -m json.tool
python3 -c '
import json,sys
rows=json.load(sys.stdin).get("recordings") or []
assert any(r.get("session_id")==sys.argv[1] for r in rows), rows
print("recording_found", sys.argv[1], "events", next(r["event_count"] for r in rows if r["session_id"]==sys.argv[1]))
' "$SID" <<<"$LIST"

echo "==> recording manifest"
curl -sf -H "$AUTH" "$GW/v1/recordings/$SID" | python3 -c '
import json,sys
m=json.load(sys.stdin)
assert m.get("meta",{}).get("ended_at"), m.get("meta")
kinds={e.get("kind") for e in m.get("events") or []}
assert "screenshot" in kinds and "action" in kinds, kinds
print("ok events", len(m["events"]), "kinds", sorted(kinds))
'

echo
echo "OSS smoke OK — lease → screenshot → action → recycle → recordings"
echo "Watch URL was in lease response (vnc_url); open manually if desired."
