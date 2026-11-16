"""WorkerFactory — spawn/destroy ephemeral workers for burst seats."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from inventory import SeatKind


@dataclass(frozen=True)
class SpawnedWorker:
    """A running worker the pool can register as an ephemeral seat."""

    seat_id: str
    kind: SeatKind
    control_url: str  # worker HTTP base (screenshot/action), e.g. http://desk-burst-x:8000
    vnc_url: str  # human Watch URL (noVNC or phone watch_url placeholder)
    handle: str  # opaque id for destroy (e.g. k8s job name)


class WorkerFactoryError(RuntimeError):
    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


@runtime_checkable
class WorkerFactory(Protocol):
    """Create and destroy burst workers. Reserved inventory never goes through this."""

    @property
    def name(self) -> str:
        ...

    def spawn(self, kind: SeatKind, *, seat_id: str) -> SpawnedWorker:
        """Block until the worker control API is reachable (or raise)."""
        ...

    def destroy(self, handle: str) -> None:
        """Best-effort teardown; idempotent preferred."""
        ...
