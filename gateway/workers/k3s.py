"""k3s WorkerFactory — apply Job templates via kubectl.

Requires:
  - k3s (or any kube API) reachable with KUBECONFIG
  - worker images available to the cluster (import or registry)
  - manifests under deploy/k3s/ (mounted or repo-relative)

Pool calls spawn when reserved seats of a kind are full and BURST_MAX > 0.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

from inventory import SeatKind
from workers.base import SpawnedWorker, WorkerFactoryError

logger = logging.getLogger("computer.gateway.workers.k3s")

DEFAULT_NAMESPACE = "computer-burst"


class K3sWorkerFactory:
    name = "k3s"

    def __init__(
        self,
        *,
        kubeconfig: str | None = None,
        namespace: str | None = None,
        manifest_dir: str | None = None,
        vnc_base: str | None = None,
        image_desktop: str | None = None,
        image_phone: str | None = None,
        kubectl: str | None = None,
        ready_timeout_sec: float = 180.0,
    ) -> None:
        self._kubeconfig = (
            kubeconfig
            if kubeconfig is not None
            else os.environ.get("KUBECONFIG", "").strip()
        )
        self._namespace = (
            namespace
            or os.environ.get("SEAT_BURST_NAMESPACE", DEFAULT_NAMESPACE)
        ).strip()
        self._manifest_dir = Path(
            manifest_dir
            or os.environ.get(
                "SEAT_BURST_MANIFEST_DIR",
                str(Path(__file__).resolve().parents[2] / "deploy" / "k3s"),
            )
        )
        self._vnc_base = (
            vnc_base
            or os.environ.get("GATEWAY_BURST_VNC_BASE", "http://localhost:30080")
        ).rstrip("/")
        self._image_desktop = (
            image_desktop
            or os.environ.get(
                "SEAT_BURST_DESKTOP_IMAGE",
                "apron-worker:local",
            )
        )
        self._image_phone = (
            image_phone
            or os.environ.get(
                "SEAT_BURST_PHONE_IMAGE",
                "apron-phone:local",
            )
        )
        self._kubectl = kubectl or os.environ.get("KUBECTL", "kubectl")
        self._ready_timeout = ready_timeout_sec

    def _run(self, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        cmd = [self._kubectl, *args]
        env = os.environ.copy()
        if self._kubeconfig:
            env["KUBECONFIG"] = self._kubeconfig
        try:
            return subprocess.run(
                cmd,
                check=check,
                capture_output=True,
                text=True,
                timeout=120,
                env=env,
            )
        except FileNotFoundError as e:
            raise WorkerFactoryError(
                f"{self._kubectl} not found on PATH",
                code="kubectl_missing",
            ) from e
        except subprocess.TimeoutExpired as e:
            raise WorkerFactoryError("kubectl timed out", code="kubectl_timeout") from e

    def _sanitize_job_name(self, seat_id: str, *, kind: SeatKind) -> str:
        job_name = seat_id.replace("_", "-").lower()
        if kind == "phone":
            allowed = ("phone-burst", "phone-warm")
            if not any(job_name.startswith(p) for p in allowed):
                job_name = f"phone-burst-{job_name}"
        else:
            if not job_name.startswith("desk-burst"):
                job_name = f"desk-burst-{job_name}"
        job_name = "".join(c if c.isalnum() or c == "-" else "-" for c in job_name)[:63]
        return job_name

    def spawn(self, kind: SeatKind, *, seat_id: str) -> SpawnedWorker:
        if kind not in ("desktop", "phone"):
            raise WorkerFactoryError(
                f"k3s burst for kind={kind!r} not supported",
                code="kind_unsupported",
            )
        if shutil.which(self._kubectl) is None and self._kubectl == "kubectl":
            raise WorkerFactoryError(
                "kubectl not found — install k3s/kubectl or set KUBECTL",
                code="kubectl_missing",
            )

        if kind == "phone":
            template = self._manifest_dir / "phone-burst-job.yaml.template"
            image = self._image_phone
        else:
            template = self._manifest_dir / "desktop-burst-job.yaml.template"
            image = self._image_desktop

        if not template.is_file():
            raise WorkerFactoryError(
                f"missing manifest template: {template}",
                code="manifest_missing",
            )

        job_name = self._sanitize_job_name(seat_id, kind=kind)
        raw = template.read_text(encoding="utf-8")
        rendered = (
            raw.replace("{{JOB_NAME}}", job_name)
            .replace("{{NAMESPACE}}", self._namespace)
            .replace("{{IMAGE}}", image)
            .replace("{{SEAT_ID}}", seat_id)
        )

        env = os.environ.copy()
        if self._kubeconfig:
            env["KUBECONFIG"] = self._kubeconfig
        try:
            apply = subprocess.run(
                [self._kubectl, "apply", "-f", "-"],
                input=rendered,
                check=False,
                capture_output=True,
                text=True,
                timeout=120,
                env=env,
            )
        except FileNotFoundError as e:
            raise WorkerFactoryError("kubectl not found", code="kubectl_missing") from e

        if apply.returncode != 0:
            raise WorkerFactoryError(
                f"kubectl apply failed: {(apply.stderr or apply.stdout).strip()}",
                code="apply_failed",
            )

        deadline = time.monotonic() + self._ready_timeout
        while time.monotonic() < deadline:
            get = self._run(
                [
                    "get",
                    "pods",
                    "-n",
                    self._namespace,
                    "-l",
                    f"computer.burst/seat={seat_id}",
                    "-o",
                    "jsonpath={.items[0].status.phase}",
                ],
                check=False,
            )
            phase = (get.stdout or "").strip()
            if phase == "Running":
                break
            if phase in ("Failed", "Succeeded"):
                raise WorkerFactoryError(
                    f"burst pod ended in phase={phase}",
                    code="pod_failed",
                )
            time.sleep(2)
        else:
            self.destroy(job_name)
            raise WorkerFactoryError(
                f"burst pod not Running within {self._ready_timeout}s",
                code="ready_timeout",
            )

        control_url = f"http://{job_name}.{self._namespace}.svc.cluster.local:8000"
        if kind == "phone":
            # Replaced by CloudPhoneProvider watch_url after pool provision.
            vnc_url = "https://placeholder.invalid/watch"
        else:
            vnc_url = (
                f"{self._vnc_base}/vnc.html"
                f"?autoconnect=1&resize=scale&path=&seat={seat_id}"
            )

        logger.info(
            "k3s spawned %s seat=%s job=%s control=%s",
            kind,
            seat_id,
            job_name,
            control_url,
        )
        return SpawnedWorker(
            seat_id=seat_id,
            kind=kind,
            control_url=control_url,
            vnc_url=vnc_url,
            handle=job_name,
        )

    def destroy(self, handle: str) -> None:
        name = (handle or "").strip()
        if not name:
            return
        r = self._run(
            ["delete", "job", name, "-n", self._namespace, "--ignore-not-found=true"],
            check=False,
        )
        self._run(
            ["delete", "svc", name, "-n", self._namespace, "--ignore-not-found=true"],
            check=False,
        )
        if r.returncode != 0:
            logger.warning("k3s destroy job=%s: %s", name, (r.stderr or r.stdout).strip())
