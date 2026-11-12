#!/bin/sh
set -eu

# With network_mode: host we share the host's adb server (:5037). Prefer that
# server + host-mounted ~/.android keys so the phone already trusts us.
if [ ! -f /root/.android/adbkey ]; then
  echo "WARN: /root/.android/adbkey missing — mount host \$HOME/.android or the phone will show unauthorized" >&2
fi

# Optional wireless connect (pair/authorize once on the host first).
if [ -n "${ADB_CONNECT:-}" ]; then
  # Do not kill-server here — that would drop the host's authorized session
  # and restart adb with whatever keys are in the container.
  adb connect "$ADB_CONNECT" || true
fi

if [ -n "${ADB_SERIAL:-}" ]; then
  echo "phone worker: ADB_SERIAL=$ADB_SERIAL"
  adb -s "$ADB_SERIAL" get-state || echo "WARN: ADB_SERIAL not ready yet" >&2
else
  echo "phone worker: ADB_SERIAL unset (requires exactly one device)"
fi

exec uvicorn app:app --host 0.0.0.0 --port "${PORT:-8000}"
