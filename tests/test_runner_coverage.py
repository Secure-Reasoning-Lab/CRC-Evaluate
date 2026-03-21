"""Tests for Atlantis-backed coverage execution inside BenchmarkRunner."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from crsbench.evaluation.coverage.models import CoverageSummary, TimedCoverageInput
from crsbench.evaluation.runner import BenchmarkRunner


def _make_runner() -> BenchmarkRunner:
    adapter = MagicMock()
    adapter.mode = "bug-fixing"
    return BenchmarkRunner(
        adapter=adapter,
        snapshot_period=0,
    )


def test_run_post_experiment_coverage_uses_engine_timed_line_coverage(
    tmp_path: Path,
) -> None:
    benchmark_path = tmp_path / "benchmark"
    benchmark_path.mkdir()
    trial_output_dir = tmp_path / "trial-1"
    seed_dir = trial_output_dir / "output" / "seeds"
    seed_dir.mkdir(parents=True)
    seed_path = seed_dir / "seed-a"
    seed_path.write_bytes(b"seed")
    runner = _make_runner()

    engine_inits: list[dict] = []
    engine_calls: list[dict] = []

    class _FakeEngine:
        def __init__(
            self,
            *,
            jobs: int | None = None,
            runtime_workers: int | None = None,
            runtime_cpus: list[int] | None = None,
            work_dir: Path | None = None,
            source_mode: str = "pkgs",
        ) -> None:
            engine_inits.append(
                {
                    "jobs": jobs,
                    "runtime_workers": runtime_workers,
                    "runtime_cpus": runtime_cpus,
                    "work_dir": work_dir,
                    "source_mode": source_mode,
                }
            )

        def collect_timed_line_coverage(
            self,
            benchmark_path: Path,
            timed_inputs: list[TimedCoverageInput],
            *,
            harness_filter: str | None = None,
            force_rebuild: bool = False,
            use_inc_build: bool = False,
            output_dir: Path | None = None,
        ) -> tuple[list[TimedCoverageInput], CoverageSummary]:
            engine_calls.append(
                {
                    "benchmark_path": benchmark_path,
                    "timed_inputs": timed_inputs,
                    "harness_filter": harness_filter,
                    "force_rebuild": force_rebuild,
                    "use_inc_build": use_inc_build,
                    "output_dir": output_dir,
                }
            )
            return (
                [
                    timed_inputs[0].model_copy(
                        update={"lines_covered": 7, "crashed": False}
                    )
                ],
                CoverageSummary(
                    metric="line",
                    corpus_total=1,
                    corpus_contributing=1,
                    corpus_unique=1,
                    lines_covered=7,
                    lines_total=11,
                    lines_percent=63.636363636,
                    functions_covered=3,
                    functions_total=5,
                ),
            )

        def cleanup(self) -> None:
            return None

    with patch(
        "crsbench.evaluation.coverage.engine.CoverageEngine",
        _FakeEngine,
    ):
        runner._run_post_experiment_coverage(
            benchmark_path=benchmark_path,
            trial_output_dir=trial_output_dir,
            harness_name="fuzz_target",
        )

    assert engine_inits == [
        {
            "jobs": None,
            "runtime_workers": None,
            "runtime_cpus": None,
            "work_dir": None,
            "source_mode": "pkgs",
        }
    ]
    assert len(engine_calls) == 1
    assert engine_calls[0]["benchmark_path"] == benchmark_path
    assert engine_calls[0]["harness_filter"] == "fuzz_target"
    assert engine_calls[0]["output_dir"] == trial_output_dir / "coverage"
    assert len(engine_calls[0]["timed_inputs"]) == 1
    assert engine_calls[0]["timed_inputs"][0].path == seed_path

    final_coverage = json.loads((trial_output_dir / "final_coverage.json").read_text())
    assert final_coverage["harness"] == "fuzz_target"
    assert final_coverage["summary"]["lines_covered"] == 7
    assert final_coverage["summary"]["lines_total"] == 11
    assert final_coverage["summary"]["totals_available"] is True


def test_run_post_experiment_coverage_logs_unknown_totals_cleanly(
    tmp_path: Path,
) -> None:
    benchmark_path = tmp_path / "benchmark"
    benchmark_path.mkdir()
    trial_output_dir = tmp_path / "trial-1"
    seed_dir = trial_output_dir / "output" / "seeds"
    seed_dir.mkdir(parents=True)
    seed_path = seed_dir / "seed-a"
    seed_path.write_bytes(b"seed")
    runner = _make_runner()
    runner.logger = MagicMock()

    class _FakeEngine:
        def __init__(self, **_kwargs) -> None:
            return None

        def collect_timed_line_coverage(
            self,
            benchmark_path: Path,
            timed_inputs: list[TimedCoverageInput],
            *,
            harness_filter: str | None = None,
            force_rebuild: bool = False,
            use_inc_build: bool = False,
            output_dir: Path | None = None,
        ) -> tuple[list[TimedCoverageInput], CoverageSummary]:
            del benchmark_path, timed_inputs, harness_filter, force_rebuild
            del use_inc_build, output_dir
            return (
                [],
                CoverageSummary(
                    metric="line",
                    corpus_total=1,
                    lines_covered=7,
                    lines_total=0,
                    lines_percent=0.0,
                    functions_covered=3,
                    functions_total=0,
                ),
            )

        def cleanup(self) -> None:
            return None

    with patch(
        "crsbench.evaluation.coverage.engine.CoverageEngine",
        _FakeEngine,
    ):
        runner._run_post_experiment_coverage(
            benchmark_path=benchmark_path,
            trial_output_dir=trial_output_dir,
            harness_name="fuzz_target",
        )

    runner.logger.info.assert_any_call("Post-experiment coverage: 7 (total N/A)")
    final_coverage = json.loads((trial_output_dir / "final_coverage.json").read_text())
    assert final_coverage["summary"]["totals_available"] is False
