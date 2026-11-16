"""POST /v1/devices/register — phase 1 pick free physical seat + bind."""

from __future__ import annotations

from inventory import WorkerSpec
from pool import SessionPool
from workers.disabled import DisabledWorkerFactory


def _pool() -> SessionPool:
    workers = [
        WorkerSpec(
            id="phone1",
            url="http://phone-1:8003",
            vnc_url="http://localhost:6083/watch.html",
            kind="phone",
        ),
        WorkerSpec(
            id="phone-cloud1",
            url="http://phone-cloud-1:8003",
            vnc_url="https://placeholder.invalid/watch",
            kind="phone",
            provider="phone-harness",
        ),
    ]
    return SessionPool(
        workers,
        worker_factory=DisabledWorkerFactory(),
        phone_burst_max=0,
    )


def test_register_picks_free_unbound_physical():
    pool = _pool()

    def _fake_bind(seat_id, *, serial=None, connect=None, unlock=None):
        with pool._lock:
            slot = pool._find_seat_locked(seat_id)
            assert slot is not None
            slot.bound_serial = connect or serial
            slot.vnc_url = pool.effective_vnc(slot)
        return 200, {
            "ok": True,
            "seat_id": seat_id,
            "bound_serial": connect or serial,
            "vnc_url": pool.effective_vnc(slot),
        }

    pool.bind_phone_seat = _fake_bind  # type: ignore[method-assign]
    status, data = pool.register_physical_device(connect="192.168.1.10:5555")
    assert status == 200
    assert data["ok"] is True
    assert data["seat_id"] == "phone1"
    assert data["bound_serial"] == "192.168.1.10:5555"
    assert "udid=192.168.1.10%3A5555" in (data["vnc_url"] or "") or "udid=" in (
        data["vnc_url"] or ""
    )


def test_register_skips_cloud_and_already_bound():
    pool = _pool()
    with pool._lock:
        slot = pool._find_seat_locked("phone1")
        assert slot
        slot.bound_serial = "already-bound"

    status, data = pool.register_physical_device(connect="10.0.0.2:5555")
    assert status == 503
    assert data["code"] == "no_free_phone_seat"


def test_register_explicit_seat_id_rebinds():
    pool = _pool()
    with pool._lock:
        slot = pool._find_seat_locked("phone1")
        assert slot
        slot.bound_serial = "old"

    def _fake_bind(seat_id, *, serial=None, connect=None, unlock=None):
        with pool._lock:
            s = pool._find_seat_locked(seat_id)
            assert s
            s.bound_serial = connect or serial
        return 200, {
            "ok": True,
            "seat_id": seat_id,
            "bound_serial": connect or serial,
            "vnc_url": "http://localhost:6083/watch.html?udid=new",
        }

    pool.bind_phone_seat = _fake_bind  # type: ignore[method-assign]
    status, data = pool.register_physical_device(
        connect="10.0.0.3:5555",
        seat_id="phone1",
    )
    assert status == 200
    assert data["seat_id"] == "phone1"
    assert data["bound_serial"] == "10.0.0.3:5555"


def test_register_refuses_cloud_seat_id():
    pool = _pool()
    status, data = pool.register_physical_device(
        connect="10.0.0.4:5555",
        seat_id="phone-cloud1",
    )
    assert status == 400
    assert data["code"] == "not_physical_phone"


def test_register_missing_target():
    pool = _pool()
    status, data = pool.register_physical_device()
    assert status == 400
    assert data["code"] == "missing_target"


def test_register_seat_not_found():
    pool = _pool()
    status, data = pool.register_physical_device(
        connect="10.0.0.5:5555",
        seat_id="missing",
    )
    assert status == 404
    assert data["code"] == "seat_not_found"
