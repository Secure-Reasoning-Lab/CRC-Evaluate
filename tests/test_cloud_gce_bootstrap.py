"""Tests for GCE worker bootstrap metadata and startup script generation."""

import base64
import json

from crsbench.distributed.registry import RuntimeRegistration
from crsbench.validation.schemas import GceWorkerFleetConfig


def _make_fleet(**overrides) -> GceWorkerFleetConfig:
    data = {
        "project": "test-project",
        "zone": "us-central1-a",
        "worker_count": 1,
        "machine_type": "e2-standard-16",
        "boot_disk_size_gb": 200,
        "image": "projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64",
        "service_account_email": "crsbench-worker@test-project.iam.gserviceaccount.com",
        "owner_label": "team-crs",
        "labels": {"env": "prod"},
        "metadata": {
            "crsbench-install-spec": "crsbench @ file:///opt/crsbench.whl",
            "custom-key": "custom-value",
        },
        "worker_name_prefix": "gce-worker",
        "use_os_login": True,
        "ssh_via_iap": True,
        "readiness_timeout_sec": 900,
    }
    data.update(overrides)
    return GceWorkerFleetConfig(**data)


def _make_registration(**overrides) -> RuntimeRegistration:
    data = {
        "experiment": "exp-cloud-42",
        "trial_queue": "crsbench_trial",
        "build_queue": "crsbench_build",
        "verify_queue": "crsbench_verify",
        "worker_jobs": 3,
        "worker_cores_per_job": 6,
        "worker_cpu_tag": "c3",
        "benchmarks_root": "/mnt/benchmarks",
        "modes": ["delta"],
        "sanitizers": ["address"],
        "config_hash": "cfg-hash",
    }
    data.update(overrides)
    return RuntimeRegistration(**data)


def _decode_payload(encoded: str) -> dict[str, object]:
    return json.loads(base64.b64decode(encoded).decode("utf-8"))


def test_build_instance_metadata_embeds_startup_script_and_bootstrap_payload():
    """Default metadata should inline the bundled startup script and payload."""
    from crsbench.cloud.gce.metadata import (
        CRSBENCH_BOOTSTRAP_PAYLOAD_KEY,
        build_instance_metadata,
    )

    metadata = build_instance_metadata(
        experiment_name="Exp.Cloud 42",
        fleet=_make_fleet(),
        redis_host="redis.internal:6380",
        registration=_make_registration(),
        worker_name="gce-worker-001",
        startup_script="#!/usr/bin/env bash\necho boot\n",
    )

    assert metadata["startup-script"].startswith("#!/usr/bin/env bash")
    assert metadata["enable-oslogin"] == "TRUE"
    assert metadata["serial-port-enable"] == "TRUE"
    assert metadata["custom-key"] == "custom-value"

    payload = _decode_payload(metadata[CRSBENCH_BOOTSTRAP_PAYLOAD_KEY])
    assert payload["experiment"] == "Exp.Cloud 42"
    assert payload["worker_name"] == "gce-worker-001"
    assert payload["worker_jobs"] == 3
    assert payload["worker_cores_per_job"] == 6
    assert payload["readiness_timeout_sec"] == 900


def test_build_instance_metadata_uses_startup_script_url_when_configured():
    """Configured startup script URIs should use startup-script-url metadata."""
    from crsbench.cloud.gce.metadata import build_instance_metadata

    metadata = build_instance_metadata(
        experiment_name="Exp.Cloud 42",
        fleet=_make_fleet(
            startup_script_uri="gs://example-bucket/crsbench/worker-startup.sh"
        ),
        redis_host="redis.internal:6380",
        registration=_make_registration(),
        worker_name="gce-worker-001",
        startup_script="#!/usr/bin/env bash\necho ignored\n",
    )

    assert "startup-script" not in metadata
    assert (
        metadata["startup-script-url"]
        == "gs://example-bucket/crsbench/worker-startup.sh"
    )


def test_load_startup_script_contains_managed_worker_service_bootstrap():
    """Bundled startup script should install an env file and a managed worker service."""
    from crsbench.cloud.gce.metadata import load_startup_script

    startup_script = load_startup_script()

    assert "http://metadata.google.internal/computeMetadata/v1/" in startup_script
    assert "CRSBENCH_REDIS_HOST" in startup_script
    assert "CRSBENCH_CLOUD_INSTANCE_ID" in startup_script
    assert "crsbench" in startup_script
    assert "--experiment-name" in startup_script
    assert "bootstrap_failed" in startup_script
    assert "systemctl enable --now crsbench-worker.service" in startup_script
    assert "/etc/default/crsbench-worker" in startup_script
