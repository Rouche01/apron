"""Physical phone register / bind / unbind / warm-spawn helpers used by SessionPool.

Pool keeps the lock and thin wrappers (tests monkeypatch bind/unbind). This module
owns glass ADB bind, scrcpy Watch sync, and warm physical spawn.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import httpx

from workers.base import WorkerFactoryError

logger = logging.getLogger("computer.gateway.physical_phone_seats")

# Internal scrcpy-web ADB bind API (Compose phone profile). Empty → skip Watch sync.
SCRCPY_ADB_API_URL = os.environ.get("SCRCPY_ADB_API_URL", "").strip().rstrip("/")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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


def phone_hitl_vnc_base() -> str:
    return (
        os.environ.get("PHONE_HITL_URL", "").strip()
        or "http://localhost:6083/watch.html"
    )


def worker_device_bind(
    timeout: float,
    worker_url: str,
    *,
    connect: str,
    unlock: str | None,
) -> tuple[int, Any]:
    body: dict[str, Any] = {"connect": connect, "serial": connect}
    if unlock:
        body["unlock"] = unlock
    with httpx.Client(timeout=max(timeout, 60.0)) as client:
        r = client.post(f"{worker_url}/device/bind", json=body)
        try:
            data = r.json()
        except Exception:
            data = {"detail": r.text}
        return r.status_code, data


class PhysicalSlot(Protocol):
    kind: str
    provider: str | None
    state: str
    seat_id: str
    base_url: str
    bound_serial: str | None
    vnc_url: str
    ephemeral: bool
    warm_spawned: bool
    factory_handle: str | None
    session_id: str | None


class PhysicalSeatHost(Protocol):
    """SessionPool surface for physical register / bind / warm spawn."""

    _lock: Any
    _timeout: float
    _phone_warm_spawn_max: int
    _slots: list
    _warm_inflight: set[str]
    _factory: Any

    def _park_expired_leases_locked(self, now: datetime) -> None: ...

    def _find_seat_locked(self, seat_id: str) -> PhysicalSlot | None: ...

    def _destroy_factory_handle(self, handle: str | None) -> None: ...

    def _effective_vnc(self, slot: PhysicalSlot) -> str: ...

    def bind_phone_seat(
        self,
        seat_id: str,
        *,
        serial: str | None = None,
        connect: str | None = None,
        unlock: str | None = None,
    ) -> tuple[int, dict[str, Any]]: ...

    def unbind_phone_seat(self, seat_id: str) -> tuple[int, dict[str, Any]]: ...

    def unregister_physical_device(
        self, seat_id: str, *, force: bool = False
    ) -> tuple[int, dict[str, Any]]: ...


def pick_free_physical_seat(
    slots: list, *, prefer_unbound: bool = True
) -> PhysicalSlot | None:
    """Choose a free physical phone seat for device registration."""
    candidates = [
        s
        for s in slots
        if s.kind == "phone" and not s.provider and s.state == "free" and not s.ephemeral
    ]
    if prefer_unbound:
        unbound = [s for s in candidates if not s.bound_serial]
        if unbound:
            return unbound[0]
        return None
    return candidates[0] if candidates else None


def warm_spawn_in_use(host: PhysicalSeatHost) -> int:
    live = sum(1 for s in host._slots if s.warm_spawned)
    return live + len(host._warm_inflight)


def spawn_warm_physical_seat(host: PhysicalSeatHost) -> tuple[int, dict[str, Any] | str]:
    """Spawn a reserved physical phone worker (not lease-burst ephemeral).

    Returns (200, seat_id) or (status, error dict).
    """
    # Late import avoids circular import with pool at module load.
    from pool import WorkerSlot

    max_n = host._phone_warm_spawn_max
    if max_n <= 0:
        return 503, {
            "ok": False,
            "error": (
                "no free unbound physical phone seat "
                "(set PHONE_WARM_SPAWN_MAX>0 and SEAT_BURST_BACKEND to auto-spawn)"
            ),
            "code": "no_free_phone_seat",
            "warm_spawn_max": max_n,
        }

    seat_id = f"phone-warm-{uuid.uuid4().hex[:8]}"
    with host._lock:
        if warm_spawn_in_use(host) >= max_n:
            return 503, {
                "ok": False,
                "error": "warm physical spawn cap reached",
                "code": "warm_spawn_cap",
                "warm_spawn_max": max_n,
                "warm_spawn_in_use": warm_spawn_in_use(host),
            }
        host._warm_inflight.add(seat_id)

    try:
        spawned = host._factory.spawn("phone", seat_id=seat_id)
    except WorkerFactoryError as e:
        with host._lock:
            host._warm_inflight.discard(seat_id)
        return 503, {
            "ok": False,
            "error": str(e),
            "code": e.code or "spawn_failed",
        }
    except Exception as e:
        with host._lock:
            host._warm_inflight.discard(seat_id)
        return 503, {
            "ok": False,
            "error": str(e),
            "code": "spawn_failed",
        }

    hitl = phone_hitl_vnc_base()
    with host._lock:
        host._warm_inflight.discard(seat_id)
        slot = WorkerSlot(
            base_url=spawned.control_url.rstrip("/"),
            vnc_url=hitl,
            inventory_vnc_url=hitl,
            kind="phone",
            seat_id=spawned.seat_id or seat_id,
            state="free",
            ephemeral=False,
            warm_spawned=True,
            factory_handle=spawned.handle,
        )
        host._slots.append(slot)
        logger.info(
            "warm physical seat spawned seat=%s handle=%s factory=%s",
            slot.seat_id,
            slot.factory_handle,
            host._factory.name,
        )
        return 200, slot.seat_id


def register_physical_device(
    host: PhysicalSeatHost,
    *,
    serial: str | None = None,
    connect: str | None = None,
    unlock: str | None = None,
    seat_id: str | None = None,
) -> tuple[int, dict[str, Any]]:
    """Ops: bind a newly registered physical phone onto a reserved seat."""
    serial_s = (serial or "").strip() or None
    connect_s = (connect or "").strip() or None
    unlock_s = (unlock or "").strip() or None
    pin = (seat_id or "").strip() or None
    if not serial_s and not connect_s:
        return 400, {
            "ok": False,
            "error": "serial and/or connect required",
            "code": "missing_target",
        }

    target_id: str | None = pin
    spawned_warm = False
    with host._lock:
        host._park_expired_leases_locked(_utcnow())
        if pin:
            slot = host._find_seat_locked(pin)
            if not slot:
                return 404, {
                    "ok": False,
                    "error": f"seat {pin!r} not found",
                    "code": "seat_not_found",
                    "seat_id": pin,
                }
            if slot.kind != "phone" or slot.provider:
                return 400, {
                    "ok": False,
                    "error": "register only applies to physical phone seats",
                    "code": "not_physical_phone",
                    "seat_id": pin,
                }
            target_id = slot.seat_id
        else:
            slot = pick_free_physical_seat(host._slots, prefer_unbound=True)
            if slot:
                target_id = slot.seat_id

    if target_id is None:
        status, spawned = spawn_warm_physical_seat(host)
        if status >= 400:
            assert isinstance(spawned, dict)
            return status, spawned
        assert isinstance(spawned, str)
        target_id = spawned
        spawned_warm = True

    assert target_id is not None
    # Call host methods so tests can monkeypatch bind / unregister.
    status, data = host.bind_phone_seat(
        target_id,
        serial=serial_s,
        connect=connect_s,
        unlock=unlock_s,
    )
    if status >= 400:
        if spawned_warm:
            host.unregister_physical_device(target_id, force=True)
        out = dict(data) if isinstance(data, dict) else {"ok": False, "detail": data}
        out.setdefault("ok", False)
        out.setdefault("code", "bind_failed")
        out["seat_id"] = target_id
        return status, out

    return 200, {
        "ok": True,
        "seat_id": target_id,
        "bound_serial": data.get("bound_serial") if isinstance(data, dict) else None,
        "vnc_url": data.get("vnc_url") if isinstance(data, dict) else None,
        "warm_spawned": spawned_warm,
        "worker": data.get("worker") if isinstance(data, dict) else None,
        "scrcpy": data.get("scrcpy") if isinstance(data, dict) else None,
        "hint": data.get("hint") if isinstance(data, dict) else None,
    }


def unregister_physical_device(
    host: PhysicalSeatHost,
    seat_id: str,
    *,
    force: bool = False,
) -> tuple[int, dict[str, Any]]:
    """Remove a register-spawned warm physical seat (unbind + destroy worker)."""
    pin = (seat_id or "").strip()
    if not pin:
        return 400, {
            "ok": False,
            "error": "seat_id required",
            "code": "missing_seat_id",
        }

    unbind_status, unbind_data = host.unbind_phone_seat(pin)
    if unbind_status == 409 and not force:
        return 409, {
            "ok": False,
            "error": "seat is leased — release or recycle before unregister",
            "code": "seat_busy",
            "seat_id": pin,
            "unbind": unbind_data,
        }

    destroy_handle: str | None = None
    with host._lock:
        slot = host._find_seat_locked(pin)
        if not slot:
            return 404, {
                "ok": False,
                "error": f"seat {pin!r} not found",
                "code": "seat_not_found",
                "seat_id": pin,
            }
        if not slot.warm_spawned:
            return 400, {
                "ok": False,
                "error": (
                    "seat is inventory-reserved — use POST /v1/seats/{id}/unbind; "
                    "unregister only removes warm-spawned seats"
                ),
                "code": "not_warm_spawned",
                "seat_id": pin,
            }
        if slot.state == "leased" and not force:
            return 409, {
                "ok": False,
                "error": "seat is leased — release or recycle before unregister",
                "code": "seat_busy",
                "seat_id": pin,
                "session_id": slot.session_id,
            }
        destroy_handle = slot.factory_handle
        host._slots.remove(slot)

    host._destroy_factory_handle(destroy_handle)
    logger.info("warm physical seat unregistered seat=%s handle=%s", pin, destroy_handle)
    return 200, {
        "ok": True,
        "seat_id": pin,
        "destroyed": True,
        "unbind_status": unbind_status,
    }


def bind_phone_seat(
    host: PhysicalSeatHost,
    seat_id: str,
    *,
    serial: str | None = None,
    connect: str | None = None,
    unlock: str | None = None,
) -> tuple[int, dict[str, Any]]:
    """Bind a phone seat's worker (+ optional scrcpy-web) to a device."""
    with host._lock:
        host._park_expired_leases_locked(_utcnow())
        slot = host._find_seat_locked(seat_id)
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

    with httpx.Client(timeout=host._timeout) as client:
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
        scrcpy_body = {k: v for k, v in body.items() if k != "unlock"}
        try:
            with httpx.Client(timeout=host._timeout) as client:
                sr = client.post(f"{SCRCPY_ADB_API_URL}/device/bind", json=scrcpy_body)
                try:
                    scrcpy = sr.json()
                except Exception:
                    scrcpy = {"ok": False, "detail": sr.text}
                scrcpy["http_status"] = sr.status_code
        except httpx.HTTPError as e:
            scrcpy = {"ok": False, "detail": f"scrcpy adb-api unreachable: {e}"}

    with host._lock:
        slot = host._find_seat_locked(seat_id)
        if slot:
            slot.bound_serial = str(bound).strip() if bound else None
            slot.vnc_url = host._effective_vnc(slot)
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


def unbind_phone_seat(
    host: PhysicalSeatHost, seat_id: str
) -> tuple[int, dict[str, Any]]:
    with host._lock:
        host._park_expired_leases_locked(_utcnow())
        slot = host._find_seat_locked(seat_id)
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

    with httpx.Client(timeout=host._timeout) as client:
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
    with host._lock:
        slot = host._find_seat_locked(seat_id)
        if slot:
            env_serial = None
            if isinstance(data, dict):
                env_serial = data.get("bound_serial") or data.get("adb_serial_env")
            slot.bound_serial = str(env_serial).strip() if env_serial else None
            slot.vnc_url = host._effective_vnc(slot)
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
