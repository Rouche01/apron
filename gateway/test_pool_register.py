"""POST /v1/devices/register — pick free physical seat, warm-spawn, unregister."""

from __future__ import annotations

from inventory import WorkerSpec
from pool import SessionPool
from workers.base import SpawnedWorker, WorkerFactoryError
from workers.disabled import DisabledWorkerFactory


class FakeWorkerFactory:
    name = "fake"

    def __init__(self) -> None:
        self.spawned: list[str] = []
        self.destroyed: list[str] = []

    def spawn(self, kind, *, seat_id: str) -> SpawnedWorker:
        self.spawned.append(seat_id)
        return SpawnedWorker(
            seat_id=seat_id,
            kind=kind,
            control_url=f"http://{seat_id}:8000",
            vnc_url="https://placeholder.invalid/watch",
            handle=f"job-{seat_id}",
        )

    def destroy(self, handle: str) -> None:
        self.destroyed.append(handle)


def _pool(*, factory=None, warm_max: int = 0) -> SessionPool:
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
        worker_factory=factory or DisabledWorkerFactory(),
        phone_burst_max=0,
        phone_warm_spawn_max=warm_max,
    )


def _stub_bind(pool: SessionPool):
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


def test_register_picks_free_unbound_physical():
    pool = _pool()
    _stub_bind(pool)
    status, data = pool.register_physical_device(connect="192.168.1.10:5555")
    assert status == 200
    assert data["ok"] is True
    assert data["seat_id"] == "phone1"
    assert data["bound_serial"] == "192.168.1.10:5555"
    assert data.get("warm_spawned") is False
    assert "udid=" in (data["vnc_url"] or "")


def test_register_skips_cloud_and_already_bound_without_warm_max():
    pool = _pool(warm_max=0)
    with pool._lock:
        slot = pool._find_seat_locked("phone1")
        assert slot
        slot.bound_serial = "already-bound"

    status, data = pool.register_physical_device(connect="10.0.0.2:5555")
    assert status == 503
    assert data["code"] == "no_free_phone_seat"


def test_register_warm_spawns_when_no_free_unbound():
    factory = FakeWorkerFactory()
    pool = _pool(factory=factory, warm_max=1)
    with pool._lock:
        slot = pool._find_seat_locked("phone1")
        assert slot
        slot.bound_serial = "already-bound"
    _stub_bind(pool)

    status, data = pool.register_physical_device(connect="10.0.0.9:5555")
    assert status == 200
    assert data["warm_spawned"] is True
    assert data["seat_id"].startswith("phone-warm-")
    assert len(factory.spawned) == 1
    seats = {s["id"]: s for s in pool.list_seats()}
    assert seats[data["seat_id"]]["warm_spawned"] is True
    assert seats[data["seat_id"]]["ephemeral"] is False


def test_register_warm_cap():
    factory = FakeWorkerFactory()
    pool = _pool(factory=factory, warm_max=1)
    with pool._lock:
        pool._find_seat_locked("phone1").bound_serial = "x"  # type: ignore[union-attr]
    _stub_bind(pool)
    assert pool.register_physical_device(connect="10.0.0.1:5555")[0] == 200
    status, data = pool.register_physical_device(connect="10.0.0.2:5555")
    assert status == 503
    assert data["code"] == "warm_spawn_cap"


def test_unregister_destroys_warm_seat():
    factory = FakeWorkerFactory()
    pool = _pool(factory=factory, warm_max=1)
    with pool._lock:
        pool._find_seat_locked("phone1").bound_serial = "x"  # type: ignore[union-attr]
    _stub_bind(pool)

    def _fake_unbind(seat_id: str):
        with pool._lock:
            s = pool._find_seat_locked(seat_id)
            if s:
                s.bound_serial = None
                s.vnc_url = s.inventory_vnc_url
        return 200, {"ok": True, "seat_id": seat_id}

    pool.unbind_phone_seat = _fake_unbind  # type: ignore[method-assign]
    _, reg = pool.register_physical_device(connect="10.0.0.9:5555")
    seat_id = reg["seat_id"]
    status, data = pool.unregister_physical_device(seat_id)
    assert status == 200
    assert data["destroyed"] is True
    assert factory.destroyed == [f"job-{seat_id}"]
    assert seat_id not in {s["id"] for s in pool.list_seats()}


def test_unregister_refuses_inventory_seat():
    pool = _pool()
    status, data = pool.unregister_physical_device("phone1")
    assert status == 400
    assert data["code"] == "not_warm_spawned"


def test_recycle_keeps_warm_seat():
    factory = FakeWorkerFactory()
    pool = _pool(factory=factory, warm_max=1)
    with pool._lock:
        pool._find_seat_locked("phone1").bound_serial = "x"  # type: ignore[union-attr]
    _stub_bind(pool)
    _, reg = pool.register_physical_device(connect="10.0.0.9:5555")
    seat_id = reg["seat_id"]
    leased = pool.lease(kind="phone", seat_id=seat_id)
    assert leased is not None
    pool.recycle(leased.session_id)
    assert factory.destroyed == []
    seats = {s["id"]: s for s in pool.list_seats()}
    assert seats[seat_id]["state"] == "free"
    assert seats[seat_id]["warm_spawned"] is True


def test_register_explicit_seat_id_rebinds():
    pool = _pool()
    with pool._lock:
        slot = pool._find_seat_locked("phone1")
        assert slot
        slot.bound_serial = "old"
    _stub_bind(pool)
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
