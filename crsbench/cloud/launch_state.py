"""Local state persisted for remote-orchestrator cloud launches."""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from pydantic import BaseModel, ConfigDict, Field, model_validator

from crsbench.cloud.gce.models import GceWorkerRecord
from crsbench.validation.schemas import GceWorkerFleetConfig  # noqa: TC001

_STATE_DIRNAME = ".crsbench-cloud"
_INSTANCE_CACHE_BASENAME = "created-instances.cache"
_REMOTE_LOGS_DIRNAME = "remote-logs"


@dataclass(frozen=True)
class CreatedCloudInstanceRecord:
    """Append-only local ledger entry for a provisioned cloud instance."""

    provider: str
    instance_name: str
    zone: str
    project: str | None = None


class CloudLaunchState(BaseModel):
    """Locally persisted control-plane data for a launched cloud experiment."""

    model_config = ConfigDict(extra="forbid")

    experiment_name: str
    config_path: str
    experiment_filestore: str | None = None
    redis_host: str
    redis_password: str
    orchestrator_provider: str = "gce"
    orchestrator_name: str
    orchestrator_project: str
    orchestrator_zone: str
    orchestrator_internal_ip: str | None = None
    orchestrator_external_ip: str | None = None
    orchestrator_ssh_via_iap: bool = False
    worker_fleet_configs: list[GceWorkerFleetConfig] = Field(default_factory=list)
    worker_fleet_config: GceWorkerFleetConfig | None = None

    @model_validator(mode="after")
    def populate_worker_fleet_configs(self) -> "CloudLaunchState":
        """Preserve backward compatibility for legacy single-fleet launch state."""
        if self.worker_fleet_configs:
            return self
        if self.worker_fleet_config is not None:
            self.worker_fleet_configs = [self.worker_fleet_config]
        return self

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

    def resolved_worker_fleets(self) -> list[GceWorkerFleetConfig]:
        """Return all worker fleet configs recorded for this launch."""
        return list(self.worker_fleet_configs)


def redact_worker_fleet_config(fleet: GceWorkerFleetConfig) -> GceWorkerFleetConfig:
    """Return a fleet config safe to persist in local launch state."""
    return fleet.model_copy(
        update={
            "github_deploy_key_file": None,
            "hf_token": None,
        }
    )


def redact_launch_state(state: CloudLaunchState) -> CloudLaunchState:
    """Return a launch state copy with secret-bearing worker fields removed."""
    redacted_fleets = [
        redact_worker_fleet_config(fleet) for fleet in state.worker_fleet_configs
    ]
    redacted_single = (
        redact_worker_fleet_config(state.worker_fleet_config)
        if state.worker_fleet_config is not None
        else None
    )
    return state.model_copy(
        update={
            "worker_fleet_configs": redacted_fleets,
            "worker_fleet_config": redacted_single,
        }
    )


def _resolve_launch_state_dir(base_path: Path | str) -> Path:
    from pathlib import Path as _Path

    path = _Path(base_path)
    if path.is_file() or (not path.exists() and path.suffix):
        return path.parent / _STATE_DIRNAME
    return path / _STATE_DIRNAME


def cloud_state_dir(base_path: Path | str) -> Path:
    """Return the config-adjacent local state directory for cloud operations."""
    return _resolve_launch_state_dir(base_path)


def launch_state_path(base_path: Path | str, experiment_name: str) -> Path:
    """Return the on-disk path used to persist launch state for one experiment."""
    return cloud_state_dir(base_path) / f"{experiment_name}.json"


def created_instance_cache_path(base_path: Path | str) -> Path:
    """Return the append-only cache path tracking created cloud instance names."""
    return cloud_state_dir(base_path) / _INSTANCE_CACHE_BASENAME


def remote_logs_dir(base_path: Path | str, experiment_name: str) -> Path:
    """Return the local directory used to store best-effort remote VM logs."""
    return cloud_state_dir(base_path) / _REMOTE_LOGS_DIRNAME / experiment_name


def append_created_instance_records(
    base_path: Path | str,
    *,
    experiment_name: str,
    records: list[CreatedCloudInstanceRecord],
) -> Path:
    """Append provisioned instance records to the local cloud cache."""
    path = created_instance_cache_path(base_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    created_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(
                json.dumps(
                    {
                        "created_at": created_at,
                        "experiment_name": experiment_name,
                        "provider": record.provider,
                        "project": record.project,
                        "zone": record.zone,
                        "instance_name": record.instance_name,
                    },
                    sort_keys=True,
                )
            )
            handle.write("\n")
    path.chmod(0o600)
    return path


def save_launch_state(
    base_path: Path | str,
    state: CloudLaunchState,
) -> Path:
    """Persist launch state with restrictive local file permissions."""
    state = redact_launch_state(state)
    path = launch_state_path(base_path, state.experiment_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            dir=path.parent,
            prefix=f"{path.name}.",
            suffix=".tmp",
            delete=False,
            encoding="utf-8",
        ) as handle:
            handle.write(json.dumps(state.model_dump(), indent=2))
            temp_path = Path(handle.name)
        if temp_path is None:
            raise RuntimeError("Failed to allocate temporary launch-state path")
        temp_path.chmod(0o600)
        temp_path.replace(path)
        path.chmod(0o600)
    except Exception:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()
        raise
    return path


def load_launch_state(
    base_path: Path | str,
    experiment_name: str,
) -> CloudLaunchState | None:
    """Load persisted launch state for one experiment if present."""
    path = launch_state_path(base_path, experiment_name)
    if not path.is_file():
        return None
    return CloudLaunchState.model_validate_json(path.read_text(encoding="utf-8"))


def delete_launch_state(
    base_path: Path | str,
    experiment_name: str,
) -> None:
    """Remove persisted launch state once a cloud experiment is torn down."""
    path = launch_state_path(base_path, experiment_name)
    if path.exists():
        path.unlink()
