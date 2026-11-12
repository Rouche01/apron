#!/bin/sh
set -eu

# HITL Watch for phone seats. Shares ADB keys/serial with phone-1.
# Listen :8000 (Compose maps host :6083). Prefer wireless ADB_CONNECT.

if [ ! -f /root/.android/adbkey ]; then
  echo "WARN: /root/.android/adbkey missing — mount host \$HOME/.android or the phone will show unauthorized" >&2
fi

if [ -n "${ADB_CONNECT:-}" ]; then
  # Do not kill-server — preserve host-authorized keys in the mounted ~/.android.
  adb connect "$ADB_CONNECT" || true
fi

if [ -n "${ADB_SERIAL:-}" ]; then
  echo "scrcpy-web: ADB_SERIAL=$ADB_SERIAL"
  adb -s "$ADB_SERIAL" get-state || echo "WARN: ADB_SERIAL not ready yet" >&2
  # Drop stale on-device server from an older scrcpy-server.jar (e.g. 1.19-ws*).
  adb -s "$ADB_SERIAL" shell "pkill -f genymobile.scrcpy || true" >/dev/null 2>&1 || true
else
  echo "scrcpy-web: ADB_SERIAL unset (device picker /watch.html falls back to /)"
fi

# Auto-select landing: /watch.html → stream hash when ADB_SERIAL is set.
# Served from public/ (express.static). Inventory uses …/watch.html; / is the picker.
node -e '
const fs = require("fs");
const serial = process.env.ADB_SERIAL || "";
const t = fs.readFileSync("/opt/ws-scrcpy/watch.html.template", "utf8");
fs.writeFileSync("/opt/ws-scrcpy/public/watch.html", t.split("__ADB_SERIAL__").join(serial));
'

export WS_SCRCPY_CONFIG="${WS_SCRCPY_CONFIG:-/opt/ws-scrcpy/config.yaml}"
cd /opt/ws-scrcpy
exec node ./index.js
