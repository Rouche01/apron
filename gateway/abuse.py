"""Hosted abuse controls: sliding-window rate limits + env deny-list helpers.

Active when tenancy is on (wired in app.py). This module stays FastAPI-free.
"""

from __future__ import annotations

import os
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Deque


DEFAULT_RPM = 120
DEFAULT_OBSERVE_RPM = 60
DEFAULT_BILLING_RPM = 20
WINDOW_SEC = 60.0

BUCKET_GENERAL = "general"
BUCKET_OBSERVE = "observe"
BUCKET_BILLING = "billing"


def _env_nonneg_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        return default


@dataclass(frozen=True)
class RateLimitConfig:
    """Per-bucket requests-per-minute; 0 disables that bucket."""

    general_rpm: int = DEFAULT_RPM
    observe_rpm: int = DEFAULT_OBSERVE_RPM
    billing_rpm: int = DEFAULT_BILLING_RPM
    window_sec: float = WINDOW_SEC

    @classmethod
    def from_env(cls) -> RateLimitConfig:
        return cls(
            general_rpm=_env_nonneg_int("ABUSE_RATE_LIMIT_RPM", DEFAULT_RPM),
            observe_rpm=_env_nonneg_int(
                "ABUSE_RATE_LIMIT_OBSERVE_RPM", DEFAULT_OBSERVE_RPM
            ),
            billing_rpm=_env_nonneg_int(
                "ABUSE_RATE_LIMIT_BILLING_RPM", DEFAULT_BILLING_RPM
            ),
        )

    def limit_for(self, bucket: str) -> int:
        if bucket == BUCKET_OBSERVE:
            return self.observe_rpm
        if bucket == BUCKET_BILLING:
            return self.billing_rpm
        return self.general_rpm


def disabled_tenant_ids_from_env(
    raw: str | None = None,
) -> frozenset[str]:
    """Parse ABUSE_DISABLED_TENANT_IDS (comma-separated) into a frozenset."""
    if raw is None:
        raw = os.environ.get("ABUSE_DISABLED_TENANT_IDS", "")
    ids = {(p or "").strip() for p in str(raw).split(",")}
    return frozenset(x for x in ids if x)


def is_tenant_denied(
    tenant_id: str | None,
    *,
    disabled_ids: frozenset[str] | None = None,
) -> bool:
    """True if tenant_id is on the env deny list."""
    tid = (tenant_id or "").strip()
    if not tid:
        return False
    denied = (
        disabled_ids
        if disabled_ids is not None
        else disabled_tenant_ids_from_env()
    )
    return tid in denied


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    retry_after_sec: float | None = None


class SlidingWindowLimiter:
    """In-process sliding window keyed by (bucket, key).

    Thread-safe. limit<=0 for a bucket means that bucket is disabled (always allow).
    """

    def __init__(
        self,
        config: RateLimitConfig | None = None,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._config = config or RateLimitConfig()
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._hits: dict[tuple[str, str], Deque[float]] = defaultdict(deque)

    @property
    def config(self) -> RateLimitConfig:
        return self._config

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()

    def check(self, bucket: str, key: str) -> RateLimitResult:
        """Record one hit if allowed; return allow/deny + remaining."""
        limit = self._config.limit_for(bucket)
        if limit <= 0:
            return RateLimitResult(allowed=True, limit=0, remaining=0)

        kid = (key or "").strip() or "_"
        window = float(self._config.window_sec)
        now = float(self._clock())
        cutoff = now - window

        with self._lock:
            q = self._hits[(bucket, kid)]
            while q and q[0] <= cutoff:
                q.popleft()
            if len(q) >= limit:
                retry = max(0.0, (q[0] + window) - now) if q else window
                return RateLimitResult(
                    allowed=False,
                    limit=limit,
                    remaining=0,
                    retry_after_sec=retry,
                )
            q.append(now)
            remaining = max(0, limit - len(q))
            return RateLimitResult(
                allowed=True, limit=limit, remaining=remaining
            )


def observe_path(path: str) -> bool:
    """True for screenshot / observe / action control-plane paths."""
    p = (path or "").rstrip("/")
    return (
        p.endswith("/screenshot")
        or p.endswith("/observe")
        or p.endswith("/action")
    )


def billing_public_path(path: str) -> bool:
    """Public billing routes that should be IP-limited (not webhook)."""
    p = (path or "").rstrip("/")
    if p == "/v1/billing/checkout":
        return True
    if p == "/v1/billing/plans":
        return True
    if p.startswith("/v1/billing/checkout/"):
        return True
    return False
