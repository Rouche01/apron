"""Lease-burst spawn / destroy helpers used by SessionPool.

Pool keeps the lock and thin wrappers. This module owns WorkerFactory spawn of
ephemeral seats (desktop + phone) and factory handle teardown.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from inventory import SeatKind
from workers.base import WorkerFactoryError

logger = logging.getLogger("computer.gateway.burst_seats")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def phone_burst_provider() -> str:
    return (
        os.environ.get("PHONE_BURST_PROVIDER", "phone-harness").strip()
        or "phone-harness"
    )


class BurstSeatHost(Protocol):
    """SessionPool surface for lease-burst spawn / destroy."""

    _lock: Any
    _slots: list
    _burst_inflight: set[str]
    _factory: Any
    _desktop_burst_max: int
    _phone_burst_max: int

    def burst_max_for(self, kind: SeatKind) -> int: ...

    def _burst_in_use_locked(self, kind: SeatKind) -> int: ...

    def _lease_info(self, slot: Any) -> Any: ...

    def _find_seat_locked(self, seat_id: str) -> Any | None: ...

    def _provision_and_bind_cloud(self, seat_id: str, *, lease_ttl_sec: int) -> None: ...

    def _abort_claimed_lease(self, seat_id: str) -> None: ...


def burst_in_use(slots: list, inflight: set[str], kind: SeatKind) -> int:
    live = sum(1 for s in slots if s.ephemeral and s.kind == kind)
    prefix = "phone-burst-" if kind == "phone" else "desk-burst-"
    inflight_n = sum(1 for sid in inflight if sid.startswith(prefix))
    return live + inflight_n


def destroy_factory_handle(factory: Any, handle: str | None) -> None:
    if not handle:
        return
    try:
        factory.destroy(handle)
    except Exception as e:
        logger.warning("burst destroy failed handle=%s: %s", handle, e)


def lease_burst(
    host: BurstSeatHost,
    *,
    kind: SeatKind,
    agent_id: str | None,
    job_id: str | None,
    ttl: int,
) -> Any | None:
    """Spawn an ephemeral seat when reserved inventory is exhausted.

    Phone burst: spawn ADB worker Pod, then CloudPhoneProvider.provision + bind
    (same as reserved cloud seats). Watch URL comes from the provider.
    Returns LeaseInfo or None.
    """
    # Late import avoids circular import with pool at module load.
    from pool import WorkerSlot

    max_n = host.burst_max_for(kind)
    if max_n <= 0:
        return None

    prefix = "phone-burst" if kind == "phone" else "desk-burst"
    seat_id = f"{prefix}-{uuid.uuid4().hex[:8]}"
    provider = phone_burst_provider() if kind == "phone" else None

    with host._lock:
        if host._burst_in_use_locked(kind) >= max_n:
            return None
        host._burst_inflight.add(seat_id)

    try:
        spawned = host._factory.spawn(kind, seat_id=seat_id)
    except WorkerFactoryError:
        with host._lock:
            host._burst_inflight.discard(seat_id)
        raise
    except Exception as e:
        with host._lock:
            host._burst_inflight.discard(seat_id)
        raise WorkerFactoryError(str(e), code="spawn_failed") from e

    now = _utcnow()
    sid = str(uuid.uuid4())
    with host._lock:
        host._burst_inflight.discard(seat_id)
        slot = WorkerSlot(
            base_url=spawned.control_url.rstrip("/"),
            vnc_url=spawned.vnc_url,
            inventory_vnc_url=spawned.vnc_url,
            kind=spawned.kind,
            seat_id=spawned.seat_id or seat_id,
            state="leased",
            session_id=sid,
            agent_id=agent_id,
            job_id=job_id,
            expires_at=now + timedelta(seconds=ttl),
            ephemeral=True,
            factory_handle=spawned.handle,
            provider=provider,
        )
        host._slots.append(slot)
        logger.info(
            "burst leased seat=%s kind=%s handle=%s factory=%s provider=%s",
            slot.seat_id,
            slot.kind,
            slot.factory_handle,
            host._factory.name,
            provider or "-",
        )
        info = host._lease_info(slot)

    if provider:
        try:
            host._provision_and_bind_cloud(seat_id, lease_ttl_sec=ttl)
        except Exception:
            host._abort_claimed_lease(seat_id)
            raise
        with host._lock:
            slot = host._find_seat_locked(seat_id)
            if not slot or slot.state != "leased":
                return None
            return host._lease_info(slot)

    return info
