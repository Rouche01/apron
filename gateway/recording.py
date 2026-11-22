"""Session recording archive — screenshot/action journal.

Local volume or S3-compatible object storage behind the same API.
Per-tenant key prefix: t/{tenant_id|anon}/{session_id}/…
See docs/hosted.md and session_recording_archive plan.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from recording_blob import BlobStore, LocalBlobStore, build_blob_store

logger = logging.getLogger("computer.gateway.recording")

_SAFE_SESSION = re.compile(r"^[A-Za-z0-9._-]+$")
_SAFE_FRAME = re.compile(r"^[A-Za-z0-9._-]+$")
_SAFE_TENANT = re.compile(r"^[A-Za-z0-9._-]+$")

DEFAULT_TTL_SEC = 604800  # 7 days
DEFAULT_MAX_FRAME_BYTES = 2_000_000
ANON_TENANT = "anon"


class RecordingQuotaExceeded(Exception):
    """Tenant recording storage quota would be exceeded."""

    def __init__(self, message: str, *, used: int, limit: int) -> None:
        super().__init__(message)
        self.used = used
        self.limit = limit


def record_sessions_default() -> bool:
    """Env RECORD_SESSIONS_DEFAULT — every lease records unless body record=false."""
    return os.environ.get("RECORD_SESSIONS_DEFAULT", "0").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def resolve_record_flag(record: bool | None) -> bool:
    """Lease body record wins; otherwise RECORD_SESSIONS_DEFAULT."""
    if record is not None:
        return bool(record)
    return record_sessions_default()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _tenant_seg(tenant_id: str | None) -> str:
    tid = (tenant_id or "").strip() or ANON_TENANT
    if not _SAFE_TENANT.match(tid):
        raise ValueError(f"invalid tenant_id: {tenant_id!r}")
    return tid


class RecordingStore:
    """Journal store over a BlobStore (local or S3). Same /v1/recordings API."""

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        ttl_sec: int | None = None,
        max_frame_bytes: int | None = None,
        blob: BlobStore | None = None,
        quota_bytes_by_tenant: dict[str, int] | None = None,
    ) -> None:
        try:
            self.ttl_sec = max(
                60,
                int(
                    ttl_sec
                    if ttl_sec is not None
                    else os.environ.get("RECORDING_TTL_SEC", str(DEFAULT_TTL_SEC))
                ),
            )
        except ValueError:
            self.ttl_sec = DEFAULT_TTL_SEC
        try:
            self.max_frame_bytes = max(
                1,
                int(
                    max_frame_bytes
                    if max_frame_bytes is not None
                    else os.environ.get(
                        "RECORDING_MAX_FRAME_BYTES", str(DEFAULT_MAX_FRAME_BYTES)
                    )
                ),
            )
        except ValueError:
            self.max_frame_bytes = DEFAULT_MAX_FRAME_BYTES
        self._lock = threading.RLock()
        self._quota = dict(quota_bytes_by_tenant or {})
        if blob is not None:
            self.blob = blob
            self.root = Path(root) if root is not None else Path(".")
        else:
            local_root = Path(root) if root is not None else None
            self.blob = build_blob_store(local_root=local_root)
            self.root = (
                local_root
                if local_root is not None
                else getattr(self.blob, "root", Path("."))
            )

    def set_quota_bytes(self, tenant_id: str, limit: int | None) -> None:
        tid = (tenant_id or "").strip()
        if not tid:
            return
        if limit is None or limit <= 0:
            self._quota.pop(tid, None)
        else:
            self._quota[tid] = int(limit)

    def _session_prefix(self, tenant_seg: str, session_id: str) -> str:
        return f"t/{tenant_seg}/{session_id}/"

    def _index_key(self, session_id: str) -> str:
        return f"_sessions/{session_id}.json"

    def _validate_session_id(self, session_id: str) -> str:
        sid = (session_id or "").strip()
        if not sid or not _SAFE_SESSION.match(sid):
            raise ValueError(f"invalid session_id: {session_id!r}")
        return sid

    def _resolve_prefix(self, session_id: str) -> str | None:
        sid = self._validate_session_id(session_id)
        raw = self.blob.get(self._index_key(sid))
        if not raw:
            # Legacy flat layout (pre-prefix): {sid}/meta.json
            if self.blob.get(f"{sid}/meta.json") is not None:
                return f"{sid}/"
            return None
        try:
            idx = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        tenant_seg = str(idx.get("tenant_seg") or ANON_TENANT)
        return self._session_prefix(tenant_seg, sid)

    def _read_meta_at(self, prefix: str) -> dict[str, Any] | None:
        raw = self.blob.get(f"{prefix}meta.json")
        if not raw:
            return None
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _write_meta_at(self, prefix: str, meta: dict[str, Any]) -> None:
        payload = (json.dumps(meta, indent=2, sort_keys=True) + "\n").encode("utf-8")
        self.blob.put(f"{prefix}meta.json", payload, content_type="application/json")

    def _usage_bytes(self, tenant_id: str | None) -> int:
        seg = _tenant_seg(tenant_id)
        return sum(sz for _, sz in self.blob.list_keys(f"t/{seg}/"))

    def _check_quota(self, tenant_id: str | None, *, extra: int = 0) -> None:
        tid = (tenant_id or "").strip()
        if not tid or tid not in self._quota:
            return
        limit = self._quota[tid]
        used = self._usage_bytes(tid)
        if used + extra > limit:
            raise RecordingQuotaExceeded(
                f"recording quota exceeded for tenant {tid}",
                used=used,
                limit=limit,
            )

    def start(
        self,
        session_id: str,
        *,
        kind: str = "desktop",
        seat_id: str | None = None,
        agent_id: str | None = None,
        job_id: str | None = None,
        tenant_id: str | None = None,
        ttl_sec: int | None = None,
    ) -> dict[str, Any]:
        """Create recording prefix + meta.json. Idempotent if already started."""
        now = _utcnow()
        ttl = max(60, int(ttl_sec)) if ttl_sec is not None else self.ttl_sec
        with self._lock:
            sid = self._validate_session_id(session_id)
            existing_prefix = self._resolve_prefix(sid)
            if existing_prefix:
                existing = self._read_meta_at(existing_prefix)
                if existing and existing.get("session_id") == sid:
                    return existing
            self._check_quota(tenant_id)
            tenant_seg = _tenant_seg(tenant_id)
            prefix = self._session_prefix(tenant_seg, sid)
            meta: dict[str, Any] = {
                "session_id": sid,
                "kind": kind,
                "seat_id": seat_id,
                "agent_id": agent_id,
                "job_id": job_id,
                "tenant_id": tenant_id,
                "started_at": _iso(now),
                "ended_at": None,
                "expires_at": _iso(now + timedelta(seconds=ttl)),
                "event_count": 0,
                "next_seq": 1,
            }
            self._write_meta_at(prefix, meta)
            self.blob.put(
                f"{prefix}events.jsonl",
                b"",
                content_type="application/x-ndjson",
            )
            idx = json.dumps(
                {"session_id": sid, "tenant_seg": tenant_seg, "tenant_id": tenant_id}
            ).encode("utf-8")
            self.blob.put(self._index_key(sid), idx, content_type="application/json")
            logger.info(
                "recording started session=%s kind=%s tenant=%s",
                sid,
                kind,
                tenant_id or "-",
            )
            return meta

    def append(
        self,
        session_id: str,
        *,
        kind: str,
        ok: bool = True,
        body: dict[str, Any] | None = None,
        frame_png: bytes | None = None,
        frame_b64: str | None = None,
        elements_n: int | None = None,
        note: str | None = None,
    ) -> dict[str, Any] | None:
        """Append one event; optionally write a PNG frame. Returns the event or None if missing."""
        with self._lock:
            prefix = self._resolve_prefix(session_id)
            if not prefix:
                return None
            meta = self._read_meta_at(prefix)
            if not meta:
                return None
            seq = int(meta.get("next_seq") or 1)
            meta["next_seq"] = seq + 1
            meta["event_count"] = int(meta.get("event_count") or 0) + 1
            self._write_meta_at(prefix, meta)

            ts = _iso(_utcnow())
            event: dict[str, Any] = {"ts": ts, "seq": seq, "kind": kind, "ok": ok}
            if body is not None:
                event["body"] = body
            if elements_n is not None:
                event["elements_n"] = elements_n
            if note:
                event["note"] = note

            png: bytes | None = frame_png
            if png is None and frame_b64:
                try:
                    png = base64.b64decode(frame_b64, validate=False)
                except Exception:
                    event["note"] = (event.get("note") or "") + " invalid_frame_b64"
                    png = None

            if png is not None:
                if len(png) > self.max_frame_bytes:
                    event["note"] = (
                        (event.get("note") + "; " if event.get("note") else "")
                        + f"frame_omitted_too_large:{len(png)}"
                    )
                else:
                    try:
                        self._check_quota(meta.get("tenant_id"), extra=len(png))
                        rel = f"frames/{seq:06d}.png"
                        self.blob.put(
                            f"{prefix}{rel}",
                            png,
                            content_type="image/png",
                        )
                        event["frame"] = rel
                    except RecordingQuotaExceeded:
                        event["note"] = (
                            (event.get("note") + "; " if event.get("note") else "")
                            + "frame_omitted_quota"
                        )

            line = (json.dumps(event, separators=(",", ":")) + "\n").encode("utf-8")
            prev = self.blob.get(f"{prefix}events.jsonl") or b""
            self.blob.put(
                f"{prefix}events.jsonl",
                prev + line,
                content_type="application/x-ndjson",
            )
            return event

    def finalize(self, session_id: str) -> dict[str, Any] | None:
        """Set ended_at and refresh expires_at from ended_at + TTL. Does not delete."""
        now = _utcnow()
        with self._lock:
            prefix = self._resolve_prefix(session_id)
            if not prefix:
                return None
            meta = self._read_meta_at(prefix)
            if not meta:
                return None
            if not meta.get("ended_at"):
                meta["ended_at"] = _iso(now)
            ended = _parse_iso(meta.get("ended_at")) or now
            meta["expires_at"] = _iso(ended + timedelta(seconds=self.ttl_sec))
            self._write_meta_at(prefix, meta)
            logger.info("recording finalized session=%s", session_id)
            return meta

    def _load_events_at(self, prefix: str) -> list[dict[str, Any]]:
        raw = self.blob.get(f"{prefix}events.jsonl")
        if not raw:
            return []
        events: list[dict[str, Any]] = []
        for line in raw.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                events.append(obj)
        return events

    def get(self, session_id: str) -> dict[str, Any] | None:
        """Manifest: meta + events (no embedded base64 frames)."""
        with self._lock:
            prefix = self._resolve_prefix(session_id)
            if not prefix:
                return None
            meta = self._read_meta_at(prefix)
            if not meta:
                return None
            return {"meta": meta, "events": self._load_events_at(prefix)}

    def list(
        self,
        *,
        job_id: str | None = None,
        kind: str | None = None,
        tenant_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List recording summaries, newest started_at first."""
        limit = max(1, min(int(limit), 1000))
        rows: list[dict[str, Any]] = []
        with self._lock:
            if tenant_id is not None:
                prefixes = [f"t/{_tenant_seg(tenant_id)}/"]
            else:
                prefixes = ["t/"]
            seen: set[str] = set()
            for pref in prefixes:
                for key, _sz in self.blob.list_keys(pref):
                    if not key.endswith("/meta.json"):
                        continue
                    # t/{tenant}/{session}/meta.json
                    parts = key.split("/")
                    if len(parts) < 4:
                        continue
                    session_prefix = "/".join(parts[:-1]) + "/"
                    if session_prefix in seen:
                        continue
                    seen.add(session_prefix)
                    meta = self._read_meta_at(session_prefix)
                    if not meta:
                        continue
                    if job_id is not None and meta.get("job_id") != job_id:
                        continue
                    if kind is not None and meta.get("kind") != kind:
                        continue
                    if tenant_id is not None and meta.get("tenant_id") != tenant_id:
                        continue
                    rows.append(
                        {
                            "session_id": meta.get("session_id") or parts[-2],
                            "kind": meta.get("kind"),
                            "seat_id": meta.get("seat_id"),
                            "agent_id": meta.get("agent_id"),
                            "job_id": meta.get("job_id"),
                            "tenant_id": meta.get("tenant_id"),
                            "started_at": meta.get("started_at"),
                            "ended_at": meta.get("ended_at"),
                            "expires_at": meta.get("expires_at"),
                            "event_count": int(meta.get("event_count") or 0),
                        }
                    )
            # Legacy flat sessions (no t/ prefix)
            if tenant_id is None:
                for key, _sz in self.blob.list_keys(""):
                    if not key.endswith("/meta.json"):
                        continue
                    if key.startswith("t/") or key.startswith("_sessions/"):
                        continue
                    parts = key.split("/")
                    if len(parts) != 2:
                        continue
                    session_prefix = parts[0] + "/"
                    if session_prefix in seen:
                        continue
                    meta = self._read_meta_at(session_prefix)
                    if not meta:
                        continue
                    if job_id is not None and meta.get("job_id") != job_id:
                        continue
                    if kind is not None and meta.get("kind") != kind:
                        continue
                    rows.append(
                        {
                            "session_id": meta.get("session_id") or parts[0],
                            "kind": meta.get("kind"),
                            "seat_id": meta.get("seat_id"),
                            "agent_id": meta.get("agent_id"),
                            "job_id": meta.get("job_id"),
                            "tenant_id": meta.get("tenant_id"),
                            "started_at": meta.get("started_at"),
                            "ended_at": meta.get("ended_at"),
                            "expires_at": meta.get("expires_at"),
                            "event_count": int(meta.get("event_count") or 0),
                        }
                    )
        rows.sort(key=lambda r: r.get("started_at") or "", reverse=True)
        return rows[:limit]

    def belongs_to_tenant(self, session_id: str, tenant_id: str) -> bool:
        """True if recording meta.tenant_id matches (missing meta → False)."""
        with self._lock:
            try:
                prefix = self._resolve_prefix(session_id)
            except ValueError:
                return False
            if not prefix:
                return False
            meta = self._read_meta_at(prefix)
            if not meta:
                return False
            return meta.get("tenant_id") == tenant_id

    def get_frame(self, session_id: str, name: str) -> bytes | None:
        """Load a PNG frame by name (000001.png or frames/000001.png)."""
        name = (name or "").strip()
        if not name or not _SAFE_FRAME.match(name.replace("frames/", "")) or ".." in name:
            return None
        with self._lock:
            prefix = self._resolve_prefix(session_id)
            if not prefix or not self._read_meta_at(prefix):
                return None
            rel = name if name.startswith("frames/") else f"frames/{name}"
            if ".." in rel:
                return None
            return self.blob.get(f"{prefix}{rel}")

    def frame_path(self, session_id: str, name: str) -> Path | None:
        """Local-only: resolve frame file path. Prefer get_frame for S3."""
        data = self.get_frame(session_id, name)
        if data is None:
            return None
        if not isinstance(self.blob, LocalBlobStore):
            return None
        prefix = self._resolve_prefix(session_id)
        if not prefix:
            return None
        rel = name if name.startswith("frames/") else f"frames/{name}"
        path = self.blob.root / f"{prefix}{rel}"
        return path if path.is_file() else None

    def sweep(self, *, now: datetime | None = None) -> list[str]:
        """Delete recording prefixes whose expires_at is in the past."""
        now = now or _utcnow()
        deleted: list[str] = []
        with self._lock:
            candidates = self.list(limit=1000)
            for row in candidates:
                sid = str(row.get("session_id") or "")
                if not sid:
                    continue
                prefix = self._resolve_prefix(sid)
                if not prefix:
                    continue
                meta = self._read_meta_at(prefix)
                exp = _parse_iso(meta.get("expires_at") if meta else None)
                if exp is None and meta:
                    base = _parse_iso(meta.get("ended_at")) or _parse_iso(
                        meta.get("started_at")
                    )
                    if base:
                        exp = base + timedelta(seconds=self.ttl_sec)
                if exp is None or exp > now:
                    continue
                self.blob.delete_prefix(prefix)
                self.blob.delete_key(self._index_key(sid))
                deleted.append(sid)
                logger.info("recording swept session=%s", sid)
        return deleted
