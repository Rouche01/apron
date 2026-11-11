#!/bin/sh
set -eu

# Optional wireless connect (host must already have authorized the device).
if [ -n "${ADB_CONNECT:-}" ]; then
  adb connect "$ADB_CONNECT" || true
fi

if [ -n "${ADB_SERIAL:-}" ]; then
  echo "phone worker: ADB_SERIAL=$ADB_SERIAL"
else
  echo "phone worker: ADB_SERIAL unset (requires exactly one device)"
fi

exec uvicorn app:app --host 0.0.0.0 --port "${PORT:-8000}"
