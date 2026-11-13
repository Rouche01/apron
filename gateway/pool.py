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
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import httpx

from inventory import SeatKind, WorkerSpec
from providers import get as get_cloud_provider
from providers.base import CloudPhoneProviderError, CloudSession

logger = logging.getLogger("computer.gateway.pool")

SlotState = Literal["free", "leased", "parked"]

# Internal scrcpy-web ADB bind API (Compose phone profile). Empty → skip Watch sync.
SCRCPY_ADB_API_URL = os.environ.get("SCRCPY_ADB_API_URL", "").strip().rstrip("/")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _clamp_ttl(ttl: int, *, minimum: int = 30) -> int:
    return ttl if ttl >= minimum else minimum


def vnc_url_with_udid(base_vnc: str, serial: str | None) -> str:
    """Attach ?udid= to phone watch URLs so HITL opens the bound device."""
    if not serial or not base_vnc:
        return base_vnc
    parsed = urlparse(base_vnc)
    path = parsed.path or ""
    # Leave desktop noVNC alone.
    if "vnc.html" in path:
        return base_vnc
    qs = parse_qs(parsed.query, keep_blank_values=True)
    qs["udid"] = [serial]
    # Drop stale serial= if present; udid is canonical for watch.html.
    qs.pop("serial", None)
    new_query = urlencode({k: v[-1] if isinstance(v, list) else v for k, v in qs.items()})
    return urlunparse(
        (parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, parsed.fragment)
    )


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
    ) -> None:
        if not workers:
            raise ValueError("at least one worker is required")
        self._default_ttl = default_ttl_sec
        self._parked_ttl = max(30, parked_ttl_sec)
        self._timeout = http_timeout
        self._lock = threading.Lock()
        self._slots: list[WorkerSlot] = []
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

    def _cloud_teardown_payload_locked(self, slot: WorkerSlot) -> dict[str, str] | None:
        if slot.provider and slot.provider_session_id:
            return {
                "provider": slot.provider,
                "session_id": slot.provider_session_id,
            }
        return None

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
    ) -> tuple[list[str], list[dict[str, str]]]:
        """Free parked slots past idle TTL; return reset URLs + cloud teardowns."""
        urls: list[str] = []
        teardowns: list[dict[str, str]] = []
        for slot in self._slots:
            if slot.state != "parked" or slot.parked_at is None:
                continue
            age = (now - slot.parked_at).total_seconds()
            if age >= self._parked_ttl:
                urls.append(slot.base_url)
                td = self._cloud_teardown_payload_locked(slot)
                if td:
                    teardowns.append(td)
                self._clear_to_free_locked(slot)
        return urls, teardowns

    def _teardown_cloud(self, payload: dict[str, str]) -> None:
        provider_name = payload.get("provider") or ""
        session_id = payload.get("session_id") or ""
        if not provider_name or not session_id:
            return
        try:
            get_cloud_provider(provider_name).teardown(session_id)
        except Exception as e:
            logger.warning(
                "cloud teardown failed provider=%s session=%s: %s",
                provider_name,
                session_id,
                e,
            )

    def sweep_expired(self) -> list[str]:
        """Park lease-TTL expiry; free parked-idle slots and return URLs to reset."""
        with self._lock:
            now = _utcnow()
            self._park_expired_leases_locked(now)
            urls, teardowns = self._sweep_parked_idle_locked(now)
        for td in teardowns:
            self._teardown_cloud(td)
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
                }
                if slot.kind == "phone":
                    seat["bound_serial"] = slot.bound_serial
                    if slot.provider:
                        seat["provider"] = slot.provider
                        seat["provider_session_id"] = slot.provider_session_id
                seats.append(seat)
            return seats

    def _cloud_provision_timeout(self, lease_ttl_sec: int) -> int:
        return max(int(lease_ttl_sec) + self._parked_ttl, 900)

    def _worker_device_bind(
        self,
        worker_url: str,
        *,
        connect: str,
        unlock: str | None,
    ) -> tuple[int, Any]:
        body: dict[str, Any] = {"connect": connect, "serial": connect}
        if unlock:
            body["unlock"] = unlock
        with httpx.Client(timeout=max(self._timeout, 60.0)) as client:
            r = client.post(f"{worker_url}/device/bind", json=body)
            try:
                data = r.json()
            except Exception:
                data = {"detail": r.text}
            return r.status_code, data

    def _apply_cloud_session_locked(self, slot: WorkerSlot, session: CloudSession) -> None:
        slot.provider_session_id = session.provider_session_id
        slot.cloud_watch_url = session.watch_url
        slot.cloud_unlock = session.unlock
        slot.bound_serial = session.adb_connect
        slot.vnc_url = session.watch_url

    def _provision_and_bind_cloud(self, seat_id: str, *, lease_ttl_sec: int) -> None:
        """Provision via CloudPhoneProvider and bind the phone worker (outside lock)."""
        with self._lock:
            slot = self._find_seat_locked(seat_id)
            if not slot or not slot.provider:
                raise CloudPhoneProviderError(
                    f"seat {seat_id} is not a cloud phone seat",
                    code="not_cloud_seat",
                )
            provider_name = slot.provider
            worker_url = slot.base_url

        provider = get_cloud_provider(provider_name)
        session = provider.provision(
            timeout_sec=self._cloud_provision_timeout(lease_ttl_sec)
        )
        status, data = self._worker_device_bind(
            worker_url,
            connect=session.adb_connect,
            unlock=session.unlock,
        )
        ok = status < 400 and isinstance(data, dict) and bool(data.get("ok", True))
        if not ok:
            try:
                provider.teardown(session.provider_session_id)
            except Exception as e:
                logger.warning(
                    "teardown after bind failure session=%s: %s",
                    session.provider_session_id,
                    e,
                )
            raise CloudPhoneProviderError(
                f"phone worker bind failed after provision: {data}",
                code="worker_bind_failed",
            )

        with self._lock:
            slot = self._find_seat_locked(seat_id)
            if not slot or slot.state != "leased":
                try:
                    provider.teardown(session.provider_session_id)
                except Exception:
                    pass
                raise CloudPhoneProviderError(
                    f"seat {seat_id} lost lease during cloud provision",
                    code="lease_lost",
                )
            self._apply_cloud_session_locked(slot, session)

    def _reconnect_cloud_adb(self, seat_id: str) -> None:
        """Re-adb connect (+ unlock) for an existing cloud session (park → acquire)."""
        with self._lock:
            slot = self._find_seat_locked(seat_id)
            if not slot or not slot.provider or not slot.provider_session_id:
                return
            if not slot.bound_serial:
                return
            provider_name = slot.provider
            worker_url = slot.base_url
            connect = slot.bound_serial
            unlock = slot.cloud_unlock
            watch = slot.cloud_watch_url
            sid = slot.provider_session_id

        session = CloudSession(
            provider_session_id=sid,
            adb_host=connect.rsplit(":", 1)[0] if ":" in connect else connect,
            adb_port=int(connect.rsplit(":", 1)[1]) if ":" in connect else 5555,
            unlock=unlock,
            watch_url=watch or "",
        )
        hint = get_cloud_provider(provider_name).reconnect_hint(session)
        status, data = self._worker_device_bind(
            worker_url,
            connect=hint.adb_connect,
            unlock=hint.unlock,
        )
        if status >= 400 or (isinstance(data, dict) and data.get("ok") is False):
            logger.warning(
                "cloud ADB reconnect failed seat=%s: %s %s",
                seat_id,
                status,
                data,
            )

    def _abort_claimed_lease(self, seat_id: str) -> None:
        with self._lock:
            slot = self._find_seat_locked(seat_id)
            if slot and slot.state == "leased" and not slot.provider_session_id:
                self._clear_to_free_locked(slot)

    def lease(
        self,
        *,
        agent_id: str | None = None,
        job_id: str | None = None,
        ttl_sec: int | None = None,
        kind: SeatKind = "desktop",
    ) -> LeaseInfo | None:
        """Reattach a parked seat for job_id if present; else claim a free worker of kind.

        Cloud seats: provision + worker bind after claim. Raises CloudPhoneProviderError
        on provider/bind failure (seat returned to free).
        """
        ttl = _clamp_ttl(ttl_sec if ttl_sec is not None else self._default_ttl)
        now = _utcnow()
        cloud_reattach_seat: str | None = None
        cloud_provision_seat: str | None = None
        info: LeaseInfo | None = None

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

            if info is None:
                for slot in self._slots:
                    if slot.state != "free" or slot.kind != kind:
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
            return None

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
        """leased|parked → free; teardown cloud session; return worker URL to reset."""
        teardown: dict[str, str] | None = None
        with self._lock:
            self._park_expired_leases_locked(_utcnow())
            slot = self._find_locked(session_id)
            if not slot or slot.state == "free":
                return None
            url = slot.base_url
            teardown = self._cloud_teardown_payload_locked(slot)
            self._clear_to_free_locked(slot)
        if teardown:
            self._teardown_cloud(teardown)
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
        with self._lock:
            self._park_expired_leases_locked(_utcnow())
            slot = self._find_seat_locked(seat_id)
            if not slot:
                return 404, {"ok": False, "detail": "seat not found"}
            if slot.kind != "phone":
                return 400, {"ok": False, "detail": "only phone seats support device bind"}
            if slot.provider:
                return 400, {
                    "ok": False,
                    "detail": (
                        "cloud seats are bound by the provider on lease — "
                        f"manual bind refused (provider={slot.provider})"
                    ),
                    "provider": slot.provider,
                }
            if slot.state == "leased":
                return 409, {
                    "ok": False,
                    "detail": "seat is leased — release or recycle before rebinding",
                    "session_id": slot.session_id,
                }
            worker_url = slot.base_url

        body: dict[str, Any] = {}
        if serial:
            body["serial"] = serial
        if connect:
            body["connect"] = connect
        if unlock:
            body["unlock"] = unlock

        with httpx.Client(timeout=self._timeout) as client:
            try:
                r = client.post(f"{worker_url}/device/bind", json=body)
                try:
                    data = r.json()
                except Exception:
                    data = {"detail": r.text}
            except httpx.HTTPError as e:
                return 503, {"ok": False, "detail": f"phone worker unreachable: {e}"}

        if r.status_code >= 400:
            detail = data if isinstance(data, dict) else {"detail": data}
            if isinstance(detail, dict) and "ok" not in detail:
                detail = {"ok": False, **detail}
            return r.status_code, detail  # type: ignore[return-value]

        bound = None
        if isinstance(data, dict):
            bound = data.get("bound_serial") or serial or connect

        scrcpy: dict[str, Any] | None = None
        if SCRCPY_ADB_API_URL and (serial or connect):
            # Scrcpy adb-api: connect only (no unlock). Phone worker holds the unlock.
            scrcpy_body = {k: v for k, v in body.items() if k != "unlock"}
            try:
                with httpx.Client(timeout=self._timeout) as client:
                    sr = client.post(f"{SCRCPY_ADB_API_URL}/device/bind", json=scrcpy_body)
                    try:
                        scrcpy = sr.json()
                    except Exception:
                        scrcpy = {"ok": False, "detail": sr.text}
                    scrcpy["http_status"] = sr.status_code
            except httpx.HTTPError as e:
                scrcpy = {"ok": False, "detail": f"scrcpy adb-api unreachable: {e}"}

        with self._lock:
            slot = self._find_seat_locked(seat_id)
            if slot:
                slot.bound_serial = str(bound).strip() if bound else None
                slot.vnc_url = self._effective_vnc(slot)
                vnc = slot.vnc_url
            else:
                vnc = None

        out: dict[str, Any] = {
            "ok": bool(isinstance(data, dict) and data.get("ok", True)),
            "seat_id": seat_id,
            "bound_serial": bound,
            "vnc_url": vnc,
            "worker": data,
        }
        if scrcpy is not None:
            out["scrcpy"] = scrcpy
        if isinstance(data, dict) and data.get("hint"):
            out["hint"] = data["hint"]
        return 200 if out["ok"] else 503, out

    def unbind_phone_seat(self, seat_id: str) -> tuple[int, dict[str, Any]]:
        with self._lock:
            self._park_expired_leases_locked(_utcnow())
            slot = self._find_seat_locked(seat_id)
            if not slot:
                return 404, {"ok": False, "detail": "seat not found"}
            if slot.kind != "phone":
                return 400, {"ok": False, "detail": "only phone seats support device bind"}
            if slot.provider:
                return 400, {
                    "ok": False,
                    "detail": (
                        "cloud seats are unbound by recycle/teardown — "
                        f"manual unbind refused (provider={slot.provider})"
                    ),
                    "provider": slot.provider,
                }
            if slot.state == "leased":
                return 409, {
                    "ok": False,
                    "detail": "seat is leased — release or recycle before unbinding",
                    "session_id": slot.session_id,
                }
            worker_url = slot.base_url

        with httpx.Client(timeout=self._timeout) as client:
            try:
                r = client.post(f"{worker_url}/device/unbind", json={})
                try:
                    data = r.json()
                except Exception:
                    data = {"detail": r.text}
            except httpx.HTTPError as e:
                return 503, {"ok": False, "detail": f"phone worker unreachable: {e}"}

        if r.status_code >= 400:
            return r.status_code, data if isinstance(data, dict) else {"ok": False, "detail": data}

        bound_serial: str | None = None
        with self._lock:
            slot = self._find_seat_locked(seat_id)
            if slot:
                # Fall back to worker-reported bound (env ADB_SERIAL) if any.
                env_serial = None
                if isinstance(data, dict):
                    env_serial = data.get("bound_serial") or data.get("adb_serial_env")
                slot.bound_serial = str(env_serial).strip() if env_serial else None
                slot.vnc_url = self._effective_vnc(slot)
                bound_serial = slot.bound_serial
                vnc = slot.vnc_url
            else:
                vnc = None

        return 200, {
            "ok": True,
            "seat_id": seat_id,
            "bound_serial": bound_serial,
            "vnc_url": vnc,
            "worker": data,
        }

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
