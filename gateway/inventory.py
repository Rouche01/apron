"""Worker inventory: JSON file (WORKER_INVENTORY_PATH) with optional legacy env lists."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

SeatKind = Literal["desktop", "phone"]
VALID_KINDS: frozenset[str] = frozenset({"desktop", "phone"})

# Inventory provider ids for cloud phone seats (CloudPhoneProvider registry).
# Physical phone seats omit provider. Expand when registering a new impl.
KNOWN_CLOUD_PROVIDERS: frozenset[str] = frozenset({"phone-harness"})


@dataclass(frozen=True)
class WorkerSpec:
    url: str
    vnc_url: str
    kind: SeatKind = "desktop"
    id: str | None = None
    # Cloud phone tooling id (e.g. phone-harness). None = physical / local ADB bind.
    provider: str | None = None


def with_vnc_view_defaults(url: str) -> str:
    """Ensure lease vnc_url works behind Caddy HTTPS + stock websockify."""
    u = url.strip()
    if not u:
        return u
    parsed = urlparse(u)
    q = parse_qs(parsed.query, keep_blank_values=True)
    if "autoconnect" not in q:
        q["autoconnect"] = ["1"]
    if "resize" not in q:
        q["resize"] = ["scale"]
    if "path" not in q:
        q["path"] = [""]
    if parsed.scheme == "https" and "port" not in q and not parsed.port:
        q["port"] = ["443"]
    pairs: list[tuple[str, str]] = [(k, vals[-1]) for k, vals in q.items()]
    return urlunparse(parsed._replace(query=urlencode(pairs)))


def _normalize_kind(raw: object, *, index: int) -> SeatKind:
    if raw is None or raw == "":
        return "desktop"
    if not isinstance(raw, str) or raw not in VALID_KINDS:
        raise ValueError(
            f"inventory[{index}].kind must be one of {sorted(VALID_KINDS)}, got {raw!r}"
        )
    return raw  # type: ignore[return-value]


def _normalize_provider(
    raw: object, *, kind: SeatKind, index: int
) -> str | None:
    """Optional cloud provider on phone seats only."""
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(
            f"inventory[{index}].provider must be a non-empty string when set"
        )
    provider = raw.strip()
    if kind != "phone":
        raise ValueError(
            f"inventory[{index}].provider is only valid when kind is 'phone' "
            f"(got kind={kind!r}, provider={provider!r})"
        )
    if provider not in KNOWN_CLOUD_PROVIDERS:
        raise ValueError(
            f"inventory[{index}].provider must be one of "
            f"{sorted(KNOWN_CLOUD_PROVIDERS)}, got {provider!r}"
        )
    return provider


def _assign_default_ids(specs: list[WorkerSpec]) -> list[WorkerSpec]:
    """Fill missing ids: desk1… / phone1… per kind order."""
    counters: dict[SeatKind, int] = {"desktop": 0, "phone": 0}
    out: list[WorkerSpec] = []
    for spec in specs:
        if spec.id:
            out.append(spec)
            continue
        counters[spec.kind] += 1
        prefix = "desk" if spec.kind == "desktop" else "phone"
        out.append(
            WorkerSpec(
                url=spec.url,
                vnc_url=spec.vnc_url,
                kind=spec.kind,
                id=f"{prefix}{counters[spec.kind]}",
                provider=spec.provider,
            )
        )
    return out


def load_inventory_file(path: str | Path, *, vnc_defaults: Callable[[str], str] = with_vnc_view_defaults) -> list[WorkerSpec]:
    p = Path(path)
    if p.is_dir():
        raise RuntimeError(
            f"WORKER_INVENTORY_PATH is a directory ({p}). "
            "Docker creates an empty directory when the host file is missing — "
            "set WORKER_INVENTORY_FILE in .env to an existing JSON file "
            "(e.g. ./gateway/workers.json or a copy of workers.hsv.example.json)."
        )
    if not p.is_file():
        raise RuntimeError(
            f"WORKER_INVENTORY_PATH not found or not a file: {p}. "
            "Point WORKER_INVENTORY_FILE / WORKER_INVENTORY_PATH at the inventory for this environment."
        )
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise RuntimeError(f"invalid worker inventory JSON at {p}: {e}") from e
    if not isinstance(data, list) or not data:
        raise RuntimeError(f"worker inventory at {p} must be a non-empty JSON array")

    specs: list[WorkerSpec] = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            raise RuntimeError(f"inventory[{i}] must be an object")
        url = item.get("url")
        vnc = item.get("vnc_url")
        if not isinstance(url, str) or not url.strip():
            raise RuntimeError(f"inventory[{i}].url is required")
        if not isinstance(vnc, str) or not vnc.strip():
            raise RuntimeError(f"inventory[{i}].vnc_url is required")
        seat_id = item.get("id")
        if seat_id is not None and (not isinstance(seat_id, str) or not seat_id.strip()):
            raise RuntimeError(f"inventory[{i}].id must be a non-empty string when set")
        kind = _normalize_kind(item.get("kind"), index=i)
        try:
            provider = _normalize_provider(item.get("provider"), kind=kind, index=i)
        except ValueError as e:
            raise RuntimeError(str(e)) from e
        # Phone HITL is scrcpy-web (/watch.html) or cloud watch_url — skip noVNC defaults.
        vnc_url = vnc.strip() if kind == "phone" else vnc_defaults(vnc.strip())
        specs.append(
            WorkerSpec(
                url=url.strip().rstrip("/"),
                vnc_url=vnc_url,
                kind=kind,
                id=seat_id.strip() if isinstance(seat_id, str) else None,
                provider=provider,
            )
        )

    ids = [s.id for s in specs if s.id]
    if len(ids) != len(set(ids)):
        raise RuntimeError("duplicate worker inventory id values")
    return _assign_default_ids(specs)


def load_legacy_env(*, vnc_defaults: Callable[[str], str] = with_vnc_view_defaults) -> list[WorkerSpec]:
    """Compat: WORKER_URLS + WORKER_VNC_URLS → desktop-only specs."""
    urls = [
        u.strip().rstrip("/")
        for u in os.environ.get("WORKER_URLS", "http://127.0.0.1:8000").split(",")
        if u.strip()
    ]
    vncs_raw = os.environ.get("WORKER_VNC_URLS", "").strip()
    if vncs_raw:
        vncs = [vnc_defaults(v) for v in vncs_raw.split(",") if v.strip()]
    else:
        vncs = [vnc_defaults(f"http://127.0.0.1:{6080 + i}/vnc.html") for i in range(len(urls))]
    if len(vncs) != len(urls):
        raise RuntimeError(
            f"WORKER_URLS ({len(urls)}) and WORKER_VNC_URLS ({len(vncs)}) length mismatch"
        )
    return _assign_default_ids(
        [WorkerSpec(url=u, vnc_url=v, kind="desktop") for u, v in zip(urls, vncs, strict=True)]
    )


def load_workers(*, require_inventory: bool | None = None) -> list[WorkerSpec]:
    """Prefer WORKER_INVENTORY_PATH; else legacy parallel env lists (all desktop).

    Set REQUIRE_WORKER_INVENTORY=1 (Compose does) to refuse legacy fallback.
    """
    path = os.environ.get("WORKER_INVENTORY_PATH", "").strip()
    if require_inventory is None:
        require_inventory = os.environ.get("REQUIRE_WORKER_INVENTORY", "").strip() in {
            "1",
            "true",
            "yes",
        }
    if path:
        return load_inventory_file(path)
    if require_inventory:
        raise RuntimeError(
            "REQUIRE_WORKER_INVENTORY is set but WORKER_INVENTORY_PATH is empty. "
            "Mount an inventory JSON and set WORKER_INVENTORY_PATH "
            "(Compose: WORKER_INVENTORY_FILE → /etc/computer/workers.json)."
        )
    return load_legacy_env()
