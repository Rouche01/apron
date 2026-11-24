#!/usr/bin/env bash
# Static checks: worker control ports must not be published on the public face.
# Usage: scripts/hosted-security-check.sh [compose-file ...]
# Exit 0 = OK; 1 = failure.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

files=("$@")
if [[ ${#files[@]} -eq 0 ]]; then
  files=(docker-compose.yml)
  if [[ -f docker-compose.override.yml ]]; then
    files+=(docker-compose.override.yml)
  fi
fi

fail=0
warn=0

echo "hosted-security-check: scanning ${files[*]}"

# Publish mapping whose *container* port is 8000 (desktop worker control).
# Allow scrcpy-web HITL (host 6083 → container 8000) and explicit loopback binds.
check_file() {
  local f="$1"
  [[ -f "$f" ]] || {
    echo "FAIL: missing file $f" >&2
    fail=1
    return
  }

  # Host publishes container :8000 without 127.0.0.1 (public/all-interfaces risk).
  # Matches: "8000:8000", "0.0.0.0:8000:8000", "6083:8000" (scrcpy — warn if not loopback)
  while IFS= read -r line; do
    # strip comments
    local cleaned="${line%%#*}"
    [[ "$cleaned" =~ ports: ]] && continue
    if [[ "$cleaned" =~ \"([^\"]+)\" ]]; then
      local mapping="${BASH_REMATCH[1]}"
    else
      continue
    fi
    # Only care about mappings that target container port 8000
    if [[ ! "$mapping" =~ :8000$ ]] && [[ ! "$mapping" =~ :8000/ ]]; then
      continue
    fi
    # Loopback-bound — OK for hosted
    if [[ "$mapping" =~ ^127\.0\.0\.1: ]]; then
      echo "OK: loopback publish $mapping ($f)"
      continue
    fi
    # scrcpy HITL often 6083:8000 — warn (should be loopback on public hosts)
    if [[ "$mapping" =~ ^6083:8000 ]]; then
      echo "WARN: scrcpy $mapping in $f — bind 127.0.0.1:6083:8000 on hosted hosts" >&2
      warn=$((warn + 1))
      continue
    fi
    echo "FAIL: worker/control-like publish $mapping in $f (use internal network only, or 127.0.0.1:…)" >&2
    fail=1
  done < <(grep -E '^\s+-\s+"[^"]+"' "$f" || true)

  # Phone control 8003 published without loopback
  while IFS= read -r line; do
    local cleaned="${line%%#*}"
    if [[ "$cleaned" =~ \"([^\"]*8003[^\"]*)\" ]]; then
      local mapping="${BASH_REMATCH[1]}"
      if [[ "$mapping" =~ 8003 ]] && [[ ! "$mapping" =~ ^127\.0\.0\.1: ]]; then
        if [[ "$mapping" =~ :8003$ ]] || [[ "$mapping" == "8003:8003" ]]; then
          echo "WARN: phone control $mapping in $f — use 127.0.0.1:8003:8003 on hosted hosts" >&2
          warn=$((warn + 1))
        fi
      fi
    fi
  done < <(grep -E '8003' "$f" || true)
}

for f in "${files[@]}"; do
  check_file "$f"
done

# Desktop workers must not list container port 8000 in their ports: block (heuristic:
# no "8000:8000" anywhere is already covered; confirm workers expose 6080 only in default compose).
if grep -nE 'worker-[12]:' -A20 docker-compose.yml 2>/dev/null | grep -E '8000:8000|"8000:' >/dev/null 2>&1; then
  echo "FAIL: worker-1/2 appears to publish :8000" >&2
  fail=1
else
  echo "OK: worker-1/2 do not publish :8000 in docker-compose.yml"
fi

if [[ "$fail" -ne 0 ]]; then
  echo "hosted-security-check: FAILED" >&2
  exit 1
fi
echo "hosted-security-check: passed (${warn} warning(s))"
exit 0
