"""Default burst backend — refuses spawn (fixed inventory only)."""

from __future__ import annotations

from inventory import SeatKind
from workers.base import SpawnedWorker, WorkerFactoryError


class DisabledWorkerFactory:
    name = "disabled"

    def spawn(self, kind: SeatKind, *, seat_id: str) -> SpawnedWorker:
        raise WorkerFactoryError(
            f"burst disabled (SEAT_BURST_BACKEND=disabled); no free reserved {kind} seats",
            code="burst_disabled",
        )

    def destroy(self, handle: str) -> None:
        return
