"""Computer Runtime Gateway — model-agnostic session lease + observe/act proxy."""

from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

import providers  # noqa: F401 — registers CloudPhoneProvider impls (phone-harness, …)
from inventory import SeatKind, load_workers
from pool import SeatPinError, SessionPool
from providers import registered_names
from providers.base import CloudPhoneProviderError
from workers import build_worker_factory
from workers.base import WorkerFactoryError

logger = logging.getLogger("computer.gateway")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

DEFAULT_TTL = int(os.environ.get("SESSION_TTL_SEC", "600"))
PARKED_TTL = int(os.environ.get("PARKED_TTL_SEC", "1800"))
PUBLIC_BASE = os.environ.get("GATEWAY_PUBLIC_BASE", "http://localhost:9000").rstrip("/")
API_TOKEN = os.environ.get("COMPUTER_API_TOKEN", "").strip()

_WORKERS = load_workers()
_registered = registered_names()
for _w in _WORKERS:
    if _w.provider and _w.provider not in _registered:
        raise RuntimeError(
            f"inventory seat {_w.id!r} provider={_w.provider!r} is not registered; "
            f"known: {sorted(_registered) or '(none)'}"
        )
_FACTORY = build_worker_factory()
pool = SessionPool(
    _WORKERS,
    default_ttl_sec=DEFAULT_TTL,
    parked_ttl_sec=PARKED_TTL,
    worker_factory=_FACTORY,
)

_security = HTTPBearer(auto_error=False)


def _reaper_loop(stop: threading.Event) -> None:
    while not stop.wait(15):
        # Lease TTL → park (no reset). Parked idle TTL → free + Firefox reset.
        for worker_url in pool.sweep_expired():
            logger.info("parked idle TTL; recycling worker %s", worker_url)
            pool.reset_worker(worker_url)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    stop = threading.Event()
    t = threading.Thread(target=_reaper_loop, args=(stop,), daemon=True, name="lease-reaper")
    t.start()
    if not API_TOKEN:
        logger.warning("COMPUTER_API_TOKEN unset — gateway auth is disabled (dev only)")
    inv = os.environ.get("WORKER_INVENTORY_PATH", "").strip() or "(legacy WORKER_URLS)"
    logger.info(
        "gateway ready: %d workers, public_base=%s, inventory=%s, "
        "burst_backend=%s desktop_burst_max=%s phone_burst_max=%s",
        pool.worker_count,
        PUBLIC_BASE,
        inv,
        _FACTORY.name,
        pool.burst_max_for("desktop"),
        pool.burst_max_for("phone"),
    )
    for w in _WORKERS:
        logger.info(
            "seat id=%s kind=%s provider=%s url=%s vnc_url=%s",
            w.id,
            w.kind,
            w.provider or "-",
            w.url,
            w.vnc_url,
        )
    yield
    stop.set()


app = FastAPI(
    title="Computer Runtime Gateway",
    version="0.3.0",
    description=(
        "Model-agnostic computer sessions (desktop or phone): lease a worker by kind, "
        "screenshot/act, soft-release (park warm), acquire or job_id-reattach, recycle to wipe. "
        "Inference stays outside — any agent can bind via this OpenAPI."
    ),
    lifespan=lifespan,
)


def require_auth(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_security)],
) -> None:
    if not API_TOKEN:
        return
    if creds is None or creds.scheme.lower() != "bearer" or creds.credentials != API_TOKEN:
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")


AuthDep = Annotated[None, Depends(require_auth)]


class LeaseRequest(BaseModel):
    agent_id: str | None = None
    job_id: str | None = None
    ttl_sec: int | None = Field(default=None, ge=30, le=86400)
    kind: SeatKind = Field(
        default="desktop",
        description="Seat kind to lease: desktop (default) or phone",
    )
    seat_id: str | None = Field(
        default=None,
        description=(
            "Exact seat pin. Required to use a physical phone seat; "
            "without it, kind=phone claims cloud seats only."
        ),
    )


class LeaseResponse(BaseModel):
    session_id: str
    api_base: str
    vnc_url: str
    expires_at: datetime
    kind: SeatKind = "desktop"
    seat_id: str | None = None
    agent_id: str | None = None
    job_id: str | None = None


class HeartbeatRequest(BaseModel):
    ttl_sec: int | None = Field(default=None, ge=30, le=86400)


class AcquireRequest(BaseModel):
    ttl_sec: int | None = Field(default=None, ge=30, le=86400)
    agent_id: str | None = None


class ActionBody(BaseModel):
    type: str
    x: int | None = None
    y: int | None = None
    x1: int | None = None
    y1: int | None = None
    x2: int | None = None
    y2: int | None = None
    text: str | None = None
    direction: str = "down"
    amount: int = 3
    duration_ms: int | None = None
    package: str | None = None
    uri: str | None = None
    ref: str | None = None


def _api_base(session_id: str) -> str:
    return f"{PUBLIC_BASE}/v1/sessions/{session_id}"


def _lease_response(info) -> LeaseResponse:
    return LeaseResponse(
        session_id=info.session_id,
        api_base=_api_base(info.session_id),
        vnc_url=info.vnc_url,
        expires_at=info.expires_at,
        kind=info.kind,
        seat_id=info.seat_id or None,
        agent_id=info.agent_id,
        job_id=info.job_id,
    )


@app.middleware("http")
async def job_id_log(request: Request, call_next):
    job = request.headers.get("x-job-id")
    started = time.perf_counter()
    response: Response = await call_next(request)
    ms = (time.perf_counter() - started) * 1000
    if job:
        logger.info(
            "job_id=%s %s %s -> %s (%.1fms)",
            job,
            request.method,
            request.url.path,
            response.status_code,
            ms,
        )
    return response


@app.get("/health", tags=["ops"])
def health():
    return {
        "ok": True,
        "service": "computer-gateway",
        "workers": pool.worker_count,
        "leased": pool.leased_count(),
        "parked": pool.parked_count(),
        "free": pool.free_count(),
    }


@app.get("/v1/seats", tags=["ops"])
def list_seats(_: AuthDep):
    """Per-worker inventory: state, session_id, vnc_url, job_id (for Desk inspect/recycle)."""
    seats = pool.list_seats()
    return {
        "ok": True,
        "workers": pool.worker_count,
        "free": pool.free_count(),
        "leased": pool.leased_count(),
        "parked": pool.parked_count(),
        "seats": seats,
    }


class SeatBindBody(BaseModel):
    """Bind a phone seat to a USB serial or wireless adb target."""

    serial: str | None = Field(
        default=None,
        description="adb serial (USB id or ip:port after connect)",
    )
    connect: str | None = Field(
        default=None,
        description="Optional wireless/cloud host:port — runs adb connect, then binds",
    )
    unlock: str | None = Field(
        default=None,
        description="Optional vendor unlock after connect (e.g. Phone Harness shell unlock code)",
    )


@app.get("/v1/seats/{seat_id}/devices", tags=["ops"])
def seat_devices(seat_id: str, _: AuthDep):
    """List adb devices visible to a phone seat (Desk bind UI)."""
    slot = pool.get_seat(seat_id)
    if not slot:
        raise HTTPException(status_code=404, detail="seat not found")
    if slot.kind != "phone":
        raise HTTPException(status_code=400, detail="only phone seats expose devices")
    status, data = pool.seat_proxy_get(seat_id, "/devices")
    if status >= 400:
        raise HTTPException(status_code=status, detail=data)
    if isinstance(data, dict):
        # Hydrate gateway slot from worker (survives gateway recreate).
        bound = data.get("bound_serial")
        if bound:
            pool.set_seat_bound_serial(seat_id, str(bound))
        slot = pool.get_seat(seat_id) or slot
        data = {
            **data,
            "seat_id": seat_id,
            "vnc_url": pool.effective_vnc(slot),
            "state": slot.state,
        }
    return data


@app.get("/v1/seats/{seat_id}/device", tags=["ops"])
def seat_device(seat_id: str, _: AuthDep):
    """Current bind status for a phone seat."""
    slot = pool.get_seat(seat_id)
    if not slot:
        raise HTTPException(status_code=404, detail="seat not found")
    if slot.kind != "phone":
        raise HTTPException(status_code=400, detail="only phone seats expose device status")
    status, data = pool.seat_proxy_get(seat_id, "/device")
    if status >= 400:
        raise HTTPException(status_code=status, detail=data)
    if isinstance(data, dict):
        bound = data.get("bound_serial")
        if bound:
            pool.set_seat_bound_serial(seat_id, str(bound))
        slot = pool.get_seat(seat_id) or slot
        data = {
            **data,
            "seat_id": seat_id,
            "bound_serial": slot.bound_serial or data.get("bound_serial"),
            "vnc_url": pool.effective_vnc(slot),
            "state": slot.state,
        }
    return data


class DeviceRegisterBody(BaseModel):
    """Register a physical phone onto a free reserved seat (ops / Desk)."""

    serial: str | None = Field(
        default=None,
        description="adb serial (USB id or ip:port after connect)",
    )
    connect: str | None = Field(
        default=None,
        description="Wireless adb host:port — runs adb connect, then binds",
    )
    unlock: str | None = Field(
        default=None,
        description="Optional unlock after connect (usually omitted for physical)",
    )
    seat_id: str | None = Field(
        default=None,
        description="Optional exact physical seat; otherwise first free unbound phone seat",
    )


@app.post("/v1/devices/register", tags=["ops"])
def devices_register(body: DeviceRegisterBody, _: AuthDep):
    """Bind a physical device to a free phone seat; return seat_id for lease pin.

    Phase 1: does not spawn workers — needs an existing free unbound physical seat
    (or pass seat_id to rebind that seat). Human Allow once on first ADB trust.
    Pipeline then leases with {\"kind\":\"phone\",\"seat_id\":…}.
    """
    if not (body.serial or "").strip() and not (body.connect or "").strip():
        raise HTTPException(
            status_code=400,
            detail={
                "ok": False,
                "error": "serial and/or connect required",
                "code": "missing_target",
            },
        )
    status, data = pool.register_physical_device(
        serial=(body.serial or "").strip() or None,
        connect=(body.connect or "").strip() or None,
        unlock=(body.unlock or "").strip() or None,
        seat_id=(body.seat_id or "").strip() or None,
    )
    if status >= 400:
        raise HTTPException(status_code=status, detail=data)
    return data


@app.post("/v1/seats/{seat_id}/bind", tags=["ops"])
def seat_bind(seat_id: str, body: SeatBindBody, _: AuthDep):
    """Bind a phone seat to a device without recreating containers.

    Human must tap Allow / Authorize on the phone the first time this host's
    adb keys appear (Compose mounts ~/.android into phone-1 + scrcpy-web).
    Refuses while the seat is leased. Prefer POST /v1/devices/register when
    Desk should pick a free seat and return seat_id for the agent pin.
    """
    if not (body.serial or "").strip() and not (body.connect or "").strip():
        raise HTTPException(status_code=400, detail="serial and/or connect required")
    status, data = pool.bind_phone_seat(
        seat_id,
        serial=(body.serial or "").strip() or None,
        connect=(body.connect or "").strip() or None,
        unlock=(body.unlock or "").strip() or None,
    )
    if status >= 400:
        raise HTTPException(status_code=status, detail=data)
    return data


@app.post("/v1/seats/{seat_id}/unbind", tags=["ops"])
def seat_unbind(seat_id: str, _: AuthDep):
    """Clear runtime bind; fall back to ADB_SERIAL env on the phone worker."""
    status, data = pool.unbind_phone_seat(seat_id)
    if status >= 400:
        raise HTTPException(status_code=status, detail=data)
    return data


@app.get("/ready", tags=["ops"])
def ready():
    """200 when at least one worker is free and a healthy worker exists."""
    free = pool.free_count()
    healthy = any(pool.worker_healthy(u) for u in pool.worker_urls())
    accept_new = free > 0 and healthy
    body = {
        "ok": accept_new,
        "healthy_workers": healthy,
        "free": free,
        "free_desktop": pool.free_count_for("desktop"),
        "free_phone": pool.free_count_for("phone"),
        "leased": pool.leased_count(),
        "parked": pool.parked_count(),
    }
    if not accept_new:
        return JSONResponse(status_code=503, content=body)
    return body


@app.post("/v1/sessions", response_model=LeaseResponse, tags=["sessions"])
def create_session(
    _: AuthDep,
    body: LeaseRequest = LeaseRequest(),
    x_job_id: Annotated[str | None, Header()] = None,
):
    """Lease a free worker of the requested kind, or reattach a parked seat when job_id matches.

    Phone without seat_id uses cloud seats only; pass seat_id to pin a physical (or any) seat.
    """
    job_id = body.job_id or x_job_id
    kind: SeatKind = body.kind
    try:
        info = pool.lease(
            agent_id=body.agent_id,
            job_id=job_id,
            ttl_sec=body.ttl_sec,
            kind=kind,
            seat_id=body.seat_id,
        )
    except SeatPinError as e:
        status = {
            "seat_not_found": 404,
            "seat_kind_mismatch": 400,
            "seat_busy": 409,
        }.get(e.code, 400)
        raise HTTPException(
            status_code=status,
            detail={
                "ok": False,
                "error": str(e),
                "kind": kind,
                "code": e.code,
                "seat_id": body.seat_id,
            },
        ) from e
    except CloudPhoneProviderError as e:
        detail: dict = {
            "ok": False,
            "error": str(e),
            "kind": kind,
            "code": e.code,
        }
        if e.retry_after_seconds is not None:
            detail["retry_after_seconds"] = e.retry_after_seconds
        raise HTTPException(status_code=503, detail=detail) from e
    except WorkerFactoryError as e:
        raise HTTPException(
            status_code=503,
            detail={
                "ok": False,
                "error": str(e),
                "kind": kind,
                "code": e.code or "burst_failed",
                "burst_max": pool.burst_max_for(kind),
                "burst_in_use": pool.burst_count_for(kind),
            },
        ) from e
    if not info:
        raise HTTPException(
            status_code=503,
            detail={
                "ok": False,
                "error": f"no free {kind} workers",
                "kind": kind,
                "free": pool.free_count(),
                "free_for_kind": pool.free_count_for(kind),
                "claimable_free_for_kind": pool.claimable_free_count_for(kind),
                "parked": pool.parked_count(),
                "burst_max": pool.burst_max_for(kind),
                "burst_in_use": pool.burst_count_for(kind),
            },
        )
    logger.info(
        "leased session=%s kind=%s seat=%s agent=%s job=%s worker=%s",
        info.session_id,
        info.kind,
        info.seat_id,
        info.agent_id,
        info.job_id,
        info.worker_base_url,
    )
    return _lease_response(info)


@app.post("/v1/sessions/{session_id}/heartbeat", response_model=LeaseResponse, tags=["sessions"])
def heartbeat(session_id: str, _: AuthDep, body: HeartbeatRequest = HeartbeatRequest()):
    info = pool.heartbeat(session_id, ttl_sec=body.ttl_sec)
    if not info:
        raise HTTPException(status_code=404, detail="session not found or expired")
    return _lease_response(info)


@app.post("/v1/sessions/{session_id}/acquire", response_model=LeaseResponse, tags=["sessions"])
def acquire_session(
    session_id: str,
    _: AuthDep,
    body: AcquireRequest = AcquireRequest(),
):
    """Re-lease a parked desktop by session_id (serial same-job leases)."""
    result = pool.acquire(
        session_id,
        ttl_sec=body.ttl_sec,
        agent_id=body.agent_id,
    )
    if result.error == "not_found":
        raise HTTPException(status_code=404, detail="session not found or expired")
    if result.error == "already_leased":
        raise HTTPException(status_code=409, detail="session already leased")
    assert result.info is not None
    logger.info(
        "acquired session=%s worker=%s",
        session_id,
        result.info.worker_base_url,
    )
    return _lease_response(result.info)


@app.delete("/v1/sessions/{session_id}", tags=["sessions"])
def delete_session(
    session_id: str,
    _: AuthDep,
    recycle: Annotated[bool, Query(description="Wipe Firefox and free the worker")] = False,
):
    """Soft release parks the warm seat. Pass recycle=true to wipe and free."""
    if recycle:
        return _recycle_session(session_id)
    info = pool.release(session_id)
    if not info:
        raise HTTPException(status_code=404, detail="session not found or expired")
    # Phone: soft force-stop on park (keep app data). Desktop park stays warm (no reset).
    if info.kind == "phone":
        pool.reset_worker(info.worker_base_url, hard=False)
    logger.info(
        "parked session=%s kind=%s worker=%s",
        session_id,
        info.kind,
        info.worker_base_url,
    )
    return {"ok": True, "session_id": session_id, "parked": True}


def _recycle_session(session_id: str) -> dict:
    worker_url = pool.recycle(session_id)
    if not worker_url:
        raise HTTPException(status_code=404, detail="session not found or expired")
    pool.reset_worker(worker_url, hard=True)
    logger.info("recycled session=%s worker=%s", session_id, worker_url)
    return {"ok": True, "session_id": session_id, "recycled": True}


@app.post("/v1/sessions/{session_id}/recycle", tags=["sessions"])
def recycle_session(session_id: str, _: AuthDep):
    """Wipe Firefox and free the worker (from leased or parked)."""
    return _recycle_session(session_id)


@app.get("/v1/sessions/{session_id}/screenshot", tags=["observe"])
def screenshot(session_id: str, _: AuthDep):
    status, data = pool.proxy_get(session_id, "/screenshot")
    if status >= 400:
        raise HTTPException(status_code=status, detail=data)
    return data


@app.get("/v1/sessions/{session_id}/observe", tags=["observe"])
def observe(
    session_id: str,
    _: AuthDep,
    max_elements: Annotated[int, Query(ge=1, le=500)] = 200,
):
    """Phone: screenshot + trimmed UIAutomator elements. Desktop workers return 404."""
    status, data = pool.proxy_get(
        session_id, f"/observe?max_elements={max_elements}"
    )
    if status >= 400:
        raise HTTPException(status_code=status, detail=data)
    return data


@app.post("/v1/sessions/{session_id}/action", tags=["act"])
def action(session_id: str, body: ActionBody, _: AuthDep):
    status, data = pool.proxy_post(
        session_id, "/action", body.model_dump(exclude_none=True)
    )
    if status >= 400:
        raise HTTPException(status_code=status, detail=data)
    return data


@app.post("/v1/sessions/{session_id}/reset", tags=["sessions"])
def reset_session(session_id: str, _: AuthDep):
    """Reset Firefox on the leased worker without releasing the lease."""
    status, data = pool.proxy_post(session_id, "/session/reset", {})
    if status >= 400:
        raise HTTPException(status_code=status, detail=data)
    return data
