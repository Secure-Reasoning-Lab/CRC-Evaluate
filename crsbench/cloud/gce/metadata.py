"""Metadata and startup payload assembly for GCE-backed workers."""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from crsbench.distributed.registry import RuntimeRegistration
    from crsbench.validation.schemas import GceWorkerFleetConfig

CRSBENCH_BOOTSTRAP_PAYLOAD_KEY = "crsbench-bootstrap-payload"
CRSBENCH_INSTALL_SPEC_KEY = "crsbench-install-spec"
CRSBENCH_EXPERIMENT_METADATA_KEY = "crsbench-experiment"
CRSBENCH_WORKER_NAME_METADATA_KEY = "crsbench-worker-name"
CRSBENCH_READINESS_TIMEOUT_METADATA_KEY = "crsbench-readiness-timeout-sec"
GCE_ENABLE_OSLOGIN_KEY = "enable-oslogin"
GCE_SERIAL_PORT_ENABLE_KEY = "serial-port-enable"
GCE_STARTUP_SCRIPT_KEY = "startup-script"
GCE_STARTUP_SCRIPT_URL_KEY = "startup-script-url"

_STARTUP_SCRIPT_PATH = Path(__file__).with_name("startup") / "worker.sh"
_LABEL_PATTERN = re.compile(r"[^a-z0-9_-]+")


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


def build_worker_labels(
    *,
    experiment_name: str,
    fleet: GceWorkerFleetConfig,
) -> dict[str, str]:
    """Render deterministic GCE labels for a CRSBench worker fleet."""
    labels = {
        _sanitize_label_key(key): _sanitize_label_value(value)
        for key, value in fleet.labels.items()
    }
    labels["owner"] = _sanitize_label_value(
        fleet.owner_label or fleet.labels.get("owner", "crsbench")
    )
    labels["crsbench-experiment"] = _sanitize_label_value(experiment_name)
    labels["crsbench-role"] = "worker"
    return labels


def build_bootstrap_payload(
    *,
    experiment_name: str,
    worker_name: str,
    redis_host: str,
    registration: RuntimeRegistration,
    fleet: GceWorkerFleetConfig,
) -> dict[str, object]:
    """Build the minimal configless-worker payload consumed at VM boot."""
    return {
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


def encode_bootstrap_payload(payload: dict[str, object]) -> str:
    """Encode bootstrap payload as base64 JSON for instance metadata transport."""
    return base64.b64encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")


def load_startup_script() -> str:
    """Load the bundled worker startup script."""
    return _STARTUP_SCRIPT_PATH.read_text(encoding="utf-8")


def build_instance_metadata(
    *,
    experiment_name: str,
    fleet: GceWorkerFleetConfig,
    redis_host: str,
    registration: RuntimeRegistration,
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
        )
    )
    metadata[CRSBENCH_EXPERIMENT_METADATA_KEY] = experiment_name
    metadata[CRSBENCH_WORKER_NAME_METADATA_KEY] = worker_name
    metadata[CRSBENCH_READINESS_TIMEOUT_METADATA_KEY] = str(fleet.readiness_timeout_sec)
    metadata[GCE_ENABLE_OSLOGIN_KEY] = "TRUE"
    metadata[GCE_SERIAL_PORT_ENABLE_KEY] = "TRUE"
    metadata["block-project-ssh-keys"] = "TRUE"
    if fleet.ssh_via_iap:
        metadata["crsbench-ssh-via-iap"] = "TRUE"

    if fleet.crsbench_install_spec:
        metadata[CRSBENCH_INSTALL_SPEC_KEY] = fleet.crsbench_install_spec

    if fleet.startup_script_uri:
        metadata[GCE_STARTUP_SCRIPT_URL_KEY] = fleet.startup_script_uri
        metadata.pop(GCE_STARTUP_SCRIPT_KEY, None)
    else:
        metadata[GCE_STARTUP_SCRIPT_KEY] = startup_script
        metadata.pop(GCE_STARTUP_SCRIPT_URL_KEY, None)
    return metadata
