"""File-based multi-tenant API keys with soft seat allowlists.

When TENANTS_PATH is unset, the gateway stays OSS single-token mode
(COMPUTER_API_TOKEN). When set, Bearer must match a tenant key or the ops token.

The registry can reload and atomically upsert tenants (billing provision) when
backed by a path — in-process lock + flock + write-temp-rename.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger("computer.gateway.tenancy")


@dataclass(frozen=True)
class Tenant:
    id: str
    api_keys: frozenset[str]
    seat_ids: frozenset[str]
    max_concurrent_leases: int  # leased+parked held by this tenant
    max_lease_ttl_sec: int | None  # clamp request TTL; None = no extra clamp
    max_recording_bytes: int | None  # recording storage quota; None = unlimited
    # Optional billing / signup fields (auth remains api_keys only)
    email: str | None = None
    plan: str | None = None
    stripe_customer_id: str | None = None
    stripe_subscription_id: str | None = None
    api_key_revealed_at: str | None = None  # ISO-8601 when key was shown once


@dataclass(frozen=True)
class TenantContext:
    """Resolved caller after Bearer auth."""

    id: str | None
    seat_ids: frozenset[str] | None  # None = all seats (ops / OSS)
    is_ops: bool = False
    max_concurrent_leases: int | None = None
    max_lease_ttl_sec: int | None = None

    def may_use_seat(self, seat_id: str) -> bool:
        if self.is_ops or self.seat_ids is None:
            return True
        return (seat_id or "") in self.seat_ids

    def allowed_seat_ids(self) -> set[str] | None:
        """None = no filter; empty set = deny all claims; else allowlist."""
        if self.is_ops or self.seat_ids is None:
            return None
        return set(self.seat_ids)

    def clamp_ttl_sec(self, ttl_sec: int | None) -> int | None:
        """Apply plan max_lease_ttl_sec when set; None input stays None (pool default)."""
        if self.is_ops or self.max_lease_ttl_sec is None:
            return ttl_sec
        cap = max(30, int(self.max_lease_ttl_sec))
        if ttl_sec is None:
            return cap
        return min(max(30, int(ttl_sec)), cap)


def filter_seats(ctx: TenantContext, seats: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if ctx.is_ops or ctx.seat_ids is None:
        return seats
    return [s for s in seats if (s.get("id") or s.get("seat_id") or "") in ctx.seat_ids]


def _opt_str(row: dict[str, Any], key: str) -> str | None:
    raw = row.get(key)
    if raw is None or raw == "":
        return None
    return str(raw).strip() or None


def _parse_tenant_row(row: dict[str, Any], index: int) -> Tenant:
    if not isinstance(row, dict):
        raise ValueError(f"tenants[{index}] must be an object")
    tid = str(row.get("id") or "").strip()
    if not tid:
        raise ValueError(f"tenants[{index}].id is required")
    keys_raw = row.get("api_keys")
    if keys_raw is None:
        keys_raw = []
    if not isinstance(keys_raw, list):
        raise ValueError(f"tenants[{index}].api_keys must be a list")
    keys = frozenset(str(k).strip() for k in keys_raw if str(k).strip())
    seats_raw = row.get("seat_ids")
    if seats_raw is None:
        seats_raw = []
    if not isinstance(seats_raw, list):
        raise ValueError(f"tenants[{index}].seat_ids must be a list")
    seats = frozenset(str(s).strip() for s in seats_raw if str(s).strip())
    max_conc = row.get("max_concurrent_leases")
    if max_conc is None or max_conc == "":
        concurrent = max(1, len(seats)) if seats else 1
    else:
        try:
            concurrent = int(max_conc)
        except (TypeError, ValueError) as e:
            raise ValueError(
                f"tenants[{index}].max_concurrent_leases must be an int"
            ) from e
        if concurrent < 1:
            raise ValueError(
                f"tenants[{index}].max_concurrent_leases must be >= 1"
            )
    max_ttl_raw = row.get("max_lease_ttl_sec")
    max_ttl: int | None
    if max_ttl_raw is None or max_ttl_raw == "":
        max_ttl = None
    else:
        try:
            max_ttl = int(max_ttl_raw)
        except (TypeError, ValueError) as e:
            raise ValueError(
                f"tenants[{index}].max_lease_ttl_sec must be an int"
            ) from e
        if max_ttl < 30:
            raise ValueError(
                f"tenants[{index}].max_lease_ttl_sec must be >= 30"
            )
    max_rec_raw = row.get("max_recording_bytes")
    max_rec: int | None
    if max_rec_raw is None or max_rec_raw == "":
        max_rec = None
    else:
        try:
            max_rec = int(max_rec_raw)
        except (TypeError, ValueError) as e:
            raise ValueError(
                f"tenants[{index}].max_recording_bytes must be an int"
            ) from e
        if max_rec < 1:
            raise ValueError(
                f"tenants[{index}].max_recording_bytes must be >= 1"
            )
    return Tenant(
        id=tid,
        api_keys=keys,
        seat_ids=seats,
        max_concurrent_leases=concurrent,
        max_lease_ttl_sec=max_ttl,
        max_recording_bytes=max_rec,
        email=_opt_str(row, "email"),
        plan=_opt_str(row, "plan"),
        stripe_customer_id=_opt_str(row, "stripe_customer_id"),
        stripe_subscription_id=_opt_str(row, "stripe_subscription_id"),
        api_key_revealed_at=_opt_str(row, "api_key_revealed_at"),
    )


def _parse_tenants_document(raw: Any) -> list[Tenant]:
    if not isinstance(raw, dict) or not isinstance(raw.get("tenants"), list):
        raise ValueError('tenants file must be {"tenants": [...]}')
    tenants: list[Tenant] = []
    for i, row in enumerate(raw["tenants"]):
        tenants.append(_parse_tenant_row(row, i))
    return tenants


def _tenant_to_row(t: Tenant) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": t.id,
        "api_keys": sorted(t.api_keys),
        "seat_ids": sorted(t.seat_ids),
        "max_concurrent_leases": t.max_concurrent_leases,
    }
    if t.max_lease_ttl_sec is not None:
        row["max_lease_ttl_sec"] = t.max_lease_ttl_sec
    if t.max_recording_bytes is not None:
        row["max_recording_bytes"] = t.max_recording_bytes
    if t.email:
        row["email"] = t.email
    if t.plan:
        row["plan"] = t.plan
    if t.stripe_customer_id:
        row["stripe_customer_id"] = t.stripe_customer_id
    if t.stripe_subscription_id:
        row["stripe_subscription_id"] = t.stripe_subscription_id
    if t.api_key_revealed_at:
        row["api_key_revealed_at"] = t.api_key_revealed_at
    return row


def _index_tenants(tenants: list[Tenant]) -> dict[str, Tenant]:
    by_key: dict[str, Tenant] = {}
    for t in tenants:
        for key in t.api_keys:
            if key in by_key:
                raise ValueError(
                    f"duplicate api_key for tenants {by_key[key].id!r} and {t.id!r}"
                )
            by_key[key] = t
    return by_key


@contextmanager
def _flock(lock_path: Path) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


class TenantRegistry:
    def __init__(
        self, tenants: list[Tenant], *, path: Path | None = None
    ) -> None:
        self._path = Path(path) if path is not None else None
        self._lock = threading.RLock()
        self._tenants = list(tenants)
        self._by_key = _index_tenants(self._tenants)
        self._by_id = {t.id: t for t in self._tenants}
        if len(self._by_id) != len(self._tenants):
            seen: set[str] = set()
            for t in self._tenants:
                if t.id in seen:
                    raise ValueError(f"duplicate tenant id {t.id!r}")
                seen.add(t.id)

    @classmethod
    def from_path(cls, path: str | Path) -> TenantRegistry:
        p = Path(path)
        if not p.is_file():
            raise FileNotFoundError(f"TENANTS_PATH not found: {p}")
        raw = json.loads(p.read_text(encoding="utf-8"))
        tenants = _parse_tenants_document(raw)
        reg = cls(tenants, path=p)
        logger.info(
            "tenancy loaded: %d tenants, %d api keys path=%s",
            len(tenants),
            len(reg._by_key),
            p,
        )
        return reg

    @property
    def path(self) -> Path | None:
        return self._path

    @property
    def enabled(self) -> bool:
        return True

    def _replace_tenants_unlocked(self, tenants: list[Tenant]) -> None:
        by_key = _index_tenants(tenants)
        by_id = {t.id: t for t in tenants}
        if len(by_id) != len(tenants):
            seen: set[str] = set()
            for t in tenants:
                if t.id in seen:
                    raise ValueError(f"duplicate tenant id {t.id!r}")
                seen.add(t.id)
        self._tenants = list(tenants)
        self._by_key = by_key
        self._by_id = by_id

    def _lock_path(self) -> Path:
        if self._path is None:
            raise RuntimeError("TenantRegistry has no path; cannot lock/write")
        return self._path.with_name(self._path.name + ".lock")

    def _atomic_write_unlocked(self, tenants: list[Tenant]) -> None:
        if self._path is None:
            raise RuntimeError("TenantRegistry has no path; cannot write")
        doc = {"tenants": [_tenant_to_row(t) for t in tenants]}
        text = json.dumps(doc, indent=2, sort_keys=False) + "\n"
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, self._path)

    def reload(self) -> None:
        """Re-read tenants from disk into this registry (live without process restart)."""
        if self._path is None:
            raise RuntimeError("TenantRegistry has no path; cannot reload")
        with self._lock:
            with _flock(self._lock_path()):
                if not self._path.is_file():
                    raise FileNotFoundError(f"TENANTS_PATH not found: {self._path}")
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                tenants = _parse_tenants_document(raw)
                self._replace_tenants_unlocked(tenants)
                logger.info(
                    "tenancy reloaded: %d tenants, %d api keys path=%s",
                    len(tenants),
                    len(self._by_key),
                    self._path,
                )

    def claimed_seat_ids(self) -> set[str]:
        """Union of all tenant seat allowlists (for billing auto-claim)."""
        with self._lock:
            out: set[str] = set()
            for t in self._tenants:
                out |= set(t.seat_ids)
            return out

    def get(self, tenant_id: str) -> Tenant | None:
        with self._lock:
            return self._by_id.get(tenant_id)

    def get_by_stripe_customer(self, customer_id: str) -> Tenant | None:
        cid = (customer_id or "").strip()
        if not cid:
            return None
        with self._lock:
            for t in self._tenants:
                if t.stripe_customer_id == cid:
                    return t
            return None

    def upsert_tenant(self, tenant: Tenant, *, allow_seat_steal: bool = False) -> Tenant:
        """Insert or replace a tenant by id; persist atomically when path is set.

        Raises ValueError if another tenant already claims one of tenant.seat_ids
        (unless allow_seat_steal). In-memory-only registries (no path) update RAM only.
        """
        with self._lock:
            if self._path is not None:
                with _flock(self._lock_path()):
                    # Re-read so we merge against the latest file
                    if self._path.is_file():
                        raw = json.loads(self._path.read_text(encoding="utf-8"))
                        current = _parse_tenants_document(raw)
                    else:
                        current = list(self._tenants)
                    return self._upsert_into(current, tenant, allow_seat_steal=allow_seat_steal)
            return self._upsert_into(
                list(self._tenants), tenant, allow_seat_steal=allow_seat_steal
            )

    def _upsert_into(
        self,
        current: list[Tenant],
        tenant: Tenant,
        *,
        allow_seat_steal: bool,
    ) -> Tenant:
        tid = (tenant.id or "").strip()
        if not tid:
            raise ValueError("tenant.id is required")
        tenant = replace(tenant, id=tid)

        by_id = {t.id: t for t in current}
        for other in current:
            if other.id == tid:
                continue
            overlap = other.seat_ids & tenant.seat_ids
            if overlap and not allow_seat_steal:
                raise ValueError(
                    f"seats already claimed by tenant {other.id!r}: {sorted(overlap)}"
                )
            key_clash = other.api_keys & tenant.api_keys
            if key_clash:
                raise ValueError(
                    f"api_key already used by tenant {other.id!r}"
                )

        by_id[tid] = tenant
        updated = list(by_id.values())
        # Stable-ish order: existing order, new ids appended
        ordered: list[Tenant] = []
        seen: set[str] = set()
        for t in current:
            if t.id in by_id and t.id not in seen:
                ordered.append(by_id[t.id])
                seen.add(t.id)
        if tid not in seen:
            ordered.append(tenant)
        self._replace_tenants_unlocked(ordered)
        if self._path is not None:
            self._atomic_write_unlocked(ordered)
            logger.info(
                "tenancy upsert tenant=%s plan=%s seats=%s path=%s",
                tid,
                tenant.plan or "-",
                sorted(tenant.seat_ids),
                self._path,
            )
        return tenant

    def authenticate_key(self, token: str) -> TenantContext | None:
        with self._lock:
            t = self._by_key.get(token)
            if not t:
                return None
            return TenantContext(
                id=t.id,
                seat_ids=t.seat_ids,
                is_ops=False,
                max_concurrent_leases=t.max_concurrent_leases,
                max_lease_ttl_sec=t.max_lease_ttl_sec,
            )

    def tenant_ids(self) -> list[str]:
        with self._lock:
            return [t.id for t in self._tenants]

    def iter_tenants(self) -> list[Tenant]:
        with self._lock:
            return list(self._tenants)


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
