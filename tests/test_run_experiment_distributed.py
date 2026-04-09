"""Regression tests for distributed run orchestration."""

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest
from crsbench.cloud.models import build_cloud_launch_plan
from crsbench.distributed.job_lifecycle import JobLifecycleRecord, JobState
from crsbench.distributed.jobs import _build_trial_output_path
from crsbench.distributed.runtime_session import LockContentionError
from crsbench.evaluation.results import TrialMetadata, TrialResult
from crsbench.run_experiment import (
    Trial,
    _build_artifact_checker,
    _build_monitor_callbacks,
    _collect_monitored_results,
    _dedupe_results_by_logical_trial,
    _get_experiment_queue_stats,
    _monitor_jobs_basic,
    _monitor_jobs_rich,
    _prepare_trial_dir_for_retry,
    build_trial_id,
    build_trial_queue_job_id,
    generate_final_report,
    get_crs_cpu_count,
    get_crs_memory,
    monitor_jobs,
    run_experiment_distributed,
)
from crsbench.utils.apprise_notify import (
    AppriseNotificationConfig,
    format_completion_message,
    format_failure_message,
)
from crsbench.validation.schemas import (
    BenchmarkHarness,
    CloudBootstrapConfig,
    ExperimentConfig,
    HarnessFile,
)


def test_build_artifact_checker_recognizes_success_and_fail_markers(
    tmp_path: Path,
) -> None:
    """Timeout recovery should treat either terminal marker as authoritative."""
    config = MagicMock()
    config.experiment_filestore = tmp_path
    config.experiment = "exp-test"

    checker = _build_artifact_checker(config)

    success_dir = _build_trial_output_path(
        filestore=tmp_path.resolve(),
        experiment_name="exp-test",
        crs="crs-a",
        benchmark="bench-a",
        harness="fuzz_target",
        mode="delta",
        sanitizer="address",
        trial_num=1,
        target_cpv_id=None,
    )
    success_dir.mkdir(parents=True, exist_ok=True)
    (success_dir / ".success").touch()

    fail_dir = _build_trial_output_path(
        filestore=tmp_path.resolve(),
        experiment_name="exp-test",
        crs="crs-b",
        benchmark="bench-b",
        harness="harness-b",
        mode="delta",
        sanitizer="address",
        trial_num=2,
        target_cpv_id=None,
    )
    fail_dir.mkdir(parents=True, exist_ok=True)
    (fail_dir / ".fail").touch()

    assert checker("crs-a:bench-a:fuzz_target:delta:address:1:-") is JobState.COMPLETED
    assert checker("crs-b:bench-b:harness-b:delta:address:2:-") is JobState.FAILED
    assert checker("crs-c:bench-c:harness-c:delta:address:3:-") is None


def _make_result(
    *,
    crs: str = "crs-a",
    benchmark: str = "bench-a",
    harness: str = "fuzz_target",
    trial_num: int = 1,
    mode: str = "delta",
    sanitizer: str = "address",
    success: bool = True,
    error: str | None = None,
    worker_machine: str | None = None,
) -> TrialResult:
    return TrialResult(
        crs=crs,
        benchmark=benchmark,
        harness=harness,
        trial_num=trial_num,
        crs_type="bug-finding",
        mode=mode,
        sanitizer=sanitizer,
        success=success,
        execution_time=1.0,
        error=error,
        report={},
        metadata=TrialMetadata(
            timestamp_start=0.0,
            timestamp_end=1.0,
            worker_machine=worker_machine,
        ),
    )


def _make_distributed_test_config(tmp_path: Path) -> MagicMock:
    config = MagicMock()
    config.redis_host = "localhost"
    config.resources = None
    config.keep_only_results = False
    config.experiment_filestore = tmp_path
    config.report_filestore = tmp_path / "reports"
    config.experiment = "exp-test"
    return config


def _record_report(*_args, **_kwargs) -> None:
    call_order = _record_report.call_order
    call_order.append("report")


def _record_notify(*_args, **_kwargs) -> None:
    call_order = _record_notify.call_order
    call_order.append("notify")


@pytest.mark.notification
def test_distributed_run_sends_completion_notification_after_final_report(
    tmp_path: Path,
) -> None:
    config = _make_distributed_test_config(tmp_path)
    notification_config = AppriseNotificationConfig(urls=("mailto://example",))
    session = MagicMock()
    queue = MagicMock()
    session.trial_queue = queue
    session.register_or_raise.return_value = None

    trial = _make_trial(None)
    result = _make_result(success=True)
    call_order: list[str] = []
    _record_report.call_order = call_order
    _record_notify.call_order = call_order

    with (
        patch(
            "crsbench.distributed.runtime_session.DistributedRuntimeSession.for_run",
            return_value=session,
        ),
        patch(
            "crsbench.distributed.queue.get_existing_trials",
            return_value={"queued": {}, "started": {}, "failed": {}, "finished": {}},
        ),
        patch(
            "crsbench.distributed.queue.get_existing_trial_jobs",
            return_value={"queued": [], "started": [], "failed": [], "finished": []},
        ),
        patch("crsbench.distributed.queue.handle_orphaned_jobs", return_value=0),
        patch(
            "crsbench.distributed.registry.RuntimeRegistration.from_experiment_config"
        ),
        patch("crsbench.run_experiment.dump_trial_matrix"),
        patch("crsbench.run_experiment.monitor_jobs", return_value=[result]),
        patch(
            "crsbench.run_experiment.generate_final_report",
            side_effect=_record_report,
        ),
        patch(
            "crsbench.run_experiment.load_apprise_notification_config",
            return_value=notification_config,
        ),
        patch(
            "crsbench.run_experiment.send_apprise_message",
            side_effect=_record_notify,
        ) as send_mock,
    ):
        run_experiment_distributed("exp-test", config, [trial], queue_mode="fresh")

    assert call_order == ["report", "notify"]
    send_mock.assert_called_once()
    sent_config = send_mock.call_args.args[0]
    assert sent_config == notification_config
    body = send_mock.call_args.kwargs["body"]
    assert format_completion_message("Distributed exp-test") in body
    assert "Mode: distributed" in body
    assert "Logical trials: 1" in body
    assert "Successful: 1" in body
    assert "Failed: 0" in body
    assert f"Report path: {config.report_filestore / config.experiment}" in body


@pytest.mark.notification
def test_distributed_run_sends_failure_notification_when_post_run_cleanup_fails(
    tmp_path: Path,
) -> None:
    config = _make_distributed_test_config(tmp_path)
    config.keep_only_results = True
    notification_config = AppriseNotificationConfig(urls=("mailto://example",))
    session = MagicMock()
    queue = MagicMock()
    session.trial_queue = queue
    session.register_or_raise.return_value = None
    session.cleanup.return_value = None

    trial = _make_trial(None)
    result = _make_result(success=True)

    with (
        patch(
            "crsbench.distributed.runtime_session.DistributedRuntimeSession.for_run",
            return_value=session,
        ),
        patch(
            "crsbench.distributed.queue.get_existing_trials",
            return_value={"queued": {}, "started": {}, "failed": {}, "finished": {}},
        ),
        patch(
            "crsbench.distributed.queue.get_existing_trial_jobs",
            return_value={"queued": [], "started": [], "failed": [], "finished": []},
        ),
        patch("crsbench.distributed.queue.handle_orphaned_jobs", return_value=0),
        patch(
            "crsbench.distributed.registry.RuntimeRegistration.from_experiment_config"
        ),
        patch("crsbench.run_experiment.dump_trial_matrix"),
        patch("crsbench.run_experiment.monitor_jobs", return_value=[result]),
        patch("crsbench.run_experiment.generate_final_report"),
        patch(
            "crsbench.run_experiment._cleanup_experiment_artifacts",
            side_effect=RuntimeError("cleanup boom"),
        ),
        patch(
            "crsbench.run_experiment.load_apprise_notification_config",
            return_value=notification_config,
        ),
        patch("crsbench.run_experiment.send_apprise_message") as send_mock,
    ):
        with pytest.raises(RuntimeError, match="cleanup boom"):
            run_experiment_distributed("exp-test", config, [trial], queue_mode="fresh")

    send_mock.assert_called_once()
    body = send_mock.call_args.kwargs["body"]
    assert format_failure_message("Distributed exp-test", "cleanup boom") in body
    assert "Tracked jobs: 1" in body
    assert format_completion_message("Distributed exp-test") not in body


@pytest.mark.notification
def test_distributed_run_sends_failure_notification_on_orchestrator_exception(
    tmp_path: Path,
) -> None:
    config = _make_distributed_test_config(tmp_path)
    notification_config = AppriseNotificationConfig(urls=("mailto://example",))
    session = MagicMock()
    queue = MagicMock()
    session.trial_queue = queue
    session.register_or_raise.return_value = None

    trial = _make_trial(None)

    with (
        patch(
            "crsbench.distributed.runtime_session.DistributedRuntimeSession.for_run",
            return_value=session,
        ),
        patch(
            "crsbench.distributed.queue.get_existing_trials",
            return_value={"queued": {}, "started": {}, "failed": {}, "finished": {}},
        ),
        patch(
            "crsbench.distributed.queue.get_existing_trial_jobs",
            return_value={"queued": [], "started": [], "failed": [], "finished": []},
        ),
        patch("crsbench.distributed.queue.handle_orphaned_jobs", return_value=0),
        patch(
            "crsbench.distributed.registry.RuntimeRegistration.from_experiment_config"
        ),
        patch("crsbench.run_experiment.dump_trial_matrix"),
        patch("crsbench.run_experiment.monitor_jobs", side_effect=RuntimeError("boom")),
        patch(
            "crsbench.run_experiment.load_apprise_notification_config",
            return_value=notification_config,
        ),
        patch("crsbench.run_experiment.send_apprise_message") as send_mock,
    ):
        with pytest.raises(RuntimeError, match="boom"):
            run_experiment_distributed("exp-test", config, [trial], queue_mode="fresh")

    send_mock.assert_called_once()
    sent_config = send_mock.call_args.args[0]
    assert sent_config == notification_config
    body = send_mock.call_args.kwargs["body"]
    assert format_failure_message("Distributed exp-test", "boom") in body
    assert "Mode: distributed" in body
    assert "Tracked jobs: 1" in body


@pytest.mark.notification
def test_distributed_run_sends_failure_notification_when_enqueue_fails(
    tmp_path: Path,
) -> None:
    config = _make_distributed_test_config(tmp_path)
    config.max_total_time = 123
    config.model_dump.return_value = {}
    notification_config = AppriseNotificationConfig(urls=("mailto://example",))
    session = MagicMock()
    queue = MagicMock()
    session.trial_queue = queue
    session.register_or_raise.return_value = None
    session.lifecycle_store = MagicMock()
    session.registry = MagicMock()

    trial_one = _make_trial(None)
    trial_two = Trial(
        crs=trial_one.crs,
        benchmark_harness=trial_one.benchmark_harness,
        trial_num=2,
        mode=trial_one.mode,
        sanitizer=trial_one.sanitizer,
        target_cpv_id=None,
    )

    first_job = MagicMock()
    first_job.id = "job-1"
    queue.enqueue.side_effect = [first_job, RuntimeError("enqueue boom")]

    with (
        patch(
            "crsbench.distributed.runtime_session.DistributedRuntimeSession.for_run",
            return_value=session,
        ),
        patch(
            "crsbench.distributed.queue.get_existing_trials",
            return_value={"queued": {}, "started": {}, "failed": {}, "finished": {}},
        ),
        patch(
            "crsbench.distributed.queue.get_existing_trial_jobs",
            return_value={"queued": [], "started": [], "failed": [], "finished": []},
        ),
        patch("crsbench.distributed.queue.handle_orphaned_jobs", return_value=0),
        patch(
            "crsbench.distributed.registry.RuntimeRegistration.from_experiment_config"
        ),
        patch("crsbench.run_experiment.dump_trial_matrix"),
        patch("crsbench.run_experiment.get_crs_cpu_count", return_value=None),
        patch("crsbench.run_experiment.get_crs_memory", return_value=None),
        patch(
            "crsbench.run_experiment.load_apprise_notification_config",
            return_value=notification_config,
        ),
        patch("crsbench.run_experiment.send_apprise_message") as send_mock,
    ):
        with pytest.raises(RuntimeError, match="enqueue boom"):
            run_experiment_distributed(
                "exp-test",
                config,
                [trial_one, trial_two],
                queue_mode="fresh",
            )

    send_mock.assert_called_once()
    body = send_mock.call_args.kwargs["body"]
    assert format_failure_message("Distributed exp-test", "enqueue boom") in body
    assert "Tracked jobs: 1" in body


@pytest.mark.notification
def test_distributed_run_sends_failure_notification_on_auto_continue_exception(
    tmp_path: Path,
) -> None:
    config = _make_distributed_test_config(tmp_path)
    notification_config = AppriseNotificationConfig(urls=("mailto://example",))
    session = MagicMock()
    session.trial_queue = MagicMock()
    session.register_or_raise.return_value = None
    session.resume_or_raise.return_value = []

    existing_job = MagicMock()
    existing_job.id = "job-existing"
    existing = {
        "queued": {"existing": existing_job},
        "started": {},
        "failed": {},
        "finished": {},
    }
    physical_existing = {
        "queued": [existing_job],
        "started": [],
        "failed": [],
        "finished": [],
    }

    with (
        patch(
            "sys.stdin.isatty",
            return_value=False,
        ),
        patch(
            "crsbench.distributed.runtime_session.DistributedRuntimeSession.for_run",
            return_value=session,
        ),
        patch(
            "crsbench.distributed.queue.get_existing_trials",
            return_value=existing,
        ),
        patch(
            "crsbench.distributed.queue.get_existing_trial_jobs",
            return_value=physical_existing,
        ),
        patch("crsbench.distributed.queue.handle_orphaned_jobs", return_value=0),
        patch(
            "crsbench.distributed.registry.RuntimeRegistration.from_experiment_config"
        ),
        patch(
            "crsbench.run_experiment.dump_trial_matrix",
            side_effect=RuntimeError("auto continue boom"),
        ),
        patch(
            "crsbench.run_experiment.load_apprise_notification_config",
            return_value=notification_config,
        ),
        patch("crsbench.run_experiment.send_apprise_message") as send_mock,
    ):
        with pytest.raises(RuntimeError, match="auto continue boom"):
            run_experiment_distributed("exp-test", config, [], queue_mode=None)

    send_mock.assert_called_once()
    body = send_mock.call_args.kwargs["body"]
    assert format_failure_message("Distributed exp-test", "auto continue boom") in body
    assert "Tracked jobs: 1" in body


@pytest.mark.notification
def test_distributed_run_skips_notification_when_no_tracked_jobs_exist(
    tmp_path: Path,
) -> None:
    config = _make_distributed_test_config(tmp_path)
    notification_config = AppriseNotificationConfig(urls=("mailto://example",))
    session = MagicMock()
    session.trial_queue = MagicMock()

    with (
        patch(
            "crsbench.distributed.runtime_session.DistributedRuntimeSession.for_run",
            return_value=session,
        ),
        patch(
            "crsbench.distributed.queue.get_existing_trials",
            return_value={"queued": {}, "started": {}, "failed": {}, "finished": {}},
        ),
        patch(
            "crsbench.distributed.queue.get_existing_trial_jobs",
            return_value={"queued": [], "started": [], "failed": [], "finished": []},
        ),
        patch(
            "crsbench.distributed.registry.RuntimeRegistration.from_experiment_config"
        ),
        patch("crsbench.run_experiment.dump_trial_matrix"),
        patch(
            "crsbench.run_experiment.load_apprise_notification_config",
            return_value=notification_config,
        ),
        patch("crsbench.run_experiment.send_apprise_message") as send_mock,
    ):
        run_experiment_distributed("exp-test", config, [], queue_mode="fresh")

    send_mock.assert_not_called()


def test_dedupe_results_by_logical_trial_prefers_canonical_marker(
    tmp_path: Path,
) -> None:
    config = MagicMock()
    config.experiment_filestore = tmp_path
    config.experiment = "exp-test"

    trial_dir = _build_trial_output_path(
        filestore=tmp_path.resolve(),
        experiment_name="exp-test",
        crs="crs-a",
        benchmark="bench-a",
        harness="fuzz_target",
        mode="delta",
        sanitizer="address",
        trial_num=1,
        target_cpv_id=None,
    )
    trial_dir.mkdir(parents=True, exist_ok=True)
    (trial_dir / ".success").touch()

    failed = _make_result(success=False, error="stale attempt")
    succeeded = _make_result(success=True)

    deduped = _dedupe_results_by_logical_trial([failed, succeeded], config)

    assert deduped == [succeeded]


def test_generate_final_report_counts_one_logical_trial_for_duplicate_attempts(
    tmp_path: Path,
) -> None:
    config = MagicMock()
    config.experiment_filestore = tmp_path
    config.experiment = "exp-test"
    config.report_filestore = tmp_path / "reports"

    trial_dir = _build_trial_output_path(
        filestore=tmp_path.resolve(),
        experiment_name="exp-test",
        crs="crs-a",
        benchmark="bench-a",
        harness="fuzz_target",
        mode="delta",
        sanitizer="address",
        trial_num=1,
        target_cpv_id=None,
    )
    trial_dir.mkdir(parents=True, exist_ok=True)
    (trial_dir / ".success").touch()

    failed = _make_result(success=False, error="stale attempt")
    succeeded = _make_result(success=True)

    with (
        patch("crsbench.run_experiment._generate_html_json_reports"),
        patch("crsbench.run_experiment.logger") as logger_mock,
    ):
        generate_final_report([failed, succeeded], "exp-test", config)

    logger_mock.info.assert_any_call("Total trials: 1")
    logger_mock.info.assert_any_call("Successful: 1 (100.0%)")


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


def test_continue_mode_retry_failed_revives_lifecycle_before_requeue(
    tmp_path: Path,
) -> None:
    """Explicit retry must revive lifecycle before the replacement worker can claim it."""
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
    session.lifecycle_store = MagicMock()
    session.lifecycle_store.get.return_value = JobLifecycleRecord(
        job_id="job-1",
        trial_key="trial-1",
        state=JobState.FAILED,
        claimed_by="worker-old",
    )

    failed_job = MagicMock()
    failed_job.id = "job-1"
    failed_job.meta = {}
    failed_job.kwargs = {}

    existing = {
        "queued": {},
        "started": {},
        "failed": {"f": failed_job},
        "finished": {},
        "deferred": {},
        "scheduled": {},
    }
    physical_existing = {
        "queued": [],
        "started": [],
        "failed": [failed_job],
        "finished": [],
        "deferred": [],
        "scheduled": [],
    }

    call_order: list[str] = []

    def _transition(*args, **kwargs):
        del args, kwargs
        call_order.append("transition")

    def _enqueue(job):
        assert job is failed_job
        call_order.append("enqueue")

    session.lifecycle_store.transition.side_effect = _transition
    queue.enqueue_job.side_effect = _enqueue

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
            "crsbench.distributed.queue.get_existing_trial_jobs",
            return_value=physical_existing,
        ),
        patch("crsbench.distributed.queue.handle_orphaned_jobs", return_value=0),
        patch(
            "crsbench.run_experiment._prepare_trial_dir_for_retry", return_value=True
        ),
        patch(
            "crsbench.distributed.registry.RuntimeRegistration.from_experiment_config"
        ),
        patch(
            "crsbench.run_experiment.dump_trial_matrix",
            side_effect=RuntimeError("stop after queue handling"),
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

    assert call_order == ["transition", "enqueue"]
    session.lifecycle_store.transition.assert_called_once_with(
        "exp-test",
        "job-1",
        JobState.QUEUED,
        claimed_by=None,
        detail="retry requested by orchestrator",
    )


def test_continue_mode_retry_failed_rolls_back_lifecycle_when_requeue_fails(
    tmp_path: Path,
) -> None:
    """Explicit retry must roll lifecycle back to failed when enqueue fails."""
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
    session.lifecycle_store = MagicMock()
    session.lifecycle_store.get.return_value = JobLifecycleRecord(
        job_id="job-1",
        trial_key="trial-1",
        state=JobState.FAILED,
        claimed_by=None,
    )

    failed_job = MagicMock()
    failed_job.id = "job-1"
    failed_job.meta = {}
    failed_job.kwargs = {}

    existing = {
        "queued": {},
        "started": {},
        "failed": {"f": failed_job},
        "finished": {},
        "deferred": {},
        "scheduled": {},
    }
    physical_existing = {
        "queued": [],
        "started": [],
        "failed": [failed_job],
        "finished": [],
        "deferred": [],
        "scheduled": [],
    }

    queue.enqueue_job.side_effect = RuntimeError("redis unavailable")

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
            "crsbench.distributed.queue.get_existing_trial_jobs",
            return_value=physical_existing,
        ),
        patch("crsbench.distributed.queue.handle_orphaned_jobs", return_value=0),
        patch(
            "crsbench.run_experiment._prepare_trial_dir_for_retry", return_value=True
        ),
        patch(
            "crsbench.distributed.registry.RuntimeRegistration.from_experiment_config"
        ),
        patch(
            "crsbench.run_experiment.dump_trial_matrix",
            side_effect=RuntimeError("stop after queue handling"),
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

    assert session.lifecycle_store.transition.call_args_list == [
        call(
            "exp-test",
            "job-1",
            JobState.QUEUED,
            claimed_by=None,
            detail="retry requested by orchestrator",
        ),
        call(
            "exp-test",
            "job-1",
            JobState.FAILED,
            claimed_by=None,
            detail="retry enqueue failed: redis unavailable",
        ),
    ]


def test_continue_mode_retry_failed_rolls_back_lifecycle_when_save_meta_fails(
    tmp_path: Path,
) -> None:
    """Explicit retry must roll lifecycle back when metadata write fails before enqueue."""
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
    session.lifecycle_store = MagicMock()
    session.lifecycle_store.get.return_value = JobLifecycleRecord(
        job_id="job-1",
        trial_key="trial-1",
        state=JobState.FAILED,
        claimed_by=None,
    )

    failed_job = MagicMock()
    failed_job.id = "job-1"
    failed_job.meta = {}
    failed_job.kwargs = {}
    failed_job.save_meta.side_effect = RuntimeError("meta write failed")

    existing = {
        "queued": {},
        "started": {},
        "failed": {"f": failed_job},
        "finished": {},
        "deferred": {},
        "scheduled": {},
    }
    physical_existing = {
        "queued": [],
        "started": [],
        "failed": [failed_job],
        "finished": [],
        "deferred": [],
        "scheduled": [],
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
            "crsbench.distributed.queue.get_existing_trial_jobs",
            return_value=physical_existing,
        ),
        patch("crsbench.distributed.queue.handle_orphaned_jobs", return_value=0),
        patch(
            "crsbench.run_experiment._prepare_trial_dir_for_retry", return_value=True
        ),
        patch(
            "crsbench.distributed.registry.RuntimeRegistration.from_experiment_config"
        ),
        patch(
            "crsbench.run_experiment.dump_trial_matrix",
            side_effect=RuntimeError("stop after queue handling"),
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

    queue.enqueue_job.assert_not_called()
    assert session.lifecycle_store.transition.call_args_list == [
        call(
            "exp-test",
            "job-1",
            JobState.QUEUED,
            claimed_by=None,
            detail="retry requested by orchestrator",
        ),
        call(
            "exp-test",
            "job-1",
            JobState.FAILED,
            claimed_by=None,
            detail="retry metadata update failed: meta write failed",
        ),
    ]


def test_continue_mode_retry_failed_skips_when_lifecycle_is_already_completed(
    tmp_path: Path,
) -> None:
    """Explicit retry must not resurrect a failed RQ job behind a completed shadow record."""
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
    session.lifecycle_store = MagicMock()
    session.lifecycle_store.get.return_value = JobLifecycleRecord(
        job_id="job-1",
        trial_key="trial-1",
        state=JobState.COMPLETED,
        claimed_by=None,
    )

    failed_job = MagicMock()
    failed_job.id = "job-1"
    failed_job.meta = {}
    failed_job.kwargs = {}

    existing = {
        "queued": {},
        "started": {},
        "failed": {"f": failed_job},
        "finished": {},
        "deferred": {},
        "scheduled": {},
    }
    physical_existing = {
        "queued": [],
        "started": [],
        "failed": [failed_job],
        "finished": [],
        "deferred": [],
        "scheduled": [],
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
            "crsbench.distributed.queue.get_existing_trial_jobs",
            return_value=physical_existing,
        ),
        patch("crsbench.distributed.queue.handle_orphaned_jobs", return_value=0),
        patch(
            "crsbench.run_experiment._prepare_trial_dir_for_retry", return_value=True
        ),
        patch(
            "crsbench.distributed.registry.RuntimeRegistration.from_experiment_config"
        ),
        patch(
            "crsbench.run_experiment.dump_trial_matrix",
            side_effect=RuntimeError("stop after queue handling"),
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

    queue.enqueue_job.assert_not_called()
    failed_job.save_meta.assert_not_called()
    session.lifecycle_store.transition.assert_not_called()


def test_continue_mode_monitors_existing_finished_jobs_without_reenqueue(
    tmp_path: Path,
) -> None:
    """Restarted continue mode must collect pre-existing terminal jobs."""
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
    session.registry = MagicMock()

    finished_job = MagicMock()
    finished_job.id = "job-finished"
    finished_job.kwargs = {"trial_num": 1}

    existing = {
        "queued": {},
        "started": {},
        "failed": {},
        "finished": {"k": finished_job},
        "deferred": {},
        "scheduled": {},
    }
    physical_existing = {
        "queued": [],
        "started": [],
        "failed": [],
        "finished": [finished_job],
        "deferred": [],
        "scheduled": [],
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
            "crsbench.distributed.queue.get_existing_trial_jobs",
            return_value=physical_existing,
        ),
        patch("crsbench.distributed.queue.handle_orphaned_jobs", return_value=0),
        patch("crsbench.run_experiment.dump_trial_matrix"),
        patch(
            "crsbench.distributed.registry.RuntimeRegistration.from_experiment_config",
            return_value=MagicMock(),
        ),
        patch(
            "crsbench.run_experiment.monitor_jobs",
            side_effect=RuntimeError("stop after monitor"),
        ) as monitor_jobs_mock,
    ):
        with pytest.raises(RuntimeError, match="stop after monitor"):
            run_experiment_distributed(
                "exp-test",
                config,
                [],
                queue_mode="continue",
                retry_failed=False,
            )

    queue.enqueue.assert_not_called()
    monitor_jobs_mock.assert_called_once()
    assert monitor_jobs_mock.call_args.args[1] == [finished_job]


def test_continue_mode_monitors_existing_terminal_jobs_alongside_new_enqueues(
    tmp_path: Path,
) -> None:
    """Continue mode should attach carryover terminal jobs to the monitored set."""
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
    session.registry = MagicMock()

    finished_job = MagicMock()
    finished_job.id = "job-finished"
    finished_job.kwargs = {"trial_num": 1}

    new_job = MagicMock()
    new_job.id = "job-new"
    queue.enqueue.return_value = new_job

    finished_trial = _make_trial(None)
    new_trial = Trial(
        crs="crs-a",
        benchmark_harness=finished_trial.benchmark_harness,
        trial_num=2,
        mode="delta",
        sanitizer="address",
        target_cpv_id=None,
    )

    existing = {
        "queued": {},
        "started": {},
        "failed": {},
        "finished": {
            "crs-a:bench-a:fuzz_target:delta:address:1:-": finished_job,
        },
        "deferred": {},
        "scheduled": {},
    }
    physical_existing = {
        "queued": [],
        "started": [],
        "failed": [],
        "finished": [finished_job],
        "deferred": [],
        "scheduled": [],
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
            "crsbench.distributed.queue.get_existing_trial_jobs",
            return_value=physical_existing,
        ),
        patch("crsbench.distributed.queue.handle_orphaned_jobs", return_value=0),
        patch("crsbench.run_experiment.dump_trial_matrix"),
        patch(
            "crsbench.distributed.registry.RuntimeRegistration.from_experiment_config",
            return_value=MagicMock(),
        ),
        patch(
            "crsbench.run_experiment.monitor_jobs",
            side_effect=RuntimeError("stop after monitor"),
        ) as monitor_jobs_mock,
    ):
        with pytest.raises(RuntimeError, match="stop after monitor"):
            run_experiment_distributed(
                "exp-test",
                config,
                [finished_trial, new_trial],
                queue_mode="continue",
                retry_failed=False,
            )

    queue.enqueue.assert_called_once()
    monitor_jobs_mock.assert_called_once()
    assert monitor_jobs_mock.call_args.args[1] == [finished_job, new_job]


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


@pytest.mark.notification
def test_queue_mode_quit_cleanup_failure_notifies_and_raises(
    tmp_path: Path,
) -> None:
    """Cleanup failures must still surface even when the run returns early."""
    config = MagicMock()
    config.redis_host = "localhost"
    config.resources = None
    config.keep_only_results = True
    config.experiment_filestore = tmp_path
    config.experiment = "exp-test"

    session = MagicMock()
    session.trial_queue = MagicMock()
    session.cleanup.side_effect = RuntimeError("cleanup boom")
    existing_job = MagicMock()
    existing_job.id = "job-1"
    physical_existing = {
        "queued": [existing_job],
        "started": [],
        "failed": [],
        "finished": [],
    }

    with (
        patch(
            "crsbench.distributed.runtime_session.DistributedRuntimeSession.for_run",
            return_value=session,
        ),
        patch(
            "crsbench.distributed.queue.get_existing_trials",
            return_value={
                "queued": {"k": existing_job},
                "started": {},
                "failed": {},
                "finished": {},
            },
        ),
        patch(
            "crsbench.distributed.queue.get_existing_trial_jobs",
            return_value=physical_existing,
        ),
        patch(
            "crsbench.distributed.registry.RuntimeRegistration.from_experiment_config"
        ),
        patch("crsbench.run_experiment._cleanup_experiment_artifacts") as cleanup_mock,
        patch(
            "crsbench.run_experiment.load_apprise_notification_config",
            return_value=AppriseNotificationConfig(urls=("mailto://example",)),
        ),
        patch("crsbench.run_experiment.send_apprise_message") as send_mock,
    ):
        with pytest.raises(RuntimeError, match="cleanup boom"):
            run_experiment_distributed("exp-test", config, [], queue_mode="quit")

    send_mock.assert_called_once()
    cleanup_mock.assert_not_called()
    body = send_mock.call_args.kwargs["body"]
    assert format_failure_message("Distributed exp-test", "cleanup boom") in body
    assert "Tracked jobs: 1" in body


@pytest.mark.notification
def test_interactive_queue_mode_quit_cleanup_failure_notifies_and_raises(
    tmp_path: Path,
) -> None:
    """Interactive abort must still preserve existing tracked jobs for cleanup failures."""
    config = MagicMock()
    config.redis_host = "localhost"
    config.resources = None
    config.keep_only_results = True
    config.experiment_filestore = tmp_path
    config.experiment = "exp-test"

    session = MagicMock()
    session.trial_queue = MagicMock()
    session.cleanup.side_effect = RuntimeError("cleanup boom")
    existing_job = MagicMock()
    existing_job.id = "job-1"
    physical_existing = {
        "queued": [existing_job],
        "started": [],
        "failed": [],
        "finished": [],
    }

    with (
        patch(
            "sys.stdin.isatty",
            return_value=True,
        ),
        patch(
            "crsbench.distributed.runtime_session.DistributedRuntimeSession.for_run",
            return_value=session,
        ),
        patch(
            "crsbench.distributed.queue.get_existing_trials",
            return_value={
                "queued": {"k": existing_job},
                "started": {},
                "failed": {},
                "finished": {},
            },
        ),
        patch(
            "crsbench.distributed.queue.get_existing_trial_jobs",
            return_value=physical_existing,
        ),
        patch("crsbench.run_experiment.prompt_queue_mode", return_value="quit"),
        patch(
            "crsbench.distributed.registry.RuntimeRegistration.from_experiment_config"
        ),
        patch("crsbench.run_experiment._cleanup_experiment_artifacts") as cleanup_mock,
        patch(
            "crsbench.run_experiment.load_apprise_notification_config",
            return_value=AppriseNotificationConfig(urls=("mailto://example",)),
        ),
        patch("crsbench.run_experiment.send_apprise_message") as send_mock,
    ):
        with pytest.raises(RuntimeError, match="cleanup boom"):
            run_experiment_distributed("exp-test", config, [], queue_mode=None)

    send_mock.assert_called_once()
    cleanup_mock.assert_not_called()
    body = send_mock.call_args.kwargs["body"]
    assert format_failure_message("Distributed exp-test", "cleanup boom") in body
    assert "Tracked jobs: 1" in body


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
    session.resume_or_raise.side_effect = LockContentionError("still busy")

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
    registration = MagicMock(experiment="exp-test")

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
            "crsbench.distributed.queue.handle_orphaned_jobs", return_value=0
        ) as handle_orphaned,
        patch(
            "crsbench.run_experiment._prepare_trial_dir_for_retry", return_value=True
        ),
        patch(
            "crsbench.distributed.registry.RuntimeRegistration.from_experiment_config",
            return_value=registration,
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
    session.resume_or_raise.assert_called_once()
    assert session.resume_or_raise.call_args.kwargs["registration"] is registration


@pytest.mark.notification
def test_continue_mode_lock_contention_cleanup_failure_notifies(
    tmp_path: Path,
) -> None:
    """Explicit continue aborts must still preserve tracked jobs for cleanup failures."""
    config = MagicMock()
    config.redis_host = "localhost"
    config.resources = None
    config.keep_only_results = True
    config.experiment_filestore = tmp_path
    config.experiment = "exp-test"

    session = MagicMock()
    session.trial_queue = MagicMock()
    session.register_or_raise.side_effect = LockContentionError("busy")
    session.resume_or_raise.side_effect = LockContentionError("still busy")
    session.cleanup.side_effect = RuntimeError("cleanup boom")

    existing_job = MagicMock()
    existing_job.id = "job-1"
    physical_existing = {
        "queued": [existing_job],
        "started": [],
        "failed": [],
        "finished": [],
    }

    with (
        patch(
            "crsbench.distributed.runtime_session.DistributedRuntimeSession.for_run",
            return_value=session,
        ),
        patch(
            "crsbench.distributed.queue.get_existing_trials",
            return_value={
                "queued": {"k": existing_job},
                "started": {},
                "failed": {},
                "finished": {},
            },
        ),
        patch(
            "crsbench.distributed.queue.get_existing_trial_jobs",
            return_value=physical_existing,
        ),
        patch(
            "crsbench.distributed.queue.handle_orphaned_jobs", return_value=0
        ) as handle_orphaned,
        patch(
            "crsbench.distributed.registry.RuntimeRegistration.from_experiment_config"
        ),
        patch("crsbench.run_experiment._cleanup_experiment_artifacts") as cleanup_mock,
        patch(
            "crsbench.run_experiment.load_apprise_notification_config",
            return_value=AppriseNotificationConfig(urls=("mailto://example",)),
        ),
        patch("crsbench.run_experiment.send_apprise_message") as send_mock,
    ):
        with pytest.raises(RuntimeError, match="cleanup boom"):
            run_experiment_distributed(
                "exp-test",
                config,
                [],
                queue_mode="continue",
                retry_failed=True,
            )

    handle_orphaned.assert_not_called()
    cleanup_mock.assert_not_called()
    send_mock.assert_called_once()
    body = send_mock.call_args.kwargs["body"]
    assert format_failure_message("Distributed exp-test", "cleanup boom") in body
    assert "Tracked jobs: 1" in body


@pytest.mark.notification
def test_interactive_continue_lock_contention_cleanup_failure_notifies(
    tmp_path: Path,
) -> None:
    """Interactive continue must preserve tracked jobs for cleanup failures."""
    config = MagicMock()
    config.redis_host = "localhost"
    config.resources = None
    config.keep_only_results = True
    config.experiment_filestore = tmp_path
    config.experiment = "exp-test"

    session = MagicMock()
    session.trial_queue = MagicMock()
    session.register_or_raise.side_effect = LockContentionError("busy")
    session.resume_or_raise.side_effect = LockContentionError("still busy")
    session.cleanup.side_effect = RuntimeError("cleanup boom")

    existing_job = MagicMock()
    existing_job.id = "job-1"
    physical_existing = {
        "queued": [existing_job],
        "started": [],
        "failed": [],
        "finished": [],
    }

    with (
        patch("sys.stdin.isatty", return_value=True),
        patch(
            "crsbench.distributed.runtime_session.DistributedRuntimeSession.for_run",
            return_value=session,
        ),
        patch(
            "crsbench.distributed.queue.get_existing_trials",
            return_value={
                "queued": {"k": existing_job},
                "started": {},
                "failed": {},
                "finished": {},
            },
        ),
        patch(
            "crsbench.distributed.queue.get_existing_trial_jobs",
            return_value=physical_existing,
        ),
        patch("crsbench.run_experiment.prompt_queue_mode", return_value="continue"),
        patch(
            "crsbench.distributed.queue.handle_orphaned_jobs", return_value=0
        ) as handle_orphaned,
        patch(
            "crsbench.distributed.registry.RuntimeRegistration.from_experiment_config"
        ),
        patch("crsbench.run_experiment._cleanup_experiment_artifacts") as cleanup_mock,
        patch(
            "crsbench.run_experiment.load_apprise_notification_config",
            return_value=AppriseNotificationConfig(urls=("mailto://example",)),
        ),
        patch("crsbench.run_experiment.send_apprise_message") as send_mock,
    ):
        with pytest.raises(RuntimeError, match="cleanup boom"):
            run_experiment_distributed("exp-test", config, [], queue_mode=None)

    handle_orphaned.assert_not_called()
    cleanup_mock.assert_not_called()
    send_mock.assert_called_once()
    body = send_mock.call_args.kwargs["body"]
    assert format_failure_message("Distributed exp-test", "cleanup boom") in body
    assert "Tracked jobs: 1" in body


def test_continue_mode_reclaims_stale_lock_and_continues_recovery(
    tmp_path: Path,
) -> None:
    """continue mode should use the stale-lock resume path before mutating queue state."""
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
    session.resume_or_raise.return_value = []

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
    registration = MagicMock(experiment="exp-test")

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
            "crsbench.distributed.queue.handle_orphaned_jobs", return_value=0
        ) as handle_orphaned,
        patch(
            "crsbench.run_experiment._prepare_trial_dir_for_retry", return_value=True
        ),
        patch(
            "crsbench.distributed.registry.RuntimeRegistration.from_experiment_config",
            return_value=registration,
        ),
        patch(
            "crsbench.run_experiment.dump_trial_matrix",
            side_effect=RuntimeError("stop after queue handling"),
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

    session.resume_or_raise.assert_called_once()
    assert session.resume_or_raise.call_args.kwargs["registration"] is registration
    handle_orphaned.assert_called_once()
    queue.enqueue_job.assert_called_once_with(failed_job)


def test_continue_mode_monitors_resume_collection_jobs_before_early_exit(
    tmp_path: Path,
) -> None:
    """Resume reconciliation jobs must be attached even with no visible queue residue."""
    config = MagicMock()
    config.redis_host = "localhost"
    config.resources = None
    config.keep_only_results = False
    config.experiment_filestore = tmp_path
    config.experiment = "exp-test"

    session = MagicMock()
    queue = MagicMock()
    queue.connection = MagicMock()
    session.trial_queue = queue
    session.register_or_raise.side_effect = LockContentionError("busy")
    session.resume_or_raise.return_value = ["job-syncing"]
    session.registry = MagicMock()

    syncing_job = MagicMock()
    syncing_job.id = "job-syncing"

    existing = {
        "queued": {},
        "started": {},
        "failed": {},
        "finished": {},
        "deferred": {},
        "scheduled": {},
    }
    physical_existing = {
        "queued": [],
        "started": [],
        "failed": [],
        "finished": [],
        "deferred": [],
        "scheduled": [],
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
            "crsbench.distributed.queue.get_existing_trial_jobs",
            return_value=physical_existing,
        ),
        patch("crsbench.distributed.queue.handle_orphaned_jobs", return_value=0),
        patch("crsbench.run_experiment.dump_trial_matrix"),
        patch(
            "crsbench.distributed.registry.RuntimeRegistration.from_experiment_config",
            return_value=MagicMock(),
        ),
        patch(
            "crsbench.run_experiment.monitor_jobs",
            side_effect=RuntimeError("stop after monitor"),
        ) as monitor_jobs_mock,
        patch(
            "crsbench.run_experiment._fetch_jobs_by_id",
            return_value=[syncing_job],
        ) as fetch_job,
    ):
        with pytest.raises(RuntimeError, match="stop after monitor"):
            run_experiment_distributed(
                "exp-test",
                config,
                [],
                queue_mode="continue",
                retry_failed=False,
            )

    fetch_job.assert_called_once_with(
        queue,
        ["job-syncing"],
        strict_missing=True,
    )
    monitor_jobs_mock.assert_called_once()
    assert monitor_jobs_mock.call_args.args[1] == [syncing_job]


def test_continue_mode_reconciles_resume_state_after_fresh_lock_acquisition(
    tmp_path: Path,
) -> None:
    """Continue mode must reconcile syncing/artifact state even if the old lock already expired."""
    config = MagicMock()
    config.redis_host = "localhost"
    config.resources = None
    config.keep_only_results = False
    config.experiment_filestore = tmp_path
    config.experiment = "exp-test"

    session = MagicMock()
    queue = MagicMock()
    queue.connection = MagicMock()
    session.trial_queue = queue
    session.register_or_raise.return_value = None
    session.resume_or_raise.return_value = ["job-syncing"]
    session.registry = MagicMock()

    syncing_job = MagicMock()
    syncing_job.id = "job-syncing"

    existing = {
        "queued": {},
        "started": {},
        "failed": {},
        "finished": {},
        "deferred": {},
        "scheduled": {},
    }
    physical_existing = {
        "queued": [],
        "started": [],
        "failed": [],
        "finished": [],
        "deferred": [],
        "scheduled": [],
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
            "crsbench.distributed.queue.get_existing_trial_jobs",
            return_value=physical_existing,
        ),
        patch("crsbench.distributed.queue.handle_orphaned_jobs", return_value=0),
        patch("crsbench.run_experiment.dump_trial_matrix"),
        patch(
            "crsbench.distributed.registry.RuntimeRegistration.from_experiment_config",
            return_value=MagicMock(),
        ),
        patch(
            "crsbench.run_experiment.monitor_jobs",
            side_effect=RuntimeError("stop after monitor"),
        ) as monitor_jobs_mock,
        patch(
            "crsbench.run_experiment._fetch_jobs_by_id",
            return_value=[syncing_job],
        ) as fetch_job,
    ):
        with pytest.raises(RuntimeError, match="stop after monitor"):
            run_experiment_distributed(
                "exp-test",
                config,
                [],
                queue_mode="continue",
                retry_failed=False,
            )

    session.resume_or_raise.assert_called_once()
    fetch_job.assert_called_once_with(
        queue,
        ["job-syncing"],
        strict_missing=True,
    )
    monitor_jobs_mock.assert_called_once()
    assert monitor_jobs_mock.call_args.args[1] == [syncing_job]


def test_continue_mode_skips_resume_collection_job_when_active_retry_exists(
    tmp_path: Path,
) -> None:
    """Resume-only syncing work must not stay tracked when active work for the same trial exists."""
    config = MagicMock()
    config.redis_host = "localhost"
    config.resources = None
    config.keep_only_results = False
    config.experiment_filestore = tmp_path
    config.experiment = "exp-test"

    session = MagicMock()
    queue = MagicMock()
    queue.connection = MagicMock()
    session.trial_queue = queue
    session.register_or_raise.side_effect = LockContentionError("busy")
    session.resume_or_raise.return_value = ["job-syncing"]
    session.registry = MagicMock()

    queued_job = MagicMock()
    queued_job.id = "job-queued"
    queued_job.meta = {
        "experiment_name": "exp-test",
        "crs": "crs-a",
        "benchmark": "bench-a",
        "harness": "fuzz_target",
        "mode": "delta",
        "sanitizer": "address",
        "trial_num": 1,
        "target_cpv_id": None,
    }
    queued_job.kwargs = {
        "crs": "crs-a",
        "benchmark": "bench-a",
        "harness_name": "fuzz_target",
        "mode": "delta",
        "sanitizer": "address",
        "trial_num": 1,
        "target_cpv_id": None,
    }

    syncing_job = MagicMock()
    syncing_job.id = "job-syncing"
    syncing_job.kwargs = dict(queued_job.kwargs)
    syncing_job.meta = dict(queued_job.meta)

    existing = {
        "queued": {"trial-1": queued_job},
        "started": {},
        "failed": {},
        "finished": {},
        "deferred": {},
        "scheduled": {},
    }
    physical_existing = {
        "queued": [queued_job],
        "started": [],
        "failed": [],
        "finished": [],
        "deferred": [],
        "scheduled": [],
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
            "crsbench.distributed.queue.get_existing_trial_jobs",
            return_value=physical_existing,
        ),
        patch("crsbench.distributed.queue.handle_orphaned_jobs", return_value=0),
        patch("crsbench.run_experiment.dump_trial_matrix"),
        patch(
            "crsbench.distributed.registry.RuntimeRegistration.from_experiment_config",
            return_value=MagicMock(),
        ),
        patch(
            "crsbench.run_experiment.monitor_jobs",
            side_effect=RuntimeError("stop after monitor"),
        ) as monitor_jobs_mock,
        patch(
            "crsbench.run_experiment._fetch_jobs_by_id",
            return_value=[syncing_job],
        ),
    ):
        with pytest.raises(RuntimeError, match="stop after monitor"):
            run_experiment_distributed(
                "exp-test",
                config,
                [],
                queue_mode="continue",
                retry_failed=False,
            )

    monitor_jobs_mock.assert_called_once()
    assert monitor_jobs_mock.call_args.args[1] == [queued_job]


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


def test_queue_mode_fresh_preserves_lifecycle_when_started_job_was_purged(
    tmp_path: Path,
) -> None:
    config = MagicMock()
    config.redis_host = "localhost"
    config.resources = None
    config.keep_only_results = False
    config.experiment_filestore = tmp_path
    config.experiment = "exp-test"

    queue = MagicMock()
    session = MagicMock()
    session.trial_queue = queue
    session.lifecycle_store = MagicMock()
    live_started_job = MagicMock()
    live_started_job.get_status.return_value = "started"
    live_started_job.started_at = datetime.now(timezone.utc)
    live_started_job.timeout = 600

    existing = {
        "queued": {},
        "started": {"k": MagicMock()},
        "failed": {},
        "finished": {},
    }
    physical_existing_before = {
        "queued": [],
        "started": [live_started_job],
        "failed": [],
        "finished": [],
        "deferred": [],
        "scheduled": [],
    }
    physical_existing_after = {
        "queued": [],
        "started": [],
        "failed": [],
        "finished": [],
        "deferred": [],
        "scheduled": [],
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
            "crsbench.distributed.queue.get_existing_trial_jobs",
            side_effect=[physical_existing_before, physical_existing_after],
        ),
        patch("crsbench.distributed.queue.clear_experiment_jobs") as clear_jobs,
        patch(
            "crsbench.distributed.registry.RuntimeRegistration.from_experiment_config"
        ),
        patch("crsbench.run_experiment.dump_trial_matrix"),
        patch("crsbench.run_experiment.monitor_jobs", return_value=[]),
    ):
        run_experiment_distributed("exp-test", config, [], queue_mode="fresh")

    clear_jobs.assert_called_once_with(queue, "exp-test")
    session.lifecycle_store.clear_experiment.assert_not_called()


def test_queue_mode_fresh_lifecycle_clear_is_best_effort(tmp_path: Path) -> None:
    config = MagicMock()
    config.redis_host = "localhost"
    config.resources = None
    config.keep_only_results = False
    config.experiment_filestore = tmp_path
    config.experiment = "exp-test"

    queue = MagicMock()
    session = MagicMock()
    session.trial_queue = queue
    session.lifecycle_store = MagicMock()
    session.lifecycle_store.clear_experiment.side_effect = OSError("redis down")

    existing = {
        "queued": {"k": MagicMock()},
        "started": {},
        "failed": {},
        "finished": {},
    }
    physical_existing = {
        "queued": [MagicMock()],
        "started": [],
        "failed": [],
        "finished": [],
        "deferred": [],
        "scheduled": [],
    }
    remaining_after_purge = {
        "queued": [],
        "started": [],
        "failed": [],
        "finished": [],
        "deferred": [],
        "scheduled": [],
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
            "crsbench.distributed.queue.get_existing_trial_jobs",
            side_effect=[physical_existing, remaining_after_purge],
        ),
        patch("crsbench.distributed.queue.clear_experiment_jobs"),
        patch(
            "crsbench.distributed.registry.RuntimeRegistration.from_experiment_config"
        ),
        patch("crsbench.run_experiment.dump_trial_matrix"),
        patch("crsbench.run_experiment.monitor_jobs", return_value=[]),
    ):
        run_experiment_distributed("exp-test", config, [], queue_mode="fresh")

    session.lifecycle_store.clear_experiment.assert_called_once_with("exp-test")


def test_queue_mode_fresh_clears_lifecycle_for_stale_started_job(
    tmp_path: Path,
) -> None:
    config = MagicMock()
    config.redis_host = "localhost"
    config.resources = None
    config.keep_only_results = False
    config.experiment_filestore = tmp_path
    config.experiment = "exp-test"

    queue = MagicMock()
    session = MagicMock()
    session.trial_queue = queue
    session.lifecycle_store = MagicMock()
    stale_started_job = MagicMock()
    stale_started_job.get_status.return_value = "started"
    stale_started_job.started_at = datetime.now(timezone.utc) - timedelta(minutes=15)
    stale_started_job.timeout = 60

    existing = {
        "queued": {},
        "started": {"k": MagicMock()},
        "failed": {},
        "finished": {},
    }
    physical_existing_before = {
        "queued": [],
        "started": [stale_started_job],
        "failed": [],
        "finished": [],
        "deferred": [],
        "scheduled": [],
    }
    physical_existing_after = {
        "queued": [],
        "started": [],
        "failed": [],
        "finished": [],
        "deferred": [],
        "scheduled": [],
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
            "crsbench.distributed.queue.get_existing_trial_jobs",
            side_effect=[physical_existing_before, physical_existing_after],
        ),
        patch("crsbench.distributed.queue.clear_experiment_jobs"),
        patch(
            "crsbench.distributed.registry.RuntimeRegistration.from_experiment_config"
        ),
        patch("crsbench.run_experiment.dump_trial_matrix"),
        patch("crsbench.run_experiment.monitor_jobs", return_value=[]),
    ):
        run_experiment_distributed("exp-test", config, [], queue_mode="fresh")

    session.lifecycle_store.clear_experiment.assert_called_once_with("exp-test")


def test_queue_mode_fresh_clears_lifecycle_for_non_started_registry_residue(
    tmp_path: Path,
) -> None:
    config = MagicMock()
    config.redis_host = "localhost"
    config.resources = None
    config.keep_only_results = False
    config.experiment_filestore = tmp_path
    config.experiment = "exp-test"

    queue = MagicMock()
    session = MagicMock()
    session.trial_queue = queue
    session.lifecycle_store = MagicMock()
    finished_registry_job = MagicMock()
    finished_registry_job.get_status.return_value = "finished"

    existing = {
        "queued": {},
        "started": {"k": MagicMock()},
        "failed": {},
        "finished": {},
    }
    physical_existing_before = {
        "queued": [],
        "started": [finished_registry_job],
        "failed": [],
        "finished": [],
        "deferred": [],
        "scheduled": [],
    }
    physical_existing_after = {
        "queued": [],
        "started": [],
        "failed": [],
        "finished": [],
        "deferred": [],
        "scheduled": [],
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
            "crsbench.distributed.queue.get_existing_trial_jobs",
            side_effect=[physical_existing_before, physical_existing_after],
        ),
        patch("crsbench.distributed.queue.clear_experiment_jobs"),
        patch(
            "crsbench.distributed.registry.RuntimeRegistration.from_experiment_config"
        ),
        patch("crsbench.run_experiment.dump_trial_matrix"),
        patch("crsbench.run_experiment.monitor_jobs", return_value=[]),
    ):
        run_experiment_distributed("exp-test", config, [], queue_mode="fresh")

    session.lifecycle_store.clear_experiment.assert_called_once_with("exp-test")


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
                "defaults": {
                    "readiness_timeout_sec": 1200,
                    "crsbench_install_spec": "git+ssh://git@github.com/sslab-gatech/CRSBench.git",
                    "crsbench_git_ref": "main",
                },
                "providers": {
                    "gce": {
                        "project": "test-project",
                        "profile_defaults": {
                            "machine_type": "n2d-standard-16",
                            "boot_disk_size_gb": 50,
                            "image": "projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64",
                            "service_account_email": "crsbench@test-project.iam.gserviceaccount.com",
                            "owner_label": "team-crs",
                        },
                        "instance_profiles": {
                            "gce-orchestrator-n2d": {},
                            "gce-worker-n2d": {},
                        },
                    }
                },
                "orchestrator": {
                    "zone": "us-east5-b",
                    "instance_profile": "gce-orchestrator-n2d",
                },
                "workers": {
                    "defaults": {
                        "instance_profile": "gce-worker-n2d",
                        "count": 1,
                    },
                    "placements": [
                        {
                            "zone": "us-east5-b",
                            "count": 2,
                        },
                        {
                            "zone": "us-east1-b",
                        },
                    ],
                },
            },
            "crs_compose": {"crs-a": {"num_cores": 1}},
        }
    )


def _add_secret_refs_to_provider_neutral_run_config(
    config: ExperimentConfig,
    *,
    deploy_key_ref: str = ".crsbench-keys/crsbench-deploy",
    hf_token_ref: str = "os.environ/HF_TOKEN",
) -> ExperimentConfig:
    config = config.model_copy(deep=True)
    assert config.cloud is not None
    assert config.cloud.defaults is not None
    config.cloud.defaults.github_deploy_key_path = deploy_key_ref
    if config.cloud.env is None:
        config.cloud.env = {}
    config.cloud.env["HF_TOKEN"] = hf_token_ref
    return config


def _with_evaluator_placements(config: ExperimentConfig) -> ExperimentConfig:
    raw_config = config.model_dump(mode="json", exclude_none=True)
    raw_config["cloud"]["providers"]["gce"]["instance_profiles"][
        "gce-evaluator-n2d"
    ] = raw_config["cloud"]["providers"]["gce"]["instance_profiles"]["gce-worker-n2d"]
    raw_config["cloud"]["evaluators"] = {
        "defaults": {
            "instance_profile": "gce-evaluator-n2d",
            "count": 1,
        },
        "placements": [
            {
                "zone": "us-east5-b",
            },
            {
                "zone": "us-east1-b",
                "count": 2,
            },
        ],
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


def test_build_trial_queue_job_id_is_deterministic_and_suffix_free() -> None:
    trial = _make_trial("cpv/slash")

    assert build_trial_queue_job_id("exp", trial) == build_trial_id("exp", trial, "")


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


def test_monitor_jobs_basic_delegates_to_shared_monitor() -> None:
    queue = MagicMock()
    config = MagicMock()
    job = MagicMock()
    job.id = "job-12345678"
    job.is_finished = True
    job.is_failed = False
    job.result = MagicMock(success=True)
    job.kwargs = {}
    job.meta = {}
    job.get_status.return_value = "finished"

    with (
        patch(
            "crsbench.run_experiment._get_experiment_queue_stats",
            return_value={
                "queued": 0,
                "started": 0,
                "finished": 1,
                "failed": 0,
                "workers": 1,
            },
        ),
        patch("crsbench.run_experiment.monitor_queue", create=True) as shared_monitor,
    ):
        results = _monitor_jobs_basic(
            queue=queue,
            job_list=[job],
            experiment_name="exp-test",
            config=config,
        )

    shared_monitor.assert_called_once()
    assert results == [job.result]


def test_monitor_jobs_uses_basic_renderer_when_stdout_is_not_tty() -> None:
    queue = MagicMock()
    config = MagicMock()
    job = MagicMock()

    with (
        patch(
            "crsbench.run_experiment.importlib.util.find_spec", return_value=object()
        ),
        patch("sys.stdout.isatty", return_value=False),
        patch("crsbench.run_experiment._monitor_jobs_basic", return_value=[]) as basic,
        patch("crsbench.run_experiment._monitor_jobs_rich", return_value=[]) as rich,
    ):
        monitor_jobs(queue, [job], "exp-test", config)

    basic.assert_called_once_with(
        queue,
        [job],
        "exp-test",
        config,
        disk_skipped=0,
        registry=None,
        lifecycle_store=None,
        defer_failed_retry_to_lifecycle=True,
    )
    rich.assert_not_called()


def test_monitor_jobs_uses_rich_renderer_when_stdout_is_tty() -> None:
    queue = MagicMock()
    config = MagicMock()
    job = MagicMock()

    with (
        patch(
            "crsbench.run_experiment.importlib.util.find_spec", return_value=object()
        ),
        patch("sys.stdout.isatty", return_value=True),
        patch("crsbench.run_experiment._monitor_jobs_basic", return_value=[]) as basic,
        patch("crsbench.run_experiment._monitor_jobs_rich", return_value=[]) as rich,
    ):
        monitor_jobs(queue, [job], "exp-test", config)

    rich.assert_called_once_with(
        queue,
        [job],
        "exp-test",
        config,
        disk_skipped=0,
        registry=None,
        lifecycle_store=None,
        defer_failed_retry_to_lifecycle=True,
    )
    basic.assert_not_called()


def test_monitor_callbacks_skip_stale_owner_until_authoritative_result() -> None:
    config = MagicMock()
    stale_result = _make_result(success=True, worker_machine="worker-old")
    current_result = _make_result(success=True, worker_machine="worker-new")

    job = MagicMock()
    job.id = "job-1"
    job.meta = {"worker_name": "worker-new"}
    job.result = stale_result

    lifecycle_store = MagicMock()
    lifecycle_store.get.side_effect = [
        JobLifecycleRecord(
            job_id="job-1",
            trial_key="trial-1",
            state=JobState.RUNNING,
            claimed_by="worker-new",
        ),
        JobLifecycleRecord(
            job_id="job-1",
            trial_key="trial-1",
            state=JobState.RUNNING,
            claimed_by="worker-new",
        ),
    ]

    callbacks = _build_monitor_callbacks(
        config,
        experiment_name="exp-test",
        lifecycle_store=lifecycle_store,
    )

    with patch("crsbench.run_experiment._write_orchestrator_marker") as marker:
        assert callbacks.on_job_finished(job) is False

        job.result = current_result

        assert callbacks.on_job_finished(job) is not False

    marker.assert_called_once_with(current_result, config)


def test_monitor_callbacks_defer_failed_updates_for_retried_active_jobs() -> None:
    config = MagicMock()

    job = MagicMock()
    job.id = "job-1"
    job.meta = {"worker_name": "worker-new", "retry_count": 1}
    job.kwargs = {
        "crs": "crs-a",
        "benchmark": "bench-a",
        "harness_name": "fuzz_target",
        "mode": "delta",
        "sanitizer": "address",
        "trial_num": 1,
        "target_cpv_id": None,
    }
    job.exc_info = "stale infra failure"

    lifecycle_store = MagicMock()
    lifecycle_store.get.return_value = JobLifecycleRecord(
        job_id="job-1",
        trial_key="trial-1",
        state=JobState.RUNNING,
        claimed_by="worker-new",
        retry_count=1,
    )

    callbacks = _build_monitor_callbacks(
        config,
        experiment_name="exp-test",
        lifecycle_store=lifecycle_store,
    )

    with patch("crsbench.run_experiment._write_orchestrator_marker") as marker:
        assert callbacks.on_job_failed(job) is False

    marker.assert_not_called()


def test_monitor_callbacks_consume_retried_failed_updates_without_lifecycle_monitor() -> (
    None
):
    config = MagicMock()

    job = MagicMock()
    job.id = "job-1"
    job.meta = {"worker_name": "worker-new", "retry_count": 1}
    job.kwargs = {
        "crs": "crs-a",
        "benchmark": "bench-a",
        "harness_name": "fuzz_target",
        "mode": "delta",
        "sanitizer": "address",
        "trial_num": 1,
        "target_cpv_id": None,
    }
    job.exc_info = "stale infra failure"

    lifecycle_store = MagicMock()
    lifecycle_store.get.return_value = JobLifecycleRecord(
        job_id="job-1",
        trial_key="trial-1",
        state=JobState.RUNNING,
        claimed_by="worker-new",
        retry_count=1,
    )

    callbacks = _build_monitor_callbacks(
        config,
        experiment_name="exp-test",
        lifecycle_store=lifecycle_store,
        defer_failed_retry_to_lifecycle=False,
    )

    with patch("crsbench.run_experiment._write_orchestrator_marker") as marker:
        assert callbacks.on_job_failed(job) is True

    marker.assert_not_called()


def test_monitor_callbacks_defer_abandoned_job_failures_to_lifecycle_recovery() -> None:
    config = MagicMock()

    job = MagicMock()
    job.id = "job-1"
    job.meta = {"worker_name": "worker-old", "expects_lifecycle_tracking": True}
    job.kwargs = {
        "crs": "crs-a",
        "benchmark": "bench-a",
        "harness_name": "fuzz_target",
        "mode": "delta",
        "sanitizer": "address",
        "trial_num": 1,
        "target_cpv_id": None,
    }
    job.exc_info = (
        "rq.timeouts.AbandonedJobError: Job was abandoned because the worker exited"
    )

    lifecycle_store = MagicMock()
    lifecycle_store.get.return_value = JobLifecycleRecord(
        job_id="job-1",
        trial_key="trial-1",
        state=JobState.RUNNING,
        claimed_by="worker-old",
        retry_count=0,
    )

    callbacks = _build_monitor_callbacks(
        config,
        experiment_name="exp-test",
        lifecycle_store=lifecycle_store,
    )

    with patch("crsbench.run_experiment._write_orchestrator_marker") as marker:
        assert callbacks.on_job_failed(job) is False

    marker.assert_not_called()


def test_monitor_callbacks_allow_finished_updates_for_retried_active_jobs() -> None:
    config = MagicMock()
    result = _make_result(success=True, worker_machine="worker-new")

    job = MagicMock()
    job.id = "job-1"
    job.meta = {"worker_name": "worker-new", "retry_count": 1}
    job.result = result

    lifecycle_store = MagicMock()
    lifecycle_store.get.return_value = JobLifecycleRecord(
        job_id="job-1",
        trial_key="trial-1",
        state=JobState.RUNNING,
        claimed_by="worker-new",
        retry_count=1,
    )

    callbacks = _build_monitor_callbacks(
        config,
        experiment_name="exp-test",
        lifecycle_store=lifecycle_store,
    )

    with patch("crsbench.run_experiment._write_orchestrator_marker") as marker:
        assert callbacks.on_job_finished(job) is True

    marker.assert_called_once_with(result, config)


def test_monitor_callbacks_retry_when_lifecycle_lookup_fails() -> None:
    config = MagicMock()
    result = _make_result(success=True, worker_machine="worker-new")

    job = MagicMock()
    job.id = "job-1"
    job.meta = {"worker_name": "worker-new"}
    job.result = result

    lifecycle_store = MagicMock()
    lifecycle_store.get.side_effect = RuntimeError("redis unavailable")

    callbacks = _build_monitor_callbacks(
        config,
        experiment_name="exp-test",
        lifecycle_store=lifecycle_store,
    )

    with patch("crsbench.run_experiment._write_orchestrator_marker") as marker:
        assert callbacks.on_job_finished(job) is False

    marker.assert_not_called()


def test_monitor_callbacks_allow_finished_updates_without_lifecycle_record_for_legacy_job() -> (
    None
):
    config = MagicMock()
    result = _make_result(success=True, worker_machine="worker-new")

    job = MagicMock()
    job.id = "job-1"
    job.meta = {"worker_name": "worker-new"}
    job.result = result

    lifecycle_store = MagicMock()
    lifecycle_store.get.return_value = None

    callbacks = _build_monitor_callbacks(
        config,
        experiment_name="exp-test",
        lifecycle_store=lifecycle_store,
    )

    with patch("crsbench.run_experiment._write_orchestrator_marker") as marker:
        assert callbacks.on_job_finished(job) is True

    marker.assert_called_once_with(result, config)


def test_monitor_callbacks_allow_failed_updates_without_lifecycle_record_for_legacy_job() -> (
    None
):
    config = MagicMock()

    job = MagicMock()
    job.id = "job-1"
    job.meta = {"worker_name": "worker-new"}
    job.kwargs = {
        "crs": "crs-a",
        "benchmark": "bench-a",
        "harness_name": "fuzz_target",
        "mode": "delta",
        "sanitizer": "address",
        "trial_num": 1,
        "target_cpv_id": None,
    }
    job.exc_info = "legacy failure"

    lifecycle_store = MagicMock()
    lifecycle_store.get.return_value = None

    callbacks = _build_monitor_callbacks(
        config,
        experiment_name="exp-test",
        lifecycle_store=lifecycle_store,
    )

    with patch("crsbench.run_experiment._write_orchestrator_marker") as marker:
        assert callbacks.on_job_failed(job) is True

    marker.assert_called_once()


def test_monitor_callbacks_consume_tracked_job_without_lifecycle_record_without_marker() -> (
    None
):
    config = MagicMock()
    result = _make_result(success=True, worker_machine="worker-new")

    job = MagicMock()
    job.id = "job-1"
    job.meta = {
        "worker_name": "worker-new",
        "expects_lifecycle_tracking": True,
    }
    job.result = result

    lifecycle_store = MagicMock()
    lifecycle_store.get.return_value = None

    callbacks = _build_monitor_callbacks(
        config,
        experiment_name="exp-test",
        lifecycle_store=lifecycle_store,
    )

    with patch("crsbench.run_experiment._write_orchestrator_marker") as marker:
        assert callbacks.on_job_finished(job) is True

    marker.assert_not_called()


def test_monitor_callbacks_retry_when_active_lifecycle_owner_missing() -> None:
    config = MagicMock()
    result = _make_result(success=True, worker_machine="worker-new")

    job = MagicMock()
    job.id = "job-1"
    job.meta = {}
    job.result = result

    lifecycle_store = MagicMock()
    lifecycle_store.get.return_value = JobLifecycleRecord(
        job_id="job-1",
        trial_key="trial-1",
        state=JobState.RUNNING,
        claimed_by=None,
        retry_count=0,
    )

    callbacks = _build_monitor_callbacks(
        config,
        experiment_name="exp-test",
        lifecycle_store=lifecycle_store,
    )

    with patch("crsbench.run_experiment._write_orchestrator_marker") as marker:
        assert callbacks.on_job_finished(job) is False

    marker.assert_not_called()


def test_monitor_callbacks_consume_non_active_lifecycle_without_marker_write() -> None:
    config = MagicMock()
    result = MagicMock(name="late_result")

    job = MagicMock()
    job.id = "job-1"
    job.meta = {"worker_name": "worker-old"}
    job.result = result

    lifecycle_store = MagicMock()
    lifecycle_store.get.return_value = JobLifecycleRecord(
        job_id="job-1",
        trial_key="trial-1",
        state=JobState.FAILED,
        claimed_by=None,
    )

    callbacks = _build_monitor_callbacks(
        config,
        experiment_name="exp-test",
        lifecycle_store=lifecycle_store,
    )

    with patch("crsbench.run_experiment._write_orchestrator_marker") as marker:
        assert callbacks.on_job_finished(job) is not False

    marker.assert_not_called()


def test_monitor_callbacks_write_fail_marker_for_authoritative_failed_lifecycle() -> (
    None
):
    config = MagicMock()

    job = MagicMock()
    job.id = "job-1"
    job.meta = {
        "worker_name": "worker-1",
        "expects_lifecycle_tracking": True,
    }
    job.kwargs = {
        "crs": "crs-a",
        "benchmark": "bench-a",
        "harness_name": "fuzz_target",
        "mode": "delta",
        "sanitizer": "address",
        "trial_num": 1,
        "target_cpv_id": None,
    }
    job.exc_info = "boom"

    lifecycle_store = MagicMock()
    lifecycle_store.get.return_value = JobLifecycleRecord(
        job_id="job-1",
        trial_key="trial-1",
        state=JobState.FAILED,
        claimed_by=None,
    )

    callbacks = _build_monitor_callbacks(
        config,
        experiment_name="exp-test",
        lifecycle_store=lifecycle_store,
    )

    with patch("crsbench.run_experiment._write_orchestrator_marker") as marker:
        assert callbacks.on_job_failed(job) is True

    marker.assert_called_once()


def test_monitor_callbacks_write_fail_marker_for_finished_failed_result() -> None:
    config = MagicMock()
    result = _make_result(success=False, worker_machine="worker-1")

    job = MagicMock()
    job.id = "job-1"
    job.meta = {
        "worker_name": "worker-1",
        "expects_lifecycle_tracking": True,
    }
    job.result = result

    lifecycle_store = MagicMock()
    lifecycle_store.get.return_value = JobLifecycleRecord(
        job_id="job-1",
        trial_key="trial-1",
        state=JobState.FAILED,
        claimed_by=None,
    )

    callbacks = _build_monitor_callbacks(
        config,
        experiment_name="exp-test",
        lifecycle_store=lifecycle_store,
    )

    with patch("crsbench.run_experiment._write_orchestrator_marker") as marker:
        assert callbacks.on_job_finished(job) is True

    marker.assert_called_once_with(result, config)


def test_monitor_callbacks_write_success_marker_for_authoritative_completed_lifecycle() -> (
    None
):
    config = MagicMock()
    result = _make_result(success=True, worker_machine="worker-1")

    job = MagicMock()
    job.id = "job-1"
    job.meta = {
        "worker_name": "worker-1",
        "expects_lifecycle_tracking": True,
    }
    job.result = result

    lifecycle_store = MagicMock()
    lifecycle_store.get.return_value = JobLifecycleRecord(
        job_id="job-1",
        trial_key="trial-1",
        state=JobState.COMPLETED,
        claimed_by=None,
    )

    callbacks = _build_monitor_callbacks(
        config,
        experiment_name="exp-test",
        lifecycle_store=lifecycle_store,
    )

    with patch("crsbench.run_experiment._write_orchestrator_marker") as marker:
        assert callbacks.on_job_finished(job) is True

    marker.assert_called_once_with(result, config)


def test_monitor_callbacks_reconstruct_success_when_finished_job_result_is_missing() -> (
    None
):
    config = MagicMock()

    job = MagicMock()
    job.id = "job-1"
    job.meta = {
        "worker_name": "worker-1",
        "expects_lifecycle_tracking": True,
    }
    job.kwargs = {
        "crs": "crs-a",
        "benchmark": "bench-a",
        "harness_name": "fuzz_target",
        "mode": "delta",
        "sanitizer": "address",
        "trial_num": 1,
        "target_cpv_id": None,
    }
    job.result = None

    lifecycle_store = MagicMock()
    lifecycle_store.get.return_value = JobLifecycleRecord(
        job_id="job-1",
        trial_key="trial-1",
        state=JobState.COMPLETED,
        claimed_by=None,
    )

    callbacks = _build_monitor_callbacks(
        config,
        experiment_name="exp-test",
        lifecycle_store=lifecycle_store,
    )

    with patch("crsbench.run_experiment._write_orchestrator_marker") as marker:
        assert callbacks.on_job_finished(job) is True

    written_result = marker.call_args.args[0]
    assert written_result.success is True
    marker.assert_called_once()


def test_collect_monitored_results_reconstructs_success_from_completed_lifecycle() -> (
    None
):
    config = MagicMock()

    job = MagicMock()
    job.id = "job-1"
    job.result = None
    job.is_finished = True
    job.is_failed = False
    job.kwargs = {
        "crs": "crs-a",
        "benchmark": "bench-a",
        "harness_name": "fuzz_target",
        "mode": "delta",
        "sanitizer": "address",
        "trial_num": 1,
        "target_cpv_id": None,
    }
    job.meta = {"worker_name": "worker-1"}
    job.refresh = MagicMock()

    lifecycle_store = MagicMock()
    lifecycle_store.get.return_value = JobLifecycleRecord(
        job_id="job-1",
        trial_key="trial-1",
        state=JobState.COMPLETED,
        claimed_by=None,
    )

    results = _collect_monitored_results(
        [job],
        config=config,
        experiment_name="exp-test",
        lifecycle_store=lifecycle_store,
    )

    assert len(results) == 1
    assert results[0].success is True


def test_monitor_callbacks_retry_marker_write_after_transient_failure() -> None:
    config = MagicMock()

    job = MagicMock()
    job.id = "job-1"
    job.meta = {"worker_name": "worker-1"}
    job.result = MagicMock(name="result")

    callbacks = _build_monitor_callbacks(config, experiment_name="exp-test")

    with patch(
        "crsbench.run_experiment._write_orchestrator_marker",
        side_effect=[OSError("disk busy"), None],
    ) as marker:
        assert callbacks.on_job_finished(job) is False
        assert callbacks.on_job_finished(job) is not False

    assert marker.call_count == 2


def test_monitor_callbacks_preserve_preexisting_canonical_marker_for_conflict(
    tmp_path: Path,
) -> None:
    config = MagicMock()
    config.experiment_filestore = tmp_path
    config.experiment = "exp-test"

    trial_dir = _build_trial_output_path(
        filestore=tmp_path.resolve(),
        experiment_name="exp-test",
        crs="crs-a",
        benchmark="bench-a",
        harness="fuzz_target",
        mode="delta",
        sanitizer="address",
        trial_num=1,
        target_cpv_id=None,
    )
    trial_dir.mkdir(parents=True, exist_ok=True)
    (trial_dir / ".success").touch()

    job = MagicMock()
    job.id = "job-failed"
    job.meta = {}
    job.kwargs = {
        "crs": "crs-a",
        "benchmark": "bench-a",
        "harness_name": "fuzz_target",
        "mode": "delta",
        "sanitizer": "address",
        "trial_num": 1,
        "target_cpv_id": None,
    }
    job.exc_info = "stale failure"

    callbacks = _build_monitor_callbacks(config, experiment_name="exp-test")

    with patch("crsbench.run_experiment._write_orchestrator_marker") as marker:
        assert callbacks.on_job_failed(job) is not False

    marker.assert_not_called()


def test_monitor_callbacks_preserve_session_canonical_marker_for_conflict(
    tmp_path: Path,
) -> None:
    config = MagicMock()
    config.experiment_filestore = tmp_path
    config.experiment = "exp-test"

    success_job = MagicMock()
    success_job.id = "job-success"
    success_job.meta = {}
    success_job.result = _make_result(success=True)

    failed_job = MagicMock()
    failed_job.id = "job-failed"
    failed_job.meta = {}
    failed_job.kwargs = {
        "crs": "crs-a",
        "benchmark": "bench-a",
        "harness_name": "fuzz_target",
        "mode": "delta",
        "sanitizer": "address",
        "trial_num": 1,
        "target_cpv_id": None,
    }
    failed_job.exc_info = "late duplicate failure"

    callbacks = _build_monitor_callbacks(config, experiment_name="exp-test")

    with patch("crsbench.run_experiment._write_orchestrator_marker") as marker:
        assert callbacks.on_job_finished(success_job) is not False
        assert callbacks.on_job_failed(failed_job) is not False

    marker.assert_called_once_with(success_job.result, config)


def test_get_experiment_queue_stats_uses_experiment_scoped_counts() -> None:
    """Monitor stats must ignore unrelated jobs in the shared flat queue."""
    queue = MagicMock()

    with (
        patch(
            "crsbench.distributed.queue_monitor.get_queue_stats",
            return_value={
                "queued": 51,
                "started": 12,
                "finished": 3,
                "failed": 0,
                "workers": 12,
            },
        ),
        patch(
            "crsbench.distributed.queue_monitor.get_existing_trial_jobs",
            return_value={
                "queued": [MagicMock() for _ in range(51)],
                "started": [MagicMock() for _ in range(12)],
                "finished": [],
                "failed": [],
                "deferred": [],
                "scheduled": [],
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


def test_distributed_enqueue_uses_deterministic_trial_job_id(tmp_path: Path) -> None:
    config = _make_provider_neutral_run_config(tmp_path)

    session = MagicMock()
    queue = MagicMock()
    session.trial_queue = queue
    session.cloud_readiness = MagicMock()
    session.register_or_raise.return_value = None

    trial = _make_trial("cpv-0")

    def _enqueue(*_args, **kwargs):
        assert kwargs["job_id"] == build_trial_queue_job_id("exp-test", trial)
        assert kwargs["trial_id"] != kwargs["job_id"]
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
            return_value=MagicMock(),
        ),
        patch("crsbench.cloud.quota.QuotaValidator") as mock_quota_validator_cls,
        patch(
            "crsbench.cloud.status.CloudFleetStatusManager", return_value=MagicMock()
        ),
    ):
        mock_quota_validator_cls.return_value.validate.return_value = None
        with pytest.raises(RuntimeError, match="stop after enqueue"):
            run_experiment_distributed("exp-test", config, [trial])


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
    config = _make_provider_neutral_run_config(tmp_path)

    session = MagicMock()
    queue = MagicMock()
    session.trial_queue = queue
    session.cloud_readiness = MagicMock()
    session.register_or_raise.return_value = None

    registration = MagicMock()
    call_order: list[str] = []
    manager = MagicMock()
    manager.bring_up_workers.side_effect = lambda **_kwargs: (
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
        patch("crsbench.cloud.quota.QuotaValidator") as mock_quota_validator_cls,
        patch(
            "crsbench.cloud.status.CloudFleetStatusManager",
            return_value=manager,
        ),
    ):
        mock_quota_validator_cls.return_value.validate.return_value = None
        with pytest.raises(RuntimeError, match="stop after enqueue"):
            run_experiment_distributed("exp-test", config, [_make_trial(None)])

    manager.bring_up_workers.assert_called_once()


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
            "crsbench.cloud.providers.prepare_launch_inputs",
            return_value=MagicMock(resolved_plan=resolved_plan),
        ),
        patch(
            "crsbench.cloud.providers.provider_adapter_for_launch_plan",
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


def test_provider_neutral_cloud_workers_seed_lifecycle_and_start_monitor(
    tmp_path: Path,
) -> None:
    """Cloud-backed distributed runs should seed lifecycle records and start monitor."""
    config = _make_provider_neutral_run_config(tmp_path)

    session = MagicMock()
    queue = MagicMock()
    session.trial_queue = queue
    session.cloud_readiness = MagicMock()
    session.lifecycle_store = MagicMock()
    session.register_or_raise.return_value = None

    registration = MagicMock()
    manager = MagicMock()
    manager.bring_up_workers.return_value = MagicMock(ready_count=1, requested_count=1)

    queued_job = MagicMock()
    queued_job.id = "job-123"
    queue.enqueue.return_value = queued_job

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
        patch("crsbench.cloud.quota.QuotaValidator") as mock_quota_validator_cls,
        patch(
            "crsbench.cloud.status.CloudFleetStatusManager",
            return_value=manager,
        ),
        patch(
            "crsbench.run_experiment.monitor_jobs",
            side_effect=RuntimeError("stop after monitor setup"),
        ),
    ):
        mock_quota_validator_cls.return_value.validate.return_value = None
        with pytest.raises(RuntimeError, match="stop after monitor setup"):
            run_experiment_distributed("exp-test", config, [_make_trial(None)])

    session.start_monitor.assert_called_once()
    session.lifecycle_store.set.assert_called_once()
    seeded_record = session.lifecycle_store.set.call_args.args[1]
    assert seeded_record.job_id == "job-123"
    assert seeded_record.trial_key == "crs-a:bench-a:fuzz_target:delta:address:1:-"
    assert seeded_record.state.value == "queued"


def test_provider_neutral_cloud_retry_failed_refreshes_active_existing_jobs(
    tmp_path: Path,
) -> None:
    """Retried failed jobs must trigger cloud bring-up even with no new trials."""
    config = _make_provider_neutral_run_config(tmp_path)

    session = MagicMock()
    queue = MagicMock()
    session.trial_queue = queue
    session.cloud_readiness = MagicMock()
    session.register_or_raise.return_value = None

    failed_job = MagicMock()
    failed_job.id = "job-failed"
    failed_job.meta = {}
    failed_job.kwargs = {
        "crs": "crs-a",
        "benchmark": "bench-a",
        "harness_name": "fuzz_target",
        "mode": "delta",
        "sanitizer": "address",
        "trial_num": 1,
        "target_cpv_id": None,
    }

    existing = {
        "queued": {},
        "started": {},
        "failed": {"trial-1": failed_job},
        "finished": {},
        "deferred": {},
        "scheduled": {},
    }
    refreshed_existing = {
        "queued": {"trial-1": failed_job},
        "started": {},
        "failed": {},
        "finished": {},
        "deferred": {},
        "scheduled": {},
    }
    physical_existing = {
        "queued": [],
        "started": [],
        "failed": [failed_job],
        "finished": [],
        "deferred": [],
        "scheduled": [],
    }
    refreshed_physical_existing = {
        "queued": [failed_job],
        "started": [],
        "failed": [],
        "finished": [],
        "deferred": [],
        "scheduled": [],
    }

    manager = MagicMock()
    manager.bring_up_workers.side_effect = RuntimeError("stop after bringup")

    with (
        patch(
            "crsbench.distributed.runtime_session.DistributedRuntimeSession.for_run",
            return_value=session,
        ),
        patch(
            "crsbench.distributed.queue.get_existing_trials",
            side_effect=[existing, refreshed_existing],
        ),
        patch(
            "crsbench.distributed.queue.get_existing_trial_jobs",
            side_effect=[physical_existing, refreshed_physical_existing],
        ),
        patch("crsbench.distributed.queue.handle_orphaned_jobs", return_value=0),
        patch(
            "crsbench.run_experiment._prepare_trial_dir_for_retry", return_value=True
        ),
        patch("crsbench.run_experiment.dump_trial_matrix"),
        patch(
            "crsbench.distributed.registry.RuntimeRegistration.from_experiment_config",
            return_value=MagicMock(),
        ),
        patch("crsbench.cloud.quota.QuotaValidator") as mock_quota_validator_cls,
        patch(
            "crsbench.cloud.status.CloudFleetStatusManager",
            return_value=manager,
        ),
        patch(
            "crsbench.run_experiment.monitor_jobs",
            side_effect=RuntimeError("reached monitor without bringup"),
        ),
    ):
        mock_quota_validator_cls.return_value.validate.return_value = None
        with pytest.raises(RuntimeError, match="stop after bringup"):
            run_experiment_distributed(
                "exp-test",
                config,
                [],
                queue_mode="continue",
                retry_failed=True,
            )

    queue.enqueue_job.assert_called_once_with(failed_job)


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
        assert resolved_plan.worker_placements[0].env["HF_TOKEN"] == "hf_secret_value"
        assert (
            resolved_plan.worker_placements[0].launch_defaults.github_deploy_key_path
            == expected_key_path
        )
        assert launch_plan.worker_placements[0].env["HF_TOKEN"] == "os.environ/HF_TOKEN"
        assert (
            launch_plan.worker_placements[0].launch_defaults.github_deploy_key_path
            == ".crsbench-keys/crsbench-deploy"
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
            "crsbench.cloud.providers.provider_adapter_for_launch_plan",
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


def test_provider_neutral_cloud_workers_pass_layered_env_payloads(
    tmp_path: Path,
) -> None:
    """Local cloud-worker bring-up should pass only common+worker env vars to VMs."""
    config = _make_provider_neutral_run_config(tmp_path)

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
        assert kwargs["env_passthrough_by_placement"] == [
            {
                "CRSBENCH_LLM_UPSTREAM_BASE_URL": "https://llm.example.test",
                "OPENAI_API_KEY": "openai-key",
            },
            {
                "CRSBENCH_LLM_UPSTREAM_BASE_URL": "https://llm.example.test",
                "OPENAI_API_KEY": "openai-key",
            },
        ]
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
            "crsbench.cloud.providers.prepare_launch_inputs",
            return_value=MagicMock(
                resolved_plan=resolved_plan,
                worker_placement_envs=[
                    {
                        "CRSBENCH_LLM_UPSTREAM_BASE_URL": "https://llm.example.test",
                        "OPENAI_API_KEY": "openai-key",
                    },
                    {
                        "CRSBENCH_LLM_UPSTREAM_BASE_URL": "https://llm.example.test",
                        "OPENAI_API_KEY": "openai-key",
                    },
                ],
            ),
        ) as mock_preflight,
        patch(
            "crsbench.cloud.providers.provider_adapter_for_launch_plan",
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
        cwd=Path.cwd(),
    )
    manager.bring_up_workers.assert_called_once()


def test_provider_neutral_cloud_instances_with_evaluators_pass_layered_env_payloads(
    tmp_path: Path,
) -> None:
    """Local cloud bring-up should provision evaluators with evaluator-only env vars."""
    config = _with_evaluator_placements(_make_provider_neutral_run_config(tmp_path))

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
        assert kwargs["worker_env_passthrough_by_placement"] == [
            {
                "CRSBENCH_LLM_UPSTREAM_BASE_URL": "https://llm.example.test",
                "OPENAI_API_KEY": "openai-key",
            },
            {
                "CRSBENCH_LLM_UPSTREAM_BASE_URL": "https://llm.example.test",
                "OPENAI_API_KEY": "openai-key",
            },
        ]
        assert kwargs["evaluator_env_passthrough_by_placement"] == [
            {
                "CRSBENCH_LLM_UPSTREAM_BASE_URL": "https://llm.example.test",
                "ANTHROPIC_API_KEY": "anthropic-key",
            },
            {
                "CRSBENCH_LLM_UPSTREAM_BASE_URL": "https://llm.example.test",
                "ANTHROPIC_API_KEY": "anthropic-key",
            },
        ]
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
            "crsbench.cloud.providers.prepare_launch_inputs",
            return_value=MagicMock(
                resolved_plan=resolved_plan,
                worker_placement_envs=[
                    {
                        "CRSBENCH_LLM_UPSTREAM_BASE_URL": "https://llm.example.test",
                        "OPENAI_API_KEY": "openai-key",
                    },
                    {
                        "CRSBENCH_LLM_UPSTREAM_BASE_URL": "https://llm.example.test",
                        "OPENAI_API_KEY": "openai-key",
                    },
                ],
                evaluator_placement_envs=[
                    {
                        "CRSBENCH_LLM_UPSTREAM_BASE_URL": "https://llm.example.test",
                        "ANTHROPIC_API_KEY": "anthropic-key",
                    },
                    {
                        "CRSBENCH_LLM_UPSTREAM_BASE_URL": "https://llm.example.test",
                        "ANTHROPIC_API_KEY": "anthropic-key",
                    },
                ],
            ),
        ) as mock_preflight,
        patch(
            "crsbench.cloud.providers.provider_adapter_for_launch_plan",
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
        cwd=Path.cwd(),
    )
    manager.bring_up_instances.assert_called_once()
    manager.bring_up_workers.assert_not_called()


def test_provider_neutral_preprovisioned_observe_does_not_resolve_secret_refs_again(
    tmp_path: Path,
) -> None:
    """Remote orchestrator observe path must not require operator-only secret sources."""
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

    def _observe_existing_workers(**kwargs):
        unresolved_plan = kwargs["plan"]
        assert (
            unresolved_plan.worker_placements[0].env["HF_TOKEN"]
            == "os.environ/HF_TOKEN"
        )
        raise RuntimeError("stop after existing-worker observe")

    manager.observe_existing_workers.side_effect = _observe_existing_workers

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
            "crsbench.cloud.providers.provider_adapter_for_launch_plan",
            return_value=adapter,
        ),
        patch(
            "crsbench.cloud.status.CloudFleetStatusManager",
            return_value=manager,
        ),
    ):
        with pytest.raises(RuntimeError, match="stop after existing-worker observe"):
            run_experiment_distributed("exp-test", config, [_make_trial(None)])

    manager.observe_existing_workers.assert_called_once()
    manager.bring_up_workers.assert_not_called()


def test_provider_neutral_preprovisioned_evaluators_use_combined_observe(
    tmp_path: Path,
) -> None:
    """Pre-provisioned evaluator fleets should use the combined observe path."""
    config = _with_evaluator_placements(_make_provider_neutral_run_config(tmp_path))

    session = MagicMock()
    queue = MagicMock()
    session.trial_queue = queue
    session.cloud_readiness = MagicMock()
    session.register_or_raise.return_value = None

    registration = MagicMock()
    launch_plan = build_cloud_launch_plan(config)
    adapter = MagicMock()
    manager = MagicMock()

    def _observe_existing_instances(**kwargs):
        unresolved_plan = kwargs["plan"]
        assert unresolved_plan.evaluator_placements == launch_plan.evaluator_placements
        raise RuntimeError("stop after existing-instance observe")

    manager.observe_existing_instances.side_effect = _observe_existing_instances

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
            "crsbench.cloud.providers.provider_adapter_for_launch_plan",
            return_value=adapter,
        ),
        patch(
            "crsbench.cloud.status.CloudFleetStatusManager",
            return_value=manager,
        ),
    ):
        with pytest.raises(RuntimeError, match="stop after existing-instance observe"):
            run_experiment_distributed("exp-test", config, [_make_trial(None)])

    manager.observe_existing_instances.assert_called_once()
    manager.observe_existing_workers.assert_not_called()
    manager.bring_up_instances.assert_not_called()
    manager.bring_up_workers.assert_not_called()


def test_cloud_fleet_failure_aborts_before_enqueue(tmp_path: Path) -> None:
    """Cloud bring-up failures must abort before any trial jobs are queued."""
    from crsbench.cloud.status import CloudFleetBringupError

    config = _make_provider_neutral_run_config(tmp_path)

    session = MagicMock()
    queue = MagicMock()
    session.trial_queue = queue
    session.cloud_readiness = MagicMock()
    session.register_or_raise.return_value = None

    manager = MagicMock()
    manager.bring_up_workers.side_effect = CloudFleetBringupError(
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
        patch("crsbench.cloud.quota.QuotaValidator") as mock_quota_validator_cls,
        patch(
            "crsbench.cloud.status.CloudFleetStatusManager",
            return_value=manager,
        ),
    ):
        mock_quota_validator_cls.return_value.validate.return_value = None
        with pytest.raises(
            CloudFleetBringupError,
            match="bootstrap failed: systemd unit exited",
        ):
            run_experiment_distributed("exp-test", config, [_make_trial(None)])

    queue.enqueue.assert_not_called()


def test_cloud_fleet_bringup_is_skipped_when_no_trials_remain(tmp_path: Path) -> None:
    """Runs with no remaining work should not provision cloud workers."""
    config = _make_provider_neutral_run_config(tmp_path)

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
    manager.bring_up_workers.assert_not_called()
    session.trial_queue.enqueue.assert_not_called()


def test_cloud_fleet_bringup_is_skipped_for_preprovisioned_remote_orchestrator(
    tmp_path: Path, monkeypatch
) -> None:
    """Remote orchestrator mode should observe existing workers instead of reprovisioning."""
    monkeypatch.setenv("CRSBENCH_CLOUD_PREPROVISIONED_WORKERS", "1")
    config = _make_provider_neutral_run_config(tmp_path)

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

    manager.bring_up_workers.assert_not_called()
    manager.observe_existing_workers.assert_called_once()
