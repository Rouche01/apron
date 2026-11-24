"""Billing module + /v1/billing routes."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from billing import (
    DEFAULT_RECORDING_BYTES,
    BillingDisabled,
    InsufficientSeats,
    StripeClient,
    WebhookSignatureError,
    claim_desktop_seats,
    default_recording_bytes,
    generate_api_key,
    get_plan,
    list_plans,
    load_config_from_env,
    mark_api_key_revealed,
    parse_webhook_event,
    pick_desktop_seats,
    provision_from_checkout_session,
    require_config,
    resize_tenant_plan,
    suspend_tenant,
    verify_webhook_signature,
)
from inventory import WorkerSpec
from pool import SessionPool
from recording import RecordingStore
from tenancy import TenantRegistry

_GATEWAY_DIR = Path(__file__).resolve().parent
os.environ.setdefault("WORKER_INVENTORY_PATH", str(_GATEWAY_DIR / "workers.json"))
os.environ.setdefault("REQUIRE_WORKER_INVENTORY", "1")
os.environ.setdefault("COMPUTER_API_TOKEN", "")
os.environ.setdefault(
    "RECORDING_DIR", str(_GATEWAY_DIR / ".pytest_recordings_billing")
)


def test_plans_and_key_prefix():
    assert {p["id"] for p in list_plans()} == {"starter", "pro"}
    starter = get_plan("starter")
    assert starter.seats == 1 and starter.max_concurrent_leases == 1
    pro = get_plan("PRO")
    assert pro.seats == 2
    key = generate_api_key()
    assert key.startswith("cwa_") and len(key) > 10


def test_load_config_requires_full_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    assert load_config_from_env() is None
    with pytest.raises(BillingDisabled):
        require_config()

    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test")
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
    assert load_config_from_env() is None

    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    monkeypatch.setenv("BILLING_SUCCESS_URL", "https://ex/ok?sid={CHECKOUT_SESSION_ID}")
    monkeypatch.setenv("BILLING_CANCEL_URL", "https://ex/cancel")
    monkeypatch.setenv("BILLING_PORTAL_RETURN_URL", "https://ex/account")
    cfg = load_config_from_env()
    assert cfg is not None and cfg.secret_key == "sk_test"


def test_pick_desktop_seats_prefers_free():
    seats = [
        {"id": "desk2", "kind": "desktop", "state": "leased"},
        {"id": "desk1", "kind": "desktop", "state": "free"},
        {"id": "phone1", "kind": "phone", "state": "free"},
        {"id": "burst1", "kind": "desktop", "state": "free", "ephemeral": True},
        {"id": "desk3", "kind": "desktop", "state": "free"},
    ]
    picked = pick_desktop_seats(seats, claimed=set(), n=2)
    assert picked == ["desk1", "desk3"]  # free first, sorted

    with pytest.raises(InsufficientSeats):
        pick_desktop_seats(seats, claimed={"desk1", "desk3"}, n=2)

    # Can still claim leased inventory seat when nothing free left
    only = pick_desktop_seats(seats, claimed={"desk1", "desk3"}, n=1)
    assert only == ["desk2"]


def _sign(payload: bytes, secret: str, ts: int | None = None) -> str:
    t = int(time.time()) if ts is None else ts
    signed = f"{t}.".encode() + payload
    sig = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={t},v1={sig}"


def test_webhook_signature():
    payload = b'{"type":"checkout.session.completed","data":{"object":{}}}'
    secret = "whsec_test"
    header = _sign(payload, secret)
    verify_webhook_signature(payload, header, secret=secret)
    event = parse_webhook_event(payload, header, secret=secret)
    assert event["type"] == "checkout.session.completed"

    with pytest.raises(WebhookSignatureError):
        verify_webhook_signature(payload, "t=1,v1=deadbeef", secret=secret)

    with pytest.raises(WebhookSignatureError):
        verify_webhook_signature(payload, None, secret=secret)

    old = _sign(payload, secret, ts=int(time.time()) - 10_000)
    with pytest.raises(WebhookSignatureError, match="tolerance"):
        verify_webhook_signature(payload, old, secret=secret)


def test_provision_and_suspend(tmp_path: Path):
    path = tmp_path / "tenants.json"
    path.write_text(json.dumps({"tenants": []}), encoding="utf-8")
    reg = TenantRegistry.from_path(path)
    seats = [
        {"id": "desk1", "kind": "desktop", "state": "free"},
        {"id": "desk2", "kind": "desktop", "state": "free"},
    ]

    session = {
        "id": "cs_test_1",
        "customer": "cus_acme",
        "subscription": "sub_1",
        "customer_email": "a@example.com",
        "metadata": {"plan": "starter"},
    }
    tenant = provision_from_checkout_session(
        session, registry=reg, seats_snapshot=seats
    )
    assert tenant.id == "cus_acme"
    assert tenant.plan == "starter"
    assert tenant.seat_ids == frozenset({"desk1"})
    assert tenant.max_recording_bytes == DEFAULT_RECORDING_BYTES
    assert len(tenant.api_keys) == 1
    assert next(iter(tenant.api_keys)).startswith("cwa_")
    assert reg.authenticate_key(next(iter(tenant.api_keys))) is not None

    # Idempotent
    again = provision_from_checkout_session(
        session, registry=reg, seats_snapshot=seats
    )
    assert again.api_keys == tenant.api_keys
    assert reg.claimed_seat_ids() == {"desk1"}

    # Undersupply for second customer
    session2 = {
        **session,
        "customer": "cus_beta",
        "customer_email": "b@example.com",
        "metadata": {"plan": "pro"},
    }
    with pytest.raises(InsufficientSeats):
        provision_from_checkout_session(
            session2, registry=reg, seats_snapshot=seats
        )
    assert reg.get_by_stripe_customer("cus_beta") is None

    suspended = suspend_tenant(reg, tenant)
    assert not suspended.api_keys and not suspended.seat_ids
    assert suspended.disabled is True
    assert suspended.stripe_customer_id == "cus_acme"
    assert reg.claimed_seat_ids() == set()


def test_resize_and_reveal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "tenants.json"
    path.write_text(json.dumps({"tenants": []}), encoding="utf-8")
    reg = TenantRegistry.from_path(path)
    seats = [
        {"id": "desk1", "kind": "desktop", "state": "free"},
        {"id": "desk2", "kind": "desktop", "state": "free"},
    ]
    monkeypatch.setenv("STRIPE_PRICE_STARTER", "price_s")
    monkeypatch.setenv("STRIPE_PRICE_PRO", "price_p")

    tenant = provision_from_checkout_session(
        {
            "customer": "cus_x",
            "subscription": "sub_x",
            "customer_email": "x@example.com",
            "metadata": {"plan": "starter"},
        },
        registry=reg,
        seats_snapshot=seats,
    )
    grown = resize_tenant_plan(
        reg, tenant, plan=get_plan("pro"), seats_snapshot=seats
    )
    assert grown.seat_ids == frozenset({"desk1", "desk2"})
    assert grown.plan == "pro"
    assert grown.max_concurrent_leases == 2

    shrunk = resize_tenant_plan(
        reg, grown, plan=get_plan("starter"), seats_snapshot=seats
    )
    assert len(shrunk.seat_ids) == 1
    assert shrunk.max_concurrent_leases == 1

    revealed = mark_api_key_revealed(reg, shrunk)
    assert revealed.api_key_revealed_at
    again = mark_api_key_revealed(reg, revealed)
    assert again.api_key_revealed_at == revealed.api_key_revealed_at


def test_stripe_client_checkout_and_portal(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec")
    monkeypatch.setenv("BILLING_SUCCESS_URL", "https://ex/ok")
    monkeypatch.setenv("BILLING_CANCEL_URL", "https://ex/cancel")
    monkeypatch.setenv("BILLING_PORTAL_RETURN_URL", "https://ex/account")
    monkeypatch.setenv("STRIPE_PRICE_STARTER", "price_starter")

    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("/checkout/sessions") and request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "id": "cs_123",
                    "url": "https://checkout.stripe.com/c/pay/cs_123",
                },
            )
        if request.url.path.endswith("/billing_portal/sessions"):
            return httpx.Response(
                200, json={"url": "https://billing.stripe.com/session/bp_1"}
            )
        if "/checkout/sessions/cs_123" in request.url.path:
            return httpx.Response(
                200,
                json={"id": "cs_123", "customer": "cus_1", "metadata": {"plan": "starter"}},
            )
        return httpx.Response(404, json={"error": {"message": "not found"}})

    transport = httpx.MockTransport(handler)
    client = httpx.Client(
        transport=transport,
        base_url="https://api.stripe.com/v1",
        auth=("sk_test", ""),
    )
    cfg = require_config()
    stripe = StripeClient(cfg, client=client)
    session = stripe.create_checkout_session(
        email="a@example.com", plan=get_plan("starter")
    )
    assert session["id"] == "cs_123" and "url" in session
    portal = stripe.create_portal_session(customer_id="cus_1")
    assert "billing.stripe.com" in portal["url"]
    got = stripe.retrieve_checkout_session("cs_123")
    assert got["customer"] == "cus_1"
    stripe.close()


def test_default_recording_bytes_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("BILLING_DEFAULT_RECORDING_BYTES", raising=False)
    assert default_recording_bytes() == DEFAULT_RECORDING_BYTES
    monkeypatch.setenv("BILLING_DEFAULT_RECORDING_BYTES", "0")
    assert default_recording_bytes() is None
    monkeypatch.setenv("BILLING_DEFAULT_RECORDING_BYTES", "1024")
    assert default_recording_bytes() == 1024


def test_provision_respects_recording_quota_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("BILLING_DEFAULT_RECORDING_BYTES", "0")
    path = tmp_path / "tenants.json"
    path.write_text(json.dumps({"tenants": []}), encoding="utf-8")
    reg = TenantRegistry.from_path(path)
    tenant = provision_from_checkout_session(
        {
            "customer": "cus_unlim",
            "subscription": "sub_u",
            "customer_email": "u@example.com",
            "metadata": {"plan": "starter"},
        },
        registry=reg,
        seats_snapshot=[{"id": "desk1", "kind": "desktop", "state": "free"}],
    )
    assert tenant.max_recording_bytes is None


def test_claim_uses_registry(tmp_path: Path):
    path = tmp_path / "tenants.json"
    path.write_text(
        json.dumps(
            {
                "tenants": [
                    {
                        "id": "acme",
                        "api_keys": ["k"],
                        "seat_ids": ["desk1"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    reg = TenantRegistry.from_path(path)
    seats = [
        {"id": "desk1", "kind": "desktop", "state": "free"},
        {"id": "desk2", "kind": "desktop", "state": "free"},
    ]
    assert claim_desktop_seats(registry=reg, seats_snapshot=seats, n=1) == ["desk2"]


# --- HTTP routes -------------------------------------------------------------


def _billing_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_billing")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_billing")
    monkeypatch.setenv(
        "BILLING_SUCCESS_URL",
        "https://ex.example/ok?sid={CHECKOUT_SESSION_ID}",
    )
    monkeypatch.setenv("BILLING_CANCEL_URL", "https://ex.example/cancel")
    monkeypatch.setenv("BILLING_PORTAL_RETURN_URL", "https://ex.example/account")
    monkeypatch.setenv("STRIPE_PRICE_STARTER", "price_starter")
    monkeypatch.setenv("STRIPE_PRICE_PRO", "price_pro")


@pytest.fixture
def billing_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _billing_env(monkeypatch)
    tenants_path = tmp_path / "tenants.json"
    tenants_path.write_text(json.dumps({"tenants": []}), encoding="utf-8")
    monkeypatch.setenv("TENANTS_PATH", str(tenants_path))

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
                url="http://worker-1:8000",
                vnc_url="http://localhost:6081/vnc.html",
            ),
            WorkerSpec(
                id="desk2",
                kind="desktop",
                url="http://worker-2:8000",
                vnc_url="http://localhost:6082/vnc.html",
            ),
        ],
        default_ttl_sec=600,
        parked_ttl_sec=1800,
    )
    monkeypatch.setattr(app_mod, "pool", fake)
    return TestClient(app_mod.app), reg, tenants_path


def test_billing_plans_and_checkout_disabled(billing_client, monkeypatch):
    tc, _reg, _path = billing_client
    plans = tc.get("/v1/billing/plans")
    assert plans.status_code == 200
    ids = {p["id"] for p in plans.json()["plans"]}
    assert ids == {"starter", "pro"}
    assert all(p["price_configured"] for p in plans.json()["plans"])

    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    r = tc.post(
        "/v1/billing/checkout",
        json={"email": "a@example.com", "plan": "starter"},
    )
    assert r.status_code == 404


def test_billing_checkout_creates_session(billing_client):
    tc, _reg, _path = billing_client

    mock_stripe = MagicMock()
    mock_stripe.create_checkout_session.return_value = {
        "id": "cs_route_1",
        "url": "https://checkout.stripe.com/c/pay/cs_route_1",
    }
    mock_stripe.close = MagicMock()

    with patch("app.StripeClient", return_value=mock_stripe):
        r = tc.post(
            "/v1/billing/checkout",
            json={"email": "a@example.com", "plan": "starter"},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["checkout_session_id"] == "cs_route_1"
    assert "checkout.stripe.com" in body["url"]
    mock_stripe.create_checkout_session.assert_called_once()


def test_billing_webhook_and_reveal_and_portal(billing_client):
    tc, reg, path = billing_client
    secret = "whsec_billing"

    session_obj = {
        "id": "cs_wh_1",
        "status": "complete",
        "customer": "cus_route",
        "subscription": "sub_route",
        "customer_email": "route@example.com",
        "metadata": {"plan": "starter"},
    }
    event = {
        "type": "checkout.session.completed",
        "data": {"object": session_obj},
    }
    payload = json.dumps(event).encode()
    sig = _sign(payload, secret)

    bad = tc.post(
        "/v1/billing/webhook",
        content=payload,
        headers={"Stripe-Signature": "t=1,v1=nope"},
    )
    assert bad.status_code == 400

    ok = tc.post(
        "/v1/billing/webhook",
        content=payload,
        headers={"Stripe-Signature": sig},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["tenant_id"] == "cus_route"
    tenant = reg.get_by_stripe_customer("cus_route")
    assert tenant is not None
    assert tenant.seat_ids == frozenset({"desk1"})
    api_key = next(iter(tenant.api_keys))

    # Tenant can lease only their seat
    auth = {"Authorization": f"Bearer {api_key}"}
    seats = tc.get("/v1/seats", headers=auth)
    assert seats.status_code == 200
    assert [s["id"] for s in seats.json()["seats"]] == ["desk1"]

    # Reveal once
    mock_stripe = MagicMock()
    mock_stripe.retrieve_checkout_session.return_value = session_obj
    mock_stripe.create_portal_session.return_value = {
        "url": "https://billing.stripe.com/session/bp_route"
    }
    mock_stripe.close = MagicMock()
    with patch("app.StripeClient", return_value=mock_stripe):
        first = tc.get("/v1/billing/checkout/cs_wh_1")
        assert first.status_code == 200, first.text
        assert first.json()["api_key"] == api_key
        assert first.json()["revealed"] is False

        second = tc.get("/v1/billing/checkout/cs_wh_1")
        assert second.status_code == 200
        assert second.json()["api_key"] is None
        assert second.json()["revealed"] is True

        portal = tc.post("/v1/billing/portal", headers=auth)
        assert portal.status_code == 200
        assert "billing.stripe.com" in portal.json()["url"]

    # Portal rejects missing/ops
    assert tc.post("/v1/billing/portal").status_code == 401
    ops = tc.post(
        "/v1/billing/portal",
        headers={"Authorization": "Bearer ops-token"},
    )
    assert ops.status_code == 400

    # Persist on disk
    disk = json.loads(path.read_text(encoding="utf-8"))
    assert disk["tenants"][0]["stripe_customer_id"] == "cus_route"
    assert disk["tenants"][0]["api_key_revealed_at"]


def test_billing_webhook_undersupply_500(billing_client):
    tc, reg, _path = billing_client
    # Claim both seats via first customer
    payload1 = json.dumps(
        {
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_a",
                    "customer": "cus_a",
                    "subscription": "sub_a",
                    "customer_email": "a@example.com",
                    "metadata": {"plan": "pro"},
                }
            },
        }
    ).encode()
    secret = "whsec_billing"
    r1 = tc.post(
        "/v1/billing/webhook",
        content=payload1,
        headers={"Stripe-Signature": _sign(payload1, secret)},
    )
    assert r1.status_code == 200
    assert reg.claimed_seat_ids() == {"desk1", "desk2"}

    payload2 = json.dumps(
        {
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_b",
                    "customer": "cus_b",
                    "subscription": "sub_b",
                    "customer_email": "b@example.com",
                    "metadata": {"plan": "starter"},
                }
            },
        }
    ).encode()
    r2 = tc.post(
        "/v1/billing/webhook",
        content=payload2,
        headers={"Stripe-Signature": _sign(payload2, secret)},
    )
    assert r2.status_code == 500
    assert reg.get_by_stripe_customer("cus_b") is None


def test_billing_checkout_bad_plan_and_reveal_before_provision(billing_client):
    tc, _reg, _path = billing_client
    bad = tc.post(
        "/v1/billing/checkout",
        json={"email": "a@example.com", "plan": "enterprise"},
    )
    assert bad.status_code == 400

    mock_stripe = MagicMock()
    mock_stripe.retrieve_checkout_session.return_value = {
        "id": "cs_early",
        "status": "complete",
        "customer": "cus_missing",
        "metadata": {"plan": "starter"},
    }
    mock_stripe.close = MagicMock()
    with patch("app.StripeClient", return_value=mock_stripe):
        early = tc.get("/v1/billing/checkout/cs_early")
    assert early.status_code == 404


def test_billing_webhook_resize_and_suspend(billing_client):
    tc, reg, _path = billing_client
    secret = "whsec_billing"

    provision = json.dumps(
        {
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_rs",
                    "customer": "cus_rs",
                    "subscription": "sub_rs",
                    "customer_email": "rs@example.com",
                    "metadata": {"plan": "starter"},
                }
            },
        }
    ).encode()
    assert (
        tc.post(
            "/v1/billing/webhook",
            content=provision,
            headers={"Stripe-Signature": _sign(provision, secret)},
        ).status_code
        == 200
    )
    assert reg.get("cus_rs") is not None
    assert reg.get("cus_rs").seat_ids == frozenset({"desk1"})
    api_key = next(iter(reg.get("cus_rs").api_keys))

    resize = json.dumps(
        {
            "type": "customer.subscription.updated",
            "data": {
                "object": {
                    "id": "sub_rs",
                    "customer": "cus_rs",
                    "status": "active",
                    "metadata": {"plan": "pro"},
                }
            },
        }
    ).encode()
    r = tc.post(
        "/v1/billing/webhook",
        content=resize,
        headers={"Stripe-Signature": _sign(resize, secret)},
    )
    assert r.status_code == 200, r.text
    assert reg.get("cus_rs").seat_ids == frozenset({"desk1", "desk2"})
    assert reg.get("cus_rs").plan == "pro"
    assert reg.get("cus_rs").max_concurrent_leases == 2

    deleted = json.dumps(
        {
            "type": "customer.subscription.deleted",
            "data": {
                "object": {
                    "id": "sub_rs",
                    "customer": "cus_rs",
                    "status": "canceled",
                }
            },
        }
    ).encode()
    d = tc.post(
        "/v1/billing/webhook",
        content=deleted,
        headers={"Stripe-Signature": _sign(deleted, secret)},
    )
    assert d.status_code == 200
    suspended = reg.get("cus_rs")
    assert suspended is not None
    assert not suspended.api_keys and not suspended.seat_ids
    assert suspended.disabled is True
    assert suspended.stripe_customer_id == "cus_rs"
    assert reg.authenticate_key(api_key) is None
    assert (
        tc.get("/v1/seats", headers={"Authorization": f"Bearer {api_key}"}).status_code
        == 401
    )


def test_billing_requires_tenancy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Billing config alone is not enough — TENANTS_PATH / registry required."""
    _billing_env(monkeypatch)
    monkeypatch.delenv("TENANTS_PATH", raising=False)

    import app as app_mod

    monkeypatch.setattr(app_mod, "tenants", None)
    monkeypatch.setattr(app_mod, "API_TOKEN", "ops-token")
    monkeypatch.setattr(
        app_mod, "recordings", RecordingStore(tmp_path / "rec", ttl_sec=3600)
    )
    tc = TestClient(app_mod.app)

    mock_stripe = MagicMock()
    mock_stripe.create_checkout_session.return_value = {
        "id": "cs_x",
        "url": "https://checkout.stripe.com/c/pay/cs_x",
    }
    mock_stripe.close = MagicMock()
    with patch("app.StripeClient", return_value=mock_stripe):
        r = tc.post(
            "/v1/billing/checkout",
            json={"email": "a@example.com", "plan": "starter"},
        )
    assert r.status_code == 503
    # Stripe should not be called when tenancy is missing
    mock_stripe.create_checkout_session.assert_not_called()
