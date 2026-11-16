"""On-demand worker factories for elastic (burst) seats.

Reserved seats stay in Compose inventory. Burst seats are created via a
WorkerFactory backend (default: disabled). k3s is the intended production backend.
"""

from __future__ import annotations

import os

from workers.base import SpawnedWorker, WorkerFactory, WorkerFactoryError
from workers.disabled import DisabledWorkerFactory


def build_worker_factory() -> WorkerFactory:
    """Select burst backend from SEAT_BURST_BACKEND (default disabled)."""
    backend = os.environ.get("SEAT_BURST_BACKEND", "disabled").strip().lower()
    if backend in ("", "disabled", "off", "none", "0"):
        return DisabledWorkerFactory()
    if backend == "k3s":
        from workers.k3s import K3sWorkerFactory

        return K3sWorkerFactory()
    raise WorkerFactoryError(
        f"unknown SEAT_BURST_BACKEND={backend!r}; use disabled or k3s",
        code="unknown_backend",
    )


__all__ = [
    "SpawnedWorker",
    "WorkerFactory",
    "WorkerFactoryError",
    "DisabledWorkerFactory",
    "build_worker_factory",
]
