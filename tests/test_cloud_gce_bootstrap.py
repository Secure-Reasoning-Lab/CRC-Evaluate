"""Tests for GCE worker bootstrap metadata and startup script generation."""

import base64
import json
import os
import shlex
import subprocess
from pathlib import Path

import pytest
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


def _extract_prompt_block(script: str) -> str:
    start_marker = "\n# >>> CRSBench prompt >>>\n"
    end_marker = "\n# <<< CRSBench prompt <<<\n"
    start = script.index(start_marker) + 1
    end = script.index(end_marker, start) + 1
    return script[start:end]


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
        CRSBENCH_DOWNLOAD_DELAY_SEC_KEY,
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
            gitcache=True,
            benchmark_suite="afc-final",
            benchmarks_root=Path("/srv/benchmarks"),
            benchmark_suites_root=Path("/srv/benchmark-suites"),
        ),
        download_delay_sec=10,
        worker_name="gce-worker-001",
        startup_script="#!/usr/bin/env bash\necho boot\n",
    )

    payload = _decode_payload(metadata[CRSBENCH_BOOTSTRAP_PAYLOAD_KEY])

    assert metadata[CRSBENCH_DOWNLOAD_DELAY_SEC_KEY] == "10"
    assert payload["prepare_mode"] == "skip_base_images"
    assert payload["download_benchmarks"] == "always"
    assert payload["gitcache"] is True
    assert payload["benchmark_suite"] == "afc-final"
    assert payload["benchmarks_root"] == "/srv/benchmarks"
    assert payload["benchmark_suites_root"] == "/srv/benchmark-suites"


def test_build_instance_metadata_omits_worker_name_for_regional_bulk_insert():
    """Regional bulk insert shares metadata, so worker identity must come from the VM itself."""
    from crsbench.cloud.gce.metadata import (
        CRSBENCH_BOOTSTRAP_PAYLOAD_KEY,
        CRSBENCH_WORKER_NAME_METADATA_KEY,
        build_instance_metadata,
    )

    metadata = build_instance_metadata(
        experiment_name="Exp.Cloud 42",
        fleet=_make_fleet(region="us-east5", zones=["us-east5-b", "us-east5-c"]),
        redis_host="redis.internal:6380",
        registration=_make_registration(),
        worker_name=None,
        startup_script="#!/usr/bin/env bash\necho boot\n",
    )

    payload = _decode_payload(metadata[CRSBENCH_BOOTSTRAP_PAYLOAD_KEY])

    assert "worker_name" not in payload
    assert CRSBENCH_WORKER_NAME_METADATA_KEY not in metadata


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
    assert (
        'CRSBENCH_LOCAL_CONSOLE_PASSWORD="${CRSBENCH_LOCAL_CONSOLE_PASSWORD:-crsbench}"'
        in startup_script
    )
    assert (
        "printf '%s:%s\\n' \"${CRSBENCH_USER}\" "
        '"${CRSBENCH_LOCAL_CONSOLE_PASSWORD}" | chpasswd' in startup_script
    )
    assert "PasswordAuthentication no" in startup_script
    assert "PermitRootLogin no" in startup_script
    assert "serial-getty@ttyS0.service" in startup_script
    assert "dbus-user-session" in startup_script
    assert 'instance_metadata_get "name"' in startup_script
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
    assert 'local bashrc_path="${CRSBENCH_USER_HOME}/.bashrc"' in startup_script
    assert (
        "sed -i '/^# >>> CRSBench prompt >>>$/,/^# <<< CRSBench prompt <<<$/'d"
        in startup_script
    )
    assert "__crsbench_prompt_short_host()" in startup_script
    assert "hostname -s 2>/dev/null || hostname 2>/dev/null" in startup_script
    assert 'short_host="${parts[count-2]}-${parts[count-1]}"' in startup_script
    assert "__crsbench_prompt_command_installed()" in startup_script
    assert "if ! __crsbench_prompt_command_installed; then" in startup_script
    assert (
        'PROMPT_COMMAND="__crsbench_update_prompt;${PROMPT_COMMAND}"' in startup_script
    )
    assert (
        "\\\\]%*s\\\\[\\\\e[0;36m\\\\]%s\\\\[\\\\e[0m\\\\]\\\\n\\\\$ " in startup_script
    )


@pytest.mark.parametrize(
    "loader_name",
    ["load_startup_script", "load_orchestrator_startup_script"],
)
def test_prompt_resourcing_reinstalls_prompt_command_without_duplication(
    tmp_path, loader_name
):
    """Interactive re-sourcing should restore the prompt hook without stacking duplicates."""
    from crsbench.cloud.gce import metadata

    prompt_path = tmp_path / f"{loader_name}-prompt.sh"
    prompt_path.write_text(
        _extract_prompt_block(getattr(metadata, loader_name)()),
        encoding="utf-8",
    )

    command = "\n".join(
        [
            f"source {shlex.quote(str(prompt_path))}",
            'printf "first:%s\\n" "${PROMPT_COMMAND:-}"',
            'PROMPT_COMMAND="user_override"',
            f"source {shlex.quote(str(prompt_path))}",
            'printf "second:%s\\n" "${PROMPT_COMMAND:-}"',
            f"source {shlex.quote(str(prompt_path))}",
            'printf "third:%s\\n" "${PROMPT_COMMAND:-}"',
        ]
    )

    result = subprocess.run(
        ["bash", "--norc", "-ic", command],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
        env={
            **os.environ,
            "CRSBENCH_CLOUD_INSTANCE_NAME": (
                "crsbench-afc-cc-finding-original-corpus-eval-002"
            ),
            "HOME": str(tmp_path),
            "COLUMNS": "80",
            "TERM": "xterm",
        },
        timeout=2,
    )

    assert result.returncode == 0
    assert [line for line in result.stdout.splitlines() if ":" in line] == [
        "first:__crsbench_update_prompt",
        "second:__crsbench_update_prompt;user_override",
        "third:__crsbench_update_prompt;user_override",
    ]


def test_load_startup_script_raises_fd_limits_for_issue_182():
    """Regression: GitHub issue #182 -- worker startup must raise FD limits."""

    from crsbench.cloud.gce.metadata import load_startup_script

    startup_script = load_startup_script()

    # Kernel-level sysctls (persisted and applied live before docker starts).
    assert "fs.nr_open = 1048576" in startup_script
    assert "fs.file-max = 2097152" in startup_script
    assert "/etc/sysctl.d/99-crsbench.conf" in startup_script
    assert "apply_crsbench_sysctls" in startup_script

    # systemd service unit limits (shared between worker and evaluator roles).
    assert "LimitNOFILE=1048576" in startup_script
    assert "LimitNPROC=1048576" in startup_script

    # Sysctls must be applied strictly before Docker is brought up, so the
    # daemon inherits the raised limits.
    sysctl_idx = startup_script.rfind("apply_crsbench_sysctls")
    docker_idx = startup_script.rfind("ensure_docker_ready")
    assert sysctl_idx != -1
    assert docker_idx != -1
    assert sysctl_idx < docker_idx, (
        "apply_crsbench_sysctls must run before ensure_docker_ready so the "
        "Docker daemon inherits raised fs.nr_open / fs.file-max limits"
    )


def test_build_evaluator_metadata_embeds_startup_script_and_config_payload(tmp_path):
    """Evaluator metadata should embed config payload and evaluator runtime settings."""
    from crsbench.cloud.gce.metadata import (
        CRSBENCH_BOOTSTRAP_PAYLOAD_KEY,
        CRSBENCH_DOWNLOAD_DELAY_SEC_KEY,
        build_evaluator_metadata,
    )

    config_path = tmp_path / "config.yaml"
    config_path.write_text("experiment: exp-cloud-42\n", encoding="utf-8")

    metadata = build_evaluator_metadata(
        experiment_name="Exp.Cloud 42",
        fleet=_make_fleet(worker_name_prefix="gce-evaluator"),
        redis_host="redis.internal:6380",
        registration=_make_registration(),
        bootstrap_inputs=CloudVmBootstrapInputs(
            benchmark_suite="sanity",
            gitcache=True,
        ),
        download_delay_sec=20,
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
    assert metadata[CRSBENCH_DOWNLOAD_DELAY_SEC_KEY] == "20"
    assert payload["gitcache"] is True


def test_build_evaluator_metadata_omits_evaluator_name_for_regional_bulk_insert(
    tmp_path,
):
    """Regional evaluator fleets must derive identity from the created instance name."""
    from crsbench.cloud.gce.metadata import (
        CRSBENCH_BOOTSTRAP_PAYLOAD_KEY,
        CRSBENCH_EXPERIMENT_CONFIG_B64_KEY,
        CRSBENCH_WORKER_NAME_METADATA_KEY,
        build_evaluator_metadata,
    )

    config_path = tmp_path / "config.yaml"
    config_path.write_text("experiment: exp-cloud-42\n", encoding="utf-8")

    metadata = build_evaluator_metadata(
        experiment_name="Exp.Cloud 42",
        fleet=_make_fleet(
            worker_name_prefix="gce-evaluator",
            region="us-east5",
            zones=["us-east5-b", "us-east5-c"],
        ),
        redis_host="redis.internal:6380",
        registration=_make_registration(),
        evaluator_name=None,
        experiment_config_path=config_path,
        startup_script="#!/usr/bin/env bash\necho boot\n",
    )

    payload = _decode_payload(metadata[CRSBENCH_BOOTSTRAP_PAYLOAD_KEY])

    assert "evaluator_name" not in payload
    assert CRSBENCH_WORKER_NAME_METADATA_KEY not in metadata
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


def test_load_startup_script_writes_passthrough_env_before_runtime_managed_env():
    """Runtime-managed launcher env must override passthrough values on conflict."""
    from crsbench.cloud.gce.metadata import load_startup_script

    startup_script = load_startup_script()

    passthrough_index = startup_script.index(
        'write_passthrough_env_vars "${ENV_PASSTHROUGH_B64}"'
    )
    managed_index = startup_script.index(
        'write_env_var "CRSBENCH_CLOUD_ROLE" "${CRSBENCH_STARTUP_MODE}"'
    )

    assert passthrough_index < managed_index


def test_load_startup_script_exports_download_delay_before_vm_bootstrap():
    """Worker bootstrap should export per-instance download delay before shared prepare/download."""
    from crsbench.cloud.gce.metadata import load_startup_script

    startup_script = load_startup_script()

    read_index = startup_script.index(
        'CRSBENCH_DOWNLOAD_DELAY_SEC="$(metadata_get_optional "crsbench-download-delay-sec")"'
    )
    bootstrap_index = startup_script.index("run_cloud_vm_bootstrap(")
    write_index = startup_script.index(
        'write_env_var "CRSBENCH_DOWNLOAD_DELAY_SEC" "${CRSBENCH_DOWNLOAD_DELAY_SEC}"'
    )

    assert read_index < bootstrap_index
    assert write_index > bootstrap_index


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
        crsbench_git_ref="main",
    )
    metadata = build_instance_metadata(
        experiment_name="exp-ref",
        fleet=fleet,
        redis_host="redis.internal:6380",
        registration=_make_registration(),
        worker_name="gce-worker-001",
        startup_script="#!/usr/bin/env bash\n",
    )

    assert metadata[CRSBENCH_GIT_REF_KEY] == "main"


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


def test_startup_script_preserves_passthrough_env_for_checkout_bootstrap():
    """Worker bootstrap should preserve metadata-passed secrets through sudo."""
    from crsbench.cloud.gce.metadata import load_startup_script

    script = load_startup_script()

    assert 'sudo -E -H -u "${CRSBENCH_USER}" "$@"' in script


def test_startup_script_supports_configurable_git_ssh_host():
    """Worker bootstrap should allow overriding the SSH host used for known-host setup."""
    from crsbench.cloud.gce.metadata import load_startup_script

    script = load_startup_script()

    assert 'CRSBENCH_GIT_SSH_HOST="${CRSBENCH_GIT_SSH_HOST:-github.com}"' in script
    assert (
        'ssh-keyscan -t ed25519 \\"${CRSBENCH_GIT_SSH_HOST}\\" >> '
        "${CRSBENCH_USER_HOME}/.ssh/known_hosts 2>/dev/null"
    ) in script


def test_startup_script_loads_passthrough_env_before_timezone_normalization():
    """Worker startup must load metadata-passed env before ensure_timezone runs."""
    from crsbench.cloud.gce.metadata import load_startup_script

    script = load_startup_script()

    env_load_index = script.index(
        'ENV_PASSTHROUGH_B64="$(metadata_get_optional "crsbench-env-passthrough-b64")"'
    )
    timezone_index = script.rindex("\nensure_timezone\n")

    assert env_load_index < timezone_index


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
    assert 'exec sudo -H -u "${CRSBENCH_USER}" /bin/bash -lc' in script
    assert (
        "\"cd $(printf '%q' \"${CLONE_DIR}\") && exec $(printf '%q' "
        '"${LAUNCHER_PATH}")"'
    ) in script


def test_startup_script_supports_apt_and_apk_bootstrap_dependencies():
    """Worker bootstrap should install missing dependencies on apt- or apk-based hosts."""
    from crsbench.cloud.gce.metadata import load_startup_script

    script = load_startup_script()

    assert "apt-get install -y -qq" in script
    assert "/etc/apt/keyrings" in script
    assert "download.docker.com/linux/ubuntu" in script
    assert "docker.asc" in script
    assert (
        "docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin"
        in script
    )
    assert "iftop" in script
    assert "ncdu" in script
    assert "gdu" in script
    assert "duf" in script
    assert "btop" in script
    assert "local apt_diagnostics_ready=0" in script
    assert '[[ "${apt_diagnostics_ready}" -eq 1 ]]' in script
    assert "command -v ncdu >/dev/null 2>&1" in script
    assert "command -v gdu >/dev/null 2>&1" in script
    assert "command -v duf >/dev/null 2>&1" in script
    assert "command -v btop >/dev/null 2>&1" in script
    assert "ripgrep" in script
    assert "fd-find" in script
    assert "install_packages docker.io docker-compose-v2 docker-buildx" not in script
    assert "docker compose version >/dev/null 2>&1" in script
    assert "docker buildx version >/dev/null 2>&1" in script
    assert "apk add --no-cache" in script
    assert "https://get.docker.com" not in script
    assert "Docker daemon is unavailable after waiting" in script


def test_load_startup_script_installs_gitcache_binary_and_managed_wrapper():
    """Worker bootstrap should install gitcache and optionally expose it as git."""
    from crsbench.cloud.gce.metadata import load_startup_script

    script = load_startup_script()

    assert "gitcache_release_asset_name()" in script
    assert "install_gitcache_binary()" in script
    assert "enable_gitcache_wrapper()" in script
    assert "ensure_gitcache_ready()" in script
    assert "seeraven/gitcache" in script
    assert "gitcache_v1.0.31_Ubuntu24.04_x86_64" in script
    assert (
        'ln -sfn "${CRSBENCH_MANAGED_BIN_DIR}/gitcache" '
        '"${CRSBENCH_MANAGED_BIN_DIR}/git"'
    ) in script
    assert (
        'CRSBENCH_MANAGED_BIN_DIR="${CRSBENCH_MANAGED_BIN_DIR:-/opt/crsbench-managed/bin}"'
        in script
    )
    assert 'CRSBENCH_GITCACHE_ENABLED="${CRSBENCH_GITCACHE_ENABLED:-0}"' in script
    assert 'print("1" if payload.get("gitcache") else "0")' in script
    assert (
        'CRSBENCH_MANAGED_BIN_DIR="${CRSBENCH_MANAGED_BIN_DIR:-/opt/crsbench/bin}"'
        not in script
    )


def test_startup_script_bootstraps_default_buildx_builder():
    """Worker bootstrap should bootstrap the default buildx builder."""
    from crsbench.cloud.gce.metadata import load_startup_script

    script = load_startup_script()

    assert "docker buildx inspect --bootstrap >/dev/null" in script
    assert 'docker buildx create --name "${CRSBENCH_BUILDER_NAME}"' not in script
    assert 'docker buildx use "${CRSBENCH_BUILDER_NAME}"' not in script
    assert "Driver:[[:space:]]+docker-container" not in script


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


def test_startup_script_mirrors_worker_and_evaluator_logs_to_role_file():
    """Worker/evaluator launchers should tee stdout/stderr into a role-specific log file."""
    from crsbench.cloud.gce.metadata import (
        load_evaluator_startup_script,
        load_startup_script,
    )

    worker_script = load_startup_script()
    evaluator_script = load_evaluator_startup_script()

    assert 'LOG_PATH="${STATE_DIR}/${CRSBENCH_STARTUP_MODE}.log"' in worker_script
    assert 'write_env_var "LOG_PATH" "${LOG_PATH}"' in worker_script
    assert 'exec > >(tee -a "${LOG_PATH}") 2>&1' in worker_script
    assert 'touch "${LOG_PATH}"' in worker_script
    assert (
        'chown "${CRSBENCH_USER}:${CRSBENCH_USER}" "${PAYLOAD_PATH}" "${ENV_PATH}" "${LAUNCHER_PATH}" "${LOG_PATH}"'
        in worker_script
    )
    assert (
        'CRSBENCH_STARTUP_MODE="${CRSBENCH_STARTUP_MODE:-evaluator}"'
        in evaluator_script
    )
    assert 'LOG_PATH="${STATE_DIR}/${CRSBENCH_STARTUP_MODE}.log"' in evaluator_script
    assert 'exec > >(tee -a "${LOG_PATH}") 2>&1' in evaluator_script


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


def test_worker_gitcache_install_failure_warns_and_continues_when_disabled(tmp_path):
    """Disabled wrapper mode should treat gitcache download failure as warning-only."""
    from crsbench.cloud.gce.metadata import load_startup_script

    script = load_startup_script()
    function_start = script.index("gitcache_release_asset_name() {")
    function_end = script.index("\n\nrequire_cmd curl", function_start)
    gitcache_helpers = script[function_start:function_end]

    harness_path = tmp_path / "gitcache-disabled.sh"
    harness_path.write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                "set -euo pipefail",
                f"TMPROOT={shlex.quote(str(tmp_path / 'runtime'))}",
                'CRSBENCH_MANAGED_BIN_DIR="${TMPROOT}/bin"',
                'CRSBENCH_GITCACHE_ENABLED="${CRSBENCH_GITCACHE_ENABLED:-0}"',
                'mkdir -p "${TMPROOT}"',
                gitcache_helpers,
                "curl() { return 1; }",
                "ensure_gitcache_ready",
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

    assert result.returncode == 0
    assert "gitcache install failed" in result.stderr


def test_worker_gitcache_install_failure_fails_when_wrapper_enabled(tmp_path):
    """Enabled wrapper mode should fail bootstrap if gitcache download fails."""
    from crsbench.cloud.gce.metadata import load_startup_script

    script = load_startup_script()
    function_start = script.index("gitcache_release_asset_name() {")
    function_end = script.index("\n\nrequire_cmd curl", function_start)
    gitcache_helpers = script[function_start:function_end]

    harness_path = tmp_path / "gitcache-enabled.sh"
    harness_path.write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                "set -euo pipefail",
                f"TMPROOT={shlex.quote(str(tmp_path / 'runtime'))}",
                'CRSBENCH_MANAGED_BIN_DIR="${TMPROOT}/bin"',
                "CRSBENCH_GITCACHE_ENABLED=1",
                'mkdir -p "${TMPROOT}"',
                gitcache_helpers,
                "curl() { return 1; }",
                "ensure_gitcache_ready",
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
    assert "gitcache install failed" in result.stderr


def test_build_orchestrator_metadata_embeds_config_payload_and_redis_password(tmp_path):
    """Orchestrator metadata should carry config payload and shared Redis auth."""
    from crsbench.cloud.gce.metadata import (
        CRSBENCH_DOWNLOAD_DELAY_SEC_KEY,
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
        download_delay_sec=0,
        redis_password="shared-secret",
        startup_script="#!/usr/bin/env bash\necho boot\n",
    )

    assert metadata[CRSBENCH_REDIS_PASSWORD_KEY] == "shared-secret"
    assert metadata[CRSBENCH_DOWNLOAD_DELAY_SEC_KEY] == "0"
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


def test_build_orchestrator_metadata_strips_secret_path_fields_from_config_payload(
    tmp_path,
):
    """Remote config payload should omit local-only deploy-key path references."""
    from crsbench.cloud.gce.metadata import (
        CRSBENCH_EXPERIMENT_CONFIG_B64_KEY,
        build_orchestrator_metadata,
    )

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "experiment": {"name": "exp-42"},
                "cloud": {
                    "defaults": {
                        "crsbench_install_spec": (
                            "git+ssh://git@github.com/sslab-gatech/CRSBench.git"
                        ),
                        "github_deploy_key_path": "/home/operator/.ssh/crsbench",
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    metadata = build_orchestrator_metadata(
        experiment_name="exp-42",
        orchestrator=_make_orchestrator(),
        experiment_config_path=config_path,
        redis_password="shared-secret",
        startup_script="#!/usr/bin/env bash\necho boot\n",
    )

    decoded_config = yaml.safe_load(
        base64.b64decode(metadata[CRSBENCH_EXPERIMENT_CONFIG_B64_KEY]).decode("utf-8")
    )

    assert decoded_config["cloud"]["defaults"]["crsbench_install_spec"] == (
        "git+ssh://git@github.com/sslab-gatech/CRSBench.git"
    )
    assert "github_deploy_key_path" not in decoded_config["cloud"]["defaults"]


def test_orchestrator_startup_script_consumes_config_payload_and_preprovisioned_mode():
    """Orchestrator startup should decode config payload and run under the crsbench user."""
    from crsbench.cloud.gce.metadata import load_orchestrator_startup_script

    script = load_orchestrator_startup_script()

    assert 'CRSBENCH_USER="${CRSBENCH_USER:-crsbench}"' in script
    assert (
        'CRSBENCH_LOCAL_CONSOLE_PASSWORD="${CRSBENCH_LOCAL_CONSOLE_PASSWORD:-crsbench}"'
        in script
    )
    assert (
        "printf '%s:%s\\n' \"${CRSBENCH_USER}\" "
        '"${CRSBENCH_LOCAL_CONSOLE_PASSWORD}" | chpasswd' in script
    )
    assert "PasswordAuthentication no" in script
    assert "PermitRootLogin no" in script
    assert "serial-getty@ttyS0.service" in script
    assert "crsbench-experiment-config-b64" in script
    assert "crsbench-env-passthrough-b64" in script
    assert "CRSBENCH_CLOUD_PREPROVISIONED_WORKERS" in script
    assert 'local bashrc_path="${CRSBENCH_USER_HOME}/.bashrc"' in script
    assert (
        "sed -i '/^# >>> CRSBench prompt >>>$/,/^# <<< CRSBench prompt <<<$/'d"
        in script
    )
    assert "__crsbench_prompt_short_host()" in script
    assert "hostname -s 2>/dev/null || hostname 2>/dev/null" in script
    assert 'short_host="${parts[count-2]}-${parts[count-1]}"' in script
    assert "__crsbench_prompt_command_installed()" in script
    assert "if ! __crsbench_prompt_command_installed; then" in script
    assert 'PROMPT_COMMAND="__crsbench_update_prompt;${PROMPT_COMMAND}"' in script
    assert "\\\\]%*s\\\\[\\\\e[0;36m\\\\]%s\\\\[\\\\e[0m\\\\]\\\\n\\\\$ " in script


def test_orchestrator_startup_script_waits_for_from_experiment_sentinel_before_run():
    """Launcher must block on the push-complete sentinel before crsbench run.

    Regression: previously the orchestrator service started immediately after
    user creation and validated ``inputs.pov.from_experiment*`` before the
    operator-side rsync push had landed the bundle, racing with provisioning.
    """
    from crsbench.cloud.gce.metadata import load_orchestrator_startup_script

    script = load_orchestrator_startup_script()

    assert "wait_for_from_experiment_bundle" in script
    # Sentinel path is built from the experiment name interpolated at
    # generation time, so the launcher embeds the literal '/var/lib/...' root.
    assert "/var/lib/crsbench/from-experiment/" in script
    assert ".push-complete" in script
    # The wait must happen before 'crsbench run' in the launcher body.
    assert script.index("wait_for_from_experiment_bundle\n") < script.index(
        "crsbench run --experiment-config"
    )
    # Skip-when-not-configured branch keeps non-from_experiment launches fast.
    assert "from_experiment not configured" in script


def test_orchestrator_startup_script_loads_passthrough_env_before_timezone_normalization():
    """Orchestrator startup must load metadata-passed env before ensure_timezone runs."""
    from crsbench.cloud.gce.metadata import load_orchestrator_startup_script

    script = load_orchestrator_startup_script()

    env_load_index = script.index(
        'ENV_PASSTHROUGH_B64="$(metadata_get_optional "crsbench-env-passthrough-b64")"'
    )
    timezone_index = script.rindex("\nensure_timezone\n")

    assert env_load_index < timezone_index
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


def test_orchestrator_startup_script_supports_configurable_git_ssh_host():
    """Orchestrator bootstrap should allow overriding the SSH host used for known-host setup."""
    from crsbench.cloud.gce.metadata import load_orchestrator_startup_script

    script = load_orchestrator_startup_script()

    assert 'CRSBENCH_GIT_SSH_HOST="${CRSBENCH_GIT_SSH_HOST:-github.com}"' in script
    assert (
        'ssh-keyscan -t ed25519 \\"${CRSBENCH_GIT_SSH_HOST}\\" >> '
        "${CRSBENCH_USER_HOME}/.ssh/known_hosts 2>/dev/null"
    ) in script


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


def test_orchestrator_startup_script_exports_download_delay_before_vm_bootstrap():
    """Orchestrator bootstrap should export its download delay before the shared bootstrap."""
    from crsbench.cloud.gce.metadata import load_orchestrator_startup_script

    script = load_orchestrator_startup_script()

    read_index = script.index(
        'CRSBENCH_DOWNLOAD_DELAY_SEC="$(metadata_get_optional "crsbench-download-delay-sec")"'
    )
    bootstrap_index = script.index("run_cloud_vm_bootstrap(")
    write_index = script.index(
        'write_env_var "CRSBENCH_DOWNLOAD_DELAY_SEC" "${CRSBENCH_DOWNLOAD_DELAY_SEC}"'
    )

    assert read_index < bootstrap_index
    assert write_index > bootstrap_index


def test_orchestrator_startup_script_starts_valkey_before_crsbench_run():
    """Valkey must come up inside the launcher (after sourcing ENV_PATH for the redis
    password) and before any ``crsbench run`` invocation."""
    from crsbench.cloud.gce.metadata import load_orchestrator_startup_script

    script = load_orchestrator_startup_script()

    launcher_start = script.index('cat > "${LAUNCHER_PATH}" <<EOF')
    launcher_end = script.index('\nEOF\nchmod +x "${LAUNCHER_PATH}"')
    launcher_body = script[launcher_start:launcher_end]

    valkey_def_index = launcher_body.index("ensure_valkey_running() {")
    valkey_call_index = launcher_body.index("\nensure_valkey_running\n")
    crsbench_run_index = launcher_body.index(
        'crsbench run --experiment-config "\\${CONFIG_PATH}"'
    )

    assert valkey_def_index < valkey_call_index < crsbench_run_index
    assert script.count("ensure_valkey_running() {") == 1


def test_orchestrator_startup_script_extracts_grouped_experiment_name():
    """Grouped configs should populate EXPERIMENT_NAME from experiment.name, not the whole mapping."""
    from crsbench.cloud.gce.metadata import load_orchestrator_startup_script

    script = load_orchestrator_startup_script()

    assert 'value = loaded.get("experiment")' in script
    assert "if isinstance(value, dict):" in script
    assert 'value = value.get("name")' in script
    assert "grouped 'experiment.name'" in script


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
    """Orchestrator bootstrap should install missing dependencies on apt- or apk-based hosts."""
    from crsbench.cloud.gce.metadata import load_orchestrator_startup_script

    script = load_orchestrator_startup_script()

    assert "apt-get install -y -qq" in script
    assert "/etc/apt/keyrings" in script
    assert "download.docker.com/linux/ubuntu" in script
    assert "docker.asc" in script
    assert (
        "docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin"
        in script
    )
    assert "iftop" in script
    assert "ncdu" in script
    assert "gdu" in script
    assert "duf" in script
    assert "btop" in script
    assert "local apt_diagnostics_ready=0" in script
    assert '[[ "${apt_diagnostics_ready}" -eq 1 ]]' in script
    assert "command -v ncdu >/dev/null 2>&1" in script
    assert "command -v gdu >/dev/null 2>&1" in script
    assert "command -v duf >/dev/null 2>&1" in script
    assert "command -v btop >/dev/null 2>&1" in script
    assert "ripgrep" in script
    assert "fd-find" in script
    assert "install_packages docker.io docker-compose-v2 docker-buildx" not in script
    assert "docker compose version >/dev/null 2>&1" in script
    assert "docker buildx version >/dev/null 2>&1" in script
    assert "apk add --no-cache" in script
    assert "https://get.docker.com" not in script
    assert "Docker daemon is unavailable after waiting" in script


def test_load_orchestrator_startup_script_installs_gitcache_binary_and_managed_wrapper():
    """Orchestrator bootstrap should install gitcache and optionally expose it as git."""
    from crsbench.cloud.gce.metadata import load_orchestrator_startup_script

    script = load_orchestrator_startup_script()

    assert "gitcache_release_asset_name()" in script
    assert "install_gitcache_binary()" in script
    assert "enable_gitcache_wrapper()" in script
    assert "ensure_gitcache_ready()" in script
    assert "read_gitcache_flag_from_config()" in script
    assert "seeraven/gitcache" in script
    assert "gitcache_v1.0.31_Ubuntu24.04_x86_64" in script
    assert (
        'ln -sfn "${CRSBENCH_MANAGED_BIN_DIR}/gitcache" '
        '"${CRSBENCH_MANAGED_BIN_DIR}/git"'
    ) in script
    assert (
        'CRSBENCH_MANAGED_BIN_DIR="${CRSBENCH_MANAGED_BIN_DIR:-/opt/crsbench-managed/bin}"'
        in script
    )
    assert (
        'CRSBENCH_MANAGED_BIN_DIR="${CRSBENCH_MANAGED_BIN_DIR:-/opt/crsbench/bin}"'
        not in script
    )
    assert 'CRSBENCH_GITCACHE_ENABLED="${CRSBENCH_GITCACHE_ENABLED:-0}"' in script


def test_orchestrator_startup_script_bootstraps_default_buildx_builder():
    """Orchestrator bootstrap should bootstrap the default buildx builder."""
    from crsbench.cloud.gce.metadata import load_orchestrator_startup_script

    script = load_orchestrator_startup_script()

    assert "docker buildx inspect --bootstrap >/dev/null" in script
    assert 'docker buildx create --name "${CRSBENCH_BUILDER_NAME}"' not in script
    assert 'docker buildx use "${CRSBENCH_BUILDER_NAME}"' not in script
    assert "Driver:[[:space:]]+docker-container" not in script


def test_orchestrator_startup_script_only_restarts_service_on_failure():
    """Orchestrator bootstrap should not relaunch a cleanly exited run forever."""
    from crsbench.cloud.gce.metadata import load_orchestrator_startup_script

    script = load_orchestrator_startup_script()

    assert "Restart=on-failure" in script
    assert "Restart=always" not in script


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
    # The valkey block lives inside the launcher heredoc so shell vars are escaped.
    assert '-p "\\${CRSBENCH_REDIS_BIND_HOST}:6379:6379"' in script
    assert "0.0.0.0:6379:6379" not in script


def test_orchestrator_startup_script_supports_configurable_valkey_image():
    """Orchestrator bootstrap should allow overriding the Valkey container image."""
    from crsbench.cloud.gce.metadata import load_orchestrator_startup_script

    script = load_orchestrator_startup_script()

    assert (
        'CRSBENCH_VALKEY_IMAGE="${CRSBENCH_VALKEY_IMAGE:-valkey/valkey:8.0-alpine}"'
        in script
    )
    assert '"${CRSBENCH_VALKEY_IMAGE}" \\' in script


def test_orchestrator_startup_script_does_not_globally_rewrite_sslab_gatech_https_urls():
    """Orchestrator bootstrap should leave public submodule URLs on their declared transport."""
    from crsbench.cloud.gce.metadata import load_orchestrator_startup_script

    script = load_orchestrator_startup_script()

    assert (
        'git config --global url."git@github.com:sslab-gatech/".insteadOf '
        '"https://github.com/sslab-gatech/"'
    ) not in script
    assert "GIT_SSH_COMMAND" in script


def test_orchestrator_gitcache_install_failure_warns_and_continues_when_disabled(
    tmp_path,
):
    """Disabled wrapper mode should treat orchestrator gitcache download failure as warning-only."""
    from crsbench.cloud.gce.metadata import load_orchestrator_startup_script

    script = load_orchestrator_startup_script()
    function_start = script.index("gitcache_release_asset_name() {")
    function_end = script.index("\n\nrequire_cmd curl", function_start)
    gitcache_helpers = script[function_start:function_end]

    harness_path = tmp_path / "orchestrator-gitcache-disabled.sh"
    harness_path.write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                "set -euo pipefail",
                f"TMPROOT={shlex.quote(str(tmp_path / 'runtime'))}",
                'CRSBENCH_MANAGED_BIN_DIR="${TMPROOT}/bin"',
                'CRSBENCH_GITCACHE_ENABLED="${CRSBENCH_GITCACHE_ENABLED:-0}"',
                'mkdir -p "${TMPROOT}"',
                gitcache_helpers,
                "curl() { return 1; }",
                "ensure_gitcache_ready",
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

    assert result.returncode == 0
    assert "gitcache install failed" in result.stderr


def test_orchestrator_gitcache_install_failure_fails_when_wrapper_enabled(tmp_path):
    """Enabled wrapper mode should fail orchestrator bootstrap if gitcache download fails."""
    from crsbench.cloud.gce.metadata import load_orchestrator_startup_script

    script = load_orchestrator_startup_script()
    function_start = script.index("gitcache_release_asset_name() {")
    function_end = script.index("\n\nrequire_cmd curl", function_start)
    gitcache_helpers = script[function_start:function_end]

    harness_path = tmp_path / "orchestrator-gitcache-enabled.sh"
    harness_path.write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                "set -euo pipefail",
                f"TMPROOT={shlex.quote(str(tmp_path / 'runtime'))}",
                'CRSBENCH_MANAGED_BIN_DIR="${TMPROOT}/bin"',
                "CRSBENCH_GITCACHE_ENABLED=1",
                'mkdir -p "${TMPROOT}"',
                gitcache_helpers,
                "curl() { return 1; }",
                "ensure_gitcache_ready",
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
    assert "gitcache install failed" in result.stderr


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
