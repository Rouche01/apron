#!/usr/bin/env bash
# Backup recording journals (metadata by default) from local RECORDING_DIR / Compose volume.
# For RECORDING_BACKEND=s3 use bucket versioning instead — see docs/hosted-ops.md.
set -euo pipefail

OUT_DIR=""
INCLUDE_FRAMES=0
SOURCE=""

usage() {
  cat <<'EOF'
Usage: recording-backup.sh --out DIR [--frames] [--source PATH]

  --out DIR       Destination directory for timestamped .tar.gz
  --frames        Include frames/*.png (larger)
  --source PATH   Recordings root (default: docker volume copy or RECORDING_DIR)

Env:
  RECORDING_DIR           Fallback host path when --source omitted
  COMPOSE_PROJECT_NAME    Optional; used when copying from volume gateway_recordings
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --out) OUT_DIR="${2:-}"; shift 2 ;;
    --frames) INCLUDE_FRAMES=1; shift ;;
    --source) SOURCE="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown arg: $1" >&2; usage; exit 3 ;;
  esac
done

if [[ -z "$OUT_DIR" ]]; then
  echo "--out DIR is required" >&2
  usage
  exit 3
fi

mkdir -p "$OUT_DIR"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

if [[ -n "$SOURCE" ]]; then
  if [[ ! -d "$SOURCE" ]]; then
    echo "source not a directory: $SOURCE" >&2
    exit 2
  fi
  src="$SOURCE"
elif [[ -n "${RECORDING_DIR:-}" && -d "${RECORDING_DIR}" ]]; then
  src="$RECORDING_DIR"
else
  # Copy from Compose named volume into work tree
  vol_src="$work/from-volume"
  mkdir -p "$vol_src"
  if ! docker compose run --rm --no-deps --entrypoint python \
    -v gateway_recordings:/data/recordings:ro \
    -v "$vol_src:/backup" \
    gateway \
    -c 'import shutil; shutil.copytree("/data/recordings", "/backup/recordings", dirs_exist_ok=True)'; then
    echo "failed to copy gateway_recordings via compose; pass --source or RECORDING_DIR" >&2
    exit 2
  fi
  src="$vol_src/recordings"
fi

stage="$work/stage"
mkdir -p "$stage"

# Always take index + meta + events; frames optional
if command -v rsync >/dev/null 2>&1; then
  if [[ "$INCLUDE_FRAMES" -eq 1 ]]; then
    rsync -a "$src/" "$stage/"
  else
    rsync -a --include='*/' \
      --include='_sessions/***' \
      --include='**/meta.json' \
      --include='**/events.jsonl' \
      --exclude='*' \
      "$src/" "$stage/"
  fi
else
  # Portable fallback without rsync filter richness
  cp -a "$src/." "$stage/" 2>/dev/null || true
  if [[ "$INCLUDE_FRAMES" -eq 0 ]]; then
    find "$stage" -type f -name '*.png' -delete 2>/dev/null || true
  fi
fi

ts="$(date -u +%Y%m%dT%H%M%SZ)"
label="meta"
[[ "$INCLUDE_FRAMES" -eq 1 ]] && label="full"
archive="$OUT_DIR/recordings-${label}-${ts}.tar.gz"
tar -C "$stage" -czf "$archive" .
echo "wrote $archive"
du -h "$archive" | awk '{print "size", $1}'
