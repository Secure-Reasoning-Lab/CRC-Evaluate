"""Unit tests for BenchmarkRunner skip_verification behavior."""

from pathlib import Path
from unittest.mock import MagicMock

from crsbench.evaluation.runner import BenchmarkRunner
from crsbench.validation.schemas import HarnessFile


def _make_bugfinding_runner() -> BenchmarkRunner:
    adapter = MagicMock()
    adapter.mode = "bug-finding"
    adapter.exchange_pov_dir = None
    adapter.resolve_artifacts.return_value = None
    adapter.run.return_value = MagicMock(
        execution_time=1.0,
        success=True,
        output="ok",
        build_time=None,
        run_time=None,
    )
    adapter.collect_results.return_value = {"type": "bug-finding"}
    return BenchmarkRunner(
        adapter=adapter,
        snapshot_period=60,
        oss_fuzz_path=Path("/tmp/oss-fuzz"),
    )


def test_execute_crs_with_managers_skips_live_pov_manager_when_verification_disabled(
    tmp_path: Path,
) -> None:
    runner = _make_bugfinding_runner()
    runner._prepare_runtime_inputs = MagicMock()
    runner._start_coverage_manager = MagicMock(return_value=(None, None, None))
    runner._start_pov_verification_manager = MagicMock(return_value=(MagicMock(), None))
    runner._start_patch_verification_manager = MagicMock(return_value=None)
    runner._start_snapshot_manager = MagicMock(return_value=(None, None))
    runner._stop_managers = MagicMock(return_value=None)

    runner._execute_crs_with_managers(
        harness=HarnessFile(name="fuzz", path="./fuzz.c"),
        benchmark_path=tmp_path / "benchmark",
        trial_output_dir=tmp_path / "trial",
        trial_start_time=0.0,
        skip_verification=True,
    )

    runner._start_pov_verification_manager.assert_not_called()
    assert (
        runner._start_snapshot_manager.call_args.kwargs["pov_verification_manager"]
        is None
    )


def test_execute_crs_with_managers_starts_live_pov_manager_when_verification_enabled(
    tmp_path: Path,
) -> None:
    runner = _make_bugfinding_runner()
    runner._prepare_runtime_inputs = MagicMock()
    runner._start_coverage_manager = MagicMock(return_value=(None, None, None))
    pov_manager = MagicMock()
    runner._start_pov_verification_manager = MagicMock(return_value=(pov_manager, None))
    runner._start_patch_verification_manager = MagicMock(return_value=None)
    runner._start_snapshot_manager = MagicMock(return_value=(None, None))
    runner._stop_managers = MagicMock(return_value=None)

    runner._execute_crs_with_managers(
        harness=HarnessFile(name="fuzz", path="./fuzz.c"),
        benchmark_path=tmp_path / "benchmark",
        trial_output_dir=tmp_path / "trial",
        trial_start_time=0.0,
        skip_verification=False,
    )

    runner._start_pov_verification_manager.assert_called_once()
    assert (
        runner._start_snapshot_manager.call_args.kwargs["pov_verification_manager"]
        is pov_manager
    )
