"""Unit tests for reserved-full → WorkerFactory burst lease/recycle."""

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
        self.fail_spawn = False

    def spawn(self, kind, *, seat_id: str) -> SpawnedWorker:
        if self.fail_spawn:
            raise WorkerFactoryError("spawn boom", code="spawn_failed")
        self.spawned.append(seat_id)
        return SpawnedWorker(
            seat_id=seat_id,
            kind=kind,
            control_url=f"http://{seat_id}:8000",
            vnc_url=f"http://vnc/{seat_id}",
            handle=f"job-{seat_id}",
        )

    def destroy(self, handle: str) -> None:
        self.destroyed.append(handle)


def _pool(*, burst_max: int = 1, factory=None) -> SessionPool:
    workers = [
        WorkerSpec(id="desk1", url="http://w1:8000", vnc_url="http://vnc/1", kind="desktop"),
        WorkerSpec(id="desk2", url="http://w2:8000", vnc_url="http://vnc/2", kind="desktop"),
    ]
    return SessionPool(
        workers,
        default_ttl_sec=600,
        parked_ttl_sec=1800,
        worker_factory=factory or FakeWorkerFactory(),
        desktop_burst_max=burst_max,
        phone_burst_max=0,
    )


def test_prefers_reserved_before_burst():
    factory = FakeWorkerFactory()
    pool = _pool(burst_max=2, factory=factory)
    a = pool.lease(kind="desktop")
    b = pool.lease(kind="desktop")
    assert a and b
    assert {a.seat_id, b.seat_id} == {"desk1", "desk2"}
    assert factory.spawned == []


def test_burst_when_reserved_full():
    factory = FakeWorkerFactory()
    pool = _pool(burst_max=1, factory=factory)
    pool.lease(kind="desktop")
    pool.lease(kind="desktop")
    third = pool.lease(kind="desktop", agent_id="agent-x")
    assert third is not None
    assert third.seat_id.startswith("desk-burst-")
    assert third.worker_base_url.startswith("http://desk-burst-")
    assert len(factory.spawned) == 1
    seats = {s["id"]: s for s in pool.list_seats()}
    assert seats[third.seat_id]["ephemeral"] is True
    assert seats[third.seat_id]["state"] == "leased"


def test_burst_max_zero_returns_none():
    factory = FakeWorkerFactory()
    pool = _pool(burst_max=0, factory=factory)
    pool.lease(kind="desktop")
    pool.lease(kind="desktop")
    assert pool.lease(kind="desktop") is None
    assert factory.spawned == []


def test_burst_cap_enforced():
    factory = FakeWorkerFactory()
    pool = _pool(burst_max=1, factory=factory)
    pool.lease(kind="desktop")
    pool.lease(kind="desktop")
    assert pool.lease(kind="desktop") is not None
    assert pool.lease(kind="desktop") is None
    assert len(factory.spawned) == 1


def test_recycle_destroys_ephemeral():
    factory = FakeWorkerFactory()
    pool = _pool(burst_max=1, factory=factory)
    pool.lease(kind="desktop")
    pool.lease(kind="desktop")
    third = pool.lease(kind="desktop")
    assert third is not None
    url = pool.recycle(third.session_id)
    assert url == third.worker_base_url
    assert factory.destroyed == [f"job-{third.seat_id}"]
    assert third.seat_id not in {s["id"] for s in pool.list_seats()}
    # Cap freed — can burst again
    again = pool.lease(kind="desktop")
    assert again is not None
    assert again.seat_id.startswith("desk-burst-")


def test_disabled_factory_raises_when_max_positive():
    pool = _pool(burst_max=1, factory=DisabledWorkerFactory())
    pool.lease(kind="desktop")
    pool.lease(kind="desktop")
    try:
        pool.lease(kind="desktop")
        raise AssertionError("expected WorkerFactoryError")
    except WorkerFactoryError as e:
        assert e.code == "burst_disabled"


def test_spawn_failure_propagates():
    factory = FakeWorkerFactory()
    factory.fail_spawn = True
    pool = _pool(burst_max=1, factory=factory)
    pool.lease(kind="desktop")
    pool.lease(kind="desktop")
    try:
        pool.lease(kind="desktop")
        raise AssertionError("expected WorkerFactoryError")
    except WorkerFactoryError as e:
        assert e.code == "spawn_failed"
    assert pool.burst_count_for("desktop") == 0


def _phone_pool(*, burst_max: int = 1, factory=None) -> SessionPool:
    workers = [
        WorkerSpec(
            id="phone-cloud1",
            url="http://phone-cloud-1:8003",
            vnc_url="https://placeholder.invalid/watch",
            kind="phone",
            provider="phone-harness",
        ),
    ]
    pool = SessionPool(
        workers,
        worker_factory=factory or FakeWorkerFactory(),
        desktop_burst_max=0,
        phone_burst_max=burst_max,
    )

    def _fake_provision(seat_id: str, *, lease_ttl_sec: int) -> None:
        with pool._lock:
            slot = pool._find_seat_locked(seat_id)
            assert slot is not None
            assert slot.provider == "phone-harness"
            slot.provider_session_id = f"sess-{seat_id}"
            slot.cloud_watch_url = f"https://watch.example/{seat_id}"
            slot.vnc_url = slot.cloud_watch_url
            slot.bound_serial = "10.0.0.1:5555"

    pool._provision_and_bind_cloud = _fake_provision  # type: ignore[method-assign]
    pool._teardown_cloud = lambda payload: None  # type: ignore[method-assign]
    return pool


def test_phone_prefers_reserved_cloud_before_burst():
    factory = FakeWorkerFactory()
    pool = _phone_pool(burst_max=2, factory=factory)
    info = pool.lease(kind="phone")
    assert info is not None
    assert info.seat_id == "phone-cloud1"
    assert factory.spawned == []


def test_phone_burst_when_reserved_cloud_full():
    factory = FakeWorkerFactory()
    pool = _phone_pool(burst_max=1, factory=factory)
    pool.lease(kind="phone")
    second = pool.lease(kind="phone", agent_id="overflow")
    assert second is not None
    assert second.seat_id.startswith("phone-burst-")
    assert second.vnc_url.startswith("https://watch.example/")
    assert len(factory.spawned) == 1
    seats = {s["id"]: s for s in pool.list_seats()}
    assert seats[second.seat_id]["ephemeral"] is True
    assert seats[second.seat_id]["provider"] == "phone-harness"


def test_phone_burst_recycle_destroys_pod():
    factory = FakeWorkerFactory()
    pool = _phone_pool(burst_max=1, factory=factory)
    pool.lease(kind="phone")
    second = pool.lease(kind="phone")
    assert second is not None
    pool.recycle(second.session_id)
    assert factory.destroyed == [f"job-{second.seat_id}"]
    assert second.seat_id not in {s["id"] for s in pool.list_seats()}


def test_phone_burst_provision_failure_destroys_ephemeral():
    from providers.base import CloudPhoneProviderError

    factory = FakeWorkerFactory()
    pool = _phone_pool(burst_max=1, factory=factory)
    assert pool.lease(kind="phone") is not None  # reserved cloud OK

    def _fail_provision(seat_id: str, *, lease_ttl_sec: int) -> None:
        raise CloudPhoneProviderError("no quota", code="quota")

    pool._provision_and_bind_cloud = _fail_provision  # type: ignore[method-assign]
    try:
        pool.lease(kind="phone")
        raise AssertionError("expected CloudPhoneProviderError")
    except CloudPhoneProviderError as e:
        assert e.code == "quota"
    assert factory.destroyed == [f"job-{factory.spawned[0]}"]
    assert pool.burst_count_for("phone") == 0
