"""Hosted billing: Stripe Checkout / Portal / webhooks + seat auto-claim.

Billing is off when STRIPE_SECRET_KEY is unset. No Stripe SDK — httpx + HMAC.
Routes live in app.py; this module is the edge logic (plans, Stripe I/O, provision).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

import httpx

from tenancy import Tenant, TenantRegistry

logger = logging.getLogger("computer.gateway.billing")

STRIPE_API_BASE = "https://api.stripe.com/v1"
WEBHOOK_TOLERANCE_SEC = 300
API_KEY_PREFIX = "cwa_"


class BillingError(Exception):
    """Base billing failure."""


class BillingDisabled(BillingError):
    """STRIPE_SECRET_KEY unset or incomplete config."""


class InsufficientSeats(BillingError):
    """Not enough unclaimed desktop inventory seats for the plan."""


class StripeAPIError(BillingError):
    """Stripe HTTP / API error."""


class WebhookSignatureError(BillingError):
    """Invalid or missing Stripe-Signature."""


@dataclass(frozen=True)
class PlanSpec:
    id: str
    seats: int
    max_concurrent_leases: int
    price_env: str

    @property
    def price_id(self) -> str | None:
        raw = os.environ.get(self.price_env, "").strip()
        return raw or None


PLANS: dict[str, PlanSpec] = {
    "starter": PlanSpec(
        id="starter",
        seats=1,
        max_concurrent_leases=1,
        price_env="STRIPE_PRICE_STARTER",
    ),
    "pro": PlanSpec(
        id="pro",
        seats=2,
        max_concurrent_leases=2,
        price_env="STRIPE_PRICE_PRO",
    ),
}


@dataclass(frozen=True)
class BillingConfig:
    secret_key: str
    webhook_secret: str
    success_url: str
    cancel_url: str
    portal_return_url: str
    api_base: str = STRIPE_API_BASE
    timeout_sec: float = 30.0


def billing_enabled() -> bool:
    return bool(os.environ.get("STRIPE_SECRET_KEY", "").strip())


def load_config_from_env() -> BillingConfig | None:
    """Return config when billing is usable; None when disabled."""
    secret = os.environ.get("STRIPE_SECRET_KEY", "").strip()
    if not secret:
        return None
    webhook = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
    success = os.environ.get("BILLING_SUCCESS_URL", "").strip()
    cancel = os.environ.get("BILLING_CANCEL_URL", "").strip()
    portal = os.environ.get("BILLING_PORTAL_RETURN_URL", "").strip()
    if not webhook or not success or not cancel or not portal:
        logger.warning(
            "STRIPE_SECRET_KEY set but billing incomplete "
            "(need STRIPE_WEBHOOK_SECRET, BILLING_SUCCESS_URL, "
            "BILLING_CANCEL_URL, BILLING_PORTAL_RETURN_URL)"
        )
        return None
    return BillingConfig(
        secret_key=secret,
        webhook_secret=webhook,
        success_url=success,
        cancel_url=cancel,
        portal_return_url=portal,
    )


def require_config() -> BillingConfig:
    cfg = load_config_from_env()
    if cfg is None:
        raise BillingDisabled("billing is not configured")
    return cfg


def get_plan(plan_id: str) -> PlanSpec:
    key = (plan_id or "").strip().lower()
    spec = PLANS.get(key)
    if spec is None:
        raise BillingError(f"unknown plan: {plan_id!r}")
    return spec


def list_plans() -> list[dict[str, Any]]:
    """Public plan matrix (no dollar amounts — price id presence only)."""
    rows: list[dict[str, Any]] = []
    for spec in PLANS.values():
        rows.append(
            {
                "id": spec.id,
                "seats": spec.seats,
                "max_concurrent_leases": spec.max_concurrent_leases,
                "price_configured": bool(spec.price_id),
            }
        )
    return rows


def generate_api_key() -> str:
    return API_KEY_PREFIX + secrets.token_urlsafe(24)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def pick_desktop_seats(
    seats_snapshot: list[dict[str, Any]],
    claimed: set[str],
    n: int,
) -> list[str]:
    """Choose N unclaimed non-ephemeral desktop seats; prefer state=free."""
    if n < 1:
        raise BillingError("seat claim count must be >= 1")
    candidates: list[dict[str, Any]] = []
    for s in seats_snapshot:
        sid = str(s.get("id") or "").strip()
        if not sid:
            continue
        if s.get("kind") != "desktop":
            continue
        if s.get("ephemeral"):
            continue
        if sid in claimed:
            continue
        candidates.append(s)

    free = [s for s in candidates if s.get("state") == "free"]
    busy = [s for s in candidates if s.get("state") != "free"]
    # Stable order within buckets
    free.sort(key=lambda s: str(s.get("id") or ""))
    busy.sort(key=lambda s: str(s.get("id") or ""))
    ordered = free + busy
    if len(ordered) < n:
        raise InsufficientSeats(
            f"need {n} unclaimed desktop seats, have {len(ordered)}"
        )
    return [str(s["id"]) for s in ordered[:n]]


def claim_desktop_seats(
    *,
    registry: TenantRegistry,
    seats_snapshot: list[dict[str, Any]],
    n: int,
) -> list[str]:
    return pick_desktop_seats(seats_snapshot, registry.claimed_seat_ids(), n)


class StripeClient:
    """Thin Stripe REST helper (Checkout, Portal, retrieve)."""

    def __init__(
        self,
        config: BillingConfig,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self._config = config
        self._client = client
        self._owns_client = client is None

    def _http(self) -> httpx.Client:
        if self._client is not None:
            return self._client
        self._client = httpx.Client(
            base_url=self._config.api_base,
            timeout=self._config.timeout_sec,
            auth=(self._config.secret_key, ""),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        return self._client

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    def _form_post(self, path: str, data: dict[str, str]) -> dict[str, Any]:
        try:
            r = self._http().post(path, content=urlencode(data))
        except httpx.HTTPError as e:
            raise StripeAPIError(f"stripe request failed: {e}") from e
        return self._parse_response(r)

    def _get(self, path: str) -> dict[str, Any]:
        try:
            r = self._http().get(path)
        except httpx.HTTPError as e:
            raise StripeAPIError(f"stripe request failed: {e}") from e
        return self._parse_response(r)

    @staticmethod
    def _parse_response(r: httpx.Response) -> dict[str, Any]:
        try:
            body = r.json()
        except ValueError as e:
            raise StripeAPIError(
                f"stripe non-json response status={r.status_code}"
            ) from e
        if r.status_code >= 400:
            err = body.get("error") if isinstance(body, dict) else None
            msg = ""
            if isinstance(err, dict):
                msg = str(err.get("message") or err.get("code") or "")
            raise StripeAPIError(
                msg or f"stripe error status={r.status_code}"
            )
        if not isinstance(body, dict):
            raise StripeAPIError("stripe response must be an object")
        return body

    def create_checkout_session(
        self, *, email: str, plan: PlanSpec
    ) -> dict[str, Any]:
        price = plan.price_id
        if not price:
            raise BillingError(
                f"plan {plan.id!r} has no Stripe price "
                f"(set {plan.price_env})"
            )
        email = (email or "").strip()
        if not email or "@" not in email:
            raise BillingError("valid email is required")
        data = {
            "mode": "subscription",
            "customer_email": email,
            "success_url": self._config.success_url,
            "cancel_url": self._config.cancel_url,
            "line_items[0][price]": price,
            "line_items[0][quantity]": "1",
            "metadata[plan]": plan.id,
            "subscription_data[metadata][plan]": plan.id,
        }
        session = self._form_post("/checkout/sessions", data)
        if not session.get("id") or not session.get("url"):
            raise StripeAPIError("checkout session missing id/url")
        return session

    def create_portal_session(self, *, customer_id: str) -> dict[str, Any]:
        cid = (customer_id or "").strip()
        if not cid:
            raise BillingError("stripe customer id required")
        data = {
            "customer": cid,
            "return_url": self._config.portal_return_url,
        }
        portal = self._form_post("/billing_portal/sessions", data)
        if not portal.get("url"):
            raise StripeAPIError("portal session missing url")
        return portal

    def retrieve_checkout_session(self, session_id: str) -> dict[str, Any]:
        sid = (session_id or "").strip()
        if not sid:
            raise BillingError("checkout session id required")
        return self._get(f"/checkout/sessions/{sid}")


def verify_webhook_signature(
    payload: bytes,
    sig_header: str | None,
    *,
    secret: str,
    tolerance_sec: int = WEBHOOK_TOLERANCE_SEC,
    now: float | None = None,
) -> None:
    """Raise WebhookSignatureError if Stripe-Signature is invalid."""
    if not sig_header:
        raise WebhookSignatureError("missing Stripe-Signature header")
    if not secret:
        raise WebhookSignatureError("webhook secret not configured")

    timestamp: str | None = None
    signatures: list[str] = []
    for part in sig_header.split(","):
        part = part.strip()
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        if k == "t":
            timestamp = v
        elif k == "v1":
            signatures.append(v)
    if not timestamp or not signatures:
        raise WebhookSignatureError("malformed Stripe-Signature header")

    try:
        ts = int(timestamp)
    except ValueError as e:
        raise WebhookSignatureError("invalid webhook timestamp") from e
    now_ts = time.time() if now is None else now
    if abs(now_ts - ts) > tolerance_sec:
        raise WebhookSignatureError("webhook timestamp outside tolerance")

    signed = f"{timestamp}.".encode("utf-8") + payload
    expected = hmac.new(
        secret.encode("utf-8"), signed, hashlib.sha256
    ).hexdigest()
    if not any(hmac.compare_digest(expected, s) for s in signatures):
        raise WebhookSignatureError("webhook signature mismatch")


def parse_webhook_event(
    payload: bytes,
    sig_header: str | None,
    *,
    secret: str,
    tolerance_sec: int = WEBHOOK_TOLERANCE_SEC,
    now: float | None = None,
) -> dict[str, Any]:
    verify_webhook_signature(
        payload,
        sig_header,
        secret=secret,
        tolerance_sec=tolerance_sec,
        now=now,
    )
    import json

    try:
        event = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise BillingError("webhook body is not valid JSON") from e
    if not isinstance(event, dict) or not event.get("type"):
        raise BillingError("webhook event missing type")
    return event


def _customer_id_from_session(session: dict[str, Any]) -> str:
    cust = session.get("customer")
    if isinstance(cust, str) and cust.strip():
        return cust.strip()
    if isinstance(cust, dict):
        cid = str(cust.get("id") or "").strip()
        if cid:
            return cid
    raise BillingError("checkout session missing customer id")


def _subscription_id_from_session(session: dict[str, Any]) -> str | None:
    sub = session.get("subscription")
    if isinstance(sub, str) and sub.strip():
        return sub.strip()
    if isinstance(sub, dict):
        sid = str(sub.get("id") or "").strip()
        return sid or None
    return None


def _plan_id_from_session(session: dict[str, Any]) -> str:
    meta = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
    plan = str(meta.get("plan") or "").strip().lower()
    if plan in PLANS:
        return plan
    raise BillingError("checkout session metadata.plan missing or unknown")


def _email_from_session(session: dict[str, Any]) -> str | None:
    for key in ("customer_email", "customer_details"):
        raw = session.get(key)
        if isinstance(raw, str) and "@" in raw:
            return raw.strip()
        if isinstance(raw, dict):
            email = str(raw.get("email") or "").strip()
            if email and "@" in email:
                return email
    return None


def provision_from_checkout_session(
    session: dict[str, Any],
    *,
    registry: TenantRegistry,
    seats_snapshot: list[dict[str, Any]],
) -> Tenant:
    """Claim seats + upsert tenant for a completed Checkout Session.

    Idempotent when the Stripe customer already has a tenant with seats.
    Raises InsufficientSeats without writing a partial tenant.
    """
    customer_id = _customer_id_from_session(session)
    existing = registry.get_by_stripe_customer(customer_id)
    if existing is not None and existing.seat_ids and existing.api_keys:
        logger.info(
            "billing provision idempotent customer=%s tenant=%s",
            customer_id,
            existing.id,
        )
        return existing

    plan = get_plan(_plan_id_from_session(session))
    email = _email_from_session(session) or (existing.email if existing else None)
    subscription_id = _subscription_id_from_session(session)
    if existing is not None:
        # Resubscribe / recover: keep id, reclaim seats, rotate key
        claimed_elsewhere = registry.claimed_seat_ids() - set(existing.seat_ids)
        seat_ids = pick_desktop_seats(seats_snapshot, claimed_elsewhere, plan.seats)
        api_key = generate_api_key()
        tenant = Tenant(
            id=existing.id,
            api_keys=frozenset({api_key}),
            seat_ids=frozenset(seat_ids),
            max_concurrent_leases=plan.max_concurrent_leases,
            max_lease_ttl_sec=existing.max_lease_ttl_sec,
            max_recording_bytes=existing.max_recording_bytes,
            email=email or existing.email,
            plan=plan.id,
            stripe_customer_id=customer_id,
            stripe_subscription_id=subscription_id or existing.stripe_subscription_id,
            api_key_revealed_at=None,
        )
        return registry.upsert_tenant(tenant)

    seat_ids = claim_desktop_seats(
        registry=registry, seats_snapshot=seats_snapshot, n=plan.seats
    )
    api_key = generate_api_key()
    tenant = Tenant(
        id=customer_id,
        api_keys=frozenset({api_key}),
        seat_ids=frozenset(seat_ids),
        max_concurrent_leases=plan.max_concurrent_leases,
        max_lease_ttl_sec=None,
        max_recording_bytes=None,
        email=email,
        plan=plan.id,
        stripe_customer_id=customer_id,
        stripe_subscription_id=subscription_id,
        api_key_revealed_at=None,
    )
    saved = registry.upsert_tenant(tenant)
    logger.info(
        "billing provisioned tenant=%s plan=%s seats=%s",
        saved.id,
        plan.id,
        sorted(saved.seat_ids),
    )
    return saved


def suspend_tenant(registry: TenantRegistry, tenant: Tenant) -> Tenant:
    """Clear keys and seats (stop access); keep Stripe ids for Portal / history."""
    suspended = Tenant(
        id=tenant.id,
        api_keys=frozenset(),
        seat_ids=frozenset(),
        max_concurrent_leases=max(1, tenant.max_concurrent_leases),
        max_lease_ttl_sec=tenant.max_lease_ttl_sec,
        max_recording_bytes=tenant.max_recording_bytes,
        email=tenant.email,
        plan=tenant.plan,
        stripe_customer_id=tenant.stripe_customer_id,
        stripe_subscription_id=tenant.stripe_subscription_id,
        api_key_revealed_at=tenant.api_key_revealed_at,
    )
    return registry.upsert_tenant(suspended)


def resize_tenant_plan(
    registry: TenantRegistry,
    tenant: Tenant,
    *,
    plan: PlanSpec,
    seats_snapshot: list[dict[str, Any]],
) -> Tenant:
    """Grow/shrink seat allowlist to match plan; rotate nothing."""
    current = sorted(tenant.seat_ids)
    n = plan.seats
    if len(current) == n:
        updated = Tenant(
            id=tenant.id,
            api_keys=tenant.api_keys,
            seat_ids=tenant.seat_ids,
            max_concurrent_leases=plan.max_concurrent_leases,
            max_lease_ttl_sec=tenant.max_lease_ttl_sec,
            max_recording_bytes=tenant.max_recording_bytes,
            email=tenant.email,
            plan=plan.id,
            stripe_customer_id=tenant.stripe_customer_id,
            stripe_subscription_id=tenant.stripe_subscription_id,
            api_key_revealed_at=tenant.api_key_revealed_at,
        )
        return registry.upsert_tenant(updated)

    if len(current) > n:
        keep = frozenset(current[:n])
        updated = Tenant(
            id=tenant.id,
            api_keys=tenant.api_keys,
            seat_ids=keep,
            max_concurrent_leases=plan.max_concurrent_leases,
            max_lease_ttl_sec=tenant.max_lease_ttl_sec,
            max_recording_bytes=tenant.max_recording_bytes,
            email=tenant.email,
            plan=plan.id,
            stripe_customer_id=tenant.stripe_customer_id,
            stripe_subscription_id=tenant.stripe_subscription_id,
            api_key_revealed_at=tenant.api_key_revealed_at,
        )
        return registry.upsert_tenant(updated)

    need = n - len(current)
    # Exclude all currently claimed seats (including this tenant's) so we
    # only pull net-new inventory into the allowlist.
    extra = pick_desktop_seats(
        seats_snapshot, registry.claimed_seat_ids(), need
    )
    updated = Tenant(
        id=tenant.id,
        api_keys=tenant.api_keys,
        seat_ids=frozenset(current + extra),
        max_concurrent_leases=plan.max_concurrent_leases,
        max_lease_ttl_sec=tenant.max_lease_ttl_sec,
        max_recording_bytes=tenant.max_recording_bytes,
        email=tenant.email,
        plan=plan.id,
        stripe_customer_id=tenant.stripe_customer_id,
        stripe_subscription_id=tenant.stripe_subscription_id,
        api_key_revealed_at=tenant.api_key_revealed_at,
    )
    return registry.upsert_tenant(updated)


def mark_api_key_revealed(registry: TenantRegistry, tenant: Tenant) -> Tenant:
    if tenant.api_key_revealed_at:
        return tenant
    updated = Tenant(
        id=tenant.id,
        api_keys=tenant.api_keys,
        seat_ids=tenant.seat_ids,
        max_concurrent_leases=tenant.max_concurrent_leases,
        max_lease_ttl_sec=tenant.max_lease_ttl_sec,
        max_recording_bytes=tenant.max_recording_bytes,
        email=tenant.email,
        plan=tenant.plan,
        stripe_customer_id=tenant.stripe_customer_id,
        stripe_subscription_id=tenant.stripe_subscription_id,
        api_key_revealed_at=_utcnow_iso(),
    )
    return registry.upsert_tenant(updated)


def plan_id_from_subscription(subscription: dict[str, Any]) -> str | None:
    meta = subscription.get("metadata")
    if isinstance(meta, dict):
        plan = str(meta.get("plan") or "").strip().lower()
        if plan in PLANS:
            return plan
    # Fall back: match price id on first item
    items = subscription.get("items")
    data = items.get("data") if isinstance(items, dict) else None
    if isinstance(data, list) and data:
        price = data[0].get("price") if isinstance(data[0], dict) else None
        price_id = ""
        if isinstance(price, str):
            price_id = price
        elif isinstance(price, dict):
            price_id = str(price.get("id") or "")
        for spec in PLANS.values():
            if spec.price_id and spec.price_id == price_id:
                return spec.id
    return None
