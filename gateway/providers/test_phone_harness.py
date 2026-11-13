"""Fixture-backed tests for PhoneHarnessProvider (no live API key)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from providers.base import (
    CloudPhoneProviderError,
    clear_registry_for_tests,
    get,
    register,
    registered_names,
)
from providers.phone_harness import PhoneHarnessProvider, PROVIDER_NAME

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _fresh_registry():
    clear_registry_for_tests()
    yield
    clear_registry_for_tests()


def test_registry_get_after_register():
    p = PhoneHarnessProvider(api_key="pck_test", client=httpx.Client())
    register(p)
    assert PROVIDER_NAME in registered_names()
    assert get(PROVIDER_NAME) is p


def test_registry_unknown():
    with pytest.raises(KeyError, match="unknown cloud phone provider"):
        get("not-a-vendor")


def test_provision_polls_until_ready():
    create = _load("phone_harness_create.json")
    ready = _load("phone_harness_ready.json")
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.method == "POST" and request.url.path == "/sessions":
            return httpx.Response(202, json=create)
        if request.method == "GET" and request.url.path == "/sessions/abc123def456":
            # First poll still provisioning, second ready.
            if calls.count("GET /sessions/abc123def456") == 1:
                return httpx.Response(200, json=create)
            return httpx.Response(200, json=ready)
        return httpx.Response(404, json={"error": "unexpected"})

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, base_url="https://api.example.test")
    provider = PhoneHarnessProvider(
        api_key="pck_test",
        client=client,
        poll_interval_sec=0.01,
    )
    session = provider.provision(timeout_sec=900)

    assert session.provider_session_id == "abc123def456"
    assert session.adb_connect == "live.phone-harness.com:22220"
    assert session.unlock == "ph_mfwcc5nknqaod034nwojzw0lzq"
    assert session.watch_url.startswith("https://viewer.example.com/")
    assert session.expires_at is not None
    assert "POST /sessions" in calls
    assert calls.count("GET /sessions/abc123def456") >= 2

    hint = provider.reconnect_hint(session)
    assert hint.adb_connect == session.adb_connect
    assert hint.unlock == session.unlock


def test_provision_phones_busy():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503,
            json={
                "error": "All phones are in use",
                "code": "phones_busy",
                "retry_after_seconds": 12,
            },
            headers={"Retry-After": "12"},
        )

    client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://api.example.test",
    )
    provider = PhoneHarnessProvider(api_key="pck_test", client=client)
    with pytest.raises(CloudPhoneProviderError) as ei:
        provider.provision(timeout_sec=300)
    assert ei.value.code == "phones_busy"
    assert ei.value.retry_after_seconds == 12.0


def test_teardown_idempotent_404():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        return httpx.Response(404, json={"error": "gone"})

    client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://api.example.test",
    )
    provider = PhoneHarnessProvider(api_key="pck_test", client=client)
    provider.teardown("abc123def456")  # should not raise


def test_missing_api_key():
    provider = PhoneHarnessProvider(api_key="")
    with pytest.raises(CloudPhoneProviderError, match="PHONE_HARNESS_API_KEY"):
        provider.provision(timeout_sec=60)
