"""Phone lease: cloud-default selection + optional seat_id pin."""

from __future__ import annotations

from inventory import WorkerSpec
from pool import SeatPinError, SessionPool
from workers.disabled import DisabledWorkerFactory


def _stub_cloud(pool: SessionPool) -> SessionPool:
    """Skip real CloudPhoneProvider / ADB bind in selection unit tests."""
    pool._provision_and_bind_cloud = lambda *a, **k: None  # type: ignore[method-assign]
    pool._reconnect_cloud_adb = lambda *a, **k: None  # type: ignore[method-assign]
    return pool


def _phone_pool() -> SessionPool:
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
            vnc_url="http://localhost:6083/watch.html",
            kind="phone",
            provider="phone-harness",
        ),
    ]
    return _stub_cloud(
        SessionPool(
            workers,
            default_ttl_sec=600,
            parked_ttl_sec=1800,
            worker_factory=DisabledWorkerFactory(),
            desktop_burst_max=0,
            phone_burst_max=0,
        )
    )


def test_phone_default_skips_physical_uses_cloud():
    pool = _phone_pool()
    info = pool.lease(kind="phone", agent_id="a1")
    assert info is not None
    assert info.seat_id == "phone-cloud1"
    assert pool.claimable_free_count_for("phone") == 0
    assert pool.free_count_for("phone") == 1  # physical still free


def test_phone_no_cloud_returns_none_while_physical_free():
    workers = [
        WorkerSpec(
            id="phone1",
            url="http://phone-1:8003",
            vnc_url="http://localhost:6083/watch.html",
            kind="phone",
        ),
    ]
    pool = SessionPool(
        workers,
        worker_factory=DisabledWorkerFactory(),
        phone_burst_max=0,
    )
    assert pool.lease(kind="phone") is None
    assert pool.claimable_free_count_for("phone") == 0


def test_seat_id_pins_physical():
    pool = _phone_pool()
    info = pool.lease(kind="phone", seat_id="phone1", agent_id="desk-user")
    assert info is not None
    assert info.seat_id == "phone1"
    assert info.agent_id == "desk-user"


def test_seat_id_pins_cloud_while_physical_also_free():
    pool = _phone_pool()
    info = pool.lease(kind="phone", seat_id="phone-cloud1")
    assert info is not None
    assert info.seat_id == "phone-cloud1"


def test_seat_id_not_found():
    pool = _phone_pool()
    try:
        pool.lease(kind="phone", seat_id="missing")
        raise AssertionError("expected SeatPinError")
    except SeatPinError as e:
        assert e.code == "seat_not_found"


def test_seat_id_kind_mismatch():
    workers = [
        WorkerSpec(id="desk1", url="http://w1:8000", vnc_url="http://vnc/1", kind="desktop"),
        WorkerSpec(
            id="phone1",
            url="http://phone-1:8003",
            vnc_url="http://localhost:6083/watch.html",
            kind="phone",
        ),
    ]
    pool = SessionPool(workers, worker_factory=DisabledWorkerFactory())
    try:
        pool.lease(kind="phone", seat_id="desk1")
        raise AssertionError("expected SeatPinError")
    except SeatPinError as e:
        assert e.code == "seat_kind_mismatch"


def test_seat_id_busy():
    pool = _phone_pool()
    first = pool.lease(kind="phone", seat_id="phone1")
    assert first is not None
    try:
        pool.lease(kind="phone", seat_id="phone1")
        raise AssertionError("expected SeatPinError")
    except SeatPinError as e:
        assert e.code == "seat_busy"


def test_job_id_reattach_physical_still_works():
    pool = _phone_pool()
    first = pool.lease(kind="phone", seat_id="phone1", job_id="job-a")
    assert first is not None
    pool.release(first.session_id)
    # Default cloud path must not steal; job_id reattach gets the parked physical.
    again = pool.lease(kind="phone", job_id="job-a")
    assert again is not None
    assert again.seat_id == "phone1"
    assert again.session_id == first.session_id


def test_seat_id_claims_parked():
    pool = _phone_pool()
    first = pool.lease(kind="phone", seat_id="phone1", job_id="job-b")
    assert first is not None
    pool.release(first.session_id)
    again = pool.lease(kind="phone", seat_id="phone1")
    assert again is not None
    assert again.seat_id == "phone1"
    assert again.session_id == first.session_id
