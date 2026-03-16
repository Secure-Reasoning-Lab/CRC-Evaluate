"""Local state persisted for remote-orchestrator cloud launches."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from crsbench.cloud.gce.models import GceWorkerRecord

if TYPE_CHECKING:
    from pathlib import Path

_STATE_DIRNAME = ".crsbench-cloud"


class CloudLaunchState(BaseModel):
    """Locally persisted control-plane data for a launched cloud experiment."""

    model_config = ConfigDict(extra="forbid")

    experiment_name: str
    config_path: str
    redis_host: str
    redis_password: str
    orchestrator_name: str
    orchestrator_project: str
    orchestrator_zone: str
    orchestrator_internal_ip: str | None = None
    orchestrator_external_ip: str | None = None
    orchestrator_ssh_via_iap: bool = False

    def as_orchestrator_record(self) -> GceWorkerRecord:
        """Build a collector-compatible instance record for the orchestrator VM."""
        return GceWorkerRecord(
            name=self.orchestrator_name,
            instance_id=f"orchestrator:{self.orchestrator_name}",
            status="RUNNING",
            zone=self.orchestrator_zone,
            internal_ip=self.orchestrator_internal_ip,
            external_ip=self.orchestrator_external_ip,
            labels={"crsbench-role": "orchestrator"},
        )

    def as_transport_config(self) -> SimpleNamespace:
        """Build the minimal SSH transport config expected by ArtifactCollector."""
        return SimpleNamespace(
            project=self.orchestrator_project,
            zone=self.orchestrator_zone,
            ssh_via_iap=self.orchestrator_ssh_via_iap,
        )


def launch_state_path(experiment_filestore: Path, experiment_name: str) -> Path:
    """Return the on-disk path used to persist launch state for one experiment."""
    return experiment_filestore / _STATE_DIRNAME / f"{experiment_name}.json"


def save_launch_state(
    experiment_filestore: Path,
    state: CloudLaunchState,
) -> Path:
    """Persist launch state with restrictive local file permissions."""
    path = launch_state_path(experiment_filestore, state.experiment_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state.model_dump(), indent=2), encoding="utf-8")
    path.chmod(0o600)
    return path


def load_launch_state(
    experiment_filestore: Path,
    experiment_name: str,
) -> CloudLaunchState | None:
    """Load persisted launch state for one experiment if present."""
    path = launch_state_path(experiment_filestore, experiment_name)
    if not path.is_file():
        return None
    return CloudLaunchState.model_validate_json(path.read_text(encoding="utf-8"))


def delete_launch_state(
    experiment_filestore: Path,
    experiment_name: str,
) -> None:
    """Remove persisted launch state once a cloud experiment is torn down."""
    path = launch_state_path(experiment_filestore, experiment_name)
    if path.exists():
        path.unlink()
