"""Regression tests for distributed run orchestration."""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from crsbench.cloud.models import build_cloud_launch_plan
from crsbench.distributed.jobs import _build_trial_output_path
from crsbench.distributed.runtime_session import LockContentionError
from crsbench.run_experiment import (
    Trial,
    _get_experiment_queue_stats,
    _monitor_jobs_basic,
    _monitor_jobs_rich,
    _prepare_trial_dir_for_retry,
    build_trial_id,
    get_crs_cpu_count,
    get_crs_memory,
    run_experiment_distributed,
)
from crsbench.validation.schemas import (
    BenchmarkHarness,
    CloudBootstrapConfig,
    ExperimentConfig,
    HarnessFile,
)


def test_register_failure_cleans_registry_lease() -> None:
    """If registration publish fails, lease cleanup should still run."""
    config = MagicMock()
    config.redis_host = "localhost"
    config.resources = None
    config.keep_only_results = False

    session = MagicMock()
    session.trial_queue = MagicMock()
    session.register_or_raise.side_effect = RuntimeError("register failed")

    with (
        patch(
            "crsbench.distributed.runtime_session.DistributedRuntimeSession.for_run",
            return_value=session,
        ),
        patch(
            "crsbench.distributed.queue.get_existing_trials",
            return_value={"queued": {}, "started": {}, "failed": {}, "finished": {}},
        ),
        patch("crsbench.run_experiment.dump_trial_matrix"),
        patch(
            "crsbench.distributed.registry.RuntimeRegistration.from_experiment_config"
        ),
    ):
        with pytest.raises(RuntimeError, match="register failed"):
            run_experiment_distributed("exp-test", config, [_make_trial(None)])

    session.cleanup.assert_called_once()


def test_existing_jobs_non_interactive_defaults_to_continue(tmp_path: Path) -> None:
    """Non-interactive mode must not prompt and should use scoped continue."""
    config = MagicMock()
    config.redis_host = "localhost"
    config.resources = None
    config.keep_only_results = False
    config.experiment_filestore = tmp_path
    config.experiment = "exp-test"

    session = MagicMock()
    session.trial_queue = MagicMock()
    session.register_or_raise.side_effect = RuntimeError("stop after queue handling")

    existing = {
        "queued": {"k": MagicMock()},
        "started": {},
        "failed": {},
        "finished": {},
    }

    with (
        patch(
            "crsbench.distributed.runtime_session.DistributedRuntimeSession.for_run",
            return_value=session,
        ),
        patch("sys.stdin.isatty", return_value=False),
        patch("crsbench.run_experiment.prompt_queue_mode") as prompt_mode,
        patch(
            "crsbench.distributed.queue.get_existing_trials",
            return_value=existing,
        ) as get_existing,
        patch("crsbench.distributed.queue.handle_orphaned_jobs", return_value=0),
        patch("crsbench.run_experiment.dump_trial_matrix"),
        patch(
            "crsbench.distributed.registry.RuntimeRegistration.from_experiment_config"
        ),
    ):
        with pytest.raises(RuntimeError, match="stop after queue handling"):
            run_experiment_distributed("exp-test", config, [])

    prompt_mode.assert_not_called()
    get_existing.assert_called_once_with(
        session.trial_queue, experiment_name="exp-test"
    )
    session.cleanup.assert_called_once()


def test_continue_mode_does_not_retry_failed_by_default(tmp_path: Path) -> None:
    """Continue mode should not requeue failed jobs unless retry_failed=True."""
    config = MagicMock()
    config.redis_host = "localhost"
    config.resources = None
    config.keep_only_results = False
    config.experiment_filestore = tmp_path
    config.experiment = "exp-test"

    session = MagicMock()
    queue = MagicMock()
    session.trial_queue = queue
    session.register_or_raise.return_value = None

    failed_job = MagicMock()
    failed_job.id = "job-1"
    failed_job.meta = {}
    failed_job.kwargs = {}
    existing = {
        "queued": {},
        "started": {},
        "failed": {"k": failed_job},
        "finished": {},
    }

    with (
        patch(
            "crsbench.distributed.runtime_session.DistributedRuntimeSession.for_run",
            return_value=session,
        ),
        patch(
            "crsbench.distributed.queue.get_existing_trials",
            return_value=existing,
        ),
        patch("crsbench.distributed.queue.handle_orphaned_jobs", return_value=0),
        patch(
            "crsbench.run_experiment.dump_trial_matrix",
            side_effect=RuntimeError("stop after queue handling"),
        ),
        patch(
            "crsbench.distributed.registry.RuntimeRegistration.from_experiment_config"
        ),
    ):
        with pytest.raises(RuntimeError, match="stop after queue handling"):
            run_experiment_distributed(
                "exp-test",
                config,
                [],
                queue_mode="continue",
                retry_failed=False,
            )

    queue.enqueue_job.assert_not_called()
    failed_job.save_meta.assert_not_called()


def test_continue_mode_retry_failed_requeues(tmp_path: Path) -> None:
    """Continue mode with retry_failed should mark and requeue failed jobs."""
    config = MagicMock()
    config.redis_host = "localhost"
    config.resources = None
    config.keep_only_results = False
    config.experiment_filestore = tmp_path
    config.experiment = "exp-test"
    config.results_filestore = None

    session = MagicMock()
    queue = MagicMock()
    session.trial_queue = queue
    session.register_or_raise.return_value = None

    failed_job = MagicMock()
    failed_job.id = "job-1"
    failed_job.meta = {}
    failed_job.kwargs = {
        "crs": "crs-a",
        "benchmark": "bench-a",
        "harness_name": "fuzz_a",
        "mode": "delta",
        "sanitizer": "address",
        "trial_num": 1,
        "target_cpv_id": "cpv_0",
    }
    existing = {
        "queued": {},
        "started": {},
        "failed": {"k": failed_job},
        "finished": {},
    }

    with (
        patch(
            "crsbench.distributed.runtime_session.DistributedRuntimeSession.for_run",
            return_value=session,
        ),
        patch(
            "crsbench.distributed.queue.get_existing_trials",
            return_value=existing,
        ),
        patch("crsbench.distributed.queue.handle_orphaned_jobs", return_value=0),
        patch(
            "crsbench.run_experiment.dump_trial_matrix",
            side_effect=RuntimeError("stop after queue handling"),
        ),
        patch(
            "crsbench.distributed.registry.RuntimeRegistration.from_experiment_config"
        ),
    ):
        with pytest.raises(RuntimeError, match="stop after queue handling"):
            run_experiment_distributed(
                "exp-test",
                config,
                [],
                queue_mode="continue",
                retry_failed=True,
            )

    queue.enqueue_job.assert_called_once_with(failed_job)
    assert failed_job.meta["force_retry"] is True
    failed_job.save_meta.assert_called_once()


def test_queue_mode_quit_exits_without_registration(tmp_path: Path) -> None:
    """queue_mode=quit should return early when existing jobs are present."""
    config = MagicMock()
    config.redis_host = "localhost"
    config.resources = None
    config.keep_only_results = False
    config.experiment_filestore = tmp_path
    config.experiment = "exp-test"

    session = MagicMock()
    session.trial_queue = MagicMock()
    existing = {
        "queued": {"k": MagicMock()},
        "started": {},
        "failed": {},
        "finished": {},
    }

    with (
        patch(
            "crsbench.distributed.runtime_session.DistributedRuntimeSession.for_run",
            return_value=session,
        ),
        patch(
            "crsbench.distributed.queue.get_existing_trials",
            return_value=existing,
        ),
        patch(
            "crsbench.distributed.registry.RuntimeRegistration.from_experiment_config"
        ),
    ):
        run_experiment_distributed("exp-test", config, [], queue_mode="quit")

    session.register_or_raise.assert_not_called()
    session.cleanup.assert_called_once()


def test_continue_mode_lock_contention_skips_queue_mutations(tmp_path: Path) -> None:
    """continue mode should not mutate queue state when lock contention occurs."""
    config = MagicMock()
    config.redis_host = "localhost"
    config.resources = None
    config.keep_only_results = False
    config.experiment_filestore = tmp_path
    config.experiment = "exp-test"

    session = MagicMock()
    queue = MagicMock()
    session.trial_queue = queue
    session.register_or_raise.side_effect = LockContentionError("busy")

    failed_job = MagicMock()
    failed_job.id = "job-1"
    failed_job.meta = {}
    failed_job.kwargs = {}

    existing = {
        "queued": {},
        "started": {"s": MagicMock()},
        "failed": {"f": failed_job},
        "finished": {},
    }

    with (
        patch(
            "crsbench.distributed.runtime_session.DistributedRuntimeSession.for_run",
            return_value=session,
        ),
        patch(
            "crsbench.distributed.queue.get_existing_trials",
            return_value=existing,
        ),
        patch("crsbench.distributed.queue.handle_orphaned_jobs") as handle_orphaned,
        patch(
            "crsbench.run_experiment._prepare_trial_dir_for_retry", return_value=True
        ),
        patch(
            "crsbench.distributed.registry.RuntimeRegistration.from_experiment_config"
        ),
    ):
        run_experiment_distributed(
            "exp-test",
            config,
            [],
            queue_mode="continue",
            retry_failed=True,
        )

    handle_orphaned.assert_not_called()
    queue.enqueue_job.assert_not_called()


def test_queue_mode_fresh_acquires_lock_before_clearing(tmp_path: Path) -> None:
    """fresh mode must acquire lock before purging existing experiment jobs."""
    config = MagicMock()
    config.redis_host = "localhost"
    config.resources = None
    config.keep_only_results = False
    config.experiment_filestore = tmp_path
    config.experiment = "exp-test"

    session = MagicMock()
    session.trial_queue = MagicMock()
    session.register_or_raise.side_effect = RuntimeError("lock failure")

    existing = {
        "queued": {"k": MagicMock()},
        "started": {},
        "failed": {},
        "finished": {},
    }

    with (
        patch(
            "crsbench.distributed.runtime_session.DistributedRuntimeSession.for_run",
            return_value=session,
        ),
        patch(
            "crsbench.distributed.queue.get_existing_trials",
            return_value=existing,
        ),
        patch("crsbench.distributed.queue.clear_experiment_jobs") as clear_jobs,
        patch(
            "crsbench.distributed.registry.RuntimeRegistration.from_experiment_config"
        ),
    ):
        with pytest.raises(RuntimeError, match="lock failure"):
            run_experiment_distributed("exp-test", config, [], queue_mode="fresh")

    clear_jobs.assert_not_called()


def test_prepare_trial_dir_for_retry_cleans_results_filestore(tmp_path: Path) -> None:
    config = MagicMock()
    config.experiment = "exp-test"
    config.experiment_filestore = tmp_path / "exp-store"
    config.results_filestore = tmp_path / "results-store"

    job = MagicMock()
    job.kwargs = {
        "crs": "crs-a",
        "benchmark": "bench-a",
        "harness_name": "fuzz_a",
        "mode": "delta",
        "sanitizer": "address",
        "trial_num": 1,
        "target_cpv_id": "cpv_0",
    }

    for filestore in [config.experiment_filestore, config.results_filestore]:
        trial_dir = _build_trial_output_path(
            filestore=filestore,
            experiment_name=config.experiment,
            crs="crs-a",
            benchmark="bench-a",
            harness="fuzz_a",
            mode="delta",
            sanitizer="address",
            trial_num=1,
            target_cpv_id="cpv_0",
        )
        trial_dir.mkdir(parents=True, exist_ok=True)
        (trial_dir / ".fail").touch()

    assert _prepare_trial_dir_for_retry(config, job)

    for filestore in [config.experiment_filestore, config.results_filestore]:
        trial_dir = _build_trial_output_path(
            filestore=filestore,
            experiment_name=config.experiment,
            crs="crs-a",
            benchmark="bench-a",
            harness="fuzz_a",
            mode="delta",
            sanitizer="address",
            trial_num=1,
            target_cpv_id="cpv_0",
        )
        assert (trial_dir / "retries").exists()
        assert not (trial_dir / ".fail").exists()


def _make_trial(target_cpv_id: str | None) -> Trial:
    harness = HarnessFile(name="fuzz_target", path="./fuzz_target.c")
    benchmark = BenchmarkHarness(
        name="bench-a",
        path=Path("/tmp/bench-a"),
        harness=harness,
    )
    return Trial(
        crs="crs-a",
        benchmark_harness=benchmark,
        trial_num=1,
        mode="delta",
        sanitizer="address",
        target_cpv_id=target_cpv_id,
    )


def _make_provider_neutral_run_config(tmp_path: Path) -> ExperimentConfig:
    return ExperimentConfig.model_validate(
        {
            "experiment": "exp-test",
            "task": "bugfinding",
            "benchmark_suite": "sanity",
            "mode": "delta",
            "trials": 1,
            "max_total_time": 20000,
            "redis_host": "localhost:6379",
            "experiment_filestore": str(tmp_path),
            "report_filestore": str(tmp_path / "reports"),
            "build_timeout": 600,
            "run_timeout": 600,
            "verify_timeout": 600,
            "inputs": {"pov": {"max_variants_per_cpv": 1}},
            "cloud": {
                "providers": {
                    "gce": {
                        "project": "test-project",
                        "instance_profiles": {
                            "orchestrator-n2d": {
                                "machine_type": "n2d-standard-16",
                                "boot_disk_size_gb": 50,
                                "image": "projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64",
                                "service_account_email": "crsbench-orchestrator@test-project.iam.gserviceaccount.com",
                                "owner_label": "team-crs",
                                "crsbench_install_spec": "git+ssh://git@github.com/sslab-gatech/CRSBench.git",
                            },
                            "worker-n2d": {
                                "machine_type": "n2d-standard-16",
                                "boot_disk_size_gb": 50,
                                "image": "projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64",
                                "service_account_email": "crsbench-worker@test-project.iam.gserviceaccount.com",
                                "owner_label": "team-crs",
                                "crsbench_install_spec": "git+ssh://git@github.com/sslab-gatech/CRSBench.git",
                            },
                        },
                    }
                },
                "orchestrator": {
                    "provider": "gce",
                    "zone": "us-east5-b",
                    "instance_profile": "orchestrator-n2d",
                },
                "workers": {
                    "placements": [
                        {
                            "provider": "gce",
                            "zone": "us-east5-b",
                            "worker_count": 2,
                            "instance_profile": "worker-n2d",
                        },
                        {
                            "provider": "gce",
                            "zone": "us-east1-b",
                            "worker_count": 1,
                            "instance_profile": "worker-n2d",
                        },
                    ]
                },
            },
            "crs_compose": {"crs-a": {"num_cores": 1}},
        }
    )


def _add_secret_refs_to_provider_neutral_run_config(
    config: ExperimentConfig,
    *,
    deploy_key_ref: str = "file:.crsbench-keys/crsbench-deploy",
    hf_token_ref: str = "os.environ/HF_TOKEN",
) -> ExperimentConfig:
    config = config.model_copy(deep=True)
    profiles = config.cloud.providers.gce.instance_profiles
    profiles["orchestrator-n2d"].github_deploy_key_file = deploy_key_ref
    profiles["orchestrator-n2d"].hf_token = hf_token_ref
    profiles["worker-n2d"].github_deploy_key_file = deploy_key_ref
    profiles["worker-n2d"].hf_token = hf_token_ref
    return config


def _with_env_passthrough(
    config: ExperimentConfig,
    *,
    common: list[str] | None = None,
    orchestrator: list[str] | None = None,
    workers: list[str] | None = None,
    evaluators: list[str] | None = None,
) -> ExperimentConfig:
    config = config.model_copy(deep=True)
    config.cloud.bootstrap.env_passthrough.common = list(common or [])
    config.cloud.bootstrap.env_passthrough.orchestrator = list(orchestrator or [])
    config.cloud.bootstrap.env_passthrough.workers = list(workers or [])
    config.cloud.bootstrap.env_passthrough.evaluators = list(evaluators or [])
    return config


def _with_evaluator_placements(config: ExperimentConfig) -> ExperimentConfig:
    raw_config = config.model_dump(mode="json", exclude_none=True)
    raw_config["cloud"]["providers"]["gce"]["instance_profiles"]["evaluator-n2d"] = (
        raw_config["cloud"]["providers"]["gce"]["instance_profiles"]["worker-n2d"]
    )
    raw_config["cloud"]["evaluators"] = {
        "placements": [
            {
                "provider": "gce",
                "zone": "us-east5-b",
                "evaluator_count": 1,
                "instance_profile": "evaluator-n2d",
            },
            {
                "provider": "gce",
                "zone": "us-east1-b",
                "evaluator_count": 2,
                "instance_profile": "evaluator-n2d",
            },
        ]
    }
    return ExperimentConfig.model_validate(raw_config)


def test_build_trial_id_includes_target_cpv_id() -> None:
    cpv_trial = _make_trial("cpv_7")
    trial_id = build_trial_id("exp", cpv_trial, "_abc123")
    assert "-cpv-" in trial_id
    assert "untargeted" not in trial_id
    assert "-trial1_abc123" in trial_id


def test_get_crs_cpu_count_prioritizes_service_override() -> None:
    config = MagicMock()
    config.resources = MagicMock()
    config.resources.cores_per_trial = 6
    config.crs_compose = MagicMock()
    config.crs_compose.services = {
        "crs-a": MagicMock(num_cores=10),
    }

    assert get_crs_cpu_count("crs-a", config) == 10


def test_get_crs_cpu_count_returns_none_when_unset() -> None:
    config = MagicMock()
    config.resources = None
    config.crs_compose = None

    assert get_crs_cpu_count("crs-a", config) is None


def test_get_crs_memory_prioritizes_service_override() -> None:
    config = MagicMock()
    config.resources = MagicMock()
    config.resources.memory_per_trial = "16G"
    config.crs_compose = MagicMock()
    config.crs_compose.services = {
        "crs-a": MagicMock(mem_limit="32G"),
    }

    assert get_crs_memory("crs-a", config) == "32G"


def test_build_trial_id_uses_untargeted_fallback() -> None:
    all_trial = _make_trial(None)
    trial_id = build_trial_id("exp", all_trial, "_abc123")
    assert "-untargeted-trial1_abc123" in trial_id


def test_build_trial_id_avoids_cpv_normalization_collisions() -> None:
    trial_a = _make_trial("cpv/A")
    trial_b = _make_trial("cpv_a")
    id_a = build_trial_id("exp", trial_a, "_abc123")
    id_b = build_trial_id("exp", trial_b, "_abc123")
    assert id_a != id_b


def test_monitor_jobs_basic_includes_finished_none_result() -> None:
    queue = MagicMock()
    config = MagicMock()
    job = MagicMock()
    job.id = "job-12345678"
    job.is_finished = True
    job.is_failed = False
    job.result = None
    job.kwargs = {
        "crs": "crs-a",
        "benchmark": "bench-a",
        "harness_name": "fuzz_target",
        "trial_num": 3,
        "mode": "delta",
        "sanitizer": "address",
        "target_cpv_id": "cpv_1",
    }
    job.meta = {"worker_name": "worker-a"}
    job.get_status.return_value = "finished"

    with (
        patch(
            "crsbench.distributed.queue.get_queue_stats",
            return_value={"queued": 0, "started": 0, "finished": 1, "failed": 0},
        ),
        patch("crsbench.run_experiment._write_orchestrator_marker") as marker,
    ):
        results = _monitor_jobs_basic(
            queue=queue,
            job_list=[job],
            experiment_name="exp-test",
            config=config,
        )

    assert len(results) == 1
    assert results[0].success is False
    assert results[0].error_type == "MissingJobResult"
    marker.assert_called_once()


def test_get_experiment_queue_stats_uses_experiment_scoped_counts() -> None:
    """Monitor stats must ignore unrelated jobs in the shared flat queue."""
    queue = MagicMock()

    with (
        patch(
            "crsbench.distributed.queue.get_queue_stats",
            return_value={
                "queued": 51,
                "started": 12,
                "finished": 3,
                "failed": 0,
                "workers": 12,
            },
        ),
        patch(
            "crsbench.distributed.queue.get_existing_trials",
            return_value={
                "queued": {f"q{i}": MagicMock() for i in range(51)},
                "started": {f"s{i}": MagicMock() for i in range(12)},
                "finished": {},
                "failed": {},
                "deferred": {},
                "scheduled": {},
            },
        ),
    ):
        stats = _get_experiment_queue_stats(queue, "afc-all-crs-codex-gpt-5-4-full")

    assert stats == {
        "queued": 51,
        "started": 12,
        "finished": 0,
        "failed": 0,
        "workers": 12,
    }


def test_monitor_jobs_rich_includes_finished_none_result() -> None:
    queue = MagicMock()
    config = MagicMock()
    job = MagicMock()
    job.id = "job-12345678"
    job.is_finished = True
    job.is_failed = False
    job.result = None
    job.kwargs = {
        "crs": "crs-a",
        "benchmark": "bench-a",
        "harness_name": "fuzz_target",
        "trial_num": 4,
        "mode": "delta",
        "sanitizer": "address",
        "target_cpv_id": "cpv_2",
    }
    job.meta = {"worker_name": "worker-b"}
    job.get_status.return_value = "finished"

    class DummyLive:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def update(self, *args, **kwargs):
            return None

    with (
        patch(
            "crsbench.distributed.queue.get_queue_stats",
            return_value={"queued": 0, "started": 0, "finished": 1, "failed": 0},
        ),
        patch("rich.live.Live", DummyLive),
        patch("crsbench.run_experiment._write_orchestrator_marker") as marker,
    ):
        results = _monitor_jobs_rich(
            queue=queue,
            job_list=[job],
            experiment_name="exp-test",
            config=config,
        )

    assert len(results) == 1
    assert results[0].success is False
    assert results[0].error_type == "MissingJobResult"
    marker.assert_called_once()


def test_cloud_fleet_bringup_runs_before_enqueue(tmp_path: Path) -> None:
    """Cloud-backed runs must wait for bring-up before queueing trial work."""
    from crsbench.validation.schemas import CloudConfig, GceWorkerFleetConfig

    config = MagicMock()
    config.redis_host = "localhost"
    config.resources = None
    config.keep_only_results = False
    config.experiment_filestore = tmp_path
    config.experiment = "exp-test"
    config.crs_compose = None
    config.max_total_time = 3600
    config.model_dump.return_value = {"experiment": "exp-test"}
    config.cloud = CloudConfig(
        gce=GceWorkerFleetConfig(
            project="test-project",
            zone="us-central1-a",
            worker_count=1,
            machine_type="e2-standard-16",
            boot_disk_size_gb=200,
            image="projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64",
            service_account_email="crsbench-worker@test-project.iam.gserviceaccount.com",
            owner_label="team-crs",
            crsbench_install_spec="git+ssh://git@github.com/sslab-gatech/CRSBench.git",
        )
    )

    session = MagicMock()
    queue = MagicMock()
    session.trial_queue = queue
    session.cloud_readiness = MagicMock()
    session.register_or_raise.return_value = None

    registration = MagicMock()
    call_order: list[str] = []
    manager = MagicMock()
    manager.bring_up_gce_workers.side_effect = lambda **_kwargs: (
        call_order.append("bringup") or MagicMock(ready_count=1, requested_count=1)
    )

    def _enqueue(*args, **kwargs):
        del args, kwargs
        assert call_order == ["bringup"]
        raise RuntimeError("stop after enqueue")

    queue.enqueue.side_effect = _enqueue

    with (
        patch(
            "crsbench.distributed.runtime_session.DistributedRuntimeSession.for_run",
            return_value=session,
        ),
        patch(
            "crsbench.distributed.queue.get_existing_trials",
            return_value={"queued": {}, "started": {}, "failed": {}, "finished": {}},
        ),
        patch("crsbench.run_experiment.dump_trial_matrix"),
        patch(
            "crsbench.distributed.registry.RuntimeRegistration.from_experiment_config",
            return_value=registration,
        ),
        patch(
            "crsbench.cloud.status.CloudFleetStatusManager",
            return_value=manager,
        ),
    ):
        with pytest.raises(RuntimeError, match="stop after enqueue"):
            run_experiment_distributed("exp-test", config, [_make_trial(None)])

    manager.bring_up_gce_workers.assert_called_once()


def test_legacy_cloud_workers_resolve_secret_refs_before_bringup(
    tmp_path: Path,
) -> None:
    """Legacy GCE bring-up should resolve secret refs before provisioning."""
    from crsbench.validation.schemas import CloudConfig, GceWorkerFleetConfig

    key_dir = tmp_path / ".crsbench-keys"
    key_dir.mkdir()
    expected_key_path = str((key_dir / "crsbench-deploy").resolve())
    (key_dir / "crsbench-deploy").write_text("PRIVATE KEY", encoding="utf-8")
    original_cwd = Path.cwd()

    config = MagicMock()
    config.redis_host = "localhost"
    config.resources = None
    config.keep_only_results = False
    config.experiment_filestore = tmp_path
    config.experiment = "exp-test"
    config.crs_compose = None
    config.max_total_time = 3600
    config.model_dump.return_value = {"experiment": "exp-test"}
    config.cloud = CloudConfig(
        gce=GceWorkerFleetConfig(
            project="test-project",
            zone="us-central1-a",
            worker_count=1,
            machine_type="e2-standard-16",
            boot_disk_size_gb=200,
            image="projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64",
            service_account_email="crsbench-worker@test-project.iam.gserviceaccount.com",
            owner_label="team-crs",
            crsbench_install_spec="git+ssh://git@github.com/sslab-gatech/CRSBench.git",
            github_deploy_key_file="file:.crsbench-keys/crsbench-deploy",
            hf_token="os.environ/HF_TOKEN",
        )
    )

    session = MagicMock()
    queue = MagicMock()
    session.trial_queue = queue
    session.cloud_readiness = MagicMock()
    session.register_or_raise.return_value = None

    manager = MagicMock()
    registration = MagicMock()

    def _bring_up_gce_workers(**kwargs):
        fleet = kwargs["fleet"]
        assert fleet.github_deploy_key_file == expected_key_path
        assert fleet.hf_token == "hf_secret_value"
        raise RuntimeError("stop after resolved legacy bringup")

    manager.bring_up_gce_workers.side_effect = _bring_up_gce_workers

    with (
        patch.dict(os.environ, {"HF_TOKEN": "hf_secret_value"}, clear=False),
        patch(
            "crsbench.distributed.runtime_session.DistributedRuntimeSession.for_run",
            return_value=session,
        ),
        patch(
            "crsbench.distributed.queue.get_existing_trials",
            return_value={"queued": {}, "started": {}, "failed": {}, "finished": {}},
        ),
        patch("crsbench.run_experiment.dump_trial_matrix"),
        patch(
            "crsbench.distributed.registry.RuntimeRegistration.from_experiment_config",
            return_value=registration,
        ),
        patch(
            "crsbench.cloud.status.CloudFleetStatusManager",
            return_value=manager,
        ),
    ):
        os.chdir(tmp_path)
        try:
            with pytest.raises(
                RuntimeError, match="stop after resolved legacy bringup"
            ):
                run_experiment_distributed("exp-test", config, [_make_trial(None)])
        finally:
            os.chdir(original_cwd)

    manager.bring_up_gce_workers.assert_called_once()


def test_provider_neutral_cloud_workers_validate_quota_before_bringup(
    tmp_path: Path,
) -> None:
    """Local orchestrator runs should validate quota before multi-zone worker bring-up."""
    config = _make_provider_neutral_run_config(tmp_path)
    config.cloud.bootstrap = CloudBootstrapConfig(
        prepare_mode="skip_base_images",
        download_benchmarks="always",
    )

    session = MagicMock()
    queue = MagicMock()
    session.trial_queue = queue
    session.cloud_readiness = MagicMock()
    session.register_or_raise.return_value = None

    registration = MagicMock()
    launch_plan = MagicMock()
    launch_plan.experiment_name = "exp-test"
    launch_plan.evaluator_placements = []
    adapter = MagicMock()
    validator = MagicMock()
    manager = MagicMock()
    call_order: list[str] = []
    resolved_plan = MagicMock()
    resolved_plan.experiment_name = "exp-test"
    resolved_plan.evaluator_placements = []

    validator.validate.side_effect = lambda plan, *, include_orchestrator=True: (
        call_order.append(
            f"validate:{plan.experiment_name}:include_orchestrator={include_orchestrator}"
        )
    )

    def _bring_up_workers(**kwargs):
        bootstrap_inputs = kwargs["bootstrap_inputs"]
        assert bootstrap_inputs.prepare_mode == "skip_base_images"
        assert bootstrap_inputs.download_benchmarks == "always"
        assert bootstrap_inputs.benchmark_suite == "sanity"
        call_order.append("bringup")
        return MagicMock(ready_count=3, requested_count=3)

    manager.bring_up_workers.side_effect = _bring_up_workers

    def _enqueue(*args, **kwargs):
        del args, kwargs
        assert call_order == [
            "validate:exp-test:include_orchestrator=False",
            "bringup",
        ]
        raise RuntimeError("stop after enqueue")

    queue.enqueue.side_effect = _enqueue

    with (
        patch(
            "crsbench.distributed.runtime_session.DistributedRuntimeSession.for_run",
            return_value=session,
        ),
        patch(
            "crsbench.distributed.queue.get_existing_trials",
            return_value={"queued": {}, "started": {}, "failed": {}, "finished": {}},
        ),
        patch("crsbench.run_experiment.dump_trial_matrix"),
        patch(
            "crsbench.distributed.registry.RuntimeRegistration.from_experiment_config",
            return_value=registration,
        ),
        patch(
            "crsbench.cloud.models.build_cloud_launch_plan",
            return_value=launch_plan,
        ),
        patch(
            "crsbench.cloud.gce.launch_preflight.prepare_gce_launch_inputs",
            return_value=MagicMock(resolved_plan=resolved_plan),
        ),
        patch(
            "crsbench.cloud.gce.provider.GceProviderAdapter",
            return_value=adapter,
        ),
        patch(
            "crsbench.cloud.quota.QuotaValidator",
            return_value=validator,
        ),
        patch(
            "crsbench.cloud.status.CloudFleetStatusManager",
            return_value=manager,
        ),
    ):
        with pytest.raises(RuntimeError, match="stop after enqueue"):
            run_experiment_distributed("exp-test", config, [_make_trial(None)])

    validator.validate.assert_called_once_with(launch_plan, include_orchestrator=False)
    manager.bring_up_workers.assert_called_once()
    manager.bring_up_instances.assert_not_called()
    manager.bring_up_gce_workers.assert_not_called()


def test_provider_neutral_cloud_workers_resolve_secret_refs_before_bringup(
    tmp_path: Path,
) -> None:
    """Local bring-up should use a resolved launch plan without mutating the original."""
    config = _add_secret_refs_to_provider_neutral_run_config(
        _make_provider_neutral_run_config(tmp_path)
    )

    key_dir = tmp_path / ".crsbench-keys"
    key_dir.mkdir()
    (key_dir / "crsbench-deploy").write_text("PRIVATE KEY", encoding="utf-8")
    original_cwd = Path.cwd()

    session = MagicMock()
    queue = MagicMock()
    session.trial_queue = queue
    session.cloud_readiness = MagicMock()
    session.register_or_raise.return_value = None

    registration = MagicMock()
    adapter = MagicMock()
    validator = MagicMock()
    manager = MagicMock()
    launch_plan = build_cloud_launch_plan(config)
    expected_key_path = str((key_dir / "crsbench-deploy").resolve())

    def _bring_up_workers(**kwargs):
        resolved_plan = kwargs["plan"]
        assert (
            resolved_plan.worker_placements[0].instance_profile.profile_config[
                "hf_token"
            ]
            == "hf_secret_value"
        )
        assert (
            resolved_plan.worker_placements[0].instance_profile.profile_config[
                "github_deploy_key_file"
            ]
            == expected_key_path
        )
        assert (
            launch_plan.worker_placements[0].instance_profile.profile_config["hf_token"]
            == "os.environ/HF_TOKEN"
        )
        assert (
            launch_plan.worker_placements[0].instance_profile.profile_config[
                "github_deploy_key_file"
            ]
            == "file:.crsbench-keys/crsbench-deploy"
        )
        raise RuntimeError("stop after resolved bringup")

    manager.bring_up_workers.side_effect = _bring_up_workers

    with (
        patch.dict(os.environ, {"HF_TOKEN": "hf_secret_value"}, clear=False),
        patch(
            "crsbench.distributed.runtime_session.DistributedRuntimeSession.for_run",
            return_value=session,
        ),
        patch(
            "crsbench.distributed.queue.get_existing_trials",
            return_value={"queued": {}, "started": {}, "failed": {}, "finished": {}},
        ),
        patch("crsbench.run_experiment.dump_trial_matrix"),
        patch(
            "crsbench.distributed.registry.RuntimeRegistration.from_experiment_config",
            return_value=registration,
        ),
        patch(
            "crsbench.cloud.models.build_cloud_launch_plan",
            return_value=launch_plan,
        ),
        patch(
            "crsbench.cloud.gce.provider.GceProviderAdapter",
            return_value=adapter,
        ),
        patch(
            "crsbench.cloud.quota.QuotaValidator",
            return_value=validator,
        ),
        patch(
            "crsbench.cloud.status.CloudFleetStatusManager",
            return_value=manager,
        ),
    ):
        os.chdir(tmp_path)
        try:
            with pytest.raises(RuntimeError, match="stop after resolved bringup"):
                run_experiment_distributed("exp-test", config, [_make_trial(None)])
        finally:
            os.chdir(original_cwd)

    validator.validate.assert_called_once_with(launch_plan, include_orchestrator=False)
    manager.bring_up_workers.assert_called_once()


def test_provider_neutral_cloud_workers_pass_role_specific_env_passthrough(
    tmp_path: Path,
) -> None:
    """Local cloud-worker bring-up should pass only common+worker env vars to VMs."""
    config = _with_env_passthrough(
        _make_provider_neutral_run_config(tmp_path),
        common=["CRSBENCH_LLM_UPSTREAM_BASE_URL"],
        orchestrator=["CRSBENCH_LLM_MASTER_KEY"],
        workers=["OPENAI_API_KEY"],
    )

    session = MagicMock()
    queue = MagicMock()
    session.trial_queue = queue
    session.cloud_readiness = MagicMock()
    session.register_or_raise.return_value = None

    registration = MagicMock()
    adapter = MagicMock()
    validator = MagicMock()
    manager = MagicMock()
    launch_plan = build_cloud_launch_plan(config)
    resolved_plan = MagicMock()
    resolved_plan.experiment_name = "exp-test"

    def _bring_up_workers(**kwargs):
        assert kwargs["env_passthrough"] == {
            "CRSBENCH_LLM_UPSTREAM_BASE_URL": "https://llm.example.test",
            "OPENAI_API_KEY": "openai-key",
        }
        raise RuntimeError("stop after env passthrough")

    manager.bring_up_workers.side_effect = _bring_up_workers

    with (
        patch.dict(
            os.environ,
            {
                "CRSBENCH_LLM_UPSTREAM_BASE_URL": "https://llm.example.test",
                "CRSBENCH_LLM_MASTER_KEY": "master-key",
                "OPENAI_API_KEY": "openai-key",
            },
            clear=False,
        ),
        patch(
            "crsbench.distributed.runtime_session.DistributedRuntimeSession.for_run",
            return_value=session,
        ),
        patch(
            "crsbench.distributed.queue.get_existing_trials",
            return_value={"queued": {}, "started": {}, "failed": {}, "finished": {}},
        ),
        patch("crsbench.run_experiment.dump_trial_matrix"),
        patch(
            "crsbench.distributed.registry.RuntimeRegistration.from_experiment_config",
            return_value=registration,
        ),
        patch(
            "crsbench.cloud.models.build_cloud_launch_plan",
            return_value=launch_plan,
        ),
        patch(
            "crsbench.cloud.gce.launch_preflight.prepare_gce_launch_inputs",
            return_value=MagicMock(
                resolved_plan=resolved_plan,
                worker_env={
                    "CRSBENCH_LLM_UPSTREAM_BASE_URL": "https://llm.example.test",
                    "OPENAI_API_KEY": "openai-key",
                },
            ),
        ) as mock_preflight,
        patch(
            "crsbench.cloud.gce.provider.GceProviderAdapter",
            return_value=adapter,
        ),
        patch(
            "crsbench.cloud.quota.QuotaValidator",
            return_value=validator,
        ),
        patch(
            "crsbench.cloud.status.CloudFleetStatusManager",
            return_value=manager,
        ),
    ):
        with pytest.raises(RuntimeError, match="stop after env passthrough"):
            run_experiment_distributed("exp-test", config, [_make_trial(None)])

    mock_preflight.assert_called_once_with(
        plan=launch_plan,
        bootstrap=config.cloud.bootstrap,
        cwd=Path.cwd(),
    )
    manager.bring_up_workers.assert_called_once()


def test_provider_neutral_cloud_instances_with_evaluators_pass_role_specific_env_passthrough(
    tmp_path: Path,
) -> None:
    """Local cloud bring-up should provision evaluators with evaluator-only env vars."""
    config = _with_env_passthrough(
        _with_evaluator_placements(_make_provider_neutral_run_config(tmp_path)),
        common=["CRSBENCH_LLM_UPSTREAM_BASE_URL"],
        orchestrator=["CRSBENCH_LLM_MASTER_KEY"],
        workers=["OPENAI_API_KEY"],
        evaluators=["ANTHROPIC_API_KEY"],
    )

    session = MagicMock()
    queue = MagicMock()
    session.trial_queue = queue
    session.cloud_readiness = MagicMock()
    session.register_or_raise.return_value = None

    registration = MagicMock()
    adapter = MagicMock()
    validator = MagicMock()
    manager = MagicMock()
    launch_plan = build_cloud_launch_plan(config)
    resolved_plan = MagicMock()
    resolved_plan.experiment_name = "exp-test"
    resolved_plan.evaluator_placements = launch_plan.evaluator_placements

    def _bring_up_instances(**kwargs):
        assert kwargs["worker_env_passthrough"] == {
            "CRSBENCH_LLM_UPSTREAM_BASE_URL": "https://llm.example.test",
            "OPENAI_API_KEY": "openai-key",
        }
        assert kwargs["evaluator_env_passthrough"] == {
            "CRSBENCH_LLM_UPSTREAM_BASE_URL": "https://llm.example.test",
            "ANTHROPIC_API_KEY": "anthropic-key",
        }
        raise RuntimeError("stop after evaluator env passthrough")

    manager.bring_up_instances.side_effect = _bring_up_instances

    with (
        patch.dict(
            os.environ,
            {
                "CRSBENCH_LLM_UPSTREAM_BASE_URL": "https://llm.example.test",
                "CRSBENCH_LLM_MASTER_KEY": "master-key",
                "OPENAI_API_KEY": "openai-key",
                "ANTHROPIC_API_KEY": "anthropic-key",
            },
            clear=False,
        ),
        patch(
            "crsbench.distributed.runtime_session.DistributedRuntimeSession.for_run",
            return_value=session,
        ),
        patch(
            "crsbench.distributed.queue.get_existing_trials",
            return_value={"queued": {}, "started": {}, "failed": {}, "finished": {}},
        ),
        patch("crsbench.run_experiment.dump_trial_matrix"),
        patch(
            "crsbench.distributed.registry.RuntimeRegistration.from_experiment_config",
            return_value=registration,
        ),
        patch(
            "crsbench.cloud.models.build_cloud_launch_plan",
            return_value=launch_plan,
        ),
        patch(
            "crsbench.cloud.gce.launch_preflight.prepare_gce_launch_inputs",
            return_value=MagicMock(
                resolved_plan=resolved_plan,
                worker_env={
                    "CRSBENCH_LLM_UPSTREAM_BASE_URL": "https://llm.example.test",
                    "OPENAI_API_KEY": "openai-key",
                },
                evaluator_env={
                    "CRSBENCH_LLM_UPSTREAM_BASE_URL": "https://llm.example.test",
                    "ANTHROPIC_API_KEY": "anthropic-key",
                },
            ),
        ) as mock_preflight,
        patch(
            "crsbench.cloud.gce.provider.GceProviderAdapter",
            return_value=adapter,
        ),
        patch(
            "crsbench.cloud.quota.QuotaValidator",
            return_value=validator,
        ),
        patch(
            "crsbench.cloud.status.CloudFleetStatusManager",
            return_value=manager,
        ),
    ):
        with pytest.raises(RuntimeError, match="stop after evaluator env passthrough"):
            run_experiment_distributed("exp-test", config, [_make_trial(None)])

    mock_preflight.assert_called_once_with(
        plan=launch_plan,
        bootstrap=config.cloud.bootstrap,
        cwd=Path.cwd(),
    )
    manager.bring_up_instances.assert_called_once()
    manager.bring_up_workers.assert_not_called()


def test_provider_neutral_preprovisioned_wait_does_not_resolve_secret_refs_again(
    tmp_path: Path,
) -> None:
    """Remote orchestrator wait path must not require operator-only secret sources."""
    config = _add_secret_refs_to_provider_neutral_run_config(
        _make_provider_neutral_run_config(tmp_path)
    )

    session = MagicMock()
    queue = MagicMock()
    session.trial_queue = queue
    session.cloud_readiness = MagicMock()
    session.register_or_raise.return_value = None

    registration = MagicMock()
    launch_plan = build_cloud_launch_plan(config)
    adapter = MagicMock()
    manager = MagicMock()

    def _wait_for_existing_workers(**kwargs):
        unresolved_plan = kwargs["plan"]
        assert (
            unresolved_plan.worker_placements[0].instance_profile.profile_config[
                "hf_token"
            ]
            == "os.environ/HF_TOKEN"
        )
        raise RuntimeError("stop after existing-worker wait")

    manager.wait_for_existing_workers.side_effect = _wait_for_existing_workers

    with (
        patch.dict(
            os.environ,
            {"CRSBENCH_CLOUD_PREPROVISIONED_WORKERS": "1"},
            clear=False,
        ),
        patch(
            "crsbench.distributed.runtime_session.DistributedRuntimeSession.for_run",
            return_value=session,
        ),
        patch(
            "crsbench.distributed.queue.get_existing_trials",
            return_value={"queued": {}, "started": {}, "failed": {}, "finished": {}},
        ),
        patch("crsbench.run_experiment.dump_trial_matrix"),
        patch(
            "crsbench.distributed.registry.RuntimeRegistration.from_experiment_config",
            return_value=registration,
        ),
        patch(
            "crsbench.cloud.models.build_cloud_launch_plan",
            return_value=launch_plan,
        ),
        patch(
            "crsbench.cloud.gce.provider.GceProviderAdapter",
            return_value=adapter,
        ),
        patch(
            "crsbench.cloud.status.CloudFleetStatusManager",
            return_value=manager,
        ),
    ):
        with pytest.raises(RuntimeError, match="stop after existing-worker wait"):
            run_experiment_distributed("exp-test", config, [_make_trial(None)])

    manager.wait_for_existing_workers.assert_called_once()
    manager.bring_up_workers.assert_not_called()


def test_cloud_fleet_failure_aborts_before_enqueue(tmp_path: Path) -> None:
    """Cloud bring-up failures must abort before any trial jobs are queued."""
    from crsbench.cloud.status import CloudFleetBringupError
    from crsbench.validation.schemas import CloudConfig, GceWorkerFleetConfig

    config = MagicMock()
    config.redis_host = "localhost"
    config.resources = None
    config.keep_only_results = False
    config.experiment_filestore = tmp_path
    config.experiment = "exp-test"
    config.crs_compose = None
    config.max_total_time = 3600
    config.model_dump.return_value = {"experiment": "exp-test"}
    config.cloud = CloudConfig(
        gce=GceWorkerFleetConfig(
            project="test-project",
            zone="us-central1-a",
            worker_count=1,
            machine_type="e2-standard-16",
            boot_disk_size_gb=200,
            image="projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64",
            service_account_email="crsbench-worker@test-project.iam.gserviceaccount.com",
            owner_label="team-crs",
            crsbench_install_spec="git+ssh://git@github.com/sslab-gatech/CRSBench.git",
        )
    )

    session = MagicMock()
    queue = MagicMock()
    session.trial_queue = queue
    session.cloud_readiness = MagicMock()
    session.register_or_raise.return_value = None

    manager = MagicMock()
    manager.bring_up_gce_workers.side_effect = CloudFleetBringupError(
        "gce-worker-001 bootstrap failed: systemd unit exited",
    )

    with (
        patch(
            "crsbench.distributed.runtime_session.DistributedRuntimeSession.for_run",
            return_value=session,
        ),
        patch(
            "crsbench.distributed.queue.get_existing_trials",
            return_value={"queued": {}, "started": {}, "failed": {}, "finished": {}},
        ),
        patch("crsbench.run_experiment.dump_trial_matrix"),
        patch(
            "crsbench.distributed.registry.RuntimeRegistration.from_experiment_config"
        ),
        patch(
            "crsbench.cloud.status.CloudFleetStatusManager",
            return_value=manager,
        ),
    ):
        with pytest.raises(
            CloudFleetBringupError,
            match="bootstrap failed: systemd unit exited",
        ):
            run_experiment_distributed("exp-test", config, [_make_trial(None)])

    queue.enqueue.assert_not_called()


def test_cloud_fleet_bringup_is_skipped_when_no_trials_remain(tmp_path: Path) -> None:
    """Runs with no remaining work should not provision cloud workers."""
    from crsbench.validation.schemas import CloudConfig, GceWorkerFleetConfig

    config = MagicMock()
    config.redis_host = "localhost"
    config.resources = None
    config.keep_only_results = False
    config.experiment_filestore = tmp_path
    config.experiment = "exp-test"
    config.crs_compose = None
    config.max_total_time = 3600
    config.model_dump.return_value = {"experiment": "exp-test"}
    config.cloud = CloudConfig(
        gce=GceWorkerFleetConfig(
            project="test-project",
            zone="us-central1-a",
            worker_count=1,
            machine_type="e2-standard-16",
            boot_disk_size_gb=200,
            image="projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64",
            service_account_email="crsbench-worker@test-project.iam.gserviceaccount.com",
            owner_label="team-crs",
            crsbench_install_spec="git+ssh://git@github.com/sslab-gatech/CRSBench.git",
        )
    )

    session = MagicMock()
    session.trial_queue = MagicMock()
    session.cloud_readiness = MagicMock()

    manager = MagicMock()

    with (
        patch(
            "crsbench.distributed.runtime_session.DistributedRuntimeSession.for_run",
            return_value=session,
        ),
        patch(
            "crsbench.distributed.queue.get_existing_trials",
            return_value={"queued": {}, "started": {}, "failed": {}, "finished": {}},
        ),
        patch("crsbench.run_experiment.dump_trial_matrix"),
        patch("crsbench.run_experiment.log_section"),
        patch(
            "crsbench.distributed.registry.RuntimeRegistration.from_experiment_config"
        ),
        patch(
            "crsbench.cloud.status.CloudFleetStatusManager",
            return_value=manager,
        ),
    ):
        run_experiment_distributed("exp-test", config, [])

    session.register_or_raise.assert_not_called()
    manager.bring_up_gce_workers.assert_not_called()
    session.trial_queue.enqueue.assert_not_called()


def test_cloud_fleet_bringup_is_skipped_for_preprovisioned_remote_orchestrator(
    tmp_path: Path, monkeypatch
) -> None:
    """Remote orchestrator mode should wait for existing workers instead of reprovisioning."""
    from crsbench.validation.schemas import CloudConfig, GceWorkerFleetConfig

    monkeypatch.setenv("CRSBENCH_CLOUD_PREPROVISIONED_WORKERS", "1")

    config = MagicMock()
    config.redis_host = "localhost"
    config.resources = None
    config.keep_only_results = False
    config.experiment_filestore = tmp_path
    config.experiment = "exp-test"
    config.crs_compose = None
    config.max_total_time = 3600
    config.model_dump.return_value = {"experiment": "exp-test"}
    config.cloud = CloudConfig(
        gce=GceWorkerFleetConfig(
            project="test-project",
            zone="us-central1-a",
            worker_count=1,
            machine_type="e2-standard-16",
            boot_disk_size_gb=200,
            image="projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64",
            service_account_email="crsbench-worker@test-project.iam.gserviceaccount.com",
            owner_label="team-crs",
            crsbench_install_spec="git+ssh://git@github.com/sslab-gatech/CRSBench.git",
        )
    )

    session = MagicMock()
    queue = MagicMock()
    session.trial_queue = queue
    session.cloud_readiness = MagicMock()
    session.register_or_raise.return_value = None

    def _enqueue(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("stop after enqueue")

    queue.enqueue.side_effect = _enqueue
    manager = MagicMock()

    with (
        patch(
            "crsbench.distributed.runtime_session.DistributedRuntimeSession.for_run",
            return_value=session,
        ),
        patch(
            "crsbench.distributed.queue.get_existing_trials",
            return_value={"queued": {}, "started": {}, "failed": {}, "finished": {}},
        ),
        patch("crsbench.run_experiment.dump_trial_matrix"),
        patch(
            "crsbench.distributed.registry.RuntimeRegistration.from_experiment_config"
        ),
        patch(
            "crsbench.cloud.status.CloudFleetStatusManager",
            return_value=manager,
        ),
    ):
        with pytest.raises(RuntimeError, match="stop after enqueue"):
            run_experiment_distributed("exp-test", config, [_make_trial(None)])

    manager.bring_up_gce_workers.assert_not_called()
    manager.wait_for_existing_gce_workers.assert_called_once()
