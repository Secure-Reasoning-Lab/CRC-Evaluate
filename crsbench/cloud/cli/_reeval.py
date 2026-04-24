"""Cloud re-eval submission flow."""

from __future__ import annotations

import secrets
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import yaml

from crsbench.cloud.bootstrap import CloudVmBootstrapInputs
from crsbench.cloud.collection import ArtifactCollectionError, ArtifactCollector
from crsbench.cloud.errors import CloudProvisioningError
from crsbench.cloud.launch_checks import find_launch_target_conflicts
from crsbench.cloud.launch_state import (
    CloudLaunchState,
    CreatedCloudInstanceRecord,
    append_created_instance_records,
    find_launch_state_for_source_experiment,
    save_launch_state,
)
from crsbench.cloud.models import build_cloud_launch_plan
from crsbench.cloud.providers import (
    prepare_launch_inputs,
    provider_adapter_for_launch_plan,
)
from crsbench.cloud.quota import CloudQuotaValidationError, QuotaValidator
from crsbench.cloud.reeval_bundle import build_reeval_bundle
from crsbench.cloud.reeval_remote import write_submission_state
from crsbench.cloud.types import CloudProvider
from crsbench.distributed.registry import RuntimeRegistration
from crsbench.run_experiment import load_experiment_config
from crsbench.utils.logger import get_logger

from ._launch import (
    _normalize_fleet_records,
    _project_for_fleet_record,
    _rollback_created_instances,
)

if TYPE_CHECKING:
    import argparse

    from crsbench.cloud.preflight import CloudLaunchPreflight
    from crsbench.cloud.records import CloudFleetPlacementRecord, CloudInstanceLike


logger = get_logger(__name__)
_ORCHESTRATOR_ENV_PATH = "/var/lib/crsbench/orchestrator.env"
_DEFAULT_REMOTE_CLONE_DIR = "/opt/crsbench"


def _utc_timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S")


def derive_remote_experiment_name(source_experiment_name: str) -> str:
    """Return the default remote experiment namespace for one cloud re-eval run."""
    return f"{source_experiment_name}-reeval-{_utc_timestamp()}"


def run_cloud_reeval(args: argparse.Namespace) -> int:
    """Submit an existing experiment tree for remote distributed re-evaluation."""
    config_path = Path(args.config)
    source_config = load_experiment_config(config_path)
    if source_config.cloud is None:
        logger.error(
            "Experiment config must define cloud configuration for cloud re-eval"
        )
        return 1

    source_experiment_name = source_config.experiment
    remote_experiment_name = args.remote_experiment or derive_remote_experiment_name(
        source_experiment_name
    )
    source_experiment_root = _resolve_source_experiment_root(
        source_config,
        explicit_root=args.from_path,
    )
    if not source_experiment_root.exists():
        logger.error(
            "Source experiment directory not found: {}", source_experiment_root
        )
        return 1

    remote_config = source_config.model_copy(
        update={"experiment": remote_experiment_name}
    )
    try:
        with tempfile.TemporaryDirectory(
            prefix=f"crsbench-cloud-reeval-{remote_experiment_name}-"
        ) as temp_dir_name:
            temp_dir = Path(temp_dir_name)
            bundle_result = build_reeval_bundle(
                config_path=config_path,
                source_experiment_root=source_experiment_root,
                remote_experiment_name=remote_experiment_name,
                bundle_root=temp_dir / "bundle",
            )
            launch_state = provision_cloud_reeval_fleet(
                source_config_path=config_path,
                source_config=source_config,
                remote_config=remote_config,
                remote_experiment_name=remote_experiment_name,
            )
            publish_cloud_reeval_submission(
                launch_state=launch_state,
                bundle_root=bundle_result.bundle_root,
                source_experiment_name=source_experiment_name,
                remote_experiment_name=remote_experiment_name,
                config_path=config_path,
            )
    except CloudQuotaValidationError as exc:
        logger.error("Cloud re-eval failed: {}", str(exc))
        return 1
    except Exception as exc:
        logger.error("Cloud re-eval failed: {}", str(exc))
        return 1

    logger.info(
        "Cloud re-eval submission accepted: source={} remote={}",
        source_experiment_name,
        remote_experiment_name,
    )
    return 0


def provision_cloud_reeval_fleet(
    *,
    source_config_path: Path,
    source_config,
    remote_config,
    remote_experiment_name: str,
) -> CloudLaunchState:
    """Provision an orchestrator plus evaluator fleet for cloud re-eval."""
    existing_source_state = find_launch_state_for_source_experiment(
        source_config_path,
        source_config.experiment,
    )
    if existing_source_state is not None:
        raise CloudProvisioningError(
            "Cloud re-eval already has active launch state for source experiment "
            f"{source_config.experiment!r}: "
            f"{existing_source_state.effective_remote_experiment_name()}"
        )

    registration = RuntimeRegistration.from_experiment_config(remote_config)
    bootstrap_inputs = CloudVmBootstrapInputs.from_experiment_config(remote_config)
    redis_password = secrets.token_urlsafe(24)
    launch_plan = replace(build_cloud_launch_plan(remote_config), worker_placements=[])
    preflight: CloudLaunchPreflight = prepare_launch_inputs(
        plan=launch_plan,
        cwd=Path.cwd(),
    )
    provisioning_plan = preflight.resolved_plan
    adapter = provider_adapter_for_launch_plan(provisioning_plan)
    resolved_orchestrator_config = adapter.build_orchestrator_config(provisioning_plan)
    worker_fleet_configs: list[CloudFleetPlacementRecord] = []
    evaluator_fleet_configs = _normalize_fleet_records(
        adapter,
        list(preflight.redacted_evaluator_fleets or []),
        role="evaluator",
    )

    conflicts = find_launch_target_conflicts(
        config_path=source_config_path,
        experiment_name=remote_experiment_name,
        adapter=adapter,
        plan=launch_plan,
    )
    if conflicts:
        raise CloudProvisioningError(
            f"Experiment {remote_experiment_name!r} already has cloud launch state; "
            f"{'; '.join(conflicts)}. Tear it down before launching again."
        )

    validator = QuotaValidator(adapters={"gce": adapter})
    validator.validate(launch_plan)

    orchestrator_created_records: list[CreatedCloudInstanceRecord] = []
    evaluator_created_records: list[CreatedCloudInstanceRecord] = []
    with tempfile.TemporaryDirectory(
        prefix=f"crsbench-cloud-reeval-config-{remote_experiment_name}-"
    ) as temp_dir_name:
        temp_config_path = Path(temp_dir_name) / "experiment-config.yaml"
        _write_temp_experiment_config(temp_config_path, remote_config)

        try:
            orchestrator_env = dict(preflight.orchestrator_env or {})
            orchestrator_env["CRSBENCH_CLOUD_LAUNCH_MODE"] = "reeval"
            orchestrator_record = adapter.create_orchestrator(
                plan=provisioning_plan,
                experiment_config_path=str(temp_config_path),
                env_passthrough=orchestrator_env,
                redis_password=redis_password,
            )
            if not orchestrator_record.internal_ip:
                raise CloudProvisioningError(
                    f"Provisioned orchestrator {orchestrator_record.name} has no internal IP"
                )

            orchestrator_project = resolved_orchestrator_config.project
            orchestrator_created_records = [
                CreatedCloudInstanceRecord(
                    provider=CloudProvider.GCE,
                    project=orchestrator_project,
                    zone=orchestrator_record.zone,
                    instance_name=orchestrator_record.name,
                )
            ]
            append_created_instance_records(
                source_config_path,
                experiment_name=remote_experiment_name,
                records=orchestrator_created_records,
            )

            redis_host = f"{orchestrator_record.internal_ip}:6379"
            evaluators = adapter.create_evaluators(
                plan=provisioning_plan,
                redis_host=redis_host,
                redis_password=redis_password,
                registration=registration,
                experiment_config_path=str(temp_config_path),
                bootstrap_inputs=bootstrap_inputs,
                env_passthrough_by_placement=preflight.evaluator_placement_envs,
            )
            if evaluators:
                evaluator_created_records = [
                    CreatedCloudInstanceRecord(
                        provider=CloudProvider.GCE,
                        project=_project_for_fleet_record(
                            worker.name,
                            instance_zone=worker.zone,
                            fleet_configs=evaluator_fleet_configs,
                        ),
                        zone=worker.zone,
                        instance_name=worker.name,
                    )
                    for worker in evaluators
                ]
                append_created_instance_records(
                    source_config_path,
                    experiment_name=remote_experiment_name,
                    records=evaluator_created_records,
                )
        except Exception:
            if evaluator_created_records:
                try:
                    _rollback_created_instances(evaluator_created_records)
                except Exception:
                    logger.warning(
                        "Best-effort rollback failed for re-eval evaluator instances in experiment {}",
                        remote_experiment_name,
                    )
            if orchestrator_created_records:
                try:
                    _rollback_created_instances(orchestrator_created_records)
                except Exception:
                    logger.warning(
                        "Best-effort rollback failed for re-eval orchestrator instances in experiment {}",
                        remote_experiment_name,
                    )
            raise

    remote_submission_dir = _remote_submission_dir(
        source_config, remote_experiment_name
    )
    remote_experiment_root = remote_submission_dir / "workspace"
    launch_state = CloudLaunchState(
        experiment_name=remote_experiment_name,
        launch_mode="reeval",
        source_experiment_name=source_config.experiment,
        remote_experiment_name=remote_experiment_name,
        config_path=str(source_config_path),
        experiment_filestore=str(source_config.experiment_filestore),
        remote_experiment_root=str(remote_experiment_root),
        remote_submission_dir=str(remote_submission_dir),
        remote_bundle_path=str(remote_submission_dir / "bundle"),
        redis_host=f"{orchestrator_record.internal_ip}:6379",
        redis_password=redis_password,
        orchestrator_provider=CloudProvider.GCE,
        orchestrator_name=orchestrator_record.name,
        orchestrator_project=resolved_orchestrator_config.project,
        orchestrator_zone=orchestrator_record.zone,
        orchestrator_internal_ip=orchestrator_record.internal_ip,
        orchestrator_external_ip=orchestrator_record.external_ip,
        orchestrator_ssh_via_iap=resolved_orchestrator_config.ssh_via_iap,
        worker_fleet_configs=worker_fleet_configs,
        evaluator_fleet_configs=evaluator_fleet_configs,
    )
    save_launch_state(source_config_path, launch_state)
    return launch_state


def publish_cloud_reeval_submission(
    *,
    launch_state: CloudLaunchState,
    bundle_root: Path,
    source_experiment_name: str,
    remote_experiment_name: str,
    config_path: Path,
) -> None:
    """Upload the bundle directory to the orchestrator and start remote execution."""
    remote_submission_dir = Path(
        launch_state.remote_submission_dir
        or _remote_submission_dir_from_state(launch_state)
    )
    remote_upload_dir = Path(f"{remote_submission_dir}.uploading")
    remote_workspace_root = Path(
        launch_state.remote_experiment_root or remote_submission_dir / "workspace"
    )
    client = OrchestratorRemoteClient(base_path=config_path, launch_state=launch_state)

    with tempfile.TemporaryDirectory(
        prefix=f"crsbench-cloud-reeval-upload-{remote_experiment_name}-"
    ) as temp_dir_name:
        local_submission_dir = Path(temp_dir_name) / "submission"
        _build_local_submission_dir(
            bundle_root=bundle_root,
            submission_dir=local_submission_dir,
            remote_submission_dir=remote_submission_dir,
            remote_workspace_root=remote_workspace_root,
            source_experiment_name=source_experiment_name,
            remote_experiment_name=remote_experiment_name,
        )

        client.run_or_raise(
            _remote_prepare_upload_command(
                remote_submission_dir=remote_submission_dir,
                remote_upload_dir=remote_upload_dir,
            )
        )
        client.upload_dir(local_submission_dir, str(remote_upload_dir))
        client.run_or_raise(
            _remote_publish_and_launch_command(
                remote_submission_dir=remote_submission_dir,
                remote_upload_dir=remote_upload_dir,
            )
        )


class OrchestratorRemoteClient:
    """Thin orchestrator SSH/rsync helper built on the existing collection transport."""

    def __init__(self, *, base_path: Path, launch_state: CloudLaunchState) -> None:
        self._collector = ArtifactCollector(base_path=base_path)
        self._worker = _reeval_transport_worker(launch_state)
        self._fleet = launch_state.as_transport_config()
        self._experiment_filestore = Path(
            launch_state.experiment_filestore or base_path.parent
        )

    def run_or_raise(self, command: str) -> subprocess.CompletedProcess[str]:
        known_hosts_path = self._collector._prepare_ssh_access(
            worker=self._worker,
            fleet=self._fleet,
            experiment_filestore=self._experiment_filestore,
        )
        ssh_user = self._collector._direct_ssh_user(self._fleet)
        result = self._collector._run_remote_command(
            worker=self._worker,
            fleet=self._fleet,
            known_hosts_path=known_hosts_path,
            ssh_user=ssh_user,
            command=command,
            experiment_filestore=self._experiment_filestore,
        )
        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            stdout = (result.stdout or "").strip()
            detail = (
                stderr
                or stdout
                or f"remote command failed with exit {result.returncode}"
            )
            raise CloudProvisioningError(detail)
        return result

    def upload_dir(self, local_dir: Path, remote_dir: str) -> None:
        known_hosts_path = self._collector._prepare_ssh_access(
            worker=self._worker,
            fleet=self._fleet,
            experiment_filestore=self._experiment_filestore,
        )
        ssh_user = self._collector._direct_ssh_user(self._fleet)
        try:
            if self._fleet.ssh_via_iap:
                if not ssh_user:
                    raise CloudProvisioningError(
                        f"Unable to resolve SSH user for IAP upload to {self._worker.name}"
                    )
                iap_known_hosts_path = self._collector._prepare_iap_known_hosts(
                    experiment_filestore=self._experiment_filestore,
                    host_key_alias=self._worker.name,
                )
                with self._collector._open_iap_tunnel(
                    worker=self._worker,
                    fleet=self._fleet,
                ) as local_port:
                    cmd = self._build_upload_rsync_cmd(
                        local_dir=local_dir,
                        remote_dir=remote_dir,
                        ssh_command=self._collector._build_iap_ssh_command(
                            local_port=local_port,
                            ssh_user=ssh_user,
                            known_hosts_path=iap_known_hosts_path,
                            host_key_alias=self._worker.name,
                        ),
                        remote_host="127.0.0.1",
                    )
                    self._collector._run_rsync_with_retry(cmd)
                    return

            cmd = self._build_upload_rsync_cmd(
                local_dir=local_dir,
                remote_dir=remote_dir,
                known_hosts_path=known_hosts_path,
                ssh_user=ssh_user,
            )
            self._collector._run_rsync_with_retry(cmd)
        except ArtifactCollectionError as exc:
            raise CloudProvisioningError(str(exc)) from exc
        except subprocess.CalledProcessError as exc:
            raise CloudProvisioningError(f"bundle upload failed: {exc}") from exc

    def _build_upload_rsync_cmd(
        self,
        *,
        local_dir: Path,
        remote_dir: str,
        known_hosts_path: Path | None = None,
        ssh_user: str | None = None,
        ssh_command: str | None = None,
        remote_host: str | None = None,
    ) -> list[str]:
        if (
            known_hosts_path is None
            and not self._fleet.ssh_via_iap
            and self._collector._base_path is not None
        ):
            known_hosts_path = self._collector._known_hosts_path(
                self._experiment_filestore,
                worker_name=self._worker.name,
            )
        ssh_cmd = ssh_command or self._collector._build_ssh_command(
            self._worker,
            self._fleet,
            known_hosts_path,
        )
        resolved_remote_host = remote_host or self._collector._remote_host(
            self._worker,
            self._fleet,
        )
        if ssh_user is not None and remote_host is None and not self._fleet.ssh_via_iap:
            resolved_remote_host = f"{ssh_user}@{resolved_remote_host}"

        return [
            "rsync",
            "-a",
            "--mkpath",
            "--partial-dir=.rsync-partial",
            "--delay-updates",
            "--rsync-path=sudo rsync",
            "-e",
            ssh_cmd,
            str(local_dir) + "/",
            f"{resolved_remote_host}:{remote_dir}/",
        ]


def _reeval_transport_worker(launch_state: CloudLaunchState) -> CloudInstanceLike:
    """Return a collector-compatible live-instance view of the orchestrator."""
    record = launch_state.as_orchestrator_record()
    if record.zone is None:
        raise CloudProvisioningError(
            f"Cloud re-eval orchestrator {record.name!r} is missing zone metadata"
        )
    return cast(
        "CloudInstanceLike",
        SimpleNamespace(
            name=record.name,
            instance_id=record.instance_id,
            status=record.status,
            zone=record.zone,
            internal_ip=record.internal_ip,
            external_ip=record.external_ip,
            labels=record.labels,
        ),
    )


def _resolve_source_experiment_root(config, *, explicit_root: str | None) -> Path:
    if explicit_root:
        return Path(explicit_root)
    return Path(config.experiment_filestore) / config.experiment


def _write_temp_experiment_config(config_path: Path, config) -> None:
    config_path.write_text(
        yaml.safe_dump(
            config.model_dump(mode="json", exclude_none=True),
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _remote_storage_root(config) -> Path:
    if config.cloud is not None and config.cloud.remote.experiment_root is not None:
        return Path(config.cloud.remote.experiment_root)
    return Path(config.experiment_filestore)


def _remote_submission_dir(config, remote_experiment_name: str) -> Path:
    return (
        _remote_storage_root(config)
        / ".crsbench-cloud"
        / "reeval"
        / remote_experiment_name
    )


def _remote_submission_dir_from_state(launch_state: CloudLaunchState) -> Path:
    return Path(launch_state.remote_experiment_root or ".").parent


def _build_local_submission_dir(
    *,
    bundle_root: Path,
    submission_dir: Path,
    remote_submission_dir: Path,
    remote_workspace_root: Path,
    source_experiment_name: str,
    remote_experiment_name: str,
) -> None:
    submission_dir.mkdir(parents=True, exist_ok=False)
    shutil.copytree(bundle_root, submission_dir / "bundle")
    write_submission_state(
        submission_dir / "submission.json",
        {
            "state": "uploading",
            "source_experiment_name": source_experiment_name,
            "remote_experiment_name": remote_experiment_name,
            "bundle_path": str(remote_submission_dir / "bundle"),
            "workspace_path": str(remote_workspace_root),
        },
    )


def _remote_prepare_upload_command(
    *,
    remote_submission_dir: Path,
    remote_upload_dir: Path,
) -> str:
    script = (
        "set -euo pipefail; "
        f"sudo rm -rf {shlex.quote(str(remote_upload_dir))}; "
        f"sudo mkdir -p {shlex.quote(str(remote_upload_dir.parent))}; "
        f"sudo rm -rf {shlex.quote(str(remote_submission_dir))}"
    )
    return f"bash -lc {shlex.quote(script)}"


def _remote_publish_and_launch_command(
    *,
    remote_submission_dir: Path,
    remote_upload_dir: Path,
) -> str:
    script = f"""
set -euo pipefail
source {shlex.quote(_ORCHESTRATOR_ENV_PATH)}
: "${{CLONE_DIR:={_DEFAULT_REMOTE_CLONE_DIR}}}"
: "${{CRSBENCH_USER:=crsbench}}"
sudo mv {shlex.quote(str(remote_upload_dir))} {shlex.quote(str(remote_submission_dir))}
sudo chown -R "${{CRSBENCH_USER}}:${{CRSBENCH_USER}}" {
        shlex.quote(str(remote_submission_dir))
    }
sudo -H -u "${{CRSBENCH_USER}}" env PATH="${{PATH}}" /bin/bash -lc {
        shlex.quote(
            'cd "$CLONE_DIR" && '
            "python -m crsbench.cloud.reeval_remote launch "
            f"--submission-dir {shlex.quote(str(remote_submission_dir))} "
            "--redis-host localhost:6379 "
            '--benchmarks-root "$CLONE_DIR/benchmarks"'
        )
    }
"""
    return f"bash -lc {shlex.quote(script)}"
