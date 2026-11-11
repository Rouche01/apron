"""Thin ADB helpers for the phone worker (one serial per process)."""

from __future__ import annotations

import os
import re
import subprocess
from typing import Sequence


class AdbError(RuntimeError):
    def __init__(self, message: str, *, stderr: str = "", returncode: int | None = None):
        super().__init__(message)
        self.stderr = stderr
        self.returncode = returncode


def _serial() -> str | None:
    s = os.environ.get("ADB_SERIAL", "").strip()
    return s or None


def adb_prefix() -> list[str]:
    cmd = ["adb"]
    serial = _serial()
    if serial:
        cmd.extend(["-s", serial])
    return cmd


def run_adb(
    args: Sequence[str],
    *,
    timeout: float = 30,
    check: bool = True,
    binary: bool = False,
) -> subprocess.CompletedProcess[bytes] | subprocess.CompletedProcess[str]:
    cmd = [*adb_prefix(), *args]
    try:
        r = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            timeout=timeout,
            text=not binary,
        )
    except FileNotFoundError as e:
        raise AdbError("adb not found on PATH — install Android platform-tools") from e
    except subprocess.TimeoutExpired as e:
        raise AdbError(f"adb timed out: {' '.join(cmd)}") from e
    if check and r.returncode != 0:
        err = (r.stderr or b"" if binary else r.stderr or "").strip()
        if isinstance(err, bytes):
            err = err.decode("utf-8", errors="replace")
        raise AdbError(
            f"adb failed ({r.returncode}): {' '.join(args)} — {err or 'no stderr'}",
            stderr=err,
            returncode=r.returncode,
        )
    return r


def devices() -> list[dict[str, str]]:
    r = run_adb(["devices", "-l"], timeout=10)
    assert isinstance(r.stdout, str)
    out: list[dict[str, str]] = []
    for line in r.stdout.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        out.append({"serial": serial, "state": state})
    return out


def selected_device() -> dict[str, str] | None:
    want = _serial()
    found = devices()
    if want:
        for d in found:
            if d["serial"] == want:
                return d
        return None
    ready = [d for d in found if d["state"] == "device"]
    if len(ready) == 1:
        return ready[0]
    if len(ready) > 1:
        # Ambiguous without ADB_SERIAL — still report first for diagnostics.
        return {"serial": ready[0]["serial"], "state": "ambiguous", "count": str(len(ready))}
    return None


def screencap_png() -> bytes:
    r = run_adb(["exec-out", "screencap", "-p"], timeout=45, binary=True)
    assert isinstance(r.stdout, bytes)
    if not r.stdout.startswith(b"\x89PNG"):
        raise AdbError("screencap did not return a PNG")
    return r.stdout


def wm_size() -> tuple[int, int] | None:
    r = run_adb(["shell", "wm", "size"], timeout=10, check=False)
    assert isinstance(r.stdout, str)
    # Physical size: 1080x2400  or  Override size: …
    m = re.search(r"(\d+)\s*x\s*(\d+)", r.stdout)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def shell_input(*parts: str, timeout: float = 15) -> None:
    run_adb(["shell", "input", *parts], timeout=timeout)


def tap(x: int, y: int) -> None:
    shell_input("tap", str(x), str(y))


def swipe(x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None:
    shell_input("swipe", str(x1), str(y1), str(x2), str(y2), str(max(1, duration_ms)))


def long_press(x: int, y: int, duration_ms: int = 800) -> None:
    swipe(x, y, x, y, duration_ms)


# Android keycodes used by agents
KEYCODES: dict[str, int] = {
    "back": 4,
    "home": 3,
    "recents": 187,
    "app_switch": 187,
    "enter": 66,
    "return": 66,
    "tab": 61,
    "delete": 67,
    "del": 67,
    "escape": 111,
    "esc": 111,
}


def keyevent(name_or_code: str) -> None:
    raw = name_or_code.strip()
    if raw.isdigit():
        code = int(raw)
    else:
        key = raw.lower().replace(" ", "_").replace("-", "_")
        if key not in KEYCODES:
            raise AdbError(f"unknown android key: {name_or_code!r}")
        code = KEYCODES[key]
    shell_input("keyevent", str(code))


def type_text(text: str) -> None:
    """Best-effort `input text` — spaces as %s; limited special chars."""
    if text is None:
        return
    # adb input text: space → %s; escape shell-ish chars lightly
    escaped = (
        text.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace('"', '\\"')
        .replace(" ", "%s")
        .replace("&", "\\&")
        .replace("<", "\\<")
        .replace(">", "\\>")
        .replace("(", "\\(")
        .replace(")", "\\)")
        .replace("|", "\\|")
        .replace(";", "\\;")
    )
    # Use shell -c so %s survives
    run_adb(["shell", "input", "text", escaped], timeout=30)


def force_stop(packages: Sequence[str]) -> list[str]:
    done: list[str] = []
    for pkg in packages:
        pkg = pkg.strip()
        if not pkg:
            continue
        run_adb(["shell", "am", "force-stop", pkg], timeout=20, check=False)
        done.append(pkg)
    return done


def pm_clear(packages: Sequence[str]) -> list[str]:
    done: list[str] = []
    for pkg in packages:
        pkg = pkg.strip()
        if not pkg:
            continue
        run_adb(["shell", "pm", "clear", pkg], timeout=60)
        done.append(pkg)
    return done


def launch(*, package: str | None = None, uri: str | None = None) -> None:
    if uri:
        run_adb(
            [
                "shell",
                "am",
                "start",
                "-a",
                "android.intent.action.VIEW",
                "-d",
                uri,
            ],
            timeout=20,
        )
        return
    if package:
        # Prefer explicit launcher activity resolution via monkey (works without activity name).
        run_adb(
            [
                "shell",
                "monkey",
                "-p",
                package,
                "-c",
                "android.intent.category.LAUNCHER",
                "1",
            ],
            timeout=20,
        )
        return
    raise AdbError("launch requires package or uri")
