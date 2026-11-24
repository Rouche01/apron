"""Apron Gateway — model-agnostic session lease + observe/act proxy."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

import abuse as abuse_mod
import billing as billing_mod
import physical_phone_seats
import providers  # noqa: F401 — registers CloudPhoneProvider impls (phone-harness, …)
from abuse import (
    BUCKET_BILLING,
    BUCKET_GENERAL,
    BUCKET_OBSERVE,
    RateLimitConfig,
    SlidingWindowLimiter,
)
from billing import (
    BillingError,
    InsufficientSeats,
    StripeAPIError,
    StripeClient,
    WebhookSignatureError,
    get_plan,
    list_plans,
    load_config_from_env,
    mark_api_key_revealed,
    parse_webhook_event,
    plan_id_from_subscription,
    provision_from_checkout_session,
    resize_tenant_plan,
    suspend_tenant,
)
from inventory import SeatKind, load_workers
from pool import SeatPinError, SessionPool
from providers import registered_names
from providers.base import CloudPhoneProviderError
from recording import RecordingQuotaExceeded, RecordingStore, resolve_record_flag
from tenancy import (
    TenantContext,
    filter_seats,
    load_registry_from_env,
    ops_context,
    oss_context,
)
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
recordings = RecordingStore()
try:
    tenants = load_registry_from_env()
except (OSError, ValueError, json.JSONDecodeError) as e:
    raise RuntimeError(f"invalid TENANTS_PATH: {e}") from e
if tenants is not None:
    for _t in tenants.iter_tenants():
        if _t.max_recording_bytes:
            recordings.set_quota_bytes(_t.id, _t.max_recording_bytes)

# Hosted abuse rate limits (active when TENANTS_PATH is set; see require_tenant / billing).
rate_limiter = SlidingWindowLimiter(RateLimitConfig.from_env())

_security = HTTPBearer(auto_error=False)


def _client_ip(request: Request) -> str:
    xff = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if xff:
        return xff
    if request.client and request.client.host:
        return request.client.host
    return "_"


def _rate_limit_or_429(bucket: str, key: str) -> None:
    result = rate_limiter.check(bucket, key)
    if result.allowed:
        return
    headers: dict[str, str] = {}
    if result.retry_after_sec is not None:
        headers["Retry-After"] = str(max(1, int(result.retry_after_sec + 0.999)))
    raise HTTPException(
        status_code=429,
        detail={
            "error": "rate limit exceeded",
            "code": "rate_limited",
            "limit": result.limit,
            "bucket": bucket,
        },
        headers=headers,
    )


def _enforce_billing_ip_limit(request: Request) -> None:
    """Public billing routes: per-IP limit when tenancy (hosted) is on."""
    if tenants is None:
        return
    _rate_limit_or_429(BUCKET_BILLING, _client_ip(request))


def _reaper_loop(stop: threading.Event) -> None:
    while not stop.wait(15):
        # Lease TTL → park (no reset). Parked idle TTL → free + Firefox reset.
        for worker_url in pool.sweep_expired():
            logger.info("parked idle TTL; recycling worker %s", worker_url)
            pool.reset_worker(worker_url)
        for sid in pool.drain_recording_finalizes():
            recordings.finalize(sid)
        for sid in recordings.sweep():
            logger.info("recording TTL swept session=%s", sid)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    stop = threading.Event()
    t = threading.Thread(target=_reaper_loop, args=(stop,), daemon=True, name="lease-reaper")
    t.start()
    if tenants:
        logger.info(
            "tenancy enabled: %d tenants (ops key %s)",
            len(tenants.tenant_ids()),
            "set" if API_TOKEN else "unset",
        )
        _pb = PUBLIC_BASE.lower()
        if "localhost" in _pb or "127.0.0.1" in _pb:
            logger.warning(
                "TENANTS_PATH set but GATEWAY_PUBLIC_BASE=%s looks local — "
                "hosted customers need a public HTTPS origin (see docs/hosted.md)",
                PUBLIC_BASE,
            )
    elif not API_TOKEN:
        logger.warning("COMPUTER_API_TOKEN unset — gateway auth is disabled (dev only)")
    if billing_mod.billing_enabled():
        if load_config_from_env() is None:
            logger.warning(
                "STRIPE_SECRET_KEY set but billing config incomplete "
                "(webhook secret + success/cancel/portal URLs)"
            )
        elif tenants is None:
            logger.warning(
                "billing configured but TENANTS_PATH unset — "
                "/v1/billing/* will return 503 until tenancy is enabled"
            )
        else:
            logger.info("billing enabled (Stripe Checkout + webhook)")
    if tenants is not None:
        cfg = rate_limiter.config
        logger.info(
            "abuse limits: general_rpm=%s observe_rpm=%s billing_rpm=%s",
            cfg.general_rpm,
            cfg.observe_rpm,
            cfg.billing_rpm,
        )
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
    title="Apron Gateway",
    version="0.3.0",
    description=(
        "Model-agnostic computer sessions (desktop or phone): lease a worker by kind, "
        "screenshot/act, soft-release (park warm), acquire or job_id-reattach, recycle to wipe. "
        "Inference stays outside — any agent can bind via this OpenAPI."
    ),
    lifespan=lifespan,
)


def require_tenant(
    request: Request,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_security)],
) -> TenantContext:
    """Resolve Bearer → TenantContext (OSS single-token or multi-tenant keys)."""
    token = ""
    if creds is not None and creds.scheme.lower() == "bearer":
        token = (creds.credentials or "").strip()

    if tenants is not None:
        if API_TOKEN and token == API_TOKEN:
            return ops_context()
        if token:
            ctx = tenants.authenticate_key(token)
            if ctx is not None:
                if ctx.disabled or abuse_mod.is_tenant_denied(ctx.id):
                    raise HTTPException(
                        status_code=403,
                        detail={
                            "error": "tenant disabled",
                            "code": "tenant_disabled",
                            "tenant_id": ctx.id,
                        },
                    )
                # Hosted: per-tenant rate limit (stricter on screenshot/observe/action)
                bucket = (
                    BUCKET_OBSERVE
                    if abuse_mod.observe_path(request.url.path)
                    else BUCKET_GENERAL
                )
                _rate_limit_or_429(bucket, ctx.id or "_")
                return ctx
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")

    # OSS: optional single COMPUTER_API_TOKEN
    if not API_TOKEN:
        return oss_context()
    if token == API_TOKEN:
        return oss_context()
    raise HTTPException(status_code=401, detail="invalid or missing bearer token")


TenantDep = Annotated[TenantContext, Depends(require_tenant)]
# Backward-compatible alias for any external imports / older patches
AuthDep = TenantDep


def _require_session_access(ctx: TenantContext, session_id: str) -> None:
    """Cross-tenant session ids look like missing (404)."""
    if ctx.is_ops:
        return
    if pool.session_tenant(session_id) != ctx.id:
        raise HTTPException(status_code=404, detail="session not found or expired")


def _require_seat_access(ctx: TenantContext, seat_id: str) -> None:
    if ctx.may_use_seat(seat_id):
        return
    raise HTTPException(status_code=404, detail="seat not found")


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
    record: bool | None = Field(
        default=None,
        description=(
            "Opt-in session recording journal (screenshots/actions). "
            "When omitted, uses RECORD_SESSIONS_DEFAULT."
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
    recording: bool = False


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
        recording=bool(getattr(info, "recording", False)),
    )


def _start_recording_for_lease(
    info, *, record: bool | None, tenant_id: str | None = None
) -> bool:
    """Create journal + set slot flag when record resolves true. Returns recording on."""
    if not resolve_record_flag(record):
        pool.set_recording(info.session_id, False)
        return False
    try:
        recordings.start(
            info.session_id,
            kind=info.kind,
            seat_id=info.seat_id or None,
            agent_id=info.agent_id,
            job_id=info.job_id,
            tenant_id=tenant_id,
        )
    except RecordingQuotaExceeded as e:
        logger.warning(
            "recording quota session=%s tenant=%s used=%s limit=%s",
            info.session_id,
            tenant_id,
            e.used,
            e.limit,
        )
        pool.set_recording(info.session_id, False)
        return False
    except ValueError as e:
        logger.warning("recording start failed session=%s: %s", info.session_id, e)
        pool.set_recording(info.session_id, False)
        return False
    pool.set_recording(info.session_id, True)
    return True


def _finalize_recording(session_id: str) -> None:
    pool.set_recording(session_id, False)
    recordings.finalize(session_id)


def _sync_tenant_recording_quotas() -> None:
    """Refresh RecordingStore quotas from the live tenant registry."""
    if tenants is None:
        return
    for t in tenants.iter_tenants():
        recordings.set_quota_bytes(t.id, t.max_recording_bytes)


def _require_billing_config():
    cfg = load_config_from_env()
    if cfg is None:
        raise HTTPException(status_code=404, detail="billing not configured")
    return cfg


def _require_billing_tenants():
    if tenants is None:
        raise HTTPException(
            status_code=503,
            detail="tenancy required for billing (set TENANTS_PATH)",
        )
    return tenants


class BillingCheckoutRequest(BaseModel):
    email: str
    plan: str = Field(description="starter | pro")


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
def list_seats(
    ctx: TenantDep,
    device_status: Annotated[
        bool,
        Query(
            description=(
                "If true, probe each phone seat's /device and add device_ready / "
                "device_state / device_serial (live adb). Default true."
            ),
        ),
    ] = True,
):
    """Per-worker inventory: state, session_id, vnc_url, job_id (for Desk inspect/recycle).

    Phone seats include bound_serial. With device_status=1 (default), also live
    device_ready — use GET /v1/seats/{id}/device for a single-seat preflight.
    """
    seats = filter_seats(ctx, pool.list_seats())
    body: dict = {
        "ok": True,
        "workers": pool.worker_count,
        "free": pool.free_count(),
        "leased": pool.leased_count(),
        "parked": pool.parked_count(),
        "seats": seats,
    }
    if device_status:
        summary = physical_phone_seats.enrich_seats_device_status(seats)
        body["physical_phones"] = summary
        # Hydrate gateway bound_serial from live probe when present.
        for seat in seats:
            if seat.get("kind") != "phone":
                continue
            serial = seat.get("device_serial")
            if serial and seat.get("device_ready"):
                pool.set_seat_bound_serial(str(seat["id"]), str(serial))
                seat["bound_serial"] = serial
    return body


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


class SeatPairBody(BaseModel):
    """Wireless debugging pair (adb pair) for a physical phone seat."""

    connect: str = Field(
        description="Pairing host:port from the phone's 'Pair device with pairing code' sheet",
    )
    code: str = Field(
        description="6-digit wireless debugging pairing code",
    )


@app.get("/v1/seats/{seat_id}/devices", tags=["ops"])
def seat_devices(seat_id: str, ctx: TenantDep):
    """List adb devices visible to a phone seat (Desk bind UI)."""
    _require_seat_access(ctx, seat_id)
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
def seat_device(seat_id: str, ctx: TenantDep):
    """Current bind status for a phone seat (authoritative physical preflight).

    Prefer this before leasing a pinned physical seat: selected.state should be
    'device'. For a fleet glance, GET /v1/seats?device_status=1.
    """
    _require_seat_access(ctx, seat_id)
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
def devices_register(body: DeviceRegisterBody, ctx: TenantDep):
    """Bind a physical device to a free phone seat; return seat_id for lease pin.

    Prefers a free unbound physical seat. If none and PHONE_WARM_SPAWN_MAX>0 with a
    WorkerFactory backend, spawns a warm reserved phone worker then binds.
    Human Allow once on first ADB trust. Pipeline leases with seat_id pin.
    """
    pin = (body.seat_id or "").strip() or None
    if pin:
        _require_seat_access(ctx, pin)
    elif not ctx.is_ops:
        # Customers must pin a seat in their allowlist; ops may pick any free seat.
        raise HTTPException(
            status_code=400,
            detail={
                "ok": False,
                "error": "seat_id required for non-ops tenants",
                "code": "seat_id_required",
            },
        )
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
        seat_id=pin,
    )
    if status >= 400:
        raise HTTPException(status_code=status, detail=data)
    return data


class DeviceUnregisterBody(BaseModel):
    seat_id: str = Field(description="Warm-spawned physical seat to destroy")


@app.post("/v1/devices/unregister", tags=["ops"])
def devices_unregister(body: DeviceUnregisterBody, ctx: TenantDep):
    """Destroy a register-spawned warm physical seat (unbind + WorkerFactory.destroy).

    Inventory Compose seats are not removed — use POST /v1/seats/{id}/unbind instead.
    """
    sid = (body.seat_id or "").strip()
    _require_seat_access(ctx, sid)
    status, data = pool.unregister_physical_device(sid)
    if status >= 400:
        raise HTTPException(status_code=status, detail=data)
    return data


@app.post("/v1/seats/{seat_id}/pair", tags=["ops"])
def seat_pair(seat_id: str, body: SeatPairBody, ctx: TenantDep):
    """Wireless debugging pair (adb pair) using the phone worker's ~/.android keys.

    Open "Pair device with pairing code" on the phone, pass that ephemeral
    host:port + 6-digit code here, then POST /v1/seats/{id}/bind with the main
    Wireless debugging IP & port. Refused on cloud seats and while leased.
    """
    _require_seat_access(ctx, seat_id)
    status, data = pool.pair_phone_seat(
        seat_id,
        connect=(body.connect or "").strip(),
        code=(body.code or "").strip(),
    )
    if status >= 400:
        raise HTTPException(status_code=status, detail=data)
    return data


@app.post("/v1/seats/{seat_id}/bind", tags=["ops"])
def seat_bind(seat_id: str, body: SeatBindBody, ctx: TenantDep):
    """Bind a phone seat to a device without recreating containers.

    Human must tap Allow / Authorize on the phone the first time this host's
    adb keys appear (Compose mounts ~/.android into phone-1 + scrcpy-web).
    For Wireless debugging first trust, prefer POST /v1/seats/{id}/pair first.
    Refuses while the seat is leased. Prefer POST /v1/devices/register when
    Desk should pick a free seat and return seat_id for the agent pin.
    """
    _require_seat_access(ctx, seat_id)
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
def seat_unbind(seat_id: str, ctx: TenantDep):
    """Clear runtime bind; fall back to ADB_SERIAL env on the phone worker."""
    _require_seat_access(ctx, seat_id)
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
    ctx: TenantDep,
    body: LeaseRequest = LeaseRequest(),
    x_job_id: Annotated[str | None, Header()] = None,
):
    """Lease a free worker of the requested kind, or reattach a parked seat when job_id matches.

    Phone without seat_id uses cloud seats only; pass seat_id to pin a physical (or any) seat.
    """
    job_id = body.job_id or x_job_id
    kind: SeatKind = body.kind
    allowed = ctx.allowed_seat_ids()
    tenant_id = None if ctx.is_ops else ctx.id
    ttl_sec = ctx.clamp_ttl_sec(body.ttl_sec)
    if (
        tenant_id
        and ctx.max_concurrent_leases is not None
        and not pool.tenant_lease_is_reattach(
            tenant_id, kind=kind, job_id=job_id, seat_id=body.seat_id
        )
        and pool.tenant_held_count(tenant_id) >= ctx.max_concurrent_leases
    ):
        raise HTTPException(
            status_code=429,
            detail={
                "ok": False,
                "error": "tenant concurrent lease cap reached",
                "code": "tenant_lease_cap",
                "tenant_id": tenant_id,
                "held": pool.tenant_held_count(tenant_id),
                "max_concurrent_leases": ctx.max_concurrent_leases,
            },
        )
    try:
        info = pool.lease(
            agent_id=body.agent_id,
            job_id=job_id,
            ttl_sec=ttl_sec,
            kind=kind,
            seat_id=body.seat_id,
            allowed_seat_ids=allowed,
            tenant_id=tenant_id,
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
    recording = _start_recording_for_lease(
        info, record=body.record, tenant_id=tenant_id
    )
    info.recording = recording
    logger.info(
        "leased session=%s kind=%s seat=%s agent=%s job=%s worker=%s recording=%s tenant=%s",
        info.session_id,
        info.kind,
        info.seat_id,
        info.agent_id,
        info.job_id,
        info.worker_base_url,
        recording,
        tenant_id or "-",
    )
    return _lease_response(info)


@app.post("/v1/sessions/{session_id}/heartbeat", response_model=LeaseResponse, tags=["sessions"])
def heartbeat(session_id: str, ctx: TenantDep, body: HeartbeatRequest = HeartbeatRequest()):
    _require_session_access(ctx, session_id)
    info = pool.heartbeat(session_id, ttl_sec=ctx.clamp_ttl_sec(body.ttl_sec))
    if not info:
        raise HTTPException(status_code=404, detail="session not found or expired")
    return _lease_response(info)


@app.post("/v1/sessions/{session_id}/acquire", response_model=LeaseResponse, tags=["sessions"])
def acquire_session(
    session_id: str,
    ctx: TenantDep,
    body: AcquireRequest = AcquireRequest(),
):
    """Re-lease a parked desktop by session_id (serial same-job leases)."""
    _require_session_access(ctx, session_id)
    result = pool.acquire(
        session_id,
        ttl_sec=ctx.clamp_ttl_sec(body.ttl_sec),
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
    ctx: TenantDep,
    recycle: Annotated[bool, Query(description="Wipe Firefox and free the worker")] = False,
):
    """Soft release parks the warm seat. Pass recycle=true to wipe and free."""
    _require_session_access(ctx, session_id)
    if recycle:
        return _recycle_session(session_id)
    info = pool.release(session_id)
    if not info:
        raise HTTPException(status_code=404, detail="session not found or expired")
    _finalize_recording(session_id)
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
    _finalize_recording(session_id)
    worker_url = pool.recycle(session_id)
    if not worker_url:
        raise HTTPException(status_code=404, detail="session not found or expired")
    pool.reset_worker(worker_url, hard=True)
    logger.info("recycled session=%s worker=%s", session_id, worker_url)
    return {"ok": True, "session_id": session_id, "recycled": True}


@app.post("/v1/sessions/{session_id}/recycle", tags=["sessions"])
def recycle_session(session_id: str, ctx: TenantDep):
    """Wipe Firefox and free the worker (from leased or parked)."""
    _require_session_access(ctx, session_id)
    return _recycle_session(session_id)


@app.get("/v1/sessions/{session_id}/screenshot", tags=["observe"])
def screenshot(session_id: str, ctx: TenantDep):
    _require_session_access(ctx, session_id)
    status, data = pool.proxy_get(session_id, "/screenshot")
    if status >= 400:
        raise HTTPException(status_code=status, detail=data)
    if pool.is_recording(session_id):
        frame_b64 = data.get("image") if isinstance(data, dict) else None
        recordings.append(
            session_id, kind="screenshot", ok=True, frame_b64=frame_b64
        )
    return data


@app.get("/v1/sessions/{session_id}/observe", tags=["observe"])
def observe(
    session_id: str,
    ctx: TenantDep,
    max_elements: Annotated[int, Query(ge=1, le=500)] = 200,
):
    """Phone: screenshot + trimmed UIAutomator elements. Desktop workers return 404."""
    _require_session_access(ctx, session_id)
    status, data = pool.proxy_get(
        session_id, f"/observe?max_elements={max_elements}"
    )
    if status >= 400:
        raise HTTPException(status_code=status, detail=data)
    if pool.is_recording(session_id):
        elements_n = None
        frame_b64 = None
        if isinstance(data, dict):
            frame_b64 = data.get("image")
            els = data.get("elements")
            if isinstance(els, list):
                elements_n = len(els)
        recordings.append(
            session_id,
            kind="observe",
            ok=True,
            frame_b64=frame_b64,
            elements_n=elements_n,
        )
    return data


@app.post("/v1/sessions/{session_id}/action", tags=["act"])
def action(session_id: str, body: ActionBody, ctx: TenantDep):
    _require_session_access(ctx, session_id)
    payload = body.model_dump(exclude_none=True)
    status, data = pool.proxy_post(session_id, "/action", payload)
    if status >= 400:
        raise HTTPException(status_code=status, detail=data)
    if pool.is_recording(session_id):
        recordings.append(session_id, kind="action", ok=True, body=payload)
    return data


@app.post("/v1/sessions/{session_id}/reset", tags=["sessions"])
def reset_session(session_id: str, ctx: TenantDep):
    """Reset Firefox on the leased worker without releasing the lease."""
    _require_session_access(ctx, session_id)
    status, data = pool.proxy_post(session_id, "/session/reset", {})
    if status >= 400:
        raise HTTPException(status_code=status, detail=data)
    return data


@app.get("/v1/recordings", tags=["recordings"])
def list_recordings(
    ctx: TenantDep,
    job_id: Annotated[str | None, Query()] = None,
    kind: Annotated[SeatKind | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
):
    """List session recording journals (survive park/recycle; TTL via RECORDING_TTL_SEC)."""
    tenant_filter = None if ctx.is_ops else ctx.id
    return {
        "recordings": recordings.list(
            job_id=job_id, kind=kind, tenant_id=tenant_filter, limit=limit
        ),
    }


@app.get("/v1/recordings/{session_id}", tags=["recordings"])
def get_recording(session_id: str, ctx: TenantDep):
    """Recording manifest: meta + events (frame paths relative; no embedded base64)."""
    if not ctx.is_ops and not recordings.belongs_to_tenant(session_id, ctx.id or ""):
        raise HTTPException(status_code=404, detail="recording not found")
    manifest = recordings.get(session_id)
    if not manifest:
        raise HTTPException(status_code=404, detail="recording not found")
    return manifest


@app.get("/v1/recordings/{session_id}/frames/{name}", tags=["recordings"])
def get_recording_frame(session_id: str, name: str, ctx: TenantDep):
    """Serve a captured PNG frame from a recording journal."""
    if not ctx.is_ops and not recordings.belongs_to_tenant(session_id, ctx.id or ""):
        raise HTTPException(status_code=404, detail="frame not found")
    data = recordings.get_frame(session_id, name)
    if data is None:
        raise HTTPException(status_code=404, detail="frame not found")
    return Response(content=data, media_type="image/png")


# --- Hosted billing (Stripe) -------------------------------------------------


@app.get("/v1/billing/plans", tags=["billing"])
def billing_plans(request: Request):
    """Public plan matrix (seats + whether a Stripe price id is configured)."""
    _enforce_billing_ip_limit(request)
    return {"plans": list_plans()}


@app.post("/v1/billing/checkout", tags=["billing"])
def billing_checkout(request: Request, body: BillingCheckoutRequest):
    """Start Stripe Checkout (subscription). Public — no Bearer required."""
    _enforce_billing_ip_limit(request)
    cfg = _require_billing_config()
    _require_billing_tenants()
    try:
        plan = get_plan(body.plan)
        stripe = StripeClient(cfg)
        try:
            session = stripe.create_checkout_session(email=body.email, plan=plan)
        finally:
            stripe.close()
    except BillingError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except StripeAPIError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    return {
        "url": session["url"],
        "checkout_session_id": session["id"],
    }


@app.get("/v1/billing/checkout/{session_id}", tags=["billing"])
def billing_checkout_reveal(request: Request, session_id: str):
    """Success-page key reveal: api_key returned once, then revealed=true."""
    _enforce_billing_ip_limit(request)
    cfg = _require_billing_config()
    reg = _require_billing_tenants()
    stripe = StripeClient(cfg)
    try:
        try:
            session = stripe.retrieve_checkout_session(session_id)
        except StripeAPIError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
    finally:
        stripe.close()

    if str(session.get("status") or "") != "complete":
        raise HTTPException(status_code=404, detail="checkout session not complete")

    customer = session.get("customer")
    customer_id = customer if isinstance(customer, str) else None
    if isinstance(customer, dict):
        customer_id = str(customer.get("id") or "") or None
    if not customer_id:
        raise HTTPException(status_code=404, detail="checkout session has no customer")

    tenant = reg.get_by_stripe_customer(customer_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="tenant not provisioned yet")

    seat_ids = sorted(tenant.seat_ids)
    base = {
        "tenant_id": tenant.id,
        "plan": tenant.plan,
        "seat_ids": seat_ids,
        "email": tenant.email,
    }
    if tenant.api_key_revealed_at:
        return {**base, "api_key": None, "revealed": True}

    if not tenant.api_keys:
        raise HTTPException(status_code=404, detail="tenant has no api key")

    api_key = sorted(tenant.api_keys)[0]
    mark_api_key_revealed(reg, tenant)
    return {**base, "api_key": api_key, "revealed": False}


@app.post("/v1/billing/portal", tags=["billing"])
def billing_portal(ctx: TenantDep):
    """Stripe Customer Portal (invoices / cancel). Requires tenant Bearer."""
    cfg = _require_billing_config()
    reg = _require_billing_tenants()
    if ctx.is_ops or not ctx.id:
        raise HTTPException(
            status_code=400,
            detail="portal requires a customer API key (not ops)",
        )
    tenant = reg.get(ctx.id)
    if tenant is None or not tenant.stripe_customer_id:
        raise HTTPException(status_code=404, detail="no stripe customer for tenant")
    stripe = StripeClient(cfg)
    try:
        try:
            portal = stripe.create_portal_session(
                customer_id=tenant.stripe_customer_id
            )
        except (BillingError, StripeAPIError) as e:
            raise HTTPException(status_code=502, detail=str(e)) from e
    finally:
        stripe.close()
    return {"url": portal["url"]}


@app.post("/v1/billing/webhook", tags=["billing"])
async def billing_webhook(
    request: Request,
    stripe_signature: Annotated[str | None, Header(alias="Stripe-Signature")] = None,
):
    """Stripe webhook: provision on checkout, suspend/resize on subscription events."""
    cfg = _require_billing_config()
    reg = _require_billing_tenants()
    payload = await request.body()
    try:
        event = parse_webhook_event(
            payload, stripe_signature, secret=cfg.webhook_secret
        )
    except WebhookSignatureError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except BillingError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    etype = str(event.get("type") or "")
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    obj = data.get("object") if isinstance(data, dict) else None
    if not isinstance(obj, dict):
        raise HTTPException(status_code=400, detail="webhook missing data.object")

    try:
        if etype == "checkout.session.completed":
            tenant = provision_from_checkout_session(
                obj, registry=reg, seats_snapshot=pool.list_seats()
            )
            _sync_tenant_recording_quotas()
            logger.info(
                "billing webhook provisioned tenant=%s plan=%s seats=%s",
                tenant.id,
                tenant.plan,
                sorted(tenant.seat_ids),
            )
            return {"ok": True, "tenant_id": tenant.id}

        if etype == "customer.subscription.deleted":
            customer_id = str(obj.get("customer") or "").strip()
            tenant = reg.get_by_stripe_customer(customer_id) if customer_id else None
            if tenant is not None:
                suspend_tenant(reg, tenant)
                _sync_tenant_recording_quotas()
                logger.info("billing webhook suspended tenant=%s", tenant.id)
            return {"ok": True}

        if etype == "customer.subscription.updated":
            customer_id = str(obj.get("customer") or "").strip()
            tenant = reg.get_by_stripe_customer(customer_id) if customer_id else None
            if tenant is None:
                return {"ok": True, "skipped": "unknown_customer"}
            status = str(obj.get("status") or "")
            if status in ("canceled", "unpaid", "incomplete_expired"):
                suspend_tenant(reg, tenant)
                _sync_tenant_recording_quotas()
                logger.info(
                    "billing webhook suspended tenant=%s status=%s",
                    tenant.id,
                    status,
                )
                return {"ok": True, "suspended": True}
            plan_id = plan_id_from_subscription(obj)
            if plan_id:
                plan = get_plan(plan_id)
                resize_tenant_plan(
                    reg,
                    tenant,
                    plan=plan,
                    seats_snapshot=pool.list_seats(),
                )
                _sync_tenant_recording_quotas()
                logger.info(
                    "billing webhook resized tenant=%s plan=%s",
                    tenant.id,
                    plan_id,
                )
            return {"ok": True}

    except InsufficientSeats as e:
        # 500 so Stripe retries when capacity frees up
        logger.error("billing webhook insufficient seats: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e
    except BillingError as e:
        logger.error("billing webhook error type=%s: %s", etype, e)
        raise HTTPException(status_code=500, detail=str(e)) from e

    return {"ok": True, "ignored": etype}
