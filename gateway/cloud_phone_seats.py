"""Cloud phone provision / bind / teardown helpers used by SessionPool.

Pool keeps the lock and thin wrappers (tests monkeypatch those). This module owns
provider I/O and slot field updates for cloud sessions.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from providers import get as get_cloud_provider
from providers.base import CloudPhoneProviderError, CloudSession

logger = logging.getLogger("computer.gateway.cloud_phone_seats")


class CloudSlot(Protocol):
    """WorkerSlot fields touched by cloud helpers."""

    provider: str | None
    provider_session_id: str | None
    cloud_watch_url: str | None
    cloud_unlock: str | None
    bound_serial: str | None
    vnc_url: str
    base_url: str
    state: str
    seat_id: str


class CloudSeatHost(Protocol):
    """SessionPool surface needed for provision / reconnect (lock + worker bind)."""

    _lock: Any
    _parked_ttl: int
    _timeout: float

    def _find_seat_locked(self, seat_id: str) -> CloudSlot | None: ...

    def _worker_device_bind(
        self,
        worker_url: str,
        *,
        connect: str,
        unlock: str | None,
    ) -> tuple[int, Any]: ...


def teardown_payload(slot: CloudSlot) -> dict[str, str] | None:
    if slot.provider and slot.provider_session_id:
        return {
            "provider": slot.provider,
            "session_id": slot.provider_session_id,
        }
    return None


def apply_session(slot: CloudSlot, session: CloudSession) -> None:
    slot.provider_session_id = session.provider_session_id
    slot.cloud_watch_url = session.watch_url
    slot.cloud_unlock = session.unlock
    slot.bound_serial = session.adb_connect
    slot.vnc_url = session.watch_url


def provision_timeout(lease_ttl_sec: int, parked_ttl: int) -> int:
    return max(int(lease_ttl_sec) + parked_ttl, 900)


def teardown(payload: dict[str, str]) -> None:
    provider_name = payload.get("provider") or ""
    session_id = payload.get("session_id") or ""
    if not provider_name or not session_id:
        return
    try:
        get_cloud_provider(provider_name).teardown(session_id)
    except Exception as e:
        logger.warning(
            "cloud teardown failed provider=%s session=%s: %s",
            provider_name,
            session_id,
            e,
        )


def provision_and_bind(
    host: CloudSeatHost, seat_id: str, *, lease_ttl_sec: int
) -> None:
    """Provision via CloudPhoneProvider and bind the phone worker (outside lock)."""
    with host._lock:
        slot = host._find_seat_locked(seat_id)
        if not slot or not slot.provider:
            raise CloudPhoneProviderError(
                f"seat {seat_id} is not a cloud phone seat",
                code="not_cloud_seat",
            )
        provider_name = slot.provider
        worker_url = slot.base_url

    provider = get_cloud_provider(provider_name)
    session = provider.provision(
        timeout_sec=provision_timeout(lease_ttl_sec, host._parked_ttl)
    )
    status, data = host._worker_device_bind(
        worker_url,
        connect=session.adb_connect,
        unlock=session.unlock,
    )
    ok = status < 400 and isinstance(data, dict) and bool(data.get("ok", True))
    if not ok:
        try:
            provider.teardown(session.provider_session_id)
        except Exception as e:
            logger.warning(
                "teardown after bind failure session=%s: %s",
                session.provider_session_id,
                e,
            )
        raise CloudPhoneProviderError(
            f"phone worker bind failed after provision: {data}",
            code="worker_bind_failed",
        )

    with host._lock:
        slot = host._find_seat_locked(seat_id)
        if not slot or slot.state != "leased":
            try:
                provider.teardown(session.provider_session_id)
            except Exception:
                pass
            raise CloudPhoneProviderError(
                f"seat {seat_id} lost lease during cloud provision",
                code="lease_lost",
            )
        apply_session(slot, session)


def reconnect_adb(host: CloudSeatHost, seat_id: str) -> None:
    """Re-adb connect (+ unlock) for an existing cloud session (park → acquire)."""
    with host._lock:
        slot = host._find_seat_locked(seat_id)
        if not slot or not slot.provider or not slot.provider_session_id:
            return
        if not slot.bound_serial:
            return
        provider_name = slot.provider
        worker_url = slot.base_url
        connect = slot.bound_serial
        unlock = slot.cloud_unlock
        watch = slot.cloud_watch_url
        sid = slot.provider_session_id

    session = CloudSession(
        provider_session_id=sid,
        adb_host=connect.rsplit(":", 1)[0] if ":" in connect else connect,
        adb_port=int(connect.rsplit(":", 1)[1]) if ":" in connect else 5555,
        unlock=unlock,
        watch_url=watch or "",
    )
    hint = get_cloud_provider(provider_name).reconnect_hint(session)
    status, data = host._worker_device_bind(
        worker_url,
        connect=hint.adb_connect,
        unlock=hint.unlock,
    )
    if status >= 400 or (isinstance(data, dict) and data.get("ok") is False):
        logger.warning(
            "cloud ADB reconnect failed seat=%s: %s %s",
            seat_id,
            status,
            data,
        )
