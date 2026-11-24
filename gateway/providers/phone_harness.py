"""Phone Harness CloudPhoneProvider — first registered cloud phone impl."""

from __future__ import annotations

import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx

from parseutil import env_int
from providers.base import (
    CloudPhoneProviderError,
    CloudSession,
    ReconnectHint,
    register,
)

logger = logging.getLogger("computer.gateway.providers.phone_harness")

PROVIDER_NAME = "phone-harness"
DEFAULT_API_BASE = "https://api.phone-harness.com"


def _default_timeout_sec() -> int:
    """Harness session lifetime should cover lease + park idle."""
    session = env_int("SESSION_TTL_SEC", 600)
    parked = env_int("PARKED_TTL_SEC", 1800)
    floor = env_int("PHONE_HARNESS_TIMEOUT_SEC", 0)
    if floor > 0:
        return max(floor, 900)
    return max(session + parked, 900)


def _parse_expires_at(raw: Any) -> datetime | None:
    if raw is None:
        return None
    try:
        return datetime.fromtimestamp(float(raw), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _session_from_ready(payload: dict[str, Any]) -> CloudSession:
    sid = str(payload.get("id") or "").strip()
    watch = str(payload.get("watch_url") or "").strip()
    adb = payload.get("adb")
    if not sid:
        raise CloudPhoneProviderError("phone-harness ready session missing id")
    if not watch:
        raise CloudPhoneProviderError(
            f"phone-harness session {sid} missing watch_url", code="missing_watch_url"
        )
    if not isinstance(adb, dict):
        raise CloudPhoneProviderError(
            f"phone-harness session {sid} ready but adb block missing",
            code="missing_adb",
        )
    host = str(adb.get("host") or "").strip()
    port = adb.get("port")
    code = str(adb.get("code") or "").strip() or None
    if not host or not isinstance(port, int):
        raise CloudPhoneProviderError(
            f"phone-harness session {sid} has invalid adb host/port",
            code="invalid_adb",
        )
    return CloudSession(
        provider_session_id=sid,
        adb_host=host,
        adb_port=port,
        unlock=code,
        watch_url=watch,
        expires_at=_parse_expires_at(payload.get("expires_at")),
    )


class PhoneHarnessProvider:
    """POST /sessions → poll until ready → CloudSession; DELETE on teardown."""

    name = PROVIDER_NAME

    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_base: str | None = None,
        profile_id: str | None = None,
        platform: str = "android",
        kind: str = "emulator",
        poll_interval_sec: float = 5.0,
        http_timeout: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._api_key = (api_key if api_key is not None else os.environ.get("PHONE_HARNESS_API_KEY", "")).strip()
        self._api_base = (
            api_base
            if api_base is not None
            else os.environ.get("PHONE_HARNESS_API_BASE", DEFAULT_API_BASE)
        ).strip().rstrip("/")
        self._profile_id = (
            profile_id
            if profile_id is not None
            else os.environ.get("PHONE_HARNESS_PROFILE_ID", "")
        ).strip() or None
        self._platform = platform
        self._kind = kind
        self._poll_interval = max(1.0, poll_interval_sec)
        self._http_timeout = http_timeout
        self._client = client
        self._owns_client = client is None

    def _http(self) -> httpx.Client:
        if self._client is not None:
            return self._client
        if not self._api_key:
            raise CloudPhoneProviderError(
                "PHONE_HARNESS_API_KEY is not set",
                code="missing_api_key",
            )
        self._client = httpx.Client(
            base_url=self._api_base,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=self._http_timeout,
        )
        return self._client

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    def _error_from_response(self, r: httpx.Response, *, action: str) -> CloudPhoneProviderError:
        code = None
        retry_after = None
        detail = r.text
        try:
            body = r.json()
            if isinstance(body, dict):
                detail = str(body.get("error") or body.get("detail") or detail)
                code = body.get("code")
                if isinstance(code, str):
                    code = code.strip() or None
                ra = body.get("retry_after_seconds")
                if ra is not None:
                    retry_after = float(ra)
        except Exception:
            pass
        if retry_after is None:
            hdr = r.headers.get("Retry-After")
            if hdr:
                try:
                    retry_after = float(hdr)
                except ValueError:
                    pass
        return CloudPhoneProviderError(
            f"phone-harness {action} failed ({r.status_code}): {detail}",
            code=code,
            retry_after_seconds=retry_after,
        )

    def provision(self, *, timeout_sec: int) -> CloudSession:
        lifetime = max(int(timeout_sec), 1)
        body: dict[str, Any] = {
            "platform": self._platform,
            "kind": self._kind,
            "timeout_seconds": lifetime,
        }
        if self._profile_id:
            body["profile_id"] = self._profile_id

        client = self._http()
        idem = f"cwa-{uuid.uuid4().hex}"
        r = client.post(
            "/sessions",
            json=body,
            headers={"Idempotency-Key": idem},
        )
        if r.status_code == 503:
            raise self._error_from_response(r, action="create")
        if r.status_code not in (200, 202):
            raise self._error_from_response(r, action="create")

        payload = r.json()
        if not isinstance(payload, dict):
            raise CloudPhoneProviderError("phone-harness create returned non-object")
        sid = str(payload.get("id") or "").strip()
        if not sid:
            raise CloudPhoneProviderError("phone-harness create missing session id")

        deadline = time.monotonic() + lifetime
        state = str(payload.get("state") or "")
        while state != "ready":
            if state == "error":
                err = payload.get("error") or "session entered error state"
                raise CloudPhoneProviderError(
                    f"phone-harness session {sid} failed: {err}",
                    code="session_error",
                )
            if state == "closing":
                raise CloudPhoneProviderError(
                    f"phone-harness session {sid} is closing",
                    code="session_closing",
                )
            if time.monotonic() >= deadline:
                raise CloudPhoneProviderError(
                    f"phone-harness session {sid} not ready before timeout "
                    f"({lifetime}s)",
                    code="provision_timeout",
                )
            time.sleep(self._poll_interval)
            gr = client.get(f"/sessions/{sid}")
            if gr.status_code >= 400:
                raise self._error_from_response(gr, action="poll")
            payload = gr.json()
            if not isinstance(payload, dict):
                raise CloudPhoneProviderError("phone-harness poll returned non-object")
            state = str(payload.get("state") or "")

        session = _session_from_ready(payload)
        logger.info(
            "phone-harness provisioned session=%s adb=%s expires=%s",
            session.provider_session_id,
            session.adb_connect,
            session.expires_at,
        )
        return session

    def teardown(self, provider_session_id: str) -> None:
        sid = (provider_session_id or "").strip()
        if not sid:
            return
        client = self._http()
        r = client.delete(f"/sessions/{sid}")
        # 404 / 410 = already gone — treat as success for recycle paths.
        if r.status_code in (200, 202, 204, 404, 410):
            logger.info("phone-harness teardown session=%s status=%s", sid, r.status_code)
            return
        raise self._error_from_response(r, action="teardown")

    def reconnect_hint(self, session: CloudSession) -> ReconnectHint:
        # Harness: every new adb connect needs unlock again with the same code
        # until reset; host/port stay for the session lifetime.
        return ReconnectHint(
            adb_host=session.adb_host,
            adb_port=session.adb_port,
            unlock=session.unlock,
        )


def build_from_env() -> PhoneHarnessProvider:
    return PhoneHarnessProvider()


# Self-register when the package is imported (gateway boot).
register(PhoneHarnessProvider())
