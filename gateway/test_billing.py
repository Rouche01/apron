"""Billing module: plans, webhook HMAC, seat claim, provision (no routes yet)."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from pathlib import Path

import httpx
import pytest

from billing import (
    BillingDisabled,
    InsufficientSeats,
    StripeClient,
    WebhookSignatureError,
    claim_desktop_seats,
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
from tenancy import TenantRegistry


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
