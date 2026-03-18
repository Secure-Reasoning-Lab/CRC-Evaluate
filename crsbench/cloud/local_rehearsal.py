"""Helpers for rehearsing cloud startup scripts inside local Docker containers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from crsbench.cloud.bootstrap import CloudVmBootstrapInputs
from crsbench.cloud.gce.metadata import (
    build_evaluator_metadata,
    build_instance_metadata,
    build_orchestrator_metadata,
    load_evaluator_startup_script,
    load_orchestrator_startup_script,
    load_startup_script,
)
from crsbench.distributed.registry import RuntimeRegistration
from crsbench.run_experiment import load_experiment_config
from crsbench.validation.schemas import GceOrchestratorConfig, GceWorkerFleetConfig

if TYPE_CHECKING:
    from pathlib import Path

DEFAULT_LOCAL_ZONE = "local-docker-a"
DEFAULT_LOCAL_REDIS_PASSWORD = "local-rehearsal-redis-password"


@dataclass(frozen=True)
class LocalRehearsalLayout:
    """Filesystem layout consumed by the local Docker compose rehearsal."""

    output_dir: Path
    experiment_name: str
    repo_mount_path: str
    redis_password: str
    orchestrator_metadata_dir: Path
    worker_metadata_dirs: list[Path]
    evaluator_metadata_dirs: list[Path]
    orchestrator_state_dir: Path
    worker_state_dirs: list[Path]
    evaluator_state_dirs: list[Path]


def build_local_rehearsal_layout(
    *,
    output_dir: Path,
    experiment_config_path: Path,
    repo_mount_path: str,
    worker_count: int = 2,
    evaluator_count: int = 1,
    git_ref: str,
    zone: str = DEFAULT_LOCAL_ZONE,
    redis_password: str = DEFAULT_LOCAL_REDIS_PASSWORD,
) -> LocalRehearsalLayout:
    """Write file-backed metadata trees for local startup-script rehearsal."""
    if worker_count < 1:
        raise ValueError("worker_count must be at least 1")
    if evaluator_count < 0:
        raise ValueError("evaluator_count must be non-negative")

    output_dir = output_dir.resolve()
    config = load_experiment_config(experiment_config_path)
    bootstrap_inputs = CloudVmBootstrapInputs.from_experiment_config(config)
    registration = RuntimeRegistration.from_experiment_config(config)
    install_spec = f"git+file://{repo_mount_path}"

    orchestrator = GceOrchestratorConfig(
        project="local-rehearsal",
        zone=zone,
        machine_type="n2d-standard-8",
        boot_disk_size_gb=50,
        image="projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64",
        service_account_email="orchestrator@local-rehearsal.invalid",
        owner_label="local-rehearsal",
        metadata={},
        instance_name_prefix="local-orchestrator",
        use_os_login=True,
        ssh_via_iap=False,
        crsbench_install_spec=install_spec,
        crsbench_git_ref=git_ref,
    )
    fleet = GceWorkerFleetConfig(
        project="local-rehearsal",
        zone=zone,
        worker_count=worker_count,
        machine_type="n2d-standard-8",
        boot_disk_size_gb=50,
        image="projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64",
        service_account_email="worker@local-rehearsal.invalid",
        owner_label="local-rehearsal",
        labels={},
        metadata={},
        worker_name_prefix="local-worker",
        use_os_login=True,
        ssh_via_iap=False,
        readiness_timeout_sec=1200,
        crsbench_install_spec=install_spec,
        crsbench_git_ref=git_ref,
    )
    evaluator_fleet = GceWorkerFleetConfig(
        project="local-rehearsal",
        zone=zone,
        worker_count=evaluator_count,
        machine_type="n2d-standard-8",
        boot_disk_size_gb=50,
        image="projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64",
        service_account_email="evaluator@local-rehearsal.invalid",
        owner_label="local-rehearsal",
        labels={},
        metadata={},
        worker_name_prefix="local-evaluator",
        use_os_login=True,
        ssh_via_iap=False,
        readiness_timeout_sec=1200,
        crsbench_install_spec=install_spec,
        crsbench_git_ref=git_ref,
    )

    metadata_root = output_dir / "metadata"
    state_root = output_dir / "state"
    orchestrator_metadata_dir = metadata_root / "orchestrator"
    orchestrator_state_dir = state_root / "orchestrator"
    worker_metadata_dirs: list[Path] = []
    worker_state_dirs: list[Path] = []
    evaluator_metadata_dirs: list[Path] = []
    evaluator_state_dirs: list[Path] = []

    orchestrator_metadata = build_orchestrator_metadata(
        experiment_name=config.experiment,
        orchestrator=orchestrator,
        experiment_config_path=experiment_config_path,
        redis_password=redis_password,
        startup_script=load_orchestrator_startup_script(),
    )
    _write_metadata_tree(
        metadata_dir=orchestrator_metadata_dir,
        metadata=orchestrator_metadata,
        instance_id="local-orchestrator",
        zone=zone,
    )
    orchestrator_state_dir.mkdir(parents=True, exist_ok=True)

    for worker_index in range(worker_count):
        worker_name = f"local-worker-{worker_index + 1}"
        worker_metadata = build_instance_metadata(
            experiment_name=config.experiment,
            fleet=fleet,
            redis_host="orchestrator:6379",
            redis_password=redis_password,
            registration=registration,
            bootstrap_inputs=bootstrap_inputs,
            worker_name=worker_name,
            startup_script=load_startup_script(),
        )
        worker_metadata_dir = metadata_root / worker_name
        worker_state_dir = state_root / worker_name
        _write_metadata_tree(
            metadata_dir=worker_metadata_dir,
            metadata=worker_metadata,
            instance_id=worker_name,
            zone=zone,
        )
        worker_state_dir.mkdir(parents=True, exist_ok=True)
        worker_metadata_dirs.append(worker_metadata_dir)
        worker_state_dirs.append(worker_state_dir)

    for evaluator_index in range(evaluator_count):
        evaluator_name = f"local-evaluator-{evaluator_index + 1}"
        evaluator_metadata = build_evaluator_metadata(
            experiment_name=config.experiment,
            fleet=evaluator_fleet,
            redis_host="orchestrator:6379",
            redis_password=redis_password,
            registration=registration,
            bootstrap_inputs=bootstrap_inputs,
            env_passthrough=None,
            evaluator_name=evaluator_name,
            experiment_config_path=experiment_config_path,
            startup_script=load_evaluator_startup_script(),
        )
        evaluator_metadata_dir = metadata_root / evaluator_name
        evaluator_state_dir = state_root / evaluator_name
        _write_metadata_tree(
            metadata_dir=evaluator_metadata_dir,
            metadata=evaluator_metadata,
            instance_id=evaluator_name,
            zone=zone,
        )
        evaluator_state_dir.mkdir(parents=True, exist_ok=True)
        evaluator_metadata_dirs.append(evaluator_metadata_dir)
        evaluator_state_dirs.append(evaluator_state_dir)

    return LocalRehearsalLayout(
        output_dir=output_dir,
        experiment_name=config.experiment,
        repo_mount_path=repo_mount_path,
        redis_password=redis_password,
        orchestrator_metadata_dir=orchestrator_metadata_dir,
        worker_metadata_dirs=worker_metadata_dirs,
        evaluator_metadata_dirs=evaluator_metadata_dirs,
        orchestrator_state_dir=orchestrator_state_dir,
        worker_state_dirs=worker_state_dirs,
        evaluator_state_dirs=evaluator_state_dirs,
    )


def _write_metadata_tree(
    *,
    metadata_dir: Path,
    metadata: dict[str, str],
    instance_id: str,
    zone: str,
) -> None:
    attributes_dir = metadata_dir / "attributes"
    attributes_dir.mkdir(parents=True, exist_ok=True)
    for key, value in metadata.items():
        (attributes_dir / key).write_text(str(value), encoding="utf-8")
    (metadata_dir / "id").write_text(instance_id, encoding="utf-8")
    (metadata_dir / "zone").write_text(zone, encoding="utf-8")
