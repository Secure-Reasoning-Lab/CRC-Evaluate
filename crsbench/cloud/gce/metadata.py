"""Metadata and startup payload assembly for GCE-backed workers."""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from crsbench.cloud.bootstrap import CloudVmBootstrapInputs
    from crsbench.distributed.registry import RuntimeRegistration
    from crsbench.validation.schemas import (
        GceOrchestratorConfig,
        GceWorkerFleetConfig,
    )

CRSBENCH_BOOTSTRAP_PAYLOAD_KEY = "crsbench-bootstrap-payload"
CRSBENCH_INSTALL_SPEC_KEY = "crsbench-install-spec"
CRSBENCH_GIT_REF_KEY = "crsbench-git-ref"
CRSBENCH_GITHUB_DEPLOY_KEY = "crsbench-github-deploy-key"
CRSBENCH_HF_TOKEN_KEY = "crsbench-hf-token"
CRSBENCH_ENV_PASSTHROUGH_B64_KEY = "crsbench-env-passthrough-b64"
CRSBENCH_REDIS_PASSWORD_KEY = "crsbench-redis-password"
CRSBENCH_EXPERIMENT_CONFIG_B64_KEY = "crsbench-experiment-config-b64"
CRSBENCH_EXPERIMENT_METADATA_KEY = "crsbench-experiment"
CRSBENCH_WORKER_NAME_METADATA_KEY = "crsbench-worker-name"
CRSBENCH_READINESS_TIMEOUT_METADATA_KEY = "crsbench-readiness-timeout-sec"
GCE_ENABLE_OSLOGIN_KEY = "enable-oslogin"
GCE_SERIAL_PORT_ENABLE_KEY = "serial-port-enable"
GCE_STARTUP_SCRIPT_KEY = "startup-script"
GCE_STARTUP_SCRIPT_URL_KEY = "startup-script-url"

_STARTUP_SCRIPT_PATH = Path(__file__).with_name("startup") / "worker.sh"
_ORCHESTRATOR_STARTUP_SCRIPT_PATH = (
    Path(__file__).with_name("startup") / "orchestrator.sh"
)
_LABEL_PATTERN = re.compile(r"[^a-z0-9_-]+")


class _InstallMetadataConfig(Protocol):
    metadata: dict[str, str]
    startup_script_uri: str | None
    ssh_via_iap: bool
    crsbench_install_spec: str | None
    crsbench_git_ref: str
    github_deploy_key_file: str | None
    hf_token: str | None


def _sanitize_label_key(value: str) -> str:
    cleaned = _LABEL_PATTERN.sub("-", value.strip().lower()).strip("-_")
    if not cleaned:
        return "label"
    if not cleaned[0].isalpha():
        cleaned = f"l-{cleaned}"
    return cleaned[:63].rstrip("-_") or "label"


def _sanitize_label_value(value: str) -> str:
    cleaned = _LABEL_PATTERN.sub("-", value.strip().lower()).strip("-_")
    if not cleaned:
        return "value"
    return cleaned[:63].rstrip("-_") or "value"


def _build_role_labels(
    *,
    experiment_name: str,
    fleet: GceWorkerFleetConfig,
    role: str,
) -> dict[str, str]:
    """Render deterministic GCE labels for a CRSBench role-specific fleet."""
    labels = {
        _sanitize_label_key(key): _sanitize_label_value(value)
        for key, value in fleet.labels.items()
    }
    labels["owner"] = _sanitize_label_value(
        fleet.owner_label or fleet.labels.get("owner", "crsbench")
    )
    labels["crsbench-experiment"] = _sanitize_label_value(experiment_name)
    labels["crsbench-role"] = role
    return labels


def build_worker_labels(
    *,
    experiment_name: str,
    fleet: GceWorkerFleetConfig,
) -> dict[str, str]:
    """Render deterministic GCE labels for a CRSBench worker fleet."""
    return _build_role_labels(
        experiment_name=experiment_name, fleet=fleet, role="worker"
    )


def build_evaluator_labels(
    *,
    experiment_name: str,
    fleet: GceWorkerFleetConfig,
) -> dict[str, str]:
    """Render deterministic GCE labels for a CRSBench evaluator fleet."""
    return _build_role_labels(
        experiment_name=experiment_name,
        fleet=fleet,
        role="evaluator",
    )


def build_orchestrator_labels(
    *,
    experiment_name: str,
    orchestrator: GceOrchestratorConfig,
) -> dict[str, str]:
    """Render deterministic GCE labels for a CRSBench orchestrator VM."""
    labels = {
        _sanitize_label_key(key): _sanitize_label_value(value)
        for key, value in orchestrator.labels.items()
    }
    labels["owner"] = _sanitize_label_value(
        orchestrator.owner_label or orchestrator.labels.get("owner", "crsbench")
    )
    labels["crsbench-experiment"] = _sanitize_label_value(experiment_name)
    labels["crsbench-role"] = "orchestrator"
    return labels


def build_bootstrap_payload(
    *,
    experiment_name: str,
    worker_name: str,
    redis_host: str,
    registration: RuntimeRegistration,
    fleet: GceWorkerFleetConfig,
    bootstrap_inputs: CloudVmBootstrapInputs | None = None,
) -> dict[str, object]:
    """Build the minimal configless-worker payload consumed at VM boot."""
    payload: dict[str, object] = {
        "experiment": experiment_name,
        "worker_name": worker_name,
        "redis_host": redis_host,
        "worker_jobs": registration.worker_jobs or 1,
        "worker_cores_per_job": registration.worker_cores_per_job
        or registration.cores_per_trial,
        "worker_cpu_tag": registration.worker_cpu_tag,
        "trial_queue": registration.trial_queue,
        "benchmarks_root": registration.benchmarks_root,
        "readiness_timeout_sec": fleet.readiness_timeout_sec,
    }
    if bootstrap_inputs is None:
        return payload

    selector = bootstrap_inputs.selector
    payload.update(
        {
            "prepare_mode": bootstrap_inputs.prepare_mode,
            "download_benchmarks": bootstrap_inputs.download_benchmarks,
            "benchmarks_root": str(selector.effective_benchmarks_root()),
        }
    )
    if selector.benchmark_suite is not None:
        payload["benchmark_suite"] = selector.benchmark_suite
        payload["benchmark_suites_root"] = str(
            selector.effective_benchmark_suites_root()
        )
    if selector.benchmarks is not None:
        payload["benchmarks"] = selector.benchmarks
    return payload


def build_evaluator_bootstrap_payload(
    *,
    experiment_name: str,
    evaluator_name: str,
    redis_host: str,
    registration: RuntimeRegistration,
    fleet: GceWorkerFleetConfig,
    bootstrap_inputs: CloudVmBootstrapInputs | None = None,
) -> dict[str, object]:
    """Build the minimal managed-evaluator payload consumed at VM boot."""
    payload: dict[str, object] = {
        "experiment": experiment_name,
        "evaluator_name": evaluator_name,
        "redis_host": redis_host,
        "evaluator_build_jobs": registration.evaluator_build_jobs or 1,
        "evaluator_build_cores_per_job": registration.evaluator_build_cores_per_job
        or 4,
        "evaluator_verify_jobs": registration.evaluator_verify_jobs or 1,
        "evaluator_verify_cores_per_job": registration.evaluator_verify_cores_per_job
        or 4,
        "evaluator_idle_timeout": registration.evaluator_idle_timeout or 0,
        "evaluator_cpu_tag": registration.evaluator_cpu_tag,
        "benchmarks_root": registration.benchmarks_root,
        "readiness_timeout_sec": fleet.readiness_timeout_sec,
    }
    if bootstrap_inputs is None:
        return payload

    selector = bootstrap_inputs.selector
    payload.update(
        {
            "prepare_mode": bootstrap_inputs.prepare_mode,
            "download_benchmarks": bootstrap_inputs.download_benchmarks,
            "benchmarks_root": str(selector.effective_benchmarks_root()),
        }
    )
    if selector.benchmark_suite is not None:
        payload["benchmark_suite"] = selector.benchmark_suite
        payload["benchmark_suites_root"] = str(
            selector.effective_benchmark_suites_root()
        )
    if selector.benchmarks is not None:
        payload["benchmarks"] = selector.benchmarks
    return payload


def encode_bootstrap_payload(payload: dict[str, object]) -> str:
    """Encode bootstrap payload as base64 JSON for instance metadata transport."""
    return base64.b64encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")


def load_startup_script() -> str:
    """Load the bundled worker startup script."""
    return _STARTUP_SCRIPT_PATH.read_text(encoding="utf-8")


def load_evaluator_startup_script() -> str:
    """Load the bundled evaluator startup script derived from worker bootstrap."""
    startup_script = load_startup_script()
    marker = "set -euo pipefail\n"
    override = (
        "set -euo pipefail\n"
        'CRSBENCH_STARTUP_MODE="${CRSBENCH_STARTUP_MODE:-evaluator}"\n'
    )
    if marker not in startup_script:
        raise ValueError("Worker startup script is missing the expected shell prologue")
    return startup_script.replace(marker, override, 1)


def load_orchestrator_startup_script() -> str:
    """Load the bundled orchestrator startup script."""
    return _ORCHESTRATOR_STARTUP_SCRIPT_PATH.read_text(encoding="utf-8")


def _apply_access_metadata(
    *,
    metadata: dict[str, str],
    config: _InstallMetadataConfig,
) -> None:
    metadata[GCE_ENABLE_OSLOGIN_KEY] = "TRUE"
    metadata[GCE_SERIAL_PORT_ENABLE_KEY] = "TRUE"
    metadata["block-project-ssh-keys"] = "TRUE"
    if config.ssh_via_iap:
        metadata["crsbench-ssh-via-iap"] = "TRUE"


def _apply_install_metadata(
    *,
    metadata: dict[str, str],
    config: _InstallMetadataConfig,
    env_passthrough: dict[str, str] | None = None,
) -> None:
    if config.crsbench_install_spec:
        metadata[CRSBENCH_INSTALL_SPEC_KEY] = config.crsbench_install_spec
        metadata[CRSBENCH_GIT_REF_KEY] = config.crsbench_git_ref

    if config.github_deploy_key_file:
        key_bytes = Path(config.github_deploy_key_file).read_bytes()
        metadata[CRSBENCH_GITHUB_DEPLOY_KEY] = base64.b64encode(key_bytes).decode(
            "ascii"
        )

    if config.hf_token:
        metadata[CRSBENCH_HF_TOKEN_KEY] = config.hf_token

    filtered_env_passthrough = _filter_env_passthrough(
        env_passthrough,
        skip_keys={"HF_TOKEN"} if config.hf_token else set(),
    )
    if filtered_env_passthrough:
        metadata[CRSBENCH_ENV_PASSTHROUGH_B64_KEY] = base64.b64encode(
            json.dumps(
                filtered_env_passthrough,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).decode("ascii")


def _apply_startup_script_metadata(
    *,
    metadata: dict[str, str],
    config: _InstallMetadataConfig,
    startup_script: str,
) -> None:
    if config.startup_script_uri:
        metadata[GCE_STARTUP_SCRIPT_URL_KEY] = config.startup_script_uri
        metadata.pop(GCE_STARTUP_SCRIPT_KEY, None)
        return
    metadata[GCE_STARTUP_SCRIPT_KEY] = startup_script
    metadata.pop(GCE_STARTUP_SCRIPT_URL_KEY, None)


def _read_experiment_config_bytes(experiment_config_path: str | Path) -> bytes:
    config_path = Path(experiment_config_path)
    if config_path.is_file():
        return config_path.read_bytes()
    return str(experiment_config_path).encode("utf-8")


def _filter_env_passthrough(
    env_passthrough: dict[str, str] | None,
    *,
    skip_keys: set[str],
) -> dict[str, str]:
    if not env_passthrough:
        return {}
    return {
        str(key): str(value)
        for key, value in env_passthrough.items()
        if str(key) not in skip_keys
    }


def build_instance_metadata(
    *,
    experiment_name: str,
    fleet: GceWorkerFleetConfig,
    redis_host: str,
    redis_password: str | None = None,
    registration: RuntimeRegistration,
    bootstrap_inputs: CloudVmBootstrapInputs | None = None,
    env_passthrough: dict[str, str] | None = None,
    worker_name: str,
    startup_script: str,
) -> dict[str, str]:
    """Render metadata consumed by GCE startup automation."""
    metadata = dict(fleet.metadata)
    metadata[CRSBENCH_BOOTSTRAP_PAYLOAD_KEY] = encode_bootstrap_payload(
        build_bootstrap_payload(
            experiment_name=experiment_name,
            worker_name=worker_name,
            redis_host=redis_host,
            registration=registration,
            fleet=fleet,
            bootstrap_inputs=bootstrap_inputs,
        )
    )
    metadata[CRSBENCH_EXPERIMENT_METADATA_KEY] = experiment_name
    metadata[CRSBENCH_WORKER_NAME_METADATA_KEY] = worker_name
    metadata[CRSBENCH_READINESS_TIMEOUT_METADATA_KEY] = str(fleet.readiness_timeout_sec)
    if redis_password:
        metadata[CRSBENCH_REDIS_PASSWORD_KEY] = redis_password

    _apply_access_metadata(metadata=metadata, config=fleet)
    _apply_install_metadata(
        metadata=metadata,
        config=fleet,
        env_passthrough=env_passthrough,
    )
    _apply_startup_script_metadata(
        metadata=metadata,
        config=fleet,
        startup_script=startup_script,
    )
    return metadata


def build_evaluator_metadata(
    *,
    experiment_name: str,
    fleet: GceWorkerFleetConfig,
    redis_host: str,
    registration: RuntimeRegistration,
    evaluator_name: str,
    experiment_config_path: str | Path,
    redis_password: str | None = None,
    bootstrap_inputs: CloudVmBootstrapInputs | None = None,
    env_passthrough: dict[str, str] | None = None,
    startup_script: str,
) -> dict[str, str]:
    """Render metadata consumed by the GCE evaluator bootstrap."""
    metadata = dict(fleet.metadata)
    metadata[CRSBENCH_BOOTSTRAP_PAYLOAD_KEY] = encode_bootstrap_payload(
        build_evaluator_bootstrap_payload(
            experiment_name=experiment_name,
            evaluator_name=evaluator_name,
            redis_host=redis_host,
            registration=registration,
            fleet=fleet,
            bootstrap_inputs=bootstrap_inputs,
        )
    )
    metadata[CRSBENCH_EXPERIMENT_METADATA_KEY] = experiment_name
    metadata[CRSBENCH_WORKER_NAME_METADATA_KEY] = evaluator_name
    metadata[CRSBENCH_READINESS_TIMEOUT_METADATA_KEY] = str(fleet.readiness_timeout_sec)
    metadata[CRSBENCH_EXPERIMENT_CONFIG_B64_KEY] = base64.b64encode(
        _read_experiment_config_bytes(experiment_config_path)
    ).decode("ascii")
    if redis_password:
        metadata[CRSBENCH_REDIS_PASSWORD_KEY] = redis_password

    _apply_access_metadata(metadata=metadata, config=fleet)
    _apply_install_metadata(
        metadata=metadata,
        config=fleet,
        env_passthrough=env_passthrough,
    )
    _apply_startup_script_metadata(
        metadata=metadata,
        config=fleet,
        startup_script=startup_script,
    )
    return metadata


def build_orchestrator_metadata(
    *,
    experiment_name: str,
    orchestrator: GceOrchestratorConfig,
    experiment_config_path: str | Path,
    env_passthrough: dict[str, str] | None = None,
    redis_password: str,
    startup_script: str,
) -> dict[str, str]:
    """Render metadata consumed by the GCE orchestrator bootstrap."""
    metadata = dict(orchestrator.metadata)
    metadata[CRSBENCH_EXPERIMENT_METADATA_KEY] = experiment_name
    metadata[CRSBENCH_REDIS_PASSWORD_KEY] = redis_password
    metadata[CRSBENCH_EXPERIMENT_CONFIG_B64_KEY] = base64.b64encode(
        _read_experiment_config_bytes(experiment_config_path)
    ).decode("ascii")

    _apply_access_metadata(metadata=metadata, config=orchestrator)
    _apply_install_metadata(
        metadata=metadata,
        config=orchestrator,
        env_passthrough=env_passthrough,
    )
    _apply_startup_script_metadata(
        metadata=metadata,
        config=orchestrator,
        startup_script=startup_script,
    )
    return metadata
