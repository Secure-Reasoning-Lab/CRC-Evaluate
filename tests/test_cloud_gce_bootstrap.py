"""Tests for GCE worker bootstrap metadata and startup script generation."""

import base64
import json
import shlex
import subprocess
from pathlib import Path

import yaml
from crsbench.cloud.bootstrap import CloudVmBootstrapInputs
from crsbench.distributed.registry import RuntimeRegistration
from crsbench.validation.schemas import GceOrchestratorConfig, GceWorkerFleetConfig


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
        "evaluator_build_jobs": 2,
        "evaluator_build_cores_per_job": 8,
        "evaluator_verify_jobs": 4,
        "evaluator_verify_cores_per_job": 4,
        "evaluator_idle_timeout": 600,
        "evaluator_cpu_tag": "c3d",
        "benchmarks_root": "/mnt/benchmarks",
        "modes": ["delta"],
        "sanitizers": ["address"],
        "config_hash": "cfg-hash",
    }
    data.update(overrides)
    return RuntimeRegistration(**data)


def _make_orchestrator(**overrides) -> GceOrchestratorConfig:
    data = {
        "project": "test-project",
        "zone": "us-central1-a",
        "machine_type": "e2-standard-16",
        "boot_disk_size_gb": 200,
        "image": "projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64",
        "service_account_email": "crsbench-orchestrator@test-project.iam.gserviceaccount.com",
        "owner_label": "team-crs",
        "labels": {"env": "prod"},
        "metadata": {"custom-key": "custom-value"},
        "instance_name_prefix": "gce-orchestrator",
        "use_os_login": True,
        "ssh_via_iap": True,
    }
    data.update(overrides)
    return GceOrchestratorConfig(**data)


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


def test_build_instance_metadata_includes_vm_bootstrap_policy_and_selector():
    """Worker payload should include prepare/download policy and benchmark selectors."""
    from crsbench.cloud.gce.metadata import (
        CRSBENCH_BOOTSTRAP_PAYLOAD_KEY,
        build_instance_metadata,
    )

    metadata = build_instance_metadata(
        experiment_name="Exp.Cloud 42",
        fleet=_make_fleet(),
        redis_host="redis.internal:6380",
        registration=_make_registration(),
        bootstrap_inputs=CloudVmBootstrapInputs(
            prepare_mode="skip_base_images",
            download_benchmarks="always",
            benchmark_suite="afc-final",
            benchmarks_root=Path("/srv/benchmarks"),
            benchmark_suites_root=Path("/srv/benchmark-suites"),
        ),
        worker_name="gce-worker-001",
        startup_script="#!/usr/bin/env bash\necho boot\n",
    )

    payload = _decode_payload(metadata[CRSBENCH_BOOTSTRAP_PAYLOAD_KEY])

    assert payload["prepare_mode"] == "skip_base_images"
    assert payload["download_benchmarks"] == "always"
    assert payload["benchmark_suite"] == "afc-final"
    assert payload["benchmarks_root"] == "/srv/benchmarks"
    assert payload["benchmark_suites_root"] == "/srv/benchmark-suites"


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
    """Bundled startup script should hand off the worker to a managed crsbench user service."""
    from crsbench.cloud.gce.metadata import load_startup_script

    startup_script = load_startup_script()

    assert "CRSBENCH_METADATA_BASE_URL" in startup_script
    assert "metadata.google.internal/computeMetadata/v1" in startup_script
    assert 'CRSBENCH_USER="${CRSBENCH_USER:-crsbench}"' in startup_script
    assert "CRSBENCH_REDIS_HOST" in startup_script
    assert "CRSBENCH_CLOUD_INSTANCE_ID" in startup_script
    assert "crsbench" in startup_script
    assert "--experiment-name" in startup_script
    assert "bootstrap_failed" in startup_script
    assert "loginctl enable-linger" in startup_script
    assert "NOPASSWD:ALL" in startup_script
    assert "dbus-user-session" in startup_script
    assert 'systemctl restart "user@${CRSBENCH_USER_UID}.service"' in startup_script
    assert 'CRSBENCH_USER_SERVICE_CGROUP="/sys/fs/cgroup/user.slice/' in startup_script
    assert (
        'CRSBENCH_RUNTIME_CGROUP="${CRSBENCH_USER_SERVICE_CGROUP}/crsbench"'
        in startup_script
    )
    assert (
        'CRSBENCH_OSS_CRS_CGROUP="${CRSBENCH_USER_SERVICE_CGROUP}/oss-crs"'
        in startup_script
    )
    assert "cgroup.subtree_control" in startup_script
    assert "systemctl --user enable --now crsbench-worker.service" in startup_script
    assert "/etc/systemd/system/crsbench-worker.service" not in startup_script
    assert "/etc/default/crsbench-worker" not in startup_script


def test_build_evaluator_metadata_embeds_startup_script_and_config_payload(tmp_path):
    """Evaluator metadata should embed config payload and evaluator runtime settings."""
    from crsbench.cloud.gce.metadata import (
        CRSBENCH_BOOTSTRAP_PAYLOAD_KEY,
        CRSBENCH_EXPERIMENT_CONFIG_B64_KEY,
        build_evaluator_metadata,
    )

    config_path = tmp_path / "config.yaml"
    config_path.write_text("experiment: exp-cloud-42\n", encoding="utf-8")

    metadata = build_evaluator_metadata(
        experiment_name="Exp.Cloud 42",
        fleet=_make_fleet(worker_name_prefix="gce-evaluator"),
        redis_host="redis.internal:6380",
        registration=_make_registration(),
        evaluator_name="gce-evaluator-001",
        experiment_config_path=config_path,
        startup_script="#!/usr/bin/env bash\necho boot\n",
    )

    assert metadata["startup-script"].startswith("#!/usr/bin/env bash")
    payload = _decode_payload(metadata[CRSBENCH_BOOTSTRAP_PAYLOAD_KEY])
    assert payload["experiment"] == "Exp.Cloud 42"
    assert payload["evaluator_name"] == "gce-evaluator-001"
    assert payload["evaluator_build_jobs"] == 2
    assert payload["evaluator_build_cores_per_job"] == 8
    assert payload["evaluator_verify_jobs"] == 4
    assert payload["evaluator_verify_cores_per_job"] == 4
    assert payload["evaluator_idle_timeout"] == 600
    assert payload["evaluator_cpu_tag"] == "c3d"
    decoded_config = base64.b64decode(
        metadata[CRSBENCH_EXPERIMENT_CONFIG_B64_KEY]
    ).decode("utf-8")
    assert "experiment: exp-cloud-42" in decoded_config


def test_load_evaluator_startup_script_contains_managed_evaluator_service_bootstrap():
    """Bundled evaluator startup script should hand off the evaluator to a managed user service."""
    from crsbench.cloud.gce.metadata import load_evaluator_startup_script

    startup_script = load_evaluator_startup_script()

    assert "CRSBENCH_METADATA_BASE_URL" in startup_script
    assert "CRSBENCH_CLOUD_INSTANCE_ID" in startup_script
    assert 'CRSBENCH_USER="${CRSBENCH_USER:-crsbench}"' in startup_script
    assert "crsbench-evaluator.service" in startup_script
    assert "systemctl --user enable --now crsbench-evaluator.service" in startup_script
    assert "crsbench evaluator" in startup_script
    assert "--experiment-config" in startup_script
    assert "CRSBENCH_EVALUATOR_NAME" in startup_script
    assert "CRSBENCH_CLOUD_ROLE" in startup_script
    assert "loginctl enable-linger" in startup_script
    assert "dbus-user-session" in startup_script


def test_build_instance_metadata_includes_install_spec_from_fleet_config():
    """Install spec from fleet config field should appear in instance metadata."""
    from crsbench.cloud.gce.metadata import build_instance_metadata

    fleet = _make_fleet(
        crsbench_install_spec="git+ssh://git@github.com/sslab-gatech/CRSBench.git",
        metadata={"custom-key": "custom-value"},
    )
    metadata = build_instance_metadata(
        experiment_name="exp-42",
        fleet=fleet,
        redis_host="redis.internal:6380",
        registration=_make_registration(),
        worker_name="gce-worker-001",
        startup_script="#!/usr/bin/env bash\n",
    )

    assert (
        metadata["crsbench-install-spec"]
        == "git+ssh://git@github.com/sslab-gatech/CRSBench.git"
    )


def test_build_instance_metadata_omits_install_spec_when_none():
    """When crsbench_install_spec is None and not in metadata dict, key is absent."""
    from crsbench.cloud.gce.metadata import build_instance_metadata

    fleet = _make_fleet(
        crsbench_install_spec=None,
        metadata={"custom-key": "custom-value"},
    )
    metadata = build_instance_metadata(
        experiment_name="exp-42",
        fleet=fleet,
        redis_host="redis.internal:6380",
        registration=_make_registration(),
        worker_name="gce-worker-001",
        startup_script="#!/usr/bin/env bash\n",
    )

    assert "crsbench-install-spec" not in metadata


def test_build_instance_metadata_fleet_config_install_spec_overrides_manual_metadata():
    """Field-level crsbench_install_spec should override the raw metadata dict value."""
    from crsbench.cloud.gce.metadata import build_instance_metadata

    fleet = _make_fleet(
        crsbench_install_spec="git+ssh://git@github.com/sslab-gatech/CRSBench.git",
        metadata={"crsbench-install-spec": "old-value"},
    )
    metadata = build_instance_metadata(
        experiment_name="exp-42",
        fleet=fleet,
        redis_host="redis.internal:6380",
        registration=_make_registration(),
        worker_name="gce-worker-001",
        startup_script="#!/usr/bin/env bash\n",
    )

    assert (
        metadata["crsbench-install-spec"]
        == "git+ssh://git@github.com/sslab-gatech/CRSBench.git"
    )


def test_metadata_includes_github_deploy_key(tmp_path):
    """github_deploy_key_path in fleet config causes base64-encoded key in metadata."""
    from crsbench.cloud.gce.metadata import (
        CRSBENCH_GITHUB_DEPLOY_KEY,
        build_instance_metadata,
    )

    key_content = b"-----BEGIN OPENSSH PRIVATE KEY-----\nFAKEKEYDATA\n"
    key_file = tmp_path / "crsbench-deploy"
    key_file.write_bytes(key_content)

    fleet = _make_fleet(
        github_deploy_key_path=str(key_file),
        metadata={"custom-key": "custom-value"},
    )
    metadata = build_instance_metadata(
        experiment_name="exp-42",
        fleet=fleet,
        redis_host="redis.internal:6380",
        registration=_make_registration(),
        worker_name="gce-worker-001",
        startup_script="#!/usr/bin/env bash\n",
    )

    assert CRSBENCH_GITHUB_DEPLOY_KEY in metadata
    import base64

    decoded = base64.b64decode(metadata[CRSBENCH_GITHUB_DEPLOY_KEY])
    assert decoded == key_content


def test_metadata_includes_hf_token_in_env_passthrough_blob():
    """HF_TOKEN should travel through the generic env metadata bundle."""
    from crsbench.cloud.gce.metadata import (
        CRSBENCH_ENV_PASSTHROUGH_B64_KEY,
        build_instance_metadata,
    )

    fleet = _make_fleet(
        metadata={"custom-key": "custom-value"},
    )
    metadata = build_instance_metadata(
        experiment_name="exp-42",
        fleet=fleet,
        redis_host="redis.internal:6380",
        registration=_make_registration(),
        env_passthrough={"HF_TOKEN": "hf_test_token_abc123"},
        worker_name="gce-worker-001",
        startup_script="#!/usr/bin/env bash\n",
    )

    passthrough = json.loads(
        base64.b64decode(metadata[CRSBENCH_ENV_PASSTHROUGH_B64_KEY]).decode("utf-8")
    )
    assert passthrough == {"HF_TOKEN": "hf_test_token_abc123"}


def test_metadata_includes_env_passthrough_blob_without_special_hf_token_handling():
    """Role env should be encoded exactly as provided, including HF_TOKEN."""
    from crsbench.cloud.gce.metadata import (
        CRSBENCH_ENV_PASSTHROUGH_B64_KEY,
        build_instance_metadata,
    )

    fleet = _make_fleet(
        metadata={"custom-key": "custom-value"},
    )
    metadata = build_instance_metadata(
        experiment_name="exp-42",
        fleet=fleet,
        redis_host="redis.internal:6380",
        registration=_make_registration(),
        env_passthrough={
            "HF_TOKEN": "hf_test_token_abc123",
            "CRSBENCH_LLM_MASTER_KEY": "master-key",
        },
        worker_name="gce-worker-001",
        startup_script="#!/usr/bin/env bash\n",
    )

    passthrough = json.loads(
        base64.b64decode(metadata[CRSBENCH_ENV_PASSTHROUGH_B64_KEY]).decode("utf-8")
    )
    assert passthrough == {
        "HF_TOKEN": "hf_test_token_abc123",
        "CRSBENCH_LLM_MASTER_KEY": "master-key",
    }


def test_metadata_omits_secrets_when_not_configured():
    """Default fleet (no key/token) must not include secret metadata keys."""
    from crsbench.cloud.gce.metadata import (
        CRSBENCH_ENV_PASSTHROUGH_B64_KEY,
        CRSBENCH_GITHUB_DEPLOY_KEY,
        build_instance_metadata,
    )

    fleet = _make_fleet(metadata={"custom-key": "custom-value"})
    metadata = build_instance_metadata(
        experiment_name="exp-42",
        fleet=fleet,
        redis_host="redis.internal:6380",
        registration=_make_registration(),
        worker_name="gce-worker-001",
        startup_script="#!/usr/bin/env bash\n",
    )

    assert CRSBENCH_GITHUB_DEPLOY_KEY not in metadata
    assert CRSBENCH_ENV_PASSTHROUGH_B64_KEY not in metadata


def test_metadata_includes_git_ref():
    """crsbench_git_ref in fleet config is passed as crsbench-git-ref metadata."""
    from crsbench.cloud.gce.metadata import (
        CRSBENCH_GIT_REF_KEY,
        build_instance_metadata,
    )

    fleet = _make_fleet(
        crsbench_install_spec="git+ssh://git@github.com/org/Repo.git",
        crsbench_git_ref="feat/gcp",
    )
    metadata = build_instance_metadata(
        experiment_name="exp-ref",
        fleet=fleet,
        redis_host="redis.internal:6380",
        registration=_make_registration(),
        worker_name="gce-worker-001",
        startup_script="#!/usr/bin/env bash\n",
    )

    assert metadata[CRSBENCH_GIT_REF_KEY] == "feat/gcp"


def test_metadata_git_ref_defaults_to_main():
    """When crsbench_git_ref is not set, it defaults to 'main'."""
    from crsbench.cloud.gce.metadata import (
        CRSBENCH_GIT_REF_KEY,
        build_instance_metadata,
    )

    fleet = _make_fleet(
        crsbench_install_spec="git+ssh://git@github.com/org/Repo.git",
    )
    metadata = build_instance_metadata(
        experiment_name="exp-default",
        fleet=fleet,
        redis_host="redis.internal:6380",
        registration=_make_registration(),
        worker_name="gce-worker-001",
        startup_script="#!/usr/bin/env bash\n",
    )

    assert metadata[CRSBENCH_GIT_REF_KEY] == "main"


def test_startup_script_contains_git_clone_path():
    """Startup script should include git clone, uv sync, deploy key, and env handling."""
    from crsbench.cloud.gce.metadata import load_startup_script

    script = load_startup_script()

    assert "git clone" in script
    assert "uv sync" in script
    assert "crsbench-github-deploy-key" in script
    assert "crsbench-env-passthrough-b64" in script
    assert "crsbench-git-ref" in script
    assert "git checkout" in script
    assert "python3-pip" in script


def test_startup_script_supports_public_git_clone_specs():
    """Worker bootstrap should treat any git+ URL as a clone-based install path."""
    from crsbench.cloud.gce.metadata import load_startup_script

    script = load_startup_script()

    assert 'if [[ -z "${INSTALL_SPEC}" || "${INSTALL_SPEC}" != git+* ]]; then' in script
    assert 'REPO_URL="${INSTALL_SPEC#git+}"' in script


def test_startup_script_requires_git_checkout_install_spec():
    """Worker bootstrap should reject non-git install specs for checkout-first runs."""
    from crsbench.cloud.gce.metadata import load_startup_script

    script = load_startup_script()

    assert 'if [[ -z "${INSTALL_SPEC}" || "${INSTALL_SPEC}" != git+* ]]; then' in script
    assert "cloud worker bootstrap requires git+ install spec" in script
    assert "python3 -m venv" not in script


def test_startup_script_runs_shared_vm_bootstrap_from_repo_checkout():
    """Worker bootstrap should run the shared prepare/download helper from the repo root."""
    from crsbench.cloud.gce.metadata import load_startup_script

    script = load_startup_script()

    assert "crsbench-env-passthrough-b64" in script
    assert "bootstrap_inputs_from_payload" in script
    assert "run_cloud_vm_bootstrap" in script
    assert 'CLONE_DIR="${CRSBENCH_CLONE_DIR:-/opt/crsbench}"' in script
    assert 'CRSBENCH_USER="${CRSBENCH_USER:-crsbench}"' in script
    assert 'sudo -H -u "${CRSBENCH_USER}"' in script
    assert "WorkingDirectory=${CLONE_DIR}" in script
    assert "ExecStart=/bin/bash ${LAUNCHER_PATH}" in script
    assert 'git config --global --add safe.directory "${repo_path}"' in script
    assert 'git config --global --add safe.directory "${repo_path}/.git"' in script
    assert 'write_env_var "PATH" "${VENV_BIN}' in script
    assert (
        'CRSBENCH_USER_HOME="${CRSBENCH_USER_HOME:-/home/${CRSBENCH_USER}}"' in script
    )
    assert "${CRSBENCH_USER_HOME}/.local/bin" in script


def test_startup_script_supports_file_backed_metadata_and_foreground_service_mode():
    """Worker bootstrap should run outside GCE/systemd for local Docker rehearsal."""
    from crsbench.cloud.gce.metadata import load_startup_script

    script = load_startup_script()

    assert 'CRSBENCH_METADATA_ROOT_DIR="${CRSBENCH_METADATA_ROOT_DIR:-}"' in script
    assert (
        'CRSBENCH_METADATA_BASE_URL="${CRSBENCH_METADATA_BASE_URL:-'
        'http://metadata.google.internal/computeMetadata/v1}"'
    ) in script
    assert 'if [[ -n "${CRSBENCH_METADATA_ROOT_DIR}" ]]; then' in script
    assert 'CRSBENCH_SERVICE_MANAGER="${CRSBENCH_SERVICE_MANAGER:-auto}"' in script
    assert "[[ -d /run/systemd/system ]]" in script
    assert 'exec sudo -H -u "${CRSBENCH_USER}" /bin/bash "${LAUNCHER_PATH}"' in script


def test_startup_script_supports_apt_and_apk_bootstrap_dependencies():
    """Worker bootstrap should install missing dependencies on Debian or Alpine hosts."""
    from crsbench.cloud.gce.metadata import load_startup_script

    script = load_startup_script()

    assert "apt-get install -y -qq" in script
    assert "apk add --no-cache" in script
    assert "Docker daemon is unavailable after waiting" in script


def test_startup_script_configures_timezone_and_docker_cgroupfs():
    """Worker bootstrap should align VM timezone and Docker cgroup driver for oss-crs."""
    from crsbench.cloud.gce.metadata import load_startup_script

    script = load_startup_script()

    assert "America/New_York" in script
    assert "/etc/docker/daemon.json" in script
    assert "native.cgroupdriver=cgroupfs" in script
    assert "systemctl restart docker" in script


def test_startup_script_does_not_globally_rewrite_sslab_gatech_https_urls():
    """Worker bootstrap should leave public submodule URLs on their declared transport."""
    from crsbench.cloud.gce.metadata import load_startup_script

    script = load_startup_script()

    assert (
        'git config --global url."git@github.com:sslab-gatech/".insteadOf '
        '"https://github.com/sslab-gatech/"'
    ) not in script
    assert "GIT_SSH_COMMAND" in script


def test_startup_script_waits_for_redis_before_starting_worker_service():
    """Worker launcher should poll Redis readiness before entering terminal bootstrap failure."""
    from crsbench.cloud.gce.metadata import load_startup_script

    script = load_startup_script()

    assert "probe_redis_connection" in script
    assert "RedisConnectionProbe" in script
    assert "CRSBENCH_READINESS_TIMEOUT_SEC" in script
    assert "wait_for_redis()" in script
    assert "Waiting for Redis at ${CRSBENCH_REDIS_HOST}" in script
    assert "Fatal Redis bootstrap error" in script
    assert "Timed out waiting for Redis" in script


def test_wait_for_redis_fails_fast_on_fatal_probe_error(tmp_path):
    """Launcher should stop immediately on fatal Redis auth/config probe failures."""
    from crsbench.cloud.gce.metadata import load_startup_script

    script = load_startup_script()
    function_start = script.index("wait_for_redis() {")
    function_end = script.index("\n\ncmd=(", function_start)
    wait_for_redis = script[function_start:function_end]

    stub_root = tmp_path / "stubs"
    distributed_pkg = stub_root / "crsbench" / "distributed"
    distributed_pkg.mkdir(parents=True)
    (stub_root / "crsbench" / "__init__.py").write_text("", encoding="utf-8")
    (distributed_pkg / "__init__.py").write_text("", encoding="utf-8")
    (distributed_pkg / "queue.py").write_text(
        """
from enum import StrEnum


class RedisConnectionProbe(StrEnum):
    READY = "ready"
    RETRYABLE = "retryable"
    FATAL = "fatal"


def probe_redis_connection(redis_host: str, timeout: int = 2):
    return RedisConnectionProbe.FATAL, "bad password"
""".strip(),
        encoding="utf-8",
    )

    report_path = tmp_path / "report.txt"
    harness_path = tmp_path / "wait-for-redis.sh"
    harness_path.write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                "set -euo pipefail",
                f"REPORT_PATH={shlex.quote(str(report_path))}",
                'report_bootstrap_failure() { printf "%s\\n" "$1" > "${REPORT_PATH}"; }',
                wait_for_redis,
                f"export PYTHONPATH={shlex.quote(str(stub_root))}",
                "export CRSBENCH_REDIS_HOST=redis.internal:6379",
                "export CRSBENCH_READINESS_TIMEOUT_SEC=30",
                "wait_for_redis",
            ]
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        ["bash", str(harness_path)],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
        timeout=2,
    )

    assert result.returncode == 1
    assert "Fatal Redis bootstrap error" in report_path.read_text(encoding="utf-8")
    assert "bad password" in report_path.read_text(encoding="utf-8")


def test_build_orchestrator_metadata_embeds_config_payload_and_redis_password(tmp_path):
    """Orchestrator metadata should carry config payload and shared Redis auth."""
    from crsbench.cloud.gce.metadata import (
        CRSBENCH_ENV_PASSTHROUGH_B64_KEY,
        CRSBENCH_EXPERIMENT_CONFIG_B64_KEY,
        CRSBENCH_REDIS_PASSWORD_KEY,
        build_orchestrator_metadata,
    )

    config_path = tmp_path / "config.yaml"
    config_path.write_text("experiment:\n  name: exp-42\n")

    metadata = build_orchestrator_metadata(
        experiment_name="exp-42",
        orchestrator=_make_orchestrator(),
        experiment_config_path=config_path,
        env_passthrough={
            "CRSBENCH_LLM_UPSTREAM_BASE_URL": "https://llm.example.test",
            "OPENAI_API_KEY": "openai-key",
        },
        redis_password="shared-secret",
        startup_script="#!/usr/bin/env bash\necho boot\n",
    )

    assert metadata[CRSBENCH_REDIS_PASSWORD_KEY] == "shared-secret"
    assert (
        base64.b64decode(metadata[CRSBENCH_EXPERIMENT_CONFIG_B64_KEY]).decode("utf-8")
        == config_path.read_text()
    )
    passthrough = json.loads(
        base64.b64decode(metadata[CRSBENCH_ENV_PASSTHROUGH_B64_KEY]).decode("utf-8")
    )
    assert passthrough == {
        "CRSBENCH_LLM_UPSTREAM_BASE_URL": "https://llm.example.test",
        "OPENAI_API_KEY": "openai-key",
    }


def test_orchestrator_startup_script_consumes_config_payload_and_preprovisioned_mode():
    """Orchestrator startup should decode config payload and run under the crsbench user."""
    from crsbench.cloud.gce.metadata import load_orchestrator_startup_script

    script = load_orchestrator_startup_script()

    assert 'CRSBENCH_USER="${CRSBENCH_USER:-crsbench}"' in script
    assert "crsbench-experiment-config-b64" in script
    assert "crsbench-env-passthrough-b64" in script
    assert "CRSBENCH_CLOUD_PREPROVISIONED_WORKERS" in script
    assert "crsbench-redis-password" in script
    assert "loginctl enable-linger" in script
    assert "NOPASSWD:ALL" in script
    assert "dbus-user-session" in script
    assert 'systemctl restart "user@${CRSBENCH_USER_UID}.service"' in script
    assert 'CRSBENCH_USER_SERVICE_CGROUP="/sys/fs/cgroup/user.slice/' in script
    assert (
        'CRSBENCH_RUNTIME_CGROUP="${CRSBENCH_USER_SERVICE_CGROUP}/crsbench"' in script
    )
    assert 'CRSBENCH_OSS_CRS_CGROUP="${CRSBENCH_USER_SERVICE_CGROUP}/oss-crs"' in script
    assert "cgroup.subtree_control" in script
    assert "systemctl --user enable --now crsbench-orchestrator.service" in script
    assert "python3-pip" in script
    assert "python3-yaml" in script
    assert "git checkout" in script
    assert "except Exception:" in script
    assert "yaml.safe_load" in script


def test_orchestrator_startup_script_supports_public_git_clone_specs():
    """Orchestrator bootstrap should treat any git+ URL as a clone-based install path."""
    from crsbench.cloud.gce.metadata import load_orchestrator_startup_script

    script = load_orchestrator_startup_script()

    assert 'if [[ -z "${INSTALL_SPEC}" || "${INSTALL_SPEC}" != git+* ]]; then' in script
    assert 'REPO_URL="${INSTALL_SPEC#git+}"' in script


def test_orchestrator_startup_script_requires_git_checkout_install_spec():
    """Orchestrator bootstrap should reject non-git install specs for checkout-first runs."""
    from crsbench.cloud.gce.metadata import load_orchestrator_startup_script

    script = load_orchestrator_startup_script()

    assert 'if [[ -z "${INSTALL_SPEC}" || "${INSTALL_SPEC}" != git+* ]]; then' in script
    assert "cloud orchestrator bootstrap requires git+ install spec" in script
    assert "python3 -m venv" not in script


def test_orchestrator_startup_script_runs_shared_vm_bootstrap_from_repo_checkout():
    """Orchestrator bootstrap should prepare/download from the cloned checkout first."""
    from crsbench.cloud.gce.metadata import load_orchestrator_startup_script

    script = load_orchestrator_startup_script()

    assert "CloudVmBootstrapInputs.from_experiment_config" in script
    assert "run_cloud_vm_bootstrap" in script
    assert 'CLONE_DIR="${CRSBENCH_CLONE_DIR:-/opt/crsbench}"' in script
    assert 'CRSBENCH_USER="${CRSBENCH_USER:-crsbench}"' in script
    assert 'sudo -H -u "${CRSBENCH_USER}"' in script
    assert 'git config --global --add safe.directory "${repo_path}"' in script
    assert 'git config --global --add safe.directory "${repo_path}/.git"' in script
    assert 'cd "${CLONE_DIR}"' in script


def test_orchestrator_startup_script_supports_file_backed_metadata_sources():
    """Orchestrator bootstrap should support local rehearsal without systemd."""
    from crsbench.cloud.gce.metadata import load_orchestrator_startup_script

    script = load_orchestrator_startup_script()

    assert 'CRSBENCH_SERVICE_MANAGER="${CRSBENCH_SERVICE_MANAGER:-auto}"' in script
    assert 'CRSBENCH_METADATA_ROOT_DIR="${CRSBENCH_METADATA_ROOT_DIR:-}"' in script
    assert (
        'CRSBENCH_METADATA_BASE_URL="${CRSBENCH_METADATA_BASE_URL:-'
        'http://metadata.google.internal/computeMetadata/v1}"'
    ) in script
    assert 'if [[ -n "${CRSBENCH_METADATA_ROOT_DIR}" ]]; then' in script
    assert 'exec sudo -H -u "${CRSBENCH_USER}" /bin/bash "${LAUNCHER_PATH}"' in script


def test_orchestrator_startup_script_supports_apt_and_apk_bootstrap_dependencies():
    """Orchestrator bootstrap should install missing dependencies on Debian or Alpine hosts."""
    from crsbench.cloud.gce.metadata import load_orchestrator_startup_script

    script = load_orchestrator_startup_script()

    assert "apt-get install -y -qq" in script
    assert "apk add --no-cache" in script
    assert "Docker daemon is unavailable after waiting" in script


def test_orchestrator_startup_script_configures_timezone_and_docker_cgroupfs():
    """Orchestrator bootstrap should align VM timezone and Docker cgroup driver for oss-crs."""
    from crsbench.cloud.gce.metadata import load_orchestrator_startup_script

    script = load_orchestrator_startup_script()

    assert "America/New_York" in script
    assert "/etc/docker/daemon.json" in script
    assert "native.cgroupdriver=cgroupfs" in script
    assert "systemctl restart docker" in script


def test_orchestrator_startup_script_binds_valkey_to_loopback_and_internal_ip():
    """Valkey should listen on loopback plus the discovered internal/container IP, not 0.0.0.0."""
    from crsbench.cloud.gce.metadata import load_orchestrator_startup_script

    script = load_orchestrator_startup_script()

    assert 'CRSBENCH_REDIS_BIND_HOST="${CRSBENCH_REDIS_BIND_HOST:-}"' in script
    assert 'instance_metadata_get "network-interfaces/0/ip"' in script
    assert 'write_env_var "CRSBENCH_REDIS_BIND_HOST" "${REDIS_BIND_HOST}"' in script
    assert '-p "127.0.0.1:6379:6379"' in script
    assert '-p "\\${CRSBENCH_REDIS_BIND_HOST}:6379:6379"' in script
    assert "0.0.0.0:6379:6379" not in script


def test_orchestrator_startup_script_does_not_globally_rewrite_sslab_gatech_https_urls():
    """Orchestrator bootstrap should leave public submodule URLs on their declared transport."""
    from crsbench.cloud.gce.metadata import load_orchestrator_startup_script

    script = load_orchestrator_startup_script()

    assert (
        'git config --global url."git@github.com:sslab-gatech/".insteadOf '
        '"https://github.com/sslab-gatech/"'
    ) not in script
    assert "GIT_SSH_COMMAND" in script


def test_patch_orchestrator_config_adds_top_level_and_nested_runtime_redis_host(
    tmp_path,
):
    """Remote orchestrator bootstrap should rewrite grouped runtime redis config too."""
    from crsbench.cloud.gce.orchestrator_config import (
        patch_experiment_config_for_local_redis,
    )

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
experiment: exp-42
runtime:
  redis:
    host: redis.internal:6379
""".strip()
    )

    patch_experiment_config_for_local_redis(config_path, redis_host="localhost:6379")

    patched = yaml.safe_load(config_path.read_text())
    assert patched["redis_host"] == "localhost:6379"
    assert patched["runtime"]["redis"]["host"] == "localhost:6379"
