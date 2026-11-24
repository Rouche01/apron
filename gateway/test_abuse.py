"""Abuse module: sliding-window limiter + env deny-list (no FastAPI)."""

from __future__ import annotations

import pytest

from abuse import (
    BUCKET_BILLING,
    BUCKET_GENERAL,
    BUCKET_OBSERVE,
    RateLimitConfig,
    SlidingWindowLimiter,
    billing_public_path,
    disabled_tenant_ids_from_env,
    is_tenant_denied,
    observe_path,
)


def test_disabled_tenant_ids_from_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("ABUSE_DISABLED_TENANT_IDS", raising=False)
    assert disabled_tenant_ids_from_env() == frozenset()

    monkeypatch.setenv("ABUSE_DISABLED_TENANT_IDS", " cus_a, cus_b , ,cus_a ")
    assert disabled_tenant_ids_from_env() == frozenset({"cus_a", "cus_b"})

    assert disabled_tenant_ids_from_env("x,y") == frozenset({"x", "y"})
    assert is_tenant_denied("cus_a", disabled_ids=frozenset({"cus_a"}))
    assert not is_tenant_denied("cus_z", disabled_ids=frozenset({"cus_a"}))
    assert not is_tenant_denied(None, disabled_ids=frozenset({"cus_a"}))
    assert not is_tenant_denied("", disabled_ids=frozenset({"cus_a"}))


def test_rate_limit_config_from_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("ABUSE_RATE_LIMIT_RPM", raising=False)
    monkeypatch.delenv("ABUSE_RATE_LIMIT_OBSERVE_RPM", raising=False)
    monkeypatch.delenv("ABUSE_RATE_LIMIT_BILLING_RPM", raising=False)
    cfg = RateLimitConfig.from_env()
    assert cfg.general_rpm == 120
    assert cfg.observe_rpm == 60
    assert cfg.billing_rpm == 20

    monkeypatch.setenv("ABUSE_RATE_LIMIT_RPM", "10")
    monkeypatch.setenv("ABUSE_RATE_LIMIT_OBSERVE_RPM", "0")
    monkeypatch.setenv("ABUSE_RATE_LIMIT_BILLING_RPM", "-5")
    cfg2 = RateLimitConfig.from_env()
    assert cfg2.general_rpm == 10
    assert cfg2.observe_rpm == 0
    assert cfg2.billing_rpm == 0  # clamped


def test_sliding_window_allows_then_denies():
    clock = {"t": 1000.0}

    def now() -> float:
        return clock["t"]

    lim = SlidingWindowLimiter(
        RateLimitConfig(general_rpm=3, observe_rpm=2, billing_rpm=1, window_sec=60.0),
        clock=now,
    )

    for _ in range(3):
        r = lim.check(BUCKET_GENERAL, "tenant-a")
        assert r.allowed
    denied = lim.check(BUCKET_GENERAL, "tenant-a")
    assert not denied.allowed
    assert denied.remaining == 0
    assert denied.limit == 3
    assert denied.retry_after_sec is not None and denied.retry_after_sec > 0

    # Other tenant / bucket independent
    assert lim.check(BUCKET_GENERAL, "tenant-b").allowed
    assert lim.check(BUCKET_OBSERVE, "tenant-a").allowed

    # Window slide frees a slot
    clock["t"] += 61.0
    assert lim.check(BUCKET_GENERAL, "tenant-a").allowed


def test_limit_zero_disables_bucket():
    lim = SlidingWindowLimiter(
        RateLimitConfig(general_rpm=0, observe_rpm=0, billing_rpm=0)
    )
    for _ in range(50):
        assert lim.check(BUCKET_GENERAL, "x").allowed
        assert lim.check(BUCKET_OBSERVE, "x").allowed
        assert lim.check(BUCKET_BILLING, "1.2.3.4").allowed


def test_path_helpers():
    assert observe_path("/v1/sessions/abc/screenshot")
    assert observe_path("/v1/sessions/abc/observe/")
    assert observe_path("/v1/sessions/abc/action")
    assert not observe_path("/v1/sessions")
    assert not observe_path("/v1/sessions/abc/heartbeat")

    assert billing_public_path("/v1/billing/checkout")
    assert billing_public_path("/v1/billing/plans")
    assert billing_public_path("/v1/billing/checkout/cs_123")
    assert not billing_public_path("/v1/billing/webhook")
    assert not billing_public_path("/v1/billing/portal")
