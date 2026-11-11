"""Phone worker — ADB control plane mirroring desktop worker endpoints."""

from __future__ import annotations

import base64
import logging
import os
from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, model_validator

import adbutil

logger = logging.getLogger("computer.phone")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

app = FastAPI(
    title="Computer Phone Worker",
    version="0.1.0",
    description="Android seat via ADB: screenshot / action / soft reset / hard recycle.",
)


def _pkg_list(env_name: str) -> list[str]:
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return []
    return [p.strip() for p in raw.split(",") if p.strip()]


def _force_stop_packages() -> list[str]:
    return _pkg_list("RESET_FORCE_STOP_PACKAGES")


def _clear_packages() -> list[str]:
    # Fall back to force-stop list so recycle is never a silent no-op when only one list is set.
    cleared = _pkg_list("RECYCLE_CLEAR_PACKAGES")
    return cleared or _force_stop_packages()


@app.get("/health")
def health():
    """adbd + selected device; optional scrcpy-web URL is inventory-side (HITL)."""
    checks: dict[str, bool] = {}
    detail: dict = {"ok": False, "kind": "phone", "checks": checks}

    try:
        found = adbutil.devices()
        checks["adb"] = True
        detail["devices"] = found
    except adbutil.AdbError as e:
        checks["adb"] = False
        detail["error"] = str(e)
        raise HTTPException(status_code=503, detail=detail) from e

    selected = adbutil.selected_device()
    serial_env = os.environ.get("ADB_SERIAL", "").strip() or None
    detail["adb_serial_env"] = serial_env

    if not selected:
        checks["device"] = False
        detail["error"] = "no device in 'device' state" + (
            f" matching ADB_SERIAL={serial_env}" if serial_env else ""
        )
        raise HTTPException(status_code=503, detail=detail)

    if selected.get("state") == "ambiguous":
        checks["device"] = False
        detail["error"] = "multiple devices connected; set ADB_SERIAL"
        detail["selected"] = selected
        raise HTTPException(status_code=503, detail=detail)

    if selected.get("state") != "device":
        checks["device"] = False
        detail["selected"] = selected
        detail["error"] = f"device state is {selected.get('state')!r}, want 'device'"
        raise HTTPException(status_code=503, detail=detail)

    checks["device"] = True
    detail["selected"] = selected
    size = adbutil.wm_size()
    if size:
        detail["geometry"] = {"width": size[0], "height": size[1]}
        checks["wm_size"] = True
    else:
        checks["wm_size"] = False

    # HITL is scrcpy-web URL in gateway inventory; optional probe only.
    hitl = os.environ.get("SCRCPY_WEB_URL", "").strip()
    if hitl:
        detail["scrcpy_web_url"] = hitl

    detail["ok"] = checks.get("adb") and checks.get("device")
    detail["status"] = "ok" if detail["ok"] else "degraded"
    if not detail["ok"]:
        raise HTTPException(status_code=503, detail=detail)
    return detail


@app.get("/screenshot")
def screenshot():
    try:
        png = adbutil.screencap_png()
    except adbutil.AdbError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    return {"image": base64.b64encode(png).decode()}


class Action(BaseModel):
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


@app.post("/action")
def action(a: Action):
    try:
        if a.type in ("click", "tap"):
            if a.x is None or a.y is None:
                raise HTTPException(status_code=400, detail="tap/click requires x,y")
            adbutil.tap(a.x, a.y)
        elif a.type == "double_click":
            if a.x is None or a.y is None:
                raise HTTPException(status_code=400, detail="double_click requires x,y")
            adbutil.tap(a.x, a.y)
            adbutil.tap(a.x, a.y)
        elif a.type == "long_press":
            if a.x is None or a.y is None:
                raise HTTPException(status_code=400, detail="long_press requires x,y")
            adbutil.long_press(a.x, a.y, a.duration_ms or 800)
        elif a.type == "swipe":
            if None in (a.x1, a.y1, a.x2, a.y2):
                raise HTTPException(status_code=400, detail="swipe requires x1,y1,x2,y2")
            adbutil.swipe(a.x1, a.y1, a.x2, a.y2, a.duration_ms or 300)  # type: ignore[arg-type]
        elif a.type == "scroll":
            # Approximate desktop scroll with a vertical swipe around mid-screen.
            size = adbutil.wm_size() or (1080, 2400)
            w, h = size
            cx = a.x if a.x is not None else w // 2
            cy = a.y if a.y is not None else h // 2
            delta = max(80, min(h // 3, 200 * max(1, a.amount)))
            if a.direction == "up":
                adbutil.swipe(cx, cy, cx, min(h - 1, cy + delta), a.duration_ms or 300)
            else:
                adbutil.swipe(cx, cy, cx, max(0, cy - delta), a.duration_ms or 300)
        elif a.type == "type":
            if a.text is None:
                raise HTTPException(status_code=400, detail="type requires text")
            adbutil.type_text(a.text)
        elif a.type == "key":
            if not a.text:
                raise HTTPException(status_code=400, detail="key requires text (e.g. back, home)")
            adbutil.keyevent(a.text)
        elif a.type == "launch":
            if not a.package and not a.uri:
                raise HTTPException(status_code=400, detail="launch requires package or uri")
            adbutil.launch(package=a.package, uri=a.uri)
        elif a.type == "move":
            # No hover on phone; accept as no-op for shared agent loops.
            pass
        elif a.type == "right_click":
            if a.x is None or a.y is None:
                raise HTTPException(status_code=400, detail="right_click requires x,y")
            adbutil.long_press(a.x, a.y, a.duration_ms or 800)
        else:
            raise HTTPException(status_code=400, detail=f"unknown action type: {a.type}")
    except adbutil.AdbError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    return {"ok": True, "type": a.type}


class ResetBody(BaseModel):
    hard: bool = False
    mode: Literal["soft", "hard"] | None = None

    @model_validator(mode="after")
    def _resolve_mode(self) -> ResetBody:
        if self.mode == "hard":
            self.hard = True
        elif self.mode == "soft":
            self.hard = False
        return self


@app.post("/session/reset")
def session_reset(body: ResetBody = ResetBody()):
    """Soft (default): force-stop packages. Hard: pm clear (recycle / idle wipe)."""
    try:
        if body.hard:
            pkgs = _clear_packages()
            if not pkgs:
                logger.warning("hard reset with empty RECYCLE_CLEAR_PACKAGES / RESET_FORCE_STOP_PACKAGES")
            cleared = adbutil.pm_clear(pkgs) if pkgs else []
            return {"ok": True, "reset": "hard", "cleared": cleared}
        pkgs = _force_stop_packages()
        stopped = adbutil.force_stop(pkgs) if pkgs else []
        return {"ok": True, "reset": "soft", "force_stopped": stopped}
    except adbutil.AdbError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/session/recycle")
def session_recycle():
    """Explicit hard wipe — same as POST /session/reset with hard=true."""
    return session_reset(ResetBody(hard=True))
