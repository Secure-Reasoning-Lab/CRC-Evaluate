"""Unit tests for BenchmarkRunner._stop_managers lifecycle behavior."""

from pathlib import Path
from unittest.mock import MagicMock

from crsbench.evaluation.runner import BenchmarkRunner
from crsbench.validation.schemas import HarnessFile


def _make_runner() -> BenchmarkRunner:
    adapter = MagicMock()
    adapter.mode = "bug-fixing"
    return BenchmarkRunner(adapter=adapter, snapshot_period=0)


def test_stop_managers_stops_snapshot_thread_before_final_capture() -> None:
    runner = _make_runner()
    snapshot_manager = MagicMock()
    snapshot_thread = MagicMock()
    snapshot_thread.is_alive.side_effect = [True, False]

    runner._stop_managers(
        snapshot_manager=snapshot_manager,
        snapshot_thread=snapshot_thread,
        coverage_manager=None,
        coverage_thread=None,
        harness_name="fuzz_target",
    )

    snapshot_manager.stop.assert_called_once()
    snapshot_thread.join.assert_called_once_with(timeout=15.0)
    snapshot_manager.capture_snapshot.assert_called_once()
    snapshot_manager.refresh_final_symlink.assert_called_once()


def test_stop_managers_waits_longer_and_captures_after_late_stop() -> None:
    runner = _make_runner()
    snapshot_manager = MagicMock()
    snapshot_thread = MagicMock()
    snapshot_thread.is_alive.side_effect = [True, True, False]

    runner._stop_managers(
        snapshot_manager=snapshot_manager,
        snapshot_thread=snapshot_thread,
        coverage_manager=None,
        coverage_thread=None,
        harness_name="fuzz_target",
    )

    snapshot_manager.stop.assert_called_once()
    assert snapshot_thread.join.call_count == 2
    snapshot_thread.join.assert_any_call(timeout=15.0)
    snapshot_thread.join.assert_any_call(timeout=60.0)
    snapshot_manager.capture_snapshot.assert_called_once()
    snapshot_manager.refresh_final_symlink.assert_called_once()


def test_stop_managers_reports_issue_if_snapshot_thread_never_stops() -> None:
    runner = _make_runner()
    snapshot_manager = MagicMock()
    snapshot_thread = MagicMock()
    coverage_manager = MagicMock()
    coverage_thread = MagicMock()
    coverage_thread.is_alive.return_value = False
    snapshot_thread.is_alive.side_effect = [True, True, True]

    cleanup_issue = runner._stop_managers(
        snapshot_manager=snapshot_manager,
        snapshot_thread=snapshot_thread,
        coverage_manager=coverage_manager,
        coverage_thread=coverage_thread,
        harness_name="fuzz_target",
    )

    snapshot_manager.capture_snapshot.assert_not_called()
    snapshot_manager.refresh_final_symlink.assert_not_called()
    coverage_manager.stop.assert_called_once()
    assert cleanup_issue is not None
    assert "Snapshot thread did not stop" in cleanup_issue


def test_stop_managers_reports_issue_if_snapshot_stop_raises() -> None:
    runner = _make_runner()
    snapshot_manager = MagicMock()
    snapshot_manager.stop.side_effect = RuntimeError("stop failed")
    snapshot_thread = MagicMock()
    snapshot_thread.is_alive.side_effect = [True, False]
    coverage_manager = MagicMock()
    coverage_thread = MagicMock()
    coverage_thread.is_alive.return_value = False

    cleanup_issue = runner._stop_managers(
        snapshot_manager=snapshot_manager,
        snapshot_thread=snapshot_thread,
        coverage_manager=coverage_manager,
        coverage_thread=coverage_thread,
        harness_name="fuzz_target",
    )

    snapshot_thread.join.assert_called_once_with(timeout=15.0)
    snapshot_manager.capture_snapshot.assert_called_once()
    snapshot_manager.refresh_final_symlink.assert_called_once()
    coverage_manager.stop.assert_called_once()
    assert cleanup_issue is not None
    assert "Snapshot manager cleanup failed" in cleanup_issue


def test_stop_managers_aggregates_stop_exception_and_thread_timeout() -> None:
    runner = _make_runner()
    snapshot_manager = MagicMock()
    snapshot_manager.stop.side_effect = RuntimeError("stop failed")
    snapshot_thread = MagicMock()
    snapshot_thread.is_alive.side_effect = [True, True, True]
    coverage_manager = MagicMock()
    coverage_thread = MagicMock()
    coverage_thread.is_alive.return_value = False

    cleanup_issue = runner._stop_managers(
        snapshot_manager=snapshot_manager,
        snapshot_thread=snapshot_thread,
        coverage_manager=coverage_manager,
        coverage_thread=coverage_thread,
        harness_name="fuzz_target",
    )

    assert snapshot_thread.join.call_count == 2
    snapshot_thread.join.assert_any_call(timeout=15.0)
    snapshot_thread.join.assert_any_call(timeout=60.0)
    snapshot_manager.capture_snapshot.assert_not_called()
    snapshot_manager.refresh_final_symlink.assert_not_called()
    coverage_manager.stop.assert_called_once()
    assert cleanup_issue is not None
    assert "Snapshot manager cleanup failed" in cleanup_issue
    assert "Snapshot thread did not stop" in cleanup_issue


def test_stop_managers_waits_remaining_llm_settle_time(monkeypatch) -> None:
    adapter = MagicMock()
    adapter.mode = "bug-fixing"
    runner = BenchmarkRunner(
        adapter=adapter,
        snapshot_period=0,
        llm_tracker=MagicMock(),
        llm_api_key="key",
        llm_trial_id="trial",
        llm_accounting_settle_seconds=60,
    )
    snapshot_manager = MagicMock()
    snapshot_thread = MagicMock()
    snapshot_thread.is_alive.return_value = False
    coverage_manager = MagicMock()
    coverage_thread = MagicMock()
    coverage_thread.is_alive.return_value = False
    sleep_mock = MagicMock()
    monkeypatch.setattr("crsbench.evaluation.runner.time.monotonic", lambda: 130.0)
    monkeypatch.setattr("crsbench.evaluation.runner.time.sleep", sleep_mock)

    runner._stop_managers(
        snapshot_manager=snapshot_manager,
        snapshot_thread=snapshot_thread,
        coverage_manager=coverage_manager,
        coverage_thread=coverage_thread,
        harness_name="fuzz_target",
        crs_run_end_monotonic=100.0,
    )

    sleep_mock.assert_called_once_with(30.0)
    snapshot_manager.capture_snapshot.assert_called_once()
    snapshot_manager.refresh_final_symlink.assert_called_once()


def test_stop_managers_skips_llm_settle_without_tracking(monkeypatch) -> None:
    runner = _make_runner()
    snapshot_manager = MagicMock()
    snapshot_thread = MagicMock()
    snapshot_thread.is_alive.return_value = False
    coverage_manager = MagicMock()
    coverage_thread = MagicMock()
    coverage_thread.is_alive.return_value = False
    sleep_mock = MagicMock()
    monkeypatch.setattr("crsbench.evaluation.runner.time.sleep", sleep_mock)

    runner._stop_managers(
        snapshot_manager=snapshot_manager,
        snapshot_thread=snapshot_thread,
        coverage_manager=coverage_manager,
        coverage_thread=coverage_thread,
        harness_name="fuzz_target",
        crs_run_end_monotonic=100.0,
    )

    sleep_mock.assert_not_called()
    snapshot_manager.capture_snapshot.assert_called_once()
    snapshot_manager.refresh_final_symlink.assert_called_once()


def test_stop_managers_skips_llm_settle_without_run_end_timestamp(monkeypatch) -> None:
    adapter = MagicMock()
    adapter.mode = "bug-fixing"
    runner = BenchmarkRunner(
        adapter=adapter,
        snapshot_period=0,
        llm_tracker=MagicMock(),
        llm_api_key="key",
        llm_trial_id="trial",
        llm_accounting_settle_seconds=60,
    )
    snapshot_manager = MagicMock()
    snapshot_thread = MagicMock()
    snapshot_thread.is_alive.return_value = False
    sleep_mock = MagicMock()
    monkeypatch.setattr("crsbench.evaluation.runner.time.sleep", sleep_mock)

    runner._stop_managers(
        snapshot_manager=snapshot_manager,
        snapshot_thread=snapshot_thread,
        coverage_manager=None,
        coverage_thread=None,
        harness_name="fuzz_target",
        crs_run_end_monotonic=None,
    )

    sleep_mock.assert_not_called()
    snapshot_manager.capture_snapshot.assert_called_once()
    snapshot_manager.refresh_final_symlink.assert_called_once()


def test_stop_managers_does_not_capture_final_snapshot_on_shutdown_sequencing_error() -> (
    None
):
    runner = _make_runner()
    snapshot_manager = MagicMock()
    snapshot_thread = MagicMock()
    snapshot_thread.is_alive.side_effect = RuntimeError("thread state failed")

    cleanup_issue = runner._stop_managers(
        snapshot_manager=snapshot_manager,
        snapshot_thread=snapshot_thread,
        coverage_manager=None,
        coverage_thread=None,
        harness_name="fuzz_target",
    )

    snapshot_manager.capture_snapshot.assert_not_called()
    snapshot_manager.refresh_final_symlink.assert_not_called()
    assert cleanup_issue is not None
    assert "shutdown sequencing failed" in cleanup_issue


def test_execute_crs_with_managers_skips_settle_timestamp_when_run_raises_after_run_start():
    adapter = MagicMock()
    adapter.mode = "bug-finding"

    def _run(**kwargs):
        kwargs["on_run_start"]()
        raise RuntimeError("run failed")

    adapter.run.side_effect = _run
    runner = BenchmarkRunner(
        adapter=adapter,
        snapshot_period=0,
        llm_tracker=MagicMock(),
        llm_api_key="key",
        llm_trial_id="trial",
        llm_accounting_settle_seconds=60,
    )
    runner._start_coverage_manager = MagicMock(return_value=(None, None, None))
    runner._start_pov_verification_manager = MagicMock(return_value=(None, None))
    runner._start_patch_verification_manager = MagicMock(return_value=None)
    runner._start_snapshot_manager = MagicMock(return_value=(None, None))
    stop_managers = MagicMock(return_value=None)
    runner._stop_managers = stop_managers

    runner._execute_crs_with_managers(
        harness=HarnessFile(name="fuzz", path="./fuzz.c"),
        benchmark_path=Path("/tmp/bench"),
        trial_output_dir=Path("/tmp/trial"),
        trial_start_time=0.0,
    )

    assert stop_managers.call_count == 1
    assert stop_managers.call_args.kwargs["crs_run_end_monotonic"] is None
