"""Soft tenancy: registry auth, seat allowlists, cross-tenant 404."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from inventory import WorkerSpec
from pool import SessionPool
from recording import RecordingStore
from tenancy import TenantRegistry, filter_seats, ops_context, oss_context


def test_registry_auth_and_duplicates(tmp_path: Path):
    path = tmp_path / "tenants.json"
    path.write_text(
        json.dumps(
            {
                "tenants": [
                    {
                        "id": "acme",
                        "api_keys": ["key-acme"],
                        "seat_ids": ["desk1", "desk1b"],
                        "max_concurrent_leases": 1,
                        "max_lease_ttl_sec": 120,
                    },
                    {
                        "id": "beta",
                        "api_keys": ["key-beta"],
                        "seat_ids": ["desk2"],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    reg = TenantRegistry.from_path(path)
    acme = reg.authenticate_key("key-acme")
    assert acme is not None and acme.id == "acme"
    assert acme.may_use_seat("desk1")
    assert not acme.may_use_seat("desk2")
    assert acme.max_concurrent_leases == 1
    assert acme.clamp_ttl_sec(9999) == 120
    assert acme.clamp_ttl_sec(None) == 120
    beta = reg.authenticate_key("key-beta")
    assert beta is not None and beta.max_concurrent_leases == 1  # default len(seat_ids)
    assert beta.clamp_ttl_sec(600) == 600
    assert reg.authenticate_key("nope") is None

    bad = tmp_path / "dup.json"
    bad.write_text(
        json.dumps(
            {
                "tenants": [
                    {"id": "a", "api_keys": ["same"], "seat_ids": ["desk1"]},
                    {"id": "b", "api_keys": ["same"], "seat_ids": ["desk2"]},
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate"):
        TenantRegistry.from_path(bad)


def test_filter_seats():
    from tenancy import TenantContext

    seats = [{"id": "desk1"}, {"id": "desk2"}]
    ctx = TenantContext(id="acme", seat_ids=frozenset({"desk1"}), is_ops=False)
    assert [s["id"] for s in filter_seats(ctx, seats)] == ["desk1"]
    assert len(filter_seats(ops_context(), seats)) == 2
    assert len(filter_seats(oss_context(), seats)) == 2


def test_pool_allowlist_lease():
    pool = SessionPool(
        [
            WorkerSpec(id="desk1", kind="desktop", url="http://w1:8000", vnc_url="http://v1"),
            WorkerSpec(id="desk2", kind="desktop", url="http://w2:8000", vnc_url="http://v2"),
        ],
        default_ttl_sec=600,
        parked_ttl_sec=1800,
    )
    a = pool.lease(kind="desktop", allowed_seat_ids={"desk1"}, tenant_id="acme")
    assert a is not None and a.seat_id == "desk1"
    assert pool.session_tenant(a.session_id) == "acme"

    b = pool.lease(kind="desktop", allowed_seat_ids={"desk1"}, tenant_id="acme")
    assert b is None  # desk1 taken; cannot claim desk2

    c = pool.lease(kind="desktop", allowed_seat_ids={"desk2"}, tenant_id="beta")
    assert c is not None and c.seat_id == "desk2"


_GATEWAY_DIR = Path(__file__).resolve().parent
os.environ.setdefault("WORKER_INVENTORY_PATH", str(_GATEWAY_DIR / "workers.json"))
os.environ.setdefault("REQUIRE_WORKER_INVENTORY", "1")
os.environ.setdefault("COMPUTER_API_TOKEN", "")
os.environ.setdefault(
    "RECORDING_DIR", str(_GATEWAY_DIR / ".pytest_recordings_tenancy")
)


@pytest.fixture
def tenant_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    tenants_path = tmp_path / "tenants.json"
    tenants_path.write_text(
        json.dumps(
            {
                "tenants": [
                    {
                        "id": "acme",
                        "api_keys": ["key-acme"],
                        "seat_ids": ["desk1"],
                    },
                    {
                        "id": "beta",
                        "api_keys": ["key-beta"],
                        "seat_ids": ["desk2"],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TENANTS_PATH", str(tenants_path))

    import importlib

    import app as app_mod
    import tenancy as tenancy_mod

    # Reload registry on already-imported app module
    reg = tenancy_mod.TenantRegistry.from_path(tenants_path)
    monkeypatch.setattr(app_mod, "tenants", reg)
    monkeypatch.setattr(app_mod, "API_TOKEN", "ops-token")

    store = RecordingStore(tmp_path / "rec", ttl_sec=3600)
    monkeypatch.setattr(app_mod, "recordings", store)

    fake = SessionPool(
        [
            WorkerSpec(
                id="desk1",
                kind="desktop",
                url="http://worker-1:8000",
                vnc_url="http://localhost:6081/vnc.html",
            ),
            WorkerSpec(
                id="desk2",
                kind="desktop",
                url="http://worker-2:8000",
                vnc_url="http://localhost:6082/vnc.html",
            ),
        ],
        default_ttl_sec=600,
        parked_ttl_sec=1800,
    )
    monkeypatch.setattr(app_mod, "pool", fake)
    monkeypatch.setattr(fake, "proxy_get", lambda *a, **k: (200, {"image": "YQ=="}))
    monkeypatch.setattr(fake, "proxy_post", lambda *a, **k: (200, {"ok": True}))

    return TestClient(app_mod.app), store


def test_tenant_isolation_api(tenant_client):
    tc, _store = tenant_client
    acme = {"Authorization": "Bearer key-acme"}
    beta = {"Authorization": "Bearer key-beta"}

    seats = tc.get("/v1/seats", headers=acme)
    assert seats.status_code == 200
    ids = [s["id"] for s in seats.json()["seats"]]
    assert ids == ["desk1"]

    lease = tc.post(
        "/v1/sessions",
        headers=acme,
        json={"agent_id": "a", "job_id": "j-acme", "record": True},
    )
    assert lease.status_code == 200, lease.text
    sid = lease.json()["session_id"]
    assert lease.json()["seat_id"] == "desk1"

    # Beta cannot see acme session
    assert tc.get(f"/v1/sessions/{sid}/screenshot", headers=beta).status_code == 404

    assert tc.get(f"/v1/sessions/{sid}/screenshot", headers=acme).status_code == 200
    assert tc.delete(f"/v1/sessions/{sid}?recycle=true", headers=acme).status_code == 200

    listed = tc.get("/v1/recordings", headers=acme)
    assert listed.status_code == 200
    rows = listed.json()["recordings"]
    assert len(rows) == 1 and rows[0]["session_id"] == sid

    assert tc.get("/v1/recordings", headers=beta).json()["recordings"] == []
    assert tc.get(f"/v1/recordings/{sid}", headers=beta).status_code == 404

    # Ops sees all seats
    ops = {"Authorization": "Bearer ops-token"}
    ops_seats = tc.get("/v1/seats", headers=ops)
    assert {s["id"] for s in ops_seats.json()["seats"]} == {"desk1", "desk2"}


def test_tenant_cannot_pin_other_seat(tenant_client):
    tc, _ = tenant_client
    acme = {"Authorization": "Bearer key-acme"}
    r = tc.post(
        "/v1/sessions",
        headers=acme,
        json={"seat_id": "desk2", "kind": "desktop"},
    )
    assert r.status_code == 404


@pytest.fixture
def capped_tenant_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Acme owns desk1+desk2 but max_concurrent_leases=1 and short TTL cap."""
    tenants_path = tmp_path / "tenants.json"
    tenants_path.write_text(
        json.dumps(
            {
                "tenants": [
                    {
                        "id": "acme",
                        "api_keys": ["key-acme"],
                        "seat_ids": ["desk1", "desk2"],
                        "max_concurrent_leases": 1,
                        "max_lease_ttl_sec": 60,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    import app as app_mod
    import tenancy as tenancy_mod

    reg = tenancy_mod.TenantRegistry.from_path(tenants_path)
    monkeypatch.setattr(app_mod, "tenants", reg)
    monkeypatch.setattr(app_mod, "API_TOKEN", "ops-token")
    monkeypatch.setattr(app_mod, "recordings", RecordingStore(tmp_path / "rec", ttl_sec=3600))

    fake = SessionPool(
        [
            WorkerSpec(
                id="desk1",
                kind="desktop",
                url="http://worker-1:8000",
                vnc_url="http://localhost:6081/vnc.html",
            ),
            WorkerSpec(
                id="desk2",
                kind="desktop",
                url="http://worker-2:8000",
                vnc_url="http://localhost:6082/vnc.html",
            ),
        ],
        default_ttl_sec=600,
        parked_ttl_sec=1800,
    )
    monkeypatch.setattr(app_mod, "pool", fake)
    return TestClient(app_mod.app), fake


def test_tenant_concurrent_cap_429(capped_tenant_client):
    tc, pool = capped_tenant_client
    acme = {"Authorization": "Bearer key-acme"}
    first = tc.post(
        "/v1/sessions",
        headers=acme,
        json={"job_id": "j1", "ttl_sec": 600},
    )
    assert first.status_code == 200, first.text
    # TTL clamped to 60s plan cap (not requested 600)
    from datetime import datetime, timezone

    exp = datetime.fromisoformat(first.json()["expires_at"].replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    assert 0 < (exp - now).total_seconds() <= 65

    second = tc.post(
        "/v1/sessions",
        headers=acme,
        json={"job_id": "j2"},
    )
    assert second.status_code == 429, second.text
    assert second.json()["detail"]["code"] == "tenant_lease_cap"

    # Soft park still holds the seat toward the cap
    sid = first.json()["session_id"]
    assert tc.delete(f"/v1/sessions/{sid}", headers=acme).status_code == 200
    third = tc.post("/v1/sessions", headers=acme, json={"job_id": "j3"})
    assert third.status_code == 429

    # Reattach same job does not 429
    reattach = tc.post(
        "/v1/sessions",
        headers=acme,
        json={"job_id": "j1"},
    )
    assert reattach.status_code == 200, reattach.text
    assert reattach.json()["session_id"] == sid
    assert pool.tenant_held_count("acme") == 1
