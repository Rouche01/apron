"""Exclusive session leases over a fixed set of workers (desktop or phone).

Worker states:
  free   — available for a new lease (clean / soft-reset as appropriate)
  leased — exclusive input claim (lease TTL)
  parked — warm seat kept for the same session_id/job_id (parked idle TTL)
"""

from __future__ import annotations

import logging
import os
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import httpx

import cloud_phone_seats
import physical_phone_seats
from inventory import SeatKind, WorkerSpec
from physical_phone_seats import vnc_url_with_udid
from workers.base import WorkerFactory, WorkerFactoryError
from workers.disabled import DisabledWorkerFactory

logger = logging.getLogger("computer.gateway.pool")

SlotState = Literal["free", "leased", "parked"]


class SeatPinError(Exception):
    """Exact seat_id lease failed (not found / busy / kind mismatch)."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _clamp_ttl(ttl: int, *, minimum: int = 30) -> int:
    return ttl if ttl >= minimum else minimum


def _env_burst_max(name: str, default: int = 0) -> int:
    try:
        return max(0, int(os.environ.get(name, str(default)) or default))
    except ValueError:
        return default


@dataclass
class WorkerSlot:
    base_url: str
    vnc_url: str
    kind: SeatKind = "desktop"
    seat_id: str = ""
    state: SlotState = "free"
    session_id: str | None = None
    agent_id: str | None = None
    job_id: str | None = None
    expires_at: datetime | None = None  # lease TTL; only while leased
    parked_at: datetime | None = None  # parked idle clock
    bound_serial: str | None = None  # phone runtime bind (Watch udid)
    inventory_vnc_url: str = ""  # original inventory vnc_url (before udid rewrite)
    provider: str | None = None  # inventory cloud provider id (e.g. phone-harness)
    provider_session_id: str | None = None  # live cloud session
    cloud_watch_url: str | None = None
    cloud_unlock: str | None = None
    ephemeral: bool = False  # lease-burst seat (destroyed on recycle)
    warm_spawned: bool = False  # register-spawned reserved physical (kept until unregister)
    factory_handle: str | None = None  # WorkerFactory.destroy id


@dataclass
class LeaseInfo:
    session_id: str
    worker_base_url: str
    vnc_url: str
    expires_at: datetime
    kind: SeatKind = "desktop"
    seat_id: str = ""
    agent_id: str | None = None
    job_id: str | None = None


@dataclass
class AcquireResult:
    info: LeaseInfo | None = None
    error: Literal["not_found", "already_leased"] | None = None


class SessionPool:
    def __init__(
        self,
        workers: list[WorkerSpec] | list[tuple[str, str]],
        *,
        default_ttl_sec: int = 600,
        parked_ttl_sec: int = 1800,
        http_timeout: float = 30.0,
        worker_factory: WorkerFactory | None = None,
        desktop_burst_max: int | None = None,
        phone_burst_max: int | None = None,
        phone_warm_spawn_max: int | None = None,
    ) -> None:
        if not workers:
            raise ValueError("at least one worker is required")
        self._default_ttl = default_ttl_sec
        self._parked_ttl = max(30, parked_ttl_sec)
        self._timeout = http_timeout
        self._factory: WorkerFactory = worker_factory or DisabledWorkerFactory()
        self._desktop_burst_max = (
            desktop_burst_max
            if desktop_burst_max is not None
            else _env_burst_max("DESKTOP_BURST_MAX")
        )
        self._phone_burst_max = (
            phone_burst_max
            if phone_burst_max is not None
            else _env_burst_max("PHONE_BURST_MAX")
        )
        self._phone_warm_spawn_max = (
            phone_warm_spawn_max
            if phone_warm_spawn_max is not None
            else _env_burst_max("PHONE_WARM_SPAWN_MAX")
        )
        self._lock = threading.Lock()
        self._slots: list[WorkerSlot] = []
        # seat_ids currently mid-spawn (count toward burst / warm caps)
        self._burst_inflight: set[str] = set()
        self._warm_inflight: set[str] = set()
        for i, w in enumerate(workers):
            if isinstance(w, WorkerSpec):
                self._slots.append(
                    WorkerSlot(
                        base_url=w.url.rstrip("/"),
                        vnc_url=w.vnc_url,
                        inventory_vnc_url=w.vnc_url,
                        kind=w.kind,
                        seat_id=w.id or f"desk{i + 1}",
                        provider=w.provider,
                    )
                )
            else:
                u, v = w
                self._slots.append(
                    WorkerSlot(
                        base_url=u.rstrip("/"),
                        vnc_url=v,
                        inventory_vnc_url=v,
                        kind="desktop",
                        seat_id=f"desk{i + 1}",
                    )
                )

    @property
    def worker_count(self) -> int:
        return len(self._slots)

    @property
    def parked_ttl_sec(self) -> int:
        return self._parked_ttl

    def effective_vnc(self, slot: WorkerSlot) -> str:
        # Cloud HITL: vendor watch_url (not scrcpy / udid).
        if slot.cloud_watch_url:
            return slot.cloud_watch_url
        base = slot.inventory_vnc_url or slot.vnc_url
        if slot.kind == "phone" and slot.bound_serial and not slot.provider:
            return vnc_url_with_udid(base, slot.bound_serial)
        return slot.vnc_url

    def _effective_vnc(self, slot: WorkerSlot) -> str:
        return self.effective_vnc(slot)

    def _lease_info(self, slot: WorkerSlot) -> LeaseInfo:
        assert slot.session_id is not None and slot.expires_at is not None
        return LeaseInfo(
            session_id=slot.session_id,
            worker_base_url=slot.base_url,
            vnc_url=self._effective_vnc(slot),
            expires_at=slot.expires_at,
            kind=slot.kind,
            seat_id=slot.seat_id,
            agent_id=slot.agent_id,
            job_id=slot.job_id,
        )

    def worker_urls(self) -> list[str]:
        return [s.base_url for s in self._slots]

    def free_count_for(self, kind: SeatKind) -> int:
        with self._lock:
            self._park_expired_leases_locked(_utcnow())
            return sum(1 for s in self._slots if s.state == "free" and s.kind == kind)

    def claimable_free_count_for(self, kind: SeatKind) -> int:
        """Free seats the default lease path will actually claim.

        Phone without seat_id is cloud-only (provider set); physical seats need a pin.
        """
        with self._lock:
            self._park_expired_leases_locked(_utcnow())
            n = 0
            for s in self._slots:
                if s.state != "free" or s.kind != kind:
                    continue
                if kind == "phone" and not s.provider:
                    continue
                n += 1
            return n

    def burst_max_for(self, kind: SeatKind) -> int:
        if kind == "phone":
            return self._phone_burst_max
        return self._desktop_burst_max

    def burst_count_for(self, kind: SeatKind) -> int:
        with self._lock:
            return self._burst_in_use_locked(kind)

    def _burst_in_use_locked(self, kind: SeatKind) -> int:
        live = sum(1 for s in self._slots if s.ephemeral and s.kind == kind)
        inflight = sum(
            1
            for sid in self._burst_inflight
            if sid.startswith("phone-burst-" if kind == "phone" else "desk-burst-")
        )
        return live + inflight

    def _destroy_factory_handle(self, handle: str | None) -> None:
        if not handle:
            return
        try:
            self._factory.destroy(handle)
        except Exception as e:
            logger.warning("burst destroy failed handle=%s: %s", handle, e)

    def _cloud_teardown_payload_locked(self, slot: WorkerSlot) -> dict[str, str] | None:
        return cloud_phone_seats.teardown_payload(slot)

    def _clear_to_free_locked(self, slot: WorkerSlot) -> None:
        slot.state = "free"
        slot.session_id = None
        slot.agent_id = None
        slot.job_id = None
        slot.expires_at = None
        slot.parked_at = None
        # Drop live cloud session fields; inventory provider name stays.
        slot.provider_session_id = None
        slot.cloud_watch_url = None
        slot.cloud_unlock = None
        if slot.provider:
            slot.bound_serial = None
            slot.vnc_url = slot.inventory_vnc_url

    def _park_locked(self, slot: WorkerSlot, now: datetime) -> None:
        slot.state = "parked"
        slot.expires_at = None
        slot.parked_at = now
        # session_id / agent_id / job_id / cloud session retained for reattach

    def _park_expired_leases_locked(self, now: datetime) -> None:
        for slot in self._slots:
            if (
                slot.state == "leased"
                and slot.expires_at is not None
                and slot.expires_at <= now
            ):
                self._park_locked(slot, now)

    def _sweep_parked_idle_locked(
        self, now: datetime
    ) -> tuple[list[str], list[dict[str, str]], list[str]]:
        """Free parked slots past idle TTL.

        Returns (reset_urls for reserved seats, cloud teardowns, burst destroy handles).
        Ephemeral seats are removed (not returned to free).
        """
        urls: list[str] = []
        teardowns: list[dict[str, str]] = []
        destroy_handles: list[str] = []
        for slot in list(self._slots):
            if slot.state != "parked" or slot.parked_at is None:
                continue
            age = (now - slot.parked_at).total_seconds()
            if age < self._parked_ttl:
                continue
            td = self._cloud_teardown_payload_locked(slot)
            if td:
                teardowns.append(td)
            if slot.ephemeral:
                if slot.factory_handle:
                    destroy_handles.append(slot.factory_handle)
                self._slots.remove(slot)
            else:
                urls.append(slot.base_url)
                self._clear_to_free_locked(slot)
        return urls, teardowns, destroy_handles

    def _teardown_cloud(self, payload: dict[str, str]) -> None:
        cloud_phone_seats.teardown(payload)

    def sweep_expired(self) -> list[str]:
        """Park lease-TTL expiry; free parked-idle slots and return URLs to reset."""
        with self._lock:
            now = _utcnow()
            self._park_expired_leases_locked(now)
            urls, teardowns, destroy_handles = self._sweep_parked_idle_locked(now)
        for td in teardowns:
            self._teardown_cloud(td)
        for handle in destroy_handles:
            self._destroy_factory_handle(handle)
        return urls

    def free_count(self) -> int:
        with self._lock:
            self._park_expired_leases_locked(_utcnow())
            return sum(1 for s in self._slots if s.state == "free")

    def leased_count(self) -> int:
        with self._lock:
            self._park_expired_leases_locked(_utcnow())
            return sum(1 for s in self._slots if s.state == "leased")

    def parked_count(self) -> int:
        with self._lock:
            self._park_expired_leases_locked(_utcnow())
            return sum(1 for s in self._slots if s.state == "parked")

    def list_seats(self) -> list[dict[str, Any]]:
        """Per-worker inventory for ops / Desk (state, session, vnc, kind)."""
        with self._lock:
            now = _utcnow()
            self._park_expired_leases_locked(now)
            seats: list[dict[str, Any]] = []
            for slot in self._slots:
                seat: dict[str, Any] = {
                    "id": slot.seat_id,
                    "kind": slot.kind,
                    "state": slot.state,
                    "vnc_url": self._effective_vnc(slot),
                    "worker_base_url": slot.base_url,
                    "session_id": slot.session_id,
                    "agent_id": slot.agent_id,
                    "job_id": slot.job_id,
                    "expires_at": slot.expires_at.isoformat() if slot.expires_at else None,
                    "parked_at": slot.parked_at.isoformat() if slot.parked_at else None,
                    "ephemeral": slot.ephemeral,
                    "warm_spawned": slot.warm_spawned,
                }
                if slot.kind == "phone":
                    seat["bound_serial"] = slot.bound_serial
                    if slot.provider:
                        seat["provider"] = slot.provider
                        seat["provider_session_id"] = slot.provider_session_id
                seats.append(seat)
            return seats

    def _worker_device_bind(
        self,
        worker_url: str,
        *,
        connect: str,
        unlock: str | None,
    ) -> tuple[int, Any]:
        return physical_phone_seats.worker_device_bind(
            self._timeout, worker_url, connect=connect, unlock=unlock
        )

    def _provision_and_bind_cloud(self, seat_id: str, *, lease_ttl_sec: int) -> None:
        """Provision via CloudPhoneProvider and bind the phone worker (outside lock)."""
        cloud_phone_seats.provision_and_bind(self, seat_id, lease_ttl_sec=lease_ttl_sec)

    def _reconnect_cloud_adb(self, seat_id: str) -> None:
        """Re-adb connect (+ unlock) for an existing cloud session (park → acquire)."""
        cloud_phone_seats.reconnect_adb(self, seat_id)

    def _abort_claimed_lease(self, seat_id: str) -> None:
        """Undo a claimed lease after cloud provision / burst failure."""
        destroy_handle: str | None = None
        teardown: dict[str, str] | None = None
        with self._lock:
            slot = self._find_seat_locked(seat_id)
            if not slot or slot.state != "leased":
                return
            teardown = self._cloud_teardown_payload_locked(slot)
            if slot.ephemeral:
                destroy_handle = slot.factory_handle
                self._slots.remove(slot)
            else:
                self._clear_to_free_locked(slot)
        if teardown:
            self._teardown_cloud(teardown)
        self._destroy_factory_handle(destroy_handle)

    def lease(
        self,
        *,
        agent_id: str | None = None,
        job_id: str | None = None,
        ttl_sec: int | None = None,
        kind: SeatKind = "desktop",
        seat_id: str | None = None,
    ) -> LeaseInfo | None:
        """Reattach a parked seat for job_id if present; else claim a free worker of kind.

        Phone without seat_id: cloud seats only (inventory provider set). Physical phone
        seats require an exact seat_id pin. Optional seat_id claims that seat if free or
        parked (raises SeatPinError otherwise).

        When reserved claimable seats of kind are full and BURST_MAX > 0, spawn via
        WorkerFactory. Cloud seats: provision + worker bind after claim. Raises
        CloudPhoneProviderError on provider/bind failure (seat returned to free).
        Raises WorkerFactoryError on burst spawn failure.
        """
        ttl = _clamp_ttl(ttl_sec if ttl_sec is not None else self._default_ttl)
        now = _utcnow()
        cloud_reattach_seat: str | None = None
        cloud_provision_seat: str | None = None
        info: LeaseInfo | None = None
        pin = (seat_id or "").strip() or None

        with self._lock:
            self._park_expired_leases_locked(now)

            if job_id:
                for slot in self._slots:
                    if (
                        slot.state == "parked"
                        and slot.job_id == job_id
                        and slot.kind == kind
                    ):
                        slot.state = "leased"
                        slot.parked_at = None
                        slot.agent_id = agent_id if agent_id is not None else slot.agent_id
                        slot.expires_at = now + timedelta(seconds=ttl)
                        info = self._lease_info(slot)
                        if slot.provider:
                            cloud_reattach_seat = slot.seat_id
                        break

            if info is None and pin:
                slot = self._find_seat_locked(pin)
                if not slot:
                    raise SeatPinError(f"seat {pin!r} not found", code="seat_not_found")
                if slot.kind != kind:
                    raise SeatPinError(
                        f"seat {pin!r} is kind={slot.kind!r}, requested {kind!r}",
                        code="seat_kind_mismatch",
                    )
                if slot.state == "leased":
                    raise SeatPinError(f"seat {pin!r} is already leased", code="seat_busy")
                if slot.state not in ("free", "parked"):
                    raise SeatPinError(f"seat {pin!r} is unavailable", code="seat_busy")
                if slot.state == "free":
                    slot.session_id = str(uuid.uuid4())
                    slot.agent_id = agent_id
                    slot.job_id = job_id
                else:
                    if agent_id is not None:
                        slot.agent_id = agent_id
                    if job_id is not None:
                        slot.job_id = job_id
                slot.state = "leased"
                slot.parked_at = None
                slot.expires_at = now + timedelta(seconds=ttl)
                info = self._lease_info(slot)
                if slot.provider:
                    if slot.provider_session_id:
                        cloud_reattach_seat = slot.seat_id
                    else:
                        cloud_provision_seat = slot.seat_id

            if info is None:
                for slot in self._slots:
                    if slot.state != "free" or slot.kind != kind:
                        continue
                    # Phone default: cloud only; physical requires seat_id pin above.
                    if kind == "phone" and not slot.provider:
                        continue
                    sid = str(uuid.uuid4())
                    slot.state = "leased"
                    slot.session_id = sid
                    slot.agent_id = agent_id
                    slot.job_id = job_id
                    slot.expires_at = now + timedelta(seconds=ttl)
                    slot.parked_at = None
                    info = self._lease_info(slot)
                    if slot.provider:
                        cloud_provision_seat = slot.seat_id
                    break

        if info is None:
            # Exact pin never falls through to burst.
            if pin:
                return None
            return self._lease_burst(
                kind=kind,
                agent_id=agent_id,
                job_id=job_id,
                ttl=ttl,
            )

        if cloud_reattach_seat:
            self._reconnect_cloud_adb(cloud_reattach_seat)
            with self._lock:
                slot = self._find_seat_locked(cloud_reattach_seat)
                if slot and slot.state == "leased":
                    return self._lease_info(slot)
            return info

        if cloud_provision_seat:
            try:
                self._provision_and_bind_cloud(cloud_provision_seat, lease_ttl_sec=ttl)
            except Exception:
                self._abort_claimed_lease(cloud_provision_seat)
                raise
            with self._lock:
                slot = self._find_seat_locked(cloud_provision_seat)
                if not slot or slot.state != "leased":
                    return None
                return self._lease_info(slot)

        return info

    def _phone_burst_provider(self) -> str:
        return (
            os.environ.get("PHONE_BURST_PROVIDER", "phone-harness").strip()
            or "phone-harness"
        )

    def _lease_burst(
        self,
        *,
        kind: SeatKind,
        agent_id: str | None,
        job_id: str | None,
        ttl: int,
    ) -> LeaseInfo | None:
        """Spawn an ephemeral seat when reserved inventory is exhausted.

        Phone burst: spawn ADB worker Pod, then CloudPhoneProvider.provision + bind
        (same as reserved cloud seats). Watch URL comes from the provider.
        """
        max_n = self.burst_max_for(kind)
        if max_n <= 0:
            return None

        prefix = "phone-burst" if kind == "phone" else "desk-burst"
        seat_id = f"{prefix}-{uuid.uuid4().hex[:8]}"
        phone_provider = self._phone_burst_provider() if kind == "phone" else None

        with self._lock:
            if self._burst_in_use_locked(kind) >= max_n:
                return None
            self._burst_inflight.add(seat_id)

        try:
            spawned = self._factory.spawn(kind, seat_id=seat_id)
        except WorkerFactoryError:
            with self._lock:
                self._burst_inflight.discard(seat_id)
            raise
        except Exception as e:
            with self._lock:
                self._burst_inflight.discard(seat_id)
            raise WorkerFactoryError(str(e), code="spawn_failed") from e

        now = _utcnow()
        sid = str(uuid.uuid4())
        with self._lock:
            self._burst_inflight.discard(seat_id)
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
                provider=phone_provider,
            )
            self._slots.append(slot)
            logger.info(
                "burst leased seat=%s kind=%s handle=%s factory=%s provider=%s",
                slot.seat_id,
                slot.kind,
                slot.factory_handle,
                self._factory.name,
                phone_provider or "-",
            )
            info = self._lease_info(slot)

        if phone_provider:
            try:
                self._provision_and_bind_cloud(seat_id, lease_ttl_sec=ttl)
            except Exception:
                self._abort_claimed_lease(seat_id)
                raise
            with self._lock:
                slot = self._find_seat_locked(seat_id)
                if not slot or slot.state != "leased":
                    return None
                return self._lease_info(slot)

        return info

    def acquire(
        self,
        session_id: str,
        *,
        ttl_sec: int | None = None,
        agent_id: str | None = None,
    ) -> AcquireResult:
        """Parked → leased for the same session_id."""
        ttl = _clamp_ttl(ttl_sec if ttl_sec is not None else self._default_ttl)
        now = _utcnow()
        cloud_seat: str | None = None
        with self._lock:
            self._park_expired_leases_locked(now)
            slot = self._find_locked(session_id)
            if not slot:
                return AcquireResult(error="not_found")
            if slot.state == "leased":
                return AcquireResult(error="already_leased")
            if slot.state != "parked":
                return AcquireResult(error="not_found")
            slot.state = "leased"
            slot.parked_at = None
            if agent_id is not None:
                slot.agent_id = agent_id
            slot.expires_at = now + timedelta(seconds=ttl)
            if slot.provider:
                cloud_seat = slot.seat_id
            info = self._lease_info(slot)

        if cloud_seat:
            self._reconnect_cloud_adb(cloud_seat)
            with self._lock:
                slot = self._find_locked(session_id)
                if slot and slot.state == "leased":
                    return AcquireResult(info=self._lease_info(slot))
        return AcquireResult(info=info)

    def heartbeat(self, session_id: str, ttl_sec: int | None = None) -> LeaseInfo | None:
        ttl = _clamp_ttl(ttl_sec if ttl_sec is not None else self._default_ttl)
        now = _utcnow()
        with self._lock:
            self._park_expired_leases_locked(now)
            slot = self._find_locked(session_id)
            if not slot or slot.state != "leased":
                return None
            slot.expires_at = now + timedelta(seconds=ttl)
            return self._lease_info(slot)

    def release(self, session_id: str) -> LeaseInfo | None:
        """Soft release: leased → parked. Keeps cloud provider session (billing continues)."""
        now = _utcnow()
        with self._lock:
            self._park_expired_leases_locked(now)
            slot = self._find_locked(session_id)
            if not slot or slot.state != "leased":
                return None
            self._park_locked(slot, now)
            # Synthetic expires_at for response shape (parked has no lease TTL).
            return LeaseInfo(
                session_id=session_id,
                worker_base_url=slot.base_url,
                vnc_url=self._effective_vnc(slot),
                expires_at=now,
                kind=slot.kind,
                seat_id=slot.seat_id,
                agent_id=slot.agent_id,
                job_id=slot.job_id,
            )

    def recycle(self, session_id: str) -> str | None:
        """leased|parked → free; teardown cloud / burst; return worker URL to reset.

        Ephemeral (burst) seats are removed and factory-destroyed, not returned to free.
        """
        teardown: dict[str, str] | None = None
        destroy_handle: str | None = None
        with self._lock:
            self._park_expired_leases_locked(_utcnow())
            slot = self._find_locked(session_id)
            if not slot or slot.state == "free":
                return None
            url = slot.base_url
            teardown = self._cloud_teardown_payload_locked(slot)
            if slot.ephemeral:
                destroy_handle = slot.factory_handle
                self._slots.remove(slot)
            else:
                self._clear_to_free_locked(slot)
        if teardown:
            self._teardown_cloud(teardown)
        self._destroy_factory_handle(destroy_handle)
        return url

    def worker_for(self, session_id: str) -> WorkerSlot | None:
        """Return the slot only while exclusively leased (observe/act)."""
        with self._lock:
            self._park_expired_leases_locked(_utcnow())
            slot = self._find_locked(session_id)
            if not slot or slot.state != "leased":
                return None
            return slot

    def _find_locked(self, session_id: str) -> WorkerSlot | None:
        for slot in self._slots:
            if slot.session_id == session_id:
                return slot
        return None

    def _find_seat_locked(self, seat_id: str) -> WorkerSlot | None:
        for slot in self._slots:
            if slot.seat_id == seat_id:
                return slot
        return None

    def get_seat(self, seat_id: str) -> WorkerSlot | None:
        with self._lock:
            self._park_expired_leases_locked(_utcnow())
            return self._find_seat_locked(seat_id)

    def set_seat_bound_serial(self, seat_id: str, serial: str | None) -> str | None:
        """Remember phone bind for lease/seat vnc_url (?udid=)."""
        with self._lock:
            slot = self._find_seat_locked(seat_id)
            if not slot or slot.kind != "phone":
                return None
            slot.bound_serial = str(serial).strip() if serial else None
            slot.vnc_url = self.effective_vnc(slot)
            return slot.vnc_url

    def seat_proxy_get(self, seat_id: str, path: str) -> tuple[int, Any]:
        slot = self.get_seat(seat_id)
        if not slot:
            return 404, {"detail": "seat not found"}
        with httpx.Client(timeout=self._timeout) as client:
            r = client.get(f"{slot.base_url}{path}")
            try:
                data = r.json()
            except Exception:
                data = {"detail": r.text}
            return r.status_code, data

    def seat_proxy_post(
        self, seat_id: str, path: str, body: dict | None = None
    ) -> tuple[int, Any]:
        slot = self.get_seat(seat_id)
        if not slot:
            return 404, {"detail": "seat not found"}
        with httpx.Client(timeout=self._timeout) as client:
            r = client.post(f"{slot.base_url}{path}", json=body or {})
            try:
                data = r.json()
            except Exception:
                data = {"detail": r.text}
            return r.status_code, data

    def register_physical_device(
        self,
        *,
        serial: str | None = None,
        connect: str | None = None,
        unlock: str | None = None,
        seat_id: str | None = None,
    ) -> tuple[int, dict[str, Any]]:
        """Ops: bind a newly registered physical phone onto a reserved seat.

        Prefers a free unbound physical seat. If none and PHONE_WARM_SPAWN_MAX>0,
        spawns a warm reserved worker (not lease-burst ephemeral) then binds.
        Returns seat_id for pipeline pin. Warm seats survive recycle until unregister.
        """
        return physical_phone_seats.register_physical_device(
            self,
            serial=serial,
            connect=connect,
            unlock=unlock,
            seat_id=seat_id,
        )

    def unregister_physical_device(
        self,
        seat_id: str,
        *,
        force: bool = False,
    ) -> tuple[int, dict[str, Any]]:
        """Remove a register-spawned warm physical seat (unbind + destroy worker).

        Inventory Compose seats: use /v1/seats/{id}/unbind instead (not removed).
        """
        return physical_phone_seats.unregister_physical_device(self, seat_id, force=force)

    def bind_phone_seat(
        self,
        seat_id: str,
        *,
        serial: str | None = None,
        connect: str | None = None,
        unlock: str | None = None,
    ) -> tuple[int, dict[str, Any]]:
        """Bind a phone seat's worker (+ optional scrcpy-web) to a device.

        Refuses while the seat is leased (agent has exclusive claim).
        Optional unlock is for cloud vendors (Phone Harness); omitted for physical.
        """
        return physical_phone_seats.bind_phone_seat(
            self, seat_id, serial=serial, connect=connect, unlock=unlock
        )

    def unbind_phone_seat(self, seat_id: str) -> tuple[int, dict[str, Any]]:
        return physical_phone_seats.unbind_phone_seat(self, seat_id)

    def proxy_get(self, session_id: str, path: str) -> tuple[int, Any]:
        slot = self.worker_for(session_id)
        if not slot:
            return 404, {"detail": "session not found or expired"}
        with httpx.Client(timeout=self._timeout) as client:
            r = client.get(f"{slot.base_url}{path}")
            try:
                data = r.json()
            except Exception:
                data = {"detail": r.text}
            return r.status_code, data

    def proxy_post(self, session_id: str, path: str, body: dict | None = None) -> tuple[int, Any]:
        slot = self.worker_for(session_id)
        if not slot:
            return 404, {"detail": "session not found or expired"}
        with httpx.Client(timeout=self._timeout) as client:
            r = client.post(f"{slot.base_url}{path}", json=body or {})
            try:
                data = r.json()
            except Exception:
                data = {"detail": r.text}
            return r.status_code, data

    def reset_worker(self, worker_base_url: str, *, hard: bool = True) -> None:
        """Ask worker to reset. hard=True → wipe (desktop Firefox / phone pm clear)."""
        with httpx.Client(timeout=self._timeout) as client:
            try:
                client.post(
                    f"{worker_base_url}/session/reset",
                    json={"hard": hard},
                )
            except httpx.HTTPError:
                pass

    def worker_healthy(self, worker_base_url: str) -> bool:
        with httpx.Client(timeout=5.0) as client:
            try:
                r = client.get(f"{worker_base_url}/health")
                if r.status_code != 200:
                    return False
                data = r.json()
                return bool(data.get("ok", True))
            except httpx.HTTPError:
                return False
