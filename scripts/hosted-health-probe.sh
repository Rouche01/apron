#!/usr/bin/env bash
# Probe gateway /health and /ready for hosted alerting.
# Exit: 0 ok | 1 ready-degraded (503) | 2 health/transport failure | 3 usage
set -euo pipefail

GW="${COMPUTER_GATEWAY_URL:-${GATEWAY_PUBLIC_BASE:-http://127.0.0.1:9000}}"
GW="${GW%/}"
TIMEOUT="${PROBE_TIMEOUT_SEC:-5}"
REQUIRE_READY="${REQUIRE_READY:-1}"

usage() {
  cat <<'EOF'
Usage: hosted-health-probe.sh

Env:
  COMPUTER_GATEWAY_URL / GATEWAY_PUBLIC_BASE  Base URL (default http://127.0.0.1:9000)
  PROBE_TIMEOUT_SEC                           curl max-time (default 5)
  REQUIRE_READY                               1 = treat /ready 503 as exit 1 (default)
                                              0 = only require /health 200
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT

if ! curl -sf --max-time "$TIMEOUT" "$GW/health" -o "$tmp"; then
  echo "FAIL health: $GW/health" >&2
  exit 2
fi

if ! python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if d.get("ok") else 1)' "$tmp"; then
  echo "FAIL health body: $(cat "$tmp")" >&2
  exit 2
fi

code="$(curl -s -o "$tmp" -w '%{http_code}' --max-time "$TIMEOUT" "$GW/ready" || true)"
if [[ "$code" == "200" ]]; then
  echo "OK health+ready $GW"
  exit 0
fi

if [[ "$code" == "503" ]]; then
  echo "DEGRADED ready HTTP 503: $(cat "$tmp")" >&2
  if [[ "$REQUIRE_READY" == "1" ]]; then
    exit 1
  fi
  echo "OK health (ready ignored) $GW"
  exit 0
fi

echo "FAIL ready unexpected HTTP $code: $(cat "$tmp")" >&2
exit 2
