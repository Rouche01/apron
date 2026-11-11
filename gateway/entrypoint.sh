#!/bin/sh
# Fail fast if the inventory mount is missing (Docker turns a missing host
# file into an empty directory at the container path).
set -eu

INV="${WORKER_INVENTORY_PATH:-}"
if [ -n "$INV" ]; then
  if [ -d "$INV" ]; then
    echo "FATAL: WORKER_INVENTORY_PATH is a directory: $INV" >&2
    echo "Hint: host WORKER_INVENTORY_FILE was missing; Docker mounted an empty dir." >&2
    echo "Fix: set WORKER_INVENTORY_FILE in .env to an existing JSON file, then recreate the gateway." >&2
    exit 1
  fi
  if [ ! -f "$INV" ]; then
    echo "FATAL: WORKER_INVENTORY_PATH is not a file: $INV" >&2
    exit 1
  fi
elif [ "${REQUIRE_WORKER_INVENTORY:-}" = "1" ] || [ "${REQUIRE_WORKER_INVENTORY:-}" = "true" ]; then
  echo "FATAL: REQUIRE_WORKER_INVENTORY set but WORKER_INVENTORY_PATH is empty" >&2
  exit 1
fi

exec uvicorn app:app --host 0.0.0.0 --port 9000
