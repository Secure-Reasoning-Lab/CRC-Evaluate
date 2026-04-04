"""Tests for local Docker rehearsal assets for cloud startup scripts."""

from __future__ import annotations

import base64
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def _load_render_metadata_module():
    module_path = Path("scripts/cloud-rehearsal/render_metadata.py")
    spec = importlib.util.spec_from_file_location(
        "cloud_rehearsal_render_metadata", module_path
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_build_local_rehearsal_layout_writes_file_backed_metadata(tmp_path) -> None:
    """Local rehearsal should materialize metadata trees consumable by startup scripts."""
    from crsbench.cloud.local_rehearsal import build_local_rehearsal_layout

    config_path = tmp_path / "experiment.yaml"
    config_path.write_text(
        """
experiment: local-cloud-rehearsal
task: bugfinding
benchmark_suite: sanity
mode: delta
runtime:
  trials: 1
  max_total_time: 600
  build_timeout: 60
  run_timeout: 60
  verify_timeout: 60
  redis_host: ignored:6379
  litellm:
    skip: true
  inputs:
    pov:
      max_variants_per_cpv: 1
storage:
  experiment_filestore: /tmp/crsbench/experiment-data
  report_filestore: /tmp/crsbench/report-data
resources:
  cores_per_trial: 2
  memory_per_trial: 4G
worker:
  jobs: 1
  cores_per_job: 2
evaluator:
  jobs: 1
  cores_per_job: 2
crs_compose:
  oss_crs_infra:
    shared: true
  atlantis-multilang-given_fuzzer:
    num_cores: 2
""".strip(),
        encoding="utf-8",
    )

    layout = build_local_rehearsal_layout(
        output_dir=tmp_path / "rehearsal",
        experiment_config_path=config_path,
        repo_mount_path="/src/CRSBench",
        worker_count=2,
        git_ref="test-ref",
    )

    orchestrator_root = layout.orchestrator_metadata_dir
    assert (orchestrator_root / "attributes" / "crsbench-install-spec").read_text(
        encoding="utf-8"
    ) == "git+file:///src/CRSBench"
    assert (orchestrator_root / "zone").read_text(encoding="utf-8") == "local-docker-a"

    worker_root = layout.worker_metadata_dirs[0]
    payload = json.loads(
        base64.b64decode(
            (worker_root / "attributes" / "crsbench-bootstrap-payload").read_text(
                encoding="utf-8"
            )
        ).decode("utf-8")
    )
    assert payload["redis_host"] == "orchestrator:6379"
    assert payload["benchmark_suite"] == "sanity"
    assert payload["prepare_mode"] == "full"
    assert payload["download_benchmarks"] == "auto"
    assert (worker_root / "id").read_text(encoding="utf-8").startswith("local-worker-")
    assert len(layout.worker_metadata_dirs) == 2
    assert len(layout.evaluator_metadata_dirs) == 1
    evaluator_root = layout.evaluator_metadata_dirs[0]
    evaluator_payload = json.loads(
        base64.b64decode(
            (evaluator_root / "attributes" / "crsbench-bootstrap-payload").read_text(
                encoding="utf-8"
            )
        ).decode("utf-8")
    )
    assert evaluator_payload["redis_host"] == "orchestrator:6379"
    assert evaluator_payload["experiment"] == "local-cloud-rehearsal"
    assert (
        (evaluator_root / "id")
        .read_text(encoding="utf-8")
        .startswith("local-evaluator-")
    )


def test_rehearsal_compose_uses_file_metadata_and_foreground_workers() -> None:
    """Compose harness should wire the startup-script overrides it documents."""
    compose_text = Path("scripts/cloud-rehearsal/docker-compose.yml").read_text(
        encoding="utf-8"
    )

    assert "CRSBENCH_METADATA_ROOT_DIR: /metadata-root" in compose_text
    assert "CRSBENCH_SERVICE_MANAGER: foreground" in compose_text
    assert "/metadata-root/instance:ro" in compose_text
    assert "evaluator-1:" in compose_text
    assert "/metadata/local-evaluator-1:/metadata-root/instance:ro" in compose_text
    assert "/src/CRSBench/crsbench/cloud/gce/startup/worker.sh" in compose_text


def test_rehearsal_dockerfile_uses_ubuntu_24_dind_base() -> None:
    """The rehearsal container should mirror the Ubuntu 24.04 GCE image family."""
    dockerfile_text = Path("scripts/cloud-rehearsal/Dockerfile").read_text(
        encoding="utf-8"
    )

    assert "cruizba/ubuntu-dind:noble-latest" in dockerfile_text
    assert "apt-get install -y --no-install-recommends" in dockerfile_text
    assert 'CMD ["/bin/bash"]' in dockerfile_text


def test_default_rehearsal_experiment_config_is_valid() -> None:
    """The checked-in default rehearsal config should pass experiment validation."""
    from crsbench.run_experiment import load_experiment_config

    config = load_experiment_config(
        Path("scripts/cloud-rehearsal/local-experiment.yaml")
    )

    assert config.experiment == "local-cloud-rehearsal"
    assert config.build_timeout == 3600
    assert config.crs_compose is not None
    assert set(config.crs_compose.services) == {"crs-libfuzzer"}
    assert config.evaluator is not None
    assert config.evaluator.jobs == 1
    assert config.max_total_time > (
        config.build_timeout + config.run_timeout + config.verify_timeout
    )


def test_sanity_always_rehearsal_config_is_valid() -> None:
    """The checked-in sanity+always rehearsal config should validate cleanly."""
    from crsbench.run_experiment import load_experiment_config

    config = load_experiment_config(
        Path("scripts/cloud-rehearsal/local-experiment-sanity-always.yaml")
    )

    assert config.experiment == "local-cloud-rehearsal-sanity-always"
    assert config.benchmark_suite == "sanity"
    assert config.cloud is not None
    assert config.cloud.bootstrap.download_benchmarks == "always"


def test_hf_download_rehearsal_config_is_valid() -> None:
    """The checked-in non-sanity rehearsal config should validate cleanly."""
    from crsbench.run_experiment import load_experiment_config

    config = load_experiment_config(
        Path("scripts/cloud-rehearsal/local-experiment-hf-download.yaml")
    )

    assert config.experiment == "local-cloud-rehearsal-hf-download"
    assert config.benchmark_suite == "smoke-test-bug-finding-hf-download"
    assert config.cloud is not None
    assert config.cloud.bootstrap.download_benchmarks == "auto"
    assert config.cloud.env["HF_TOKEN"] == "os.environ/HF_TOKEN"


def test_notification_rehearsal_config_is_valid() -> None:
    """The notification rehearsal config should validate cleanly."""
    from crsbench.run_experiment import load_experiment_config

    config = load_experiment_config(
        Path("scripts/cloud-rehearsal/local-experiment-notification.yaml")
    )

    assert config.experiment == "local-cloud-rehearsal-notification"
    assert config.benchmark_suite == "sanity"
    assert config.cloud is not None
    assert (
        config.cloud.env["CRSBENCH_NOTIFY_APPRISE_URLS"]
        == "os.environ/CRSBENCH_NOTIFY_APPRISE_URLS"
    )
    assert "CRSBENCH_NOTIFY_APPRISE_TITLE" not in config.cloud.env
    assert "CRSBENCH_NOTIFY_APPRISE_TAG" not in config.cloud.env


def test_build_local_rehearsal_layout_resolves_cloud_env_passthrough(
    monkeypatch,
    tmp_path,
) -> None:
    """Local rehearsal should reuse cloud env resolution for startup metadata."""
    from crsbench.cloud.local_rehearsal import build_local_rehearsal_layout

    monkeypatch.setenv("HF_TOKEN", "hf_test_token_local_rehearsal")

    layout = build_local_rehearsal_layout(
        output_dir=tmp_path / "rehearsal",
        experiment_config_path=Path(
            "scripts/cloud-rehearsal/local-experiment-hf-download.yaml"
        ),
        repo_mount_path="/src/CRSBench",
        worker_count=2,
        evaluator_count=1,
        git_ref="test-ref",
    )

    orchestrator_env = json.loads(
        base64.b64decode(
            (
                layout.orchestrator_metadata_dir
                / "attributes"
                / "crsbench-env-passthrough-b64"
            ).read_text(encoding="utf-8")
        ).decode("utf-8")
    )
    assert orchestrator_env["HF_TOKEN"] == "hf_test_token_local_rehearsal"

    worker_env = json.loads(
        base64.b64decode(
            (
                layout.worker_metadata_dirs[0]
                / "attributes"
                / "crsbench-env-passthrough-b64"
            ).read_text(encoding="utf-8")
        ).decode("utf-8")
    )
    assert worker_env["HF_TOKEN"] == "hf_test_token_local_rehearsal"

    evaluator_env = json.loads(
        base64.b64decode(
            (
                layout.evaluator_metadata_dirs[0]
                / "attributes"
                / "crsbench-env-passthrough-b64"
            ).read_text(encoding="utf-8")
        ).decode("utf-8")
    )
    assert evaluator_env["HF_TOKEN"] == "hf_test_token_local_rehearsal"

    worker_payload = json.loads(
        base64.b64decode(
            (
                layout.worker_metadata_dirs[0]
                / "attributes"
                / "crsbench-bootstrap-payload"
            ).read_text(encoding="utf-8")
        ).decode("utf-8")
    )
    assert worker_payload["benchmark_suite"] == "smoke-test-bug-finding-hf-download"
    assert worker_payload["download_benchmarks"] == "auto"


def test_build_local_rehearsal_layout_resolves_notification_env_passthrough(
    monkeypatch,
    tmp_path,
) -> None:
    """Local rehearsal should resolve notification env passthrough metadata."""
    from crsbench.cloud.local_rehearsal import build_local_rehearsal_layout

    monkeypatch.setenv(
        "CRSBENCH_NOTIFY_APPRISE_URLS",
        "discord://example/apprise",
    )

    layout = build_local_rehearsal_layout(
        output_dir=tmp_path / "rehearsal",
        experiment_config_path=Path(
            "scripts/cloud-rehearsal/local-experiment-notification.yaml"
        ),
        repo_mount_path="/src/CRSBench",
        worker_count=2,
        evaluator_count=1,
        git_ref="test-ref",
    )

    orchestrator_env = json.loads(
        base64.b64decode(
            (
                layout.orchestrator_metadata_dir
                / "attributes"
                / "crsbench-env-passthrough-b64"
            ).read_text(encoding="utf-8")
        ).decode("utf-8")
    )
    assert (
        orchestrator_env["CRSBENCH_NOTIFY_APPRISE_URLS"] == "discord://example/apprise"
    )

    worker_env = json.loads(
        base64.b64decode(
            (
                layout.worker_metadata_dirs[0]
                / "attributes"
                / "crsbench-env-passthrough-b64"
            ).read_text(encoding="utf-8")
        ).decode("utf-8")
    )
    assert worker_env["CRSBENCH_NOTIFY_APPRISE_URLS"] == "discord://example/apprise"

    evaluator_env = json.loads(
        base64.b64decode(
            (
                layout.evaluator_metadata_dirs[0]
                / "attributes"
                / "crsbench-env-passthrough-b64"
            ).read_text(encoding="utf-8")
        ).decode("utf-8")
    )
    assert evaluator_env["CRSBENCH_NOTIFY_APPRISE_URLS"] == "discord://example/apprise"


def test_build_local_rehearsal_layout_preserves_sanity_always_download_policy(
    tmp_path,
) -> None:
    """Local rehearsal metadata should preserve sanity+always bootstrap policy."""
    from crsbench.cloud.local_rehearsal import build_local_rehearsal_layout

    layout = build_local_rehearsal_layout(
        output_dir=tmp_path / "rehearsal",
        experiment_config_path=Path(
            "scripts/cloud-rehearsal/local-experiment-sanity-always.yaml"
        ),
        repo_mount_path="/src/CRSBench",
        worker_count=2,
        evaluator_count=1,
        git_ref="test-ref",
    )

    worker_payload = json.loads(
        base64.b64decode(
            (
                layout.worker_metadata_dirs[0]
                / "attributes"
                / "crsbench-bootstrap-payload"
            ).read_text(encoding="utf-8")
        ).decode("utf-8")
    )

    assert worker_payload["benchmark_suite"] == "sanity"
    assert worker_payload["download_benchmarks"] == "always"


def test_build_local_rehearsal_layout_preserves_gitcache_bootstrap_flag(
    tmp_path,
) -> None:
    """Local rehearsal metadata should preserve gitcache bootstrap policy."""
    from crsbench.cloud.local_rehearsal import build_local_rehearsal_layout

    config_path = tmp_path / "local-experiment-gitcache.yaml"
    config_path.write_text(
        """
experiment:
  name: local-cloud-rehearsal-gitcache
  task: bugfinding
  benchmark_suite: sanity
  mode: delta
runtime:
  trials: 1
  max_total_time: 7200
  build_timeout: 3600
  run_timeout: 600
  verify_timeout: 600
  redis_host: ignored:6379
  litellm:
    skip: true
  inputs:
    pov:
      max_variants_per_cpv: 1
storage:
  experiment_filestore: /tmp/crsbench/experiment-data
  report_filestore: /tmp/crsbench/report-data
resources:
  cores_per_trial: 2
  memory_per_trial: 4G
worker:
  jobs: 1
  cores_per_job: 2
evaluator:
  jobs: 1
  cores_per_job: 2
crs_compose:
  oss_crs_infra:
    shared: true
  crs-libfuzzer:
    num_cores: 2
cloud:
  bootstrap:
    gitcache: true
  providers:
    gce:
      project: local-rehearsal
      profile_defaults:
        machine_type: n2d-standard-8
        boot_disk_size_gb: 50
        image: projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64
        service_account_email: crsbench@local-rehearsal.invalid
        owner_label: local-rehearsal
      instance_profiles:
        local-orchestrator-n2d: {}
        local-worker-n2d: {}
        local-evaluator-n2d: {}
  orchestrator:
    zone: local-docker-a
    instance_profile: local-orchestrator-n2d
  workers:
    defaults:
      instance_profile: local-worker-n2d
      count: 1
    placements:
      - zone: local-docker-a
  evaluators:
    defaults:
      instance_profile: local-evaluator-n2d
      count: 1
    placements:
      - zone: local-docker-a
""".strip(),
        encoding="utf-8",
    )

    layout = build_local_rehearsal_layout(
        output_dir=tmp_path / "rehearsal",
        experiment_config_path=config_path,
        repo_mount_path="/src/CRSBench",
        worker_count=1,
        evaluator_count=1,
        git_ref="test-ref",
    )

    worker_payload = json.loads(
        base64.b64decode(
            (
                layout.worker_metadata_dirs[0]
                / "attributes"
                / "crsbench-bootstrap-payload"
            ).read_text(encoding="utf-8")
        ).decode("utf-8")
    )
    evaluator_payload = json.loads(
        base64.b64decode(
            (
                layout.evaluator_metadata_dirs[0]
                / "attributes"
                / "crsbench-bootstrap-payload"
            ).read_text(encoding="utf-8")
        ).decode("utf-8")
    )

    assert worker_payload["gitcache"] is True
    assert evaluator_payload["gitcache"] is True


def test_rehearsal_wrapper_only_resets_state_for_bringup() -> None:
    """Read-only compose subcommands should not wipe the previous rehearsal state."""
    wrapper_text = Path("scripts/cloud-rehearsal/run-local-rehearsal.sh").read_text(
        encoding="utf-8"
    )

    assert "reset_compose_runtime()" in wrapper_text
    assert "if [[ $# -eq 0 ]]; then" in wrapper_text
    assert 'if [[ "$1" == "up" ]]; then' in wrapper_text
    assert (
        'docker compose -f "${SCRIPT_DIR}/docker-compose.yml" down --remove-orphans'
        in wrapper_text
    )
    assert "CRSBENCH_LOCAL_REHEARSAL_GIT_REF" in wrapper_text
    assert "--evaluator-count 1" in wrapper_text
    assert "docker_cleanup_state" in wrapper_text
    assert 'docker run --rm -v "${STATE_DIR}:/state"' in wrapper_text


def test_render_metadata_detect_git_ref_uses_checked_out_head(monkeypatch) -> None:
    """The rehearsal renderer should pin containers to the current local checkout."""
    render_metadata = _load_render_metadata_module()

    def fake_run(*_args, **_kwargs):
        return SimpleNamespace(returncode=0, stdout="abc123\n", stderr="")

    monkeypatch.setattr(render_metadata.subprocess, "run", fake_run)

    assert render_metadata.detect_git_ref(Path("/tmp/repo")) == "abc123"


def test_render_metadata_detect_git_ref_requires_explicit_override_on_failure(
    monkeypatch,
) -> None:
    """The renderer should fail fast instead of silently rehearsing the wrong ref."""
    render_metadata = _load_render_metadata_module()

    def fake_run(*_args, **_kwargs):
        return SimpleNamespace(
            returncode=1, stdout="", stderr="fatal: not a git repository"
        )

    monkeypatch.setattr(render_metadata.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="CRSBENCH_LOCAL_REHEARSAL_GIT_REF"):
        render_metadata.detect_git_ref(Path("/tmp/repo"))
