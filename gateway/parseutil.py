"""Small shared parsers for JSON config rows and env ints."""

from __future__ import annotations

import os
from typing import Any


def opt_str(row: dict[str, Any], key: str) -> str | None:
    raw = row.get(key)
    if raw is None or raw == "":
        return None
    return str(raw).strip() or None


def opt_bool(row: dict[str, Any], key: str, *, default: bool = False) -> bool:
    raw = row.get(key)
    if raw is None or raw == "":
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    s = str(raw).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    return default


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def env_nonneg_int(name: str, default: int) -> int:
    return max(0, env_int(name, default))
