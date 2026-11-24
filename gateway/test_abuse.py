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


def test_tenant_rate_limit_429(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import json
    import os
    from pathlib import Path as P

    from fastapi.testclient import TestClient

    from abuse import RateLimitConfig, SlidingWindowLimiter
    from inventory import WorkerSpec
    from pool import SessionPool
    from recording import RecordingStore

    _gw = P(__file__).resolve().parent
    os.environ.setdefault("WORKER_INVENTORY_PATH", str(_gw / "workers.json"))
    os.environ.setdefault("REQUIRE_WORKER_INVENTORY", "1")

    tenants_path = tmp_path / "tenants.json"
    tenants_path.write_text(
        json.dumps(
            {
                "tenants": [
                    {
                        "id": "acme",
                        "api_keys": ["key-acme"],
                        "seat_ids": ["desk1"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TENANTS_PATH", str(tenants_path))

    import app as app_mod
    import tenancy as tenancy_mod

    reg = tenancy_mod.TenantRegistry.from_path(tenants_path)
    monkeypatch.setattr(app_mod, "tenants", reg)
    monkeypatch.setattr(app_mod, "API_TOKEN", "ops-token")
    monkeypatch.setattr(
        app_mod,
        "rate_limiter",
        SlidingWindowLimiter(
            RateLimitConfig(general_rpm=2, observe_rpm=1, billing_rpm=20)
        ),
    )
    monkeypatch.setattr(
        app_mod, "recordings", RecordingStore(tmp_path / "rec", ttl_sec=3600)
    )
    fake = SessionPool(
        [
            WorkerSpec(
                id="desk1",
                kind="desktop",
                url="http://w1:8000",
                vnc_url="http://v1",
            )
        ],
        default_ttl_sec=600,
        parked_ttl_sec=1800,
    )
    monkeypatch.setattr(app_mod, "pool", fake)
    tc = TestClient(app_mod.app)
    auth = {"Authorization": "Bearer key-acme"}

    assert tc.get("/v1/seats", headers=auth).status_code == 200
    assert tc.get("/v1/seats", headers=auth).status_code == 200
    denied = tc.get("/v1/seats", headers=auth)
    assert denied.status_code == 429, denied.text
    assert denied.json()["detail"]["code"] == "rate_limited"
    assert denied.headers.get("retry-after")

    # Ops exempt
    assert (
        tc.get("/v1/seats", headers={"Authorization": "Bearer ops-token"}).status_code
        == 200
    )


def test_billing_ip_rate_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import json
    import os
    from pathlib import Path as P

    from fastapi.testclient import TestClient

    from abuse import RateLimitConfig, SlidingWindowLimiter
    from inventory import WorkerSpec
    from pool import SessionPool
    from recording import RecordingStore

    _gw = P(__file__).resolve().parent
    os.environ.setdefault("WORKER_INVENTORY_PATH", str(_gw / "workers.json"))
    os.environ.setdefault("REQUIRE_WORKER_INVENTORY", "1")

    tenants_path = tmp_path / "tenants.json"
    tenants_path.write_text(json.dumps({"tenants": []}), encoding="utf-8")
    monkeypatch.setenv("TENANTS_PATH", str(tenants_path))
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec")
    monkeypatch.setenv("BILLING_SUCCESS_URL", "https://ex/ok")
    monkeypatch.setenv("BILLING_CANCEL_URL", "https://ex/cancel")
    monkeypatch.setenv("BILLING_PORTAL_RETURN_URL", "https://ex/account")

    import app as app_mod
    import tenancy as tenancy_mod

    reg = tenancy_mod.TenantRegistry.from_path(tenants_path)
    monkeypatch.setattr(app_mod, "tenants", reg)
    monkeypatch.setattr(
        app_mod,
        "rate_limiter",
        SlidingWindowLimiter(
            RateLimitConfig(general_rpm=100, observe_rpm=100, billing_rpm=2)
        ),
    )
    monkeypatch.setattr(
        app_mod, "recordings", RecordingStore(tmp_path / "rec", ttl_sec=3600)
    )
    monkeypatch.setattr(
        app_mod,
        "pool",
        SessionPool(
            [
                WorkerSpec(
                    id="desk1",
                    kind="desktop",
                    url="http://w1:8000",
                    vnc_url="http://v1",
                )
            ],
            default_ttl_sec=600,
            parked_ttl_sec=1800,
        ),
    )
    tc = TestClient(app_mod.app)
    assert tc.get("/v1/billing/plans").status_code == 200
    assert tc.get("/v1/billing/plans").status_code == 200
    denied = tc.get("/v1/billing/plans")
    assert denied.status_code == 429
    assert denied.json()["detail"]["code"] == "rate_limited"
    assert denied.json()["detail"]["bucket"] == "billing"


def test_tenant_disabled_403(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """disabled flag and env deny-list → 403; ops Bearer still works."""
    import json
    import os
    from pathlib import Path as P

    from fastapi.testclient import TestClient

    from inventory import WorkerSpec
    from pool import SessionPool
    from recording import RecordingStore

    _gw = P(__file__).resolve().parent
    os.environ.setdefault("WORKER_INVENTORY_PATH", str(_gw / "workers.json"))
    os.environ.setdefault("REQUIRE_WORKER_INVENTORY", "1")

    tenants_path = tmp_path / "tenants.json"
    tenants_path.write_text(
        json.dumps(
            {
                "tenants": [
                    {
                        "id": "acme",
                        "api_keys": ["key-acme"],
                        "seat_ids": ["desk1"],
                        "disabled": True,
                    },
                    {
                        "id": "beta",
                        "api_keys": ["key-beta"],
                        "seat_ids": ["desk2"],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TENANTS_PATH", str(tenants_path))
    monkeypatch.setenv("ABUSE_DISABLED_TENANT_IDS", "beta")

    import app as app_mod
    import tenancy as tenancy_mod

    reg = tenancy_mod.TenantRegistry.from_path(tenants_path)
    monkeypatch.setattr(app_mod, "tenants", reg)
    monkeypatch.setattr(app_mod, "API_TOKEN", "ops-token")
    monkeypatch.setattr(
        app_mod, "recordings", RecordingStore(tmp_path / "rec", ttl_sec=3600)
    )
    fake = SessionPool(
        [
            WorkerSpec(
                id="desk1",
                kind="desktop",
                url="http://w1:8000",
                vnc_url="http://v1",
            ),
            WorkerSpec(
                id="desk2",
                kind="desktop",
                url="http://w2:8000",
                vnc_url="http://v2",
            ),
        ],
        default_ttl_sec=600,
        parked_ttl_sec=1800,
    )
    monkeypatch.setattr(app_mod, "pool", fake)
    tc = TestClient(app_mod.app)

    r = tc.get("/v1/seats", headers={"Authorization": "Bearer key-acme"})
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "tenant_disabled"

    r2 = tc.get("/v1/seats", headers={"Authorization": "Bearer key-beta"})
    assert r2.status_code == 403
    assert r2.json()["detail"]["code"] == "tenant_disabled"

    ops = tc.get("/v1/seats", headers={"Authorization": "Bearer ops-token"})
    assert ops.status_code == 200
    assert {s["id"] for s in ops.json()["seats"]} == {"desk1", "desk2"}
