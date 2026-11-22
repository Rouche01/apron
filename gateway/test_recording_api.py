"""Recording lease flag, pool finalize queue, and GET /v1/recordings (TestClient)."""

from __future__ import annotations

import base64
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

# Import app only after inventory + recording paths are set (module loads at import).
_GATEWAY_DIR = Path(__file__).resolve().parent
os.environ.setdefault("WORKER_INVENTORY_PATH", str(_GATEWAY_DIR / "workers.json"))
os.environ.setdefault("REQUIRE_WORKER_INVENTORY", "1")
os.environ.setdefault("COMPUTER_API_TOKEN", "")
os.environ.setdefault(
    "RECORDING_DIR", str(_GATEWAY_DIR / ".pytest_recordings_default")
)

from fastapi.testclient import TestClient  # noqa: E402

import app as app_mod  # noqa: E402
from inventory import WorkerSpec  # noqa: E402
from pool import SessionPool  # noqa: E402
from recording import RecordingStore, resolve_record_flag  # noqa: E402


def test_resolve_record_flag(monkeypatch):
    monkeypatch.delenv("RECORD_SESSIONS_DEFAULT", raising=False)
    assert resolve_record_flag(True) is True
    assert resolve_record_flag(False) is False
    assert resolve_record_flag(None) is False
    monkeypatch.setenv("RECORD_SESSIONS_DEFAULT", "1")
    assert resolve_record_flag(None) is True
    assert resolve_record_flag(False) is False


def test_pool_recording_park_queues_finalize():
    pool = SessionPool(
        [WorkerSpec(id="d1", kind="desktop", url="http://w:8000", vnc_url="http://v")],
        default_ttl_sec=600,
        parked_ttl_sec=1800,
    )
    info = pool.lease(kind="desktop")
    assert info is not None
    assert pool.set_recording(info.session_id, True)
    assert pool.is_recording(info.session_id)
    pool.release(info.session_id)
    assert not pool.is_recording(info.session_id)
    pending = pool.drain_recording_finalizes()
    assert info.session_id in pending


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    store = RecordingStore(tmp_path / "rec", ttl_sec=3600)
    monkeypatch.setattr(app_mod, "recordings", store)
    monkeypatch.setattr(app_mod, "API_TOKEN", "")

    fake = SessionPool(
        [
            WorkerSpec(
                id="desk1",
                kind="desktop",
                url="http://worker-1:8000",
                vnc_url="http://localhost:6081/vnc.html",
            )
        ],
        default_ttl_sec=600,
        parked_ttl_sec=1800,
    )
    monkeypatch.setattr(app_mod, "pool", fake)

    png = b"\x89PNG\r\n\x1a\n" + b"fake"
    b64 = base64.b64encode(png).decode("ascii")

    def fake_proxy_get(session_id: str, path: str):
        if path.startswith("/screenshot"):
            return 200, {"image": b64}
        return 404, {"detail": "no"}

    def fake_proxy_post(session_id: str, path: str, body=None):
        return 200, {"ok": True}

    monkeypatch.setattr(fake, "proxy_get", fake_proxy_get)
    monkeypatch.setattr(fake, "proxy_post", fake_proxy_post)

    return TestClient(app_mod.app), store, fake


def test_lease_record_screenshot_list(client):
    tc, store, _pool = client
    r = tc.post(
        "/v1/sessions",
        json={"agent_id": "t", "job_id": "job-rec", "record": True},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["recording"] is True
    sid = body["session_id"]

    shot = tc.get(f"/v1/sessions/{sid}/screenshot")
    assert shot.status_code == 200

    act = tc.post(
        f"/v1/sessions/{sid}/action",
        json={"type": "click", "x": 1, "y": 2},
    )
    assert act.status_code == 200

    listed = tc.get("/v1/recordings", params={"job_id": "job-rec"})
    assert listed.status_code == 200
    rows = listed.json()["recordings"]
    assert len(rows) == 1
    assert rows[0]["session_id"] == sid
    assert rows[0]["event_count"] >= 2

    manifest = tc.get(f"/v1/recordings/{sid}")
    assert manifest.status_code == 200
    events = manifest.json()["events"]
    kinds = [e["kind"] for e in events]
    assert "screenshot" in kinds
    assert "action" in kinds
    frame_ev = next(e for e in events if e.get("frame"))
    name = Path(frame_ev["frame"]).name
    fr = tc.get(f"/v1/recordings/{sid}/frames/{name}")
    assert fr.status_code == 200
    assert fr.headers["content-type"].startswith("image/png")

    gone = tc.delete(f"/v1/sessions/{sid}")
    assert gone.status_code == 200
    meta = store.get(sid)["meta"]
    assert meta.get("ended_at")
