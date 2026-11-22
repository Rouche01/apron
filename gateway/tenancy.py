"""File-based multi-tenant API keys with soft seat allowlists.

When TENANTS_PATH is unset, the gateway stays OSS single-token mode
(COMPUTER_API_TOKEN). When set, Bearer must match a tenant key or the ops token.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("computer.gateway.tenancy")


@dataclass(frozen=True)
class Tenant:
    id: str
    api_keys: frozenset[str]
    seat_ids: frozenset[str]


@dataclass(frozen=True)
class TenantContext:
    """Resolved caller after Bearer auth."""

    id: str | None
    seat_ids: frozenset[str] | None  # None = all seats (ops / OSS)
    is_ops: bool = False

    def may_use_seat(self, seat_id: str) -> bool:
        if self.is_ops or self.seat_ids is None:
            return True
        return (seat_id or "") in self.seat_ids

    def allowed_seat_ids(self) -> set[str] | None:
        """None = no filter; empty set = deny all claims; else allowlist."""
        if self.is_ops or self.seat_ids is None:
            return None
        return set(self.seat_ids)


def filter_seats(ctx: TenantContext, seats: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if ctx.is_ops or ctx.seat_ids is None:
        return seats
    return [s for s in seats if (s.get("id") or s.get("seat_id") or "") in ctx.seat_ids]


class TenantRegistry:
    def __init__(self, tenants: list[Tenant]) -> None:
        self._tenants = list(tenants)
        self._by_key: dict[str, Tenant] = {}
        for t in self._tenants:
            for key in t.api_keys:
                if key in self._by_key:
                    raise ValueError(f"duplicate api_key for tenants {self._by_key[key].id!r} and {t.id!r}")
                self._by_key[key] = t

    @classmethod
    def from_path(cls, path: str | Path) -> TenantRegistry:
        p = Path(path)
        if not p.is_file():
            raise FileNotFoundError(f"TENANTS_PATH not found: {p}")
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or not isinstance(raw.get("tenants"), list):
            raise ValueError("tenants file must be {\"tenants\": [...]}")
        tenants: list[Tenant] = []
        for i, row in enumerate(raw["tenants"]):
            if not isinstance(row, dict):
                raise ValueError(f"tenants[{i}] must be an object")
            tid = str(row.get("id") or "").strip()
            if not tid:
                raise ValueError(f"tenants[{i}].id is required")
            keys_raw = row.get("api_keys") or []
            if not isinstance(keys_raw, list) or not keys_raw:
                raise ValueError(f"tenants[{i}].api_keys must be a non-empty list")
            keys = frozenset(str(k).strip() for k in keys_raw if str(k).strip())
            if not keys:
                raise ValueError(f"tenants[{i}].api_keys has no non-empty keys")
            seats_raw = row.get("seat_ids") or []
            if not isinstance(seats_raw, list) or not seats_raw:
                raise ValueError(f"tenants[{i}].seat_ids must be a non-empty list")
            seats = frozenset(str(s).strip() for s in seats_raw if str(s).strip())
            if not seats:
                raise ValueError(f"tenants[{i}].seat_ids has no non-empty ids")
            tenants.append(Tenant(id=tid, api_keys=keys, seat_ids=seats))
        if not tenants:
            raise ValueError("tenants file has no tenants")
        reg = cls(tenants)
        logger.info("tenancy loaded: %d tenants, %d api keys", len(tenants), len(reg._by_key))
        return reg

    @property
    def enabled(self) -> bool:
        return True

    def authenticate_key(self, token: str) -> TenantContext | None:
        t = self._by_key.get(token)
        if not t:
            return None
        return TenantContext(id=t.id, seat_ids=t.seat_ids, is_ops=False)

    def tenant_ids(self) -> list[str]:
        return [t.id for t in self._tenants]


def load_registry_from_env() -> TenantRegistry | None:
    path = os.environ.get("TENANTS_PATH", "").strip()
    if not path:
        return None
    return TenantRegistry.from_path(path)


def oss_context() -> TenantContext:
    """Full-access context when tenancy is off (or ops)."""
    return TenantContext(id=None, seat_ids=None, is_ops=True)


def ops_context() -> TenantContext:
    return TenantContext(id="ops", seat_ids=None, is_ops=True)
