"""Pluggable cloud phone providers (ADB endpoint + watch URL).

Pool / lease code talks only to this module — never import vendor SDKs there.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class CloudSession:
    """Normalized result of provisioning a cloud Android seat."""

    provider_session_id: str
    adb_host: str
    adb_port: int
    watch_url: str
    unlock: str | None = None
    expires_at: datetime | None = None

    @property
    def adb_connect(self) -> str:
        """host:port for adb connect / serial."""
        return f"{self.adb_host}:{self.adb_port}"


@dataclass(frozen=True)
class ReconnectHint:
    """How to restore ADB after a dropped transport (same cloud session)."""

    adb_host: str
    adb_port: int
    unlock: str | None = None

    @property
    def adb_connect(self) -> str:
        return f"{self.adb_host}:{self.adb_port}"


class CloudPhoneProviderError(RuntimeError):
    """Provider-level failure (busy, auth, timeout, teardown)."""

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retry_after_seconds = retry_after_seconds


@runtime_checkable
class CloudPhoneProvider(Protocol):
    """Inventory `provider` string selects a registered implementation."""

    @property
    def name(self) -> str:
        """Stable id matching inventory JSON `provider` (e.g. phone-harness)."""
        ...

    def provision(self, *, timeout_sec: int) -> CloudSession:
        """Allocate a phone and block until ADB-ready (or raise)."""
        ...

    def teardown(self, provider_session_id: str) -> None:
        """End billing / release the cloud session (idempotent preferred)."""
        ...

    def reconnect_hint(self, session: CloudSession) -> ReconnectHint:
        """ADB connect (+ unlock) needed after a dropped transport."""
        ...


_REGISTRY: dict[str, CloudPhoneProvider] = {}


def register(provider: CloudPhoneProvider) -> CloudPhoneProvider:
    """Register (or replace) a provider under provider.name."""
    name = provider.name.strip()
    if not name:
        raise ValueError("CloudPhoneProvider.name must be non-empty")
    _REGISTRY[name] = provider
    return provider


def get(name: str) -> CloudPhoneProvider:
    key = (name or "").strip()
    if not key:
        raise KeyError("empty cloud phone provider name")
    try:
        return _REGISTRY[key]
    except KeyError as e:
        known = ", ".join(sorted(_REGISTRY)) or "(none registered)"
        raise KeyError(
            f"unknown cloud phone provider {key!r}; registered: {known}"
        ) from e


def registered_names() -> frozenset[str]:
    return frozenset(_REGISTRY)


def clear_registry_for_tests() -> None:
    """Test helper — do not call from production paths."""
    _REGISTRY.clear()
