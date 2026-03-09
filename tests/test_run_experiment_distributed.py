"""Regression tests for distributed run orchestration."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from crsbench.distributed.jobs import _build_trial_output_path
from crsbench.distributed.runtime_session import LockContentionError
from crsbench.run_experiment import (
    Trial,
    _monitor_jobs_basic,
    _monitor_jobs_rich,
    _prepare_trial_dir_for_retry,
    build_trial_id,
    get_crs_cpu_count,
    get_crs_memory,
    run_experiment_distributed,
)
from crsbench.validation.schemas import BenchmarkHarness, HarnessFile


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
            run_experiment_distributed("exp-test", config, [])

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
