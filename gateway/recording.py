"""Session recording archive — screenshot/action journal on local disk.

Opt-in capture outlives park/recycle. Not continuous video.
See .cursor/plans/session_recording_archive_a389112e.plan.md
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import shutil
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("computer.gateway.recording")

_SAFE_SESSION = re.compile(r"^[A-Za-z0-9._-]+$")
_SAFE_FRAME = re.compile(r"^[A-Za-z0-9._-]+$")

DEFAULT_TTL_SEC = 604800  # 7 days
DEFAULT_MAX_FRAME_BYTES = 2_000_000


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


class RecordingStore:
    """Filesystem-backed recording store (one directory per session_id)."""

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        ttl_sec: int | None = None,
        max_frame_bytes: int | None = None,
    ) -> None:
        env_root = os.environ.get("RECORDING_DIR", "").strip()
        preferred = Path(
            root if root is not None else (env_root or "/data/recordings")
        )
        self.root = preferred
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
        self._lock = threading.Lock()
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError:
            # Local/dev without Compose volume: keep journals under cwd.
            fallback = Path.cwd() / ".data" / "recordings"
            logger.warning(
                "RECORDING_DIR %s not writable; using %s",
                self.root,
                fallback,
            )
            self.root = fallback
            self.root.mkdir(parents=True, exist_ok=True)

    def _session_dir(self, session_id: str) -> Path:
        sid = (session_id or "").strip()
        if not sid or not _SAFE_SESSION.match(sid):
            raise ValueError(f"invalid session_id: {session_id!r}")
        return self.root / sid

    def _read_meta(self, session_dir: Path) -> dict[str, Any] | None:
        path = session_dir / "meta.json"
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _write_meta(self, session_dir: Path, meta: dict[str, Any]) -> None:
        path = session_dir / "meta.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(path)

    def _next_seq_locked(self, session_dir: Path, meta: dict[str, Any]) -> int:
        seq = int(meta.get("next_seq") or 1)
        meta["next_seq"] = seq + 1
        meta["event_count"] = int(meta.get("event_count") or 0) + 1
        self._write_meta(session_dir, meta)
        return seq

    def start(
        self,
        session_id: str,
        *,
        kind: str = "desktop",
        seat_id: str | None = None,
        agent_id: str | None = None,
        job_id: str | None = None,
    ) -> dict[str, Any]:
        """Create recording directory + meta.json. Idempotent if already started."""
        now = _utcnow()
        with self._lock:
            session_dir = self._session_dir(session_id)
            existing = self._read_meta(session_dir)
            if existing and existing.get("session_id") == session_id:
                return existing
            session_dir.mkdir(parents=True, exist_ok=True)
            (session_dir / "frames").mkdir(exist_ok=True)
            (session_dir / "events.jsonl").touch(exist_ok=True)
            meta: dict[str, Any] = {
                "session_id": session_id,
                "kind": kind,
                "seat_id": seat_id,
                "agent_id": agent_id,
                "job_id": job_id,
                "started_at": _iso(now),
                "ended_at": None,
                "expires_at": _iso(now + timedelta(seconds=self.ttl_sec)),
                "event_count": 0,
                "next_seq": 1,
            }
            self._write_meta(session_dir, meta)
            logger.info("recording started session=%s kind=%s", session_id, kind)
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
            session_dir = self._session_dir(session_id)
            meta = self._read_meta(session_dir)
            if not meta:
                return None
            seq = self._next_seq_locked(session_dir, meta)
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
                    rel = f"frames/{seq:06d}.png"
                    frame_path = session_dir / rel
                    frame_path.parent.mkdir(exist_ok=True)
                    frame_path.write_bytes(png)
                    event["frame"] = rel

            with (session_dir / "events.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, separators=(",", ":")) + "\n")
            return event

    def finalize(self, session_id: str) -> dict[str, Any] | None:
        """Set ended_at and refresh expires_at from ended_at + TTL. Does not delete."""
        now = _utcnow()
        with self._lock:
            session_dir = self._session_dir(session_id)
            meta = self._read_meta(session_dir)
            if not meta:
                return None
            if not meta.get("ended_at"):
                meta["ended_at"] = _iso(now)
            ended = _parse_iso(meta.get("ended_at")) or now
            meta["expires_at"] = _iso(ended + timedelta(seconds=self.ttl_sec))
            self._write_meta(session_dir, meta)
            logger.info("recording finalized session=%s", session_id)
            return meta

    def _load_events(self, session_dir: Path) -> list[dict[str, Any]]:
        path = session_dir / "events.jsonl"
        if not path.is_file():
            return []
        events: list[dict[str, Any]] = []
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return []
        for line in text.splitlines():
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
            session_dir = self._session_dir(session_id)
            meta = self._read_meta(session_dir)
            if not meta:
                return None
            events = self._load_events(session_dir)
            return {"meta": meta, "events": events}

    def list(
        self,
        *,
        job_id: str | None = None,
        kind: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List recording summaries, newest started_at first."""
        limit = max(1, min(int(limit), 1000))
        rows: list[dict[str, Any]] = []
        with self._lock:
            if not self.root.is_dir():
                return []
            for child in self.root.iterdir():
                if not child.is_dir():
                    continue
                meta = self._read_meta(child)
                if not meta:
                    continue
                if job_id is not None and meta.get("job_id") != job_id:
                    continue
                if kind is not None and meta.get("kind") != kind:
                    continue
                rows.append(
                    {
                        "session_id": meta.get("session_id") or child.name,
                        "kind": meta.get("kind"),
                        "seat_id": meta.get("seat_id"),
                        "agent_id": meta.get("agent_id"),
                        "job_id": meta.get("job_id"),
                        "started_at": meta.get("started_at"),
                        "ended_at": meta.get("ended_at"),
                        "expires_at": meta.get("expires_at"),
                        "event_count": int(meta.get("event_count") or 0),
                    }
                )
        rows.sort(key=lambda r: r.get("started_at") or "", reverse=True)
        return rows[:limit]

    def frame_path(self, session_id: str, name: str) -> Path | None:
        """Resolve a frame file under the session frames/ dir, or None."""
        name = (name or "").strip()
        if not name or not _SAFE_FRAME.match(name) or ".." in name:
            return None
        with self._lock:
            session_dir = self._session_dir(session_id)
            if not self._read_meta(session_dir):
                return None
            # Allow "000001.png" or "frames/000001.png"
            rel = name if name.startswith("frames/") else f"frames/{name}"
            path = (session_dir / rel).resolve()
            try:
                path.relative_to(session_dir.resolve())
            except ValueError:
                return None
            return path if path.is_file() else None

    def sweep(self, *, now: datetime | None = None) -> list[str]:
        """Delete recording dirs whose expires_at is in the past. Returns deleted session_ids."""
        now = now or _utcnow()
        deleted: list[str] = []
        with self._lock:
            if not self.root.is_dir():
                return []
            for child in list(self.root.iterdir()):
                if not child.is_dir():
                    continue
                meta = self._read_meta(child)
                exp = _parse_iso(meta.get("expires_at") if meta else None)
                if exp is None and meta:
                    # Fallback: ended_at or started_at + ttl
                    base = _parse_iso(meta.get("ended_at")) or _parse_iso(
                        meta.get("started_at")
                    )
                    if base:
                        exp = base + timedelta(seconds=self.ttl_sec)
                if exp is None or exp > now:
                    continue
                sid = (meta or {}).get("session_id") or child.name
                try:
                    shutil.rmtree(child)
                    deleted.append(str(sid))
                    logger.info("recording swept session=%s", sid)
                except OSError as e:
                    logger.warning("recording sweep failed session=%s: %s", sid, e)
        return deleted
