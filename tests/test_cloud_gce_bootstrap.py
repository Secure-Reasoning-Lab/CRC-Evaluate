"""Tests for GCE worker bootstrap metadata and startup script generation."""

import base64
import json
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
    """github_deploy_key_file in fleet config causes base64-encoded key in metadata."""
    from crsbench.cloud.gce.metadata import (
        CRSBENCH_GITHUB_DEPLOY_KEY,
        build_instance_metadata,
    )

    key_content = b"-----BEGIN OPENSSH PRIVATE KEY-----\nFAKEKEYDATA\n"
    key_file = tmp_path / "crsbench-deploy"
    key_file.write_bytes(key_content)

    fleet = _make_fleet(
        github_deploy_key_file=str(key_file),
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


def test_metadata_includes_hf_token():
    """hf_token in fleet config causes crsbench-hf-token in metadata."""
    from crsbench.cloud.gce.metadata import (
        CRSBENCH_HF_TOKEN_KEY,
        build_instance_metadata,
    )

    fleet = _make_fleet(
        hf_token="hf_test_token_abc123",
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

    assert metadata[CRSBENCH_HF_TOKEN_KEY] == "hf_test_token_abc123"


def test_metadata_includes_env_passthrough_blob_and_deduplicates_hf_token():
    """Role passthrough env should be encoded without duplicating dedicated HF token."""
    from crsbench.cloud.gce.metadata import (
        CRSBENCH_ENV_PASSTHROUGH_B64_KEY,
        CRSBENCH_HF_TOKEN_KEY,
        build_instance_metadata,
    )

    fleet = _make_fleet(
        hf_token="hf_test_token_abc123",
        metadata={"custom-key": "custom-value"},
    )
    metadata = build_instance_metadata(
        experiment_name="exp-42",
        fleet=fleet,
        redis_host="redis.internal:6380",
        registration=_make_registration(),
        env_passthrough={
            "HF_TOKEN": "ignored-duplicate",
            "CRSBENCH_LLM_MASTER_KEY": "master-key",
        },
        worker_name="gce-worker-001",
        startup_script="#!/usr/bin/env bash\n",
    )

    assert metadata[CRSBENCH_HF_TOKEN_KEY] == "hf_test_token_abc123"
    passthrough = json.loads(
        base64.b64decode(metadata[CRSBENCH_ENV_PASSTHROUGH_B64_KEY]).decode("utf-8")
    )
    assert passthrough == {"CRSBENCH_LLM_MASTER_KEY": "master-key"}


def test_metadata_omits_secrets_when_not_configured():
    """Default fleet (no key/token) must not include secret metadata keys."""
    from crsbench.cloud.gce.metadata import (
        CRSBENCH_ENV_PASSTHROUGH_B64_KEY,
        CRSBENCH_GITHUB_DEPLOY_KEY,
        CRSBENCH_HF_TOKEN_KEY,
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
    assert CRSBENCH_HF_TOKEN_KEY not in metadata
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
    """Startup script should include git clone, uv sync, deploy key, and HF token handling."""
    from crsbench.cloud.gce.metadata import load_startup_script

    script = load_startup_script()

    assert "git clone" in script
    assert "uv sync" in script
    assert "crsbench-github-deploy-key" in script
    assert "crsbench-hf-token" in script
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
    assert 'CLONE_DIR="/opt/crsbench"' in script
    assert "WorkingDirectory=/opt/crsbench" in script
    assert 'write_env_var "PATH" "${VENV_BIN}' in script
    assert "/root/.local/bin" in script


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

    assert "check_redis_available" in script
    assert "CRSBENCH_READINESS_TIMEOUT_SEC" in script
    assert "wait_for_redis()" in script
    assert "Waiting for Redis at ${CRSBENCH_REDIS_HOST}" in script
    assert "Timed out waiting for Redis" in script


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
    """Orchestrator startup should decode config payload and skip worker reprovision."""
    from crsbench.cloud.gce.metadata import load_orchestrator_startup_script

    script = load_orchestrator_startup_script()

    assert "crsbench-experiment-config-b64" in script
    assert "crsbench-env-passthrough-b64" in script
    assert "CRSBENCH_CLOUD_PREPROVISIONED_WORKERS" in script
    assert "crsbench-redis-password" in script
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
    assert 'CLONE_DIR="/opt/crsbench"' in script
    assert 'cd "${CLONE_DIR}"' in script


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
