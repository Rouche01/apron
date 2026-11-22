"""Unit tests for RecordingStore (temp dir; no Docker)."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from recording import RecordingQuotaExceeded, RecordingStore


def _store(tmp: Path) -> RecordingStore:
    return RecordingStore(tmp, ttl_sec=3600, max_frame_bytes=1000)


def test_start_append_get_list(tmp_path: Path):
    store = _store(tmp_path)
    meta = store.start(
        "sess-1",
        kind="desktop",
        seat_id="desk1",
        agent_id="a1",
        job_id="job-9",
    )
    assert meta["session_id"] == "sess-1"
    assert meta["event_count"] == 0
    assert meta["ended_at"] is None

    png = b"\x89PNG\r\n\x1a\n" + b"x" * 20
    ev = store.append(
        "sess-1",
        kind="screenshot",
        ok=True,
        frame_png=png,
    )
    assert ev is not None
    assert ev["seq"] == 1
    assert ev["frame"] == "frames/000001.png"

    store.append(
        "sess-1",
        kind="action",
        ok=True,
        body={"type": "click", "x": 10, "y": 20},
    )

    manifest = store.get("sess-1")
    assert manifest is not None
    assert len(manifest["events"]) == 2
    assert manifest["meta"]["event_count"] == 2

    listed = store.list(job_id="job-9")
    assert len(listed) == 1
    assert listed[0]["session_id"] == "sess-1"
    assert listed[0]["event_count"] == 2

    frame = store.frame_path("sess-1", "000001.png")
    assert frame is not None
    assert frame.read_bytes() == png


def test_frame_b64_and_oversized(tmp_path: Path):
    store = _store(tmp_path)
    store.start("sess-2", kind="phone")
    small = base64.b64encode(b"\x89PNG" + b"y" * 10).decode("ascii")
    ev = store.append("sess-2", kind="observe", frame_b64=small, elements_n=3)
    assert ev is not None
    assert ev.get("frame")
    assert ev.get("elements_n") == 3

    big = b"z" * 2000
    ev2 = store.append("sess-2", kind="screenshot", frame_png=big)
    assert ev2 is not None
    assert "frame" not in ev2
    assert "frame_omitted_too_large" in (ev2.get("note") or "")


def test_finalize_and_sweep(tmp_path: Path):
    store = RecordingStore(tmp_path, ttl_sec=60, max_frame_bytes=1000)
    store.start("old", kind="desktop")
    store.append("old", kind="action", body={"type": "key", "text": "a"})
    meta = store.finalize("old")
    assert meta is not None
    assert meta["ended_at"]
    # Force expiry in the past via blob meta
    prefix = store._resolve_prefix("old")
    assert prefix
    m = store._read_meta_at(prefix)
    assert m is not None
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    m["expires_at"] = past.isoformat().replace("+00:00", "Z")
    store._write_meta_at(prefix, m)

    store.start("new", kind="desktop")
    deleted = store.sweep()
    assert "old" in deleted
    assert store.get("old") is None
    assert store.get("new") is not None


def test_tenant_prefix_and_quota(tmp_path: Path):
    store = RecordingStore(
        tmp_path, ttl_sec=3600, max_frame_bytes=1000, quota_bytes_by_tenant={"acme": 50}
    )
    store.start("s1", kind="desktop", tenant_id="acme")
    assert store.belongs_to_tenant("s1", "acme")
    # Tiny frame under quota
    store.append("s1", kind="screenshot", frame_png=b"\x89PNG" + b"x" * 10)
    # Fill quota then start another session should raise
    store.append("s1", kind="screenshot", frame_png=b"z" * 40)
    with pytest.raises(RecordingQuotaExceeded):
        store.start("s2", kind="desktop", tenant_id="acme")


def test_start_idempotent(tmp_path: Path):
    store = _store(tmp_path)
    a = store.start("sess-3", kind="desktop", job_id="j1")
    b = store.start("sess-3", kind="phone", job_id="j2")
    assert a["started_at"] == b["started_at"]
    assert b["job_id"] == "j1"
