"""Tests for coverage timeline analysis helpers and CLI validation."""

import argparse
import json
import sys
import threading
import time
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

from crsbench.evaluation.coverage.cli.coverage_command import (
    _run_direct_seed_timeline,
    add_coverage_subparser,
    run_coverage,
)
from crsbench.evaluation.coverage.models import (
    CoveragePovMarker,
    CoverageSummary,
    CoverageTimelineBucket,
    CoverageTimelineReport,
    TimedCoverageInput,
)
from crsbench.evaluation.coverage.reporting import (
    write_timeline_csv,
    write_timeline_json,
    write_timeline_png,
)
from crsbench.evaluation.coverage.timeline import (
    aggregate_line_coverage_buckets,
    discover_trial_seed_dir,
    load_trial_context,
    normalize_seed_inputs,
)


def test_discover_trial_seed_dir_prefers_output_seeds(tmp_path: Path) -> None:
    trial_dir = tmp_path / "trial-1"
    seeds_dir = trial_dir / "output" / "seeds"
    corpus_dir = trial_dir / "output" / "corpus"
    seeds_dir.mkdir(parents=True)
    corpus_dir.mkdir(parents=True)

    assert discover_trial_seed_dir(trial_dir) == seeds_dir


def test_discover_trial_seed_dir_uses_legacy_corpus(tmp_path: Path) -> None:
    trial_dir = tmp_path / "trial-1"
    corpus_dir = trial_dir / "output" / "corpus"
    corpus_dir.mkdir(parents=True)

    assert discover_trial_seed_dir(trial_dir) == corpus_dir


def test_load_trial_context_reads_metadata_and_pov_markers(tmp_path: Path) -> None:
    trial_dir = tmp_path / "trial-1"
    seeds_dir = trial_dir / "output" / "seeds"
    seeds_dir.mkdir(parents=True)
    (trial_dir / "metadata.json").write_text(
        """
        {
          "timestamp": "2026-03-13T00:00:00Z",
          "trial_num": 1,
          "crs": "crs-codex",
          "benchmark": "sanity-mock-c-delta-01",
          "harness": "fuzz_parse_buffer_section",
          "mode": "patch_generation",
          "source": {"path": "/tmp/source"}
        }
        """
    )
    povs_dir = trial_dir / "povs"
    povs_dir.mkdir()
    (povs_dir / "pov_store.json").write_text(
        """
        {
          "crs_run_start_time": 1000.0,
          "cpv_to_first_pov": {
            "cpv_1": {"pov_hash": "hash-b", "relative_time": 12.0},
            "cpv_0": {"pov_hash": "hash-a", "relative_time": 3.5}
          }
        }
        """
    )

    context = load_trial_context(trial_dir)

    assert context.benchmark == "sanity-mock-c-delta-01"
    assert context.harness == "fuzz_parse_buffer_section"
    assert context.seed_dir == seeds_dir
    assert context.crs_run_start_time == 1000.0
    assert context.pov_markers == [
        CoveragePovMarker(cpv_id="cpv_0", pov_hash="hash-a", relative_time=3.5),
        CoveragePovMarker(cpv_id="cpv_1", pov_hash="hash-b", relative_time=12.0),
    ]


def test_normalize_seed_inputs_deduplicates_by_hash_and_keeps_earliest(
    tmp_path: Path,
) -> None:
    seed_dir = tmp_path / "seeds"
    seed_dir.mkdir()
    first = seed_dir / "a.bin"
    first.write_bytes(b"same")
    second = seed_dir / "b.bin"
    second.write_bytes(b"same")
    third = seed_dir / "c.bin"
    third.write_bytes(b"other")
    hidden = seed_dir / ".ignored"
    hidden.write_bytes(b"hidden")

    first_mtime = 1010.0
    second_mtime = 1020.0
    third_mtime = 1030.0
    first.touch()
    second.touch()
    third.touch()
    hidden.touch()
    import os

    os.utime(first, (first_mtime, first_mtime))
    os.utime(second, (second_mtime, second_mtime))
    os.utime(third, (third_mtime, third_mtime))
    os.utime(hidden, (1005.0, 1005.0))

    normalized = normalize_seed_inputs(seed_dir, base_time=1000.0)

    assert len(normalized) == 2
    assert normalized[0].original_name == "a.bin"
    assert normalized[0].relative_time == 10.0
    assert normalized[1].original_name == "c.bin"
    assert normalized[1].relative_time == 30.0


def test_normalize_seed_inputs_uses_earliest_mtime_when_base_missing(
    tmp_path: Path,
) -> None:
    seed_dir = tmp_path / "seeds"
    seed_dir.mkdir()
    first = seed_dir / "a.bin"
    first.write_bytes(b"a")
    second = seed_dir / "b.bin"
    second.write_bytes(b"b")
    import os

    os.utime(first, (2000.0, 2000.0))
    os.utime(second, (2005.0, 2005.0))

    normalized = normalize_seed_inputs(seed_dir, base_time=None)

    assert [item.relative_time for item in normalized] == [0.0, 5.0]


def test_aggregate_line_coverage_buckets_builds_cumulative_curve() -> None:
    inputs = [
        TimedCoverageInput(
            content_hash="a",
            original_name="a.bin",
            path=Path("/tmp/a.bin"),
            relative_time=0.2,
            size=1,
            lines_covered=2,
        ),
        TimedCoverageInput(
            content_hash="b",
            original_name="b.bin",
            path=Path("/tmp/b.bin"),
            relative_time=1.2,
            size=1,
            lines_covered=4,
        ),
        TimedCoverageInput(
            content_hash="c",
            original_name="c.bin",
            path=Path("/tmp/c.bin"),
            relative_time=2.0,
            size=1,
            lines_covered=4,
        ),
    ]
    buckets = aggregate_line_coverage_buckets(
        inputs,
        lines_total=10,
        bucket_size_seconds=1,
    )

    assert [(bucket.bucket_start, bucket.inputs_seen) for bucket in buckets] == [
        (0.0, 1),
        (1.0, 2),
        (2.0, 3),
    ]
    assert [bucket.lines_covered for bucket in buckets] == [2, 4, 4]
    assert [bucket.lines_percent for bucket in buckets] == [20.0, 40.0, 40.0]


def test_aggregate_line_coverage_buckets_omits_empty_time_gaps() -> None:
    inputs = [
        TimedCoverageInput(
            content_hash="a",
            original_name="a.bin",
            path=Path("/tmp/a.bin"),
            relative_time=0.2,
            size=1,
            lines_covered=2,
        ),
        TimedCoverageInput(
            content_hash="b",
            original_name="b.bin",
            path=Path("/tmp/b.bin"),
            relative_time=5.1,
            size=1,
            lines_covered=4,
        ),
    ]

    buckets = aggregate_line_coverage_buckets(
        inputs,
        lines_total=10,
        bucket_size_seconds=1,
    )

    assert [(bucket.bucket_start, bucket.inputs_seen) for bucket in buckets] == [
        (0.0, 1),
        (5.0, 2),
    ]
    assert [bucket.lines_covered for bucket in buckets] == [2, 4]


def test_timeline_reporting_writes_json_csv_and_png(tmp_path: Path) -> None:
    report = CoverageTimelineReport(
        benchmark="sanity-mock-c-delta-01",
        harness="fuzz_parse_buffer_section",
        bucket_size_seconds=1,
        time_origin="crs_run_start_time",
        seeds=[
            TimedCoverageInput(
                content_hash="abc",
                original_name="a.bin",
                path=Path("/tmp/a.bin"),
                relative_time=1.0,
                size=1,
                lines_covered=5,
            )
        ],
        pov_markers=[
            CoveragePovMarker(cpv_id="cpv_0", pov_hash="hash-a", relative_time=1.5)
        ],
        buckets=[
            CoverageTimelineBucket(
                bucket_start=1.0,
                bucket_end=2.0,
                inputs_seen=1,
                lines_covered=5,
                lines_total=10,
                lines_percent=50.0,
            )
        ],
        final_summary=CoverageSummary(lines_covered=5, lines_total=10),
    )

    json_path = tmp_path / "coverage_timeline.json"
    csv_path = tmp_path / "coverage_timeline.csv"
    png_path = tmp_path / "coverage_timeline.png"

    write_timeline_json(report, json_path)
    write_timeline_csv(report, csv_path)
    write_timeline_png(report, png_path)

    assert json.loads(json_path.read_text())["benchmark"] == "sanity-mock-c-delta-01"
    assert csv_path.read_text().startswith(
        "bucket_start,bucket_end,inputs_seen,lines_covered,lines_total,lines_percent"
    )
    assert png_path.exists()
    assert png_path.stat().st_size > 0


def test_timeline_png_uses_covered_line_counts_on_y_axis(tmp_path: Path) -> None:
    report = CoverageTimelineReport(
        benchmark="sanity-mock-c-delta-01",
        harness="fuzz_parse_buffer_section",
        bucket_size_seconds=1,
        buckets=[
            CoverageTimelineBucket(
                bucket_start=1.0,
                bucket_end=2.0,
                inputs_seen=1,
                lines_covered=5,
                lines_total=10,
                lines_percent=50.0,
            )
        ],
    )

    observed: dict[str, object] = {}

    class _FakeAxes:
        def step(self, x_values, y_values, **kwargs):
            observed["step"] = (list(x_values), list(y_values), kwargs)

        def set_xlim(self, **kwargs):
            observed["xlim"] = kwargs

        def set_title(self, value):
            observed["title"] = value

        def set_xlabel(self, value):
            observed["xlabel"] = value

        def set_ylabel(self, value):
            observed["ylabel"] = value

        def set_ylim(self, *args):
            observed["ylim"] = args

        def grid(self, **kwargs):
            observed["grid"] = kwargs

        def legend(self, **kwargs):
            observed["legend"] = kwargs

    class _FakeFigure:
        def tight_layout(self):
            observed["tight_layout"] = True

        def savefig(self, path):
            Path(path).write_bytes(b"fake-png")
            observed["savefig"] = str(path)

    fake_matplotlib = ModuleType("matplotlib")
    fake_matplotlib.use = lambda *_args, **_kwargs: None
    fake_pyplot = ModuleType("matplotlib.pyplot")
    fake_pyplot.subplots = lambda **_kwargs: (_FakeFigure(), _FakeAxes())
    fake_pyplot.close = lambda _fig: None

    with patch.dict(
        sys.modules,
        {
            "matplotlib": fake_matplotlib,
            "matplotlib.pyplot": fake_pyplot,
        },
    ):
        write_timeline_png(report, tmp_path / "timeline.png")

    assert observed["step"] == (
        [2.0],
        [5],
        {"where": "post", "label": "Covered lines", "linewidth": 2},
    )
    assert observed["ylabel"] == "Covered lines"


def test_run_coverage_rejects_experiment_config_with_benchmarks_and_harness(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "experiment.yaml"
    config_path.write_text("experiment: test\nexperiment_filestore: /tmp/out\n")

    args = argparse.Namespace(
        verbose=False,
        experiment_config=config_path,
        experiment_dir=None,
        benchmark_path=None,
        corpus_dir=None,
        seed_dir=None,
        bucket_size_seconds=1,
        benchmark=None,
        benchmarks=tmp_path / "benchmarks",
        harness="fuzz_parse_buffer_section",
        oss_fuzz_path=None,
        force_rebuild=False,
        output=None,
        format="json",
        build_workers=None,
        verify_workers=None,
        source="pkgs",
        output_dir=None,
    )

    assert run_coverage(args) == 1


def test_run_coverage_rejects_non_positive_bucket_size(tmp_path: Path) -> None:
    args = argparse.Namespace(
        verbose=False,
        experiment_config=tmp_path / "experiment.yaml",
        experiment_dir=None,
        benchmark_path=None,
        corpus_dir=None,
        seed_dir=None,
        bucket_size_seconds=0,
        benchmark=None,
        benchmarks=None,
        harness=None,
        oss_fuzz_path=None,
        force_rebuild=False,
        output=None,
        format="json",
        build_workers=None,
        verify_workers=None,
        source="pkgs",
        output_dir=None,
    )

    assert run_coverage(args) == 1


def test_run_coverage_direct_seed_mode_requires_benchmark(tmp_path: Path) -> None:
    seed_dir = tmp_path / "seeds"
    seed_dir.mkdir()

    args = argparse.Namespace(
        verbose=False,
        experiment_config=None,
        experiment_dir=None,
        benchmark_path=None,
        corpus_dir=None,
        seed_dir=seed_dir,
        bucket_size_seconds=1,
        benchmark=None,
        benchmarks=None,
        harness="fuzz_parse_buffer_section",
        oss_fuzz_path=None,
        force_rebuild=False,
        output=None,
        format="json",
        build_workers=None,
        verify_workers=None,
        source="pkgs",
        output_dir=tmp_path / "out",
    )

    assert run_coverage(args) == 1


def test_coverage_parser_accepts_experiment_config_and_direct_seed_modes() -> None:
    parser = argparse.ArgumentParser(prog="crsbench")
    subs = parser.add_subparsers(dest="command")
    add_coverage_subparser(subs)

    experiment_args = parser.parse_args(
        [
            "coverage",
            "--experiment-config",
            "experiment-configs/experiment-config-sanity.yaml",
        ]
    )
    assert experiment_args.command == "coverage"
    assert experiment_args.experiment_config == Path(
        "experiment-configs/experiment-config-sanity.yaml"
    )
    assert experiment_args.experiment_dir is None

    experiment_dir_args = parser.parse_args(
        [
            "coverage",
            "--experiment-dir",
            "/tmp/experiment-output",
        ]
    )
    assert experiment_dir_args.command == "coverage"
    assert experiment_dir_args.experiment_dir == Path("/tmp/experiment-output")
    assert experiment_dir_args.experiment_config is None

    seed_args = parser.parse_args(
        [
            "coverage",
            "--seed-dir",
            "/tmp/seeds",
            "--benchmarks",
            "benchmarks",
            "--benchmark",
            "sanity-mock-c-delta-01",
            "--harness",
            "fuzz_parse_buffer_section",
            "--output-dir",
            "/tmp/out",
            "--jobs",
            "3",
            "--cores-per-job",
            "2",
        ]
    )
    assert seed_args.seed_dir == Path("/tmp/seeds")
    assert seed_args.benchmarks == Path("benchmarks")
    assert seed_args.benchmark == "sanity-mock-c-delta-01"
    assert seed_args.harness == "fuzz_parse_buffer_section"
    assert seed_args.output_dir == Path("/tmp/out")
    assert seed_args.jobs == 3
    assert seed_args.cores_per_job == 2
    assert not hasattr(seed_args, "benchmark_path")
    assert not hasattr(seed_args, "corpus_dir")
    assert not hasattr(seed_args, "output")
    assert not hasattr(seed_args, "format")


def test_coverage_parser_rejects_legacy_direct_mode() -> None:
    parser = argparse.ArgumentParser(prog="crsbench")
    subs = parser.add_subparsers(dest="command")
    add_coverage_subparser(subs)

    with patch.object(parser, "exit", side_effect=SystemExit) as _mock_exit:
        try:
            parser.parse_args(
                [
                    "coverage",
                    "benchmarks/sanity-mock-c-delta-01",
                    "--corpus-dir",
                    "./corpus",
                ]
            )
        except SystemExit:
            pass
        else:
            raise AssertionError("legacy coverage CLI unexpectedly parsed")


def test_coverage_parser_rejects_legacy_oss_fuzz_override() -> None:
    parser = argparse.ArgumentParser(prog="crsbench")
    subs = parser.add_subparsers(dest="command")
    add_coverage_subparser(subs)

    with patch.object(parser, "exit", side_effect=SystemExit) as _mock_exit:
        try:
            parser.parse_args(
                [
                    "coverage",
                    "--seed-dir",
                    "/tmp/seeds",
                    "--benchmark",
                    "sanity-mock-c-delta-01",
                    "--harness",
                    "fuzz_parse_buffer_section",
                    "--output-dir",
                    "/tmp/out",
                    "--oss-fuzz-path",
                    "/tmp/oss-fuzz",
                ]
            )
        except SystemExit:
            pass
        else:
            raise AssertionError("legacy --oss-fuzz-path unexpectedly parsed")


def test_coverage_parser_accepts_legacy_worker_aliases() -> None:
    parser = argparse.ArgumentParser(prog="crsbench")
    subs = parser.add_subparsers(dest="command")
    add_coverage_subparser(subs)

    args = parser.parse_args(
        [
            "coverage",
            "--seed-dir",
            "/tmp/seeds",
            "--benchmarks",
            "benchmarks",
            "--benchmark",
            "sanity-mock-c-delta-01",
            "--harness",
            "fuzz_parse_buffer_section",
            "--output-dir",
            "/tmp/out",
            "--build-workers",
            "4",
            "--verify-workers",
            "2",
        ]
    )

    assert args.jobs is None
    assert args.cores_per_job is None
    assert args.build_workers == 4
    assert args.verify_workers == 2


def test_run_coverage_rejects_experiment_config_with_experiment_dir(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "experiment.yaml"
    config_path.write_text("experiment: test\nexperiment_filestore: /tmp/out\n")

    args = argparse.Namespace(
        verbose=False,
        experiment_config=config_path,
        experiment_dir=tmp_path / "experiment-output",
        benchmark_path=None,
        corpus_dir=None,
        seed_dir=None,
        bucket_size_seconds=1,
        benchmark=None,
        benchmarks=None,
        harness=None,
        oss_fuzz_path=None,
        force_rebuild=False,
        output=None,
        format="json",
        jobs=None,
        cores_per_job=None,
        build_workers=None,
        verify_workers=None,
        source="pkgs",
        output_dir=None,
        atlantis_root=None,
    )

    assert run_coverage(args) == 1


def test_run_coverage_rejects_conflicting_job_flags(tmp_path: Path) -> None:
    args = argparse.Namespace(
        verbose=False,
        experiment_config=None,
        experiment_dir=None,
        benchmark_path=None,
        corpus_dir=None,
        seed_dir=tmp_path / "seeds",
        bucket_size_seconds=1,
        benchmark="bench-a",
        benchmarks=tmp_path / "benchmarks",
        harness="h0",
        oss_fuzz_path=None,
        force_rebuild=False,
        output=None,
        format="json",
        jobs=2,
        cores_per_job=1,
        build_workers=3,
        verify_workers=1,
        source="pkgs",
        output_dir=tmp_path / "out",
        atlantis_root=None,
    )

    assert run_coverage(args) == 1


def test_run_coverage_rejects_non_positive_cores_per_job(tmp_path: Path) -> None:
    args = argparse.Namespace(
        verbose=False,
        experiment_config=None,
        experiment_dir=None,
        benchmark_path=None,
        corpus_dir=None,
        seed_dir=tmp_path / "seeds",
        bucket_size_seconds=1,
        benchmark="bench-a",
        benchmarks=tmp_path / "benchmarks",
        harness="h0",
        oss_fuzz_path=None,
        force_rebuild=False,
        output=None,
        format="json",
        jobs=1,
        cores_per_job=0,
        build_workers=None,
        verify_workers=None,
        source="pkgs",
        output_dir=tmp_path / "out",
        atlantis_root=None,
    )

    assert run_coverage(args) == 1


def test_run_coverage_allows_experiment_dir_with_benchmarks_override(
    tmp_path: Path,
) -> None:
    args = argparse.Namespace(
        verbose=False,
        experiment_config=None,
        experiment_dir=tmp_path / "experiment-output",
        benchmark_path=None,
        corpus_dir=None,
        seed_dir=None,
        bucket_size_seconds=1,
        benchmark=None,
        benchmarks=tmp_path / "benchmarks",
        harness=None,
        oss_fuzz_path=None,
        force_rebuild=False,
        output=None,
        format="json",
        jobs=None,
        cores_per_job=None,
        build_workers=None,
        verify_workers=None,
        source="pkgs",
        output_dir=None,
        atlantis_root=None,
    )

    with patch(
        "crsbench.evaluation.coverage.cli.coverage_command._run_experiment_timeline",
        return_value=0,
    ) as mock_run:
        assert run_coverage(args) == 0

    mock_run.assert_called_once_with(args)


def test_run_experiment_timeline_uses_jobs_and_cores_per_job(
    tmp_path: Path,
) -> None:
    from crsbench.evaluation.coverage.models import CoverageTimelineReport
    from crsbench.evaluation.coverage.timeline import TrialCoverageContext

    experiment_dir = tmp_path / "experiment-output"
    benchmark_root = tmp_path / "benchmarks"
    benchmark_dir = benchmark_root / "bench-a"
    benchmark_dir.mkdir(parents=True)

    trial_dirs = []
    for index in range(3):
        trial_dir = experiment_dir / f"trial-{index}"
        trial_dir.mkdir(parents=True)
        (trial_dir / "metadata.json").write_text("{}")
        trial_dirs.append(trial_dir)

    args = argparse.Namespace(
        verbose=False,
        experiment_config=None,
        experiment_dir=experiment_dir,
        benchmark_path=None,
        corpus_dir=None,
        seed_dir=None,
        bucket_size_seconds=1,
        benchmark=None,
        benchmarks=benchmark_root,
        harness=None,
        force_rebuild=False,
        output=None,
        format="json",
        jobs=2,
        cores_per_job=2,
        build_workers=2,
        verify_workers=2,
        source="pkgs",
        output_dir=None,
    )

    contexts = {
        trial_dir: TrialCoverageContext(
            trial_dir=trial_dir,
            benchmark="bench-a",
            harness=f"h{index}",
            seed_dir=trial_dir / "output" / "seeds",
            crs_run_start_time=None,
            pov_markers=[],
        )
        for index, trial_dir in enumerate(trial_dirs)
    }
    for context in contexts.values():
        context.seed_dir.mkdir(parents=True)

    allocations: list[tuple[int, ...]] = []
    max_active = 0
    active = 0
    lock = threading.Lock()

    class _FakeEngine:
        def __init__(
            self,
            *,
            build_workers: int | None = None,
            runtime_workers: int | None = None,
            runtime_cpus: list[int] | None = None,
            source_mode: str = "pkgs",
        ):
            del build_workers, source_mode
            self.runtime_workers = runtime_workers
            self.runtime_cpus = runtime_cpus

        def cleanup(self) -> None:
            return None

    def _fake_build_timeline_report(**kwargs):
        nonlocal active, max_active
        engine = kwargs["engine"]
        with lock:
            active += 1
            max_active = max(max_active, active)
            allocations.append(tuple(engine.runtime_cpus or ()))
        time.sleep(0.05)
        with lock:
            active -= 1
        return CoverageTimelineReport(
            benchmark="bench-a",
            harness=kwargs["harness_name"],
            bucket_size_seconds=1,
            time_origin="first_seed_mtime",
            seeds=[],
            pov_markers=[],
            buckets=[],
            final_summary=CoverageSummary(lines_covered=0, lines_total=0),
        )

    with (
        patch(
            "crsbench.evaluation.coverage.cli.coverage_command.CoverageEngine",
            _FakeEngine,
        ),
        patch(
            "crsbench.evaluation.coverage.cli.coverage_command.load_trial_context",
            side_effect=lambda trial_dir: contexts[trial_dir],
        ),
        patch(
            "crsbench.evaluation.coverage.cli.coverage_command.resolve_benchmark_path",
            return_value=benchmark_dir,
        ),
        patch(
            "crsbench.evaluation.coverage.cli.coverage_command._build_timeline_report",
            side_effect=_fake_build_timeline_report,
        ),
        patch(
            "crsbench.evaluation.coverage.cli.coverage_command._write_timeline_outputs"
        ),
        patch(
            "crsbench.evaluation.coverage.cli.coverage_command._available_coverage_cpus",
            return_value=[10, 11, 12, 13],
        ),
    ):
        assert run_coverage(args) == 0

    assert len(allocations) == 3
    assert all(len(allocation) == 2 for allocation in allocations)
    assert max_active == 2
    assert set(allocations).issubset({(10, 11), (12, 13)})


def test_run_experiment_timeline_rejects_cores_per_job_larger_than_cpu_pool(
    tmp_path: Path,
) -> None:
    experiment_dir = tmp_path / "experiment-output"
    benchmark_root = tmp_path / "benchmarks"
    experiment_dir.mkdir()
    benchmark_root.mkdir()
    trial_dir = experiment_dir / "trial-0"
    trial_dir.mkdir()
    (trial_dir / "metadata.json").write_text("{}")

    args = argparse.Namespace(
        verbose=False,
        experiment_config=None,
        experiment_dir=experiment_dir,
        benchmark_path=None,
        corpus_dir=None,
        seed_dir=None,
        bucket_size_seconds=1,
        benchmark=None,
        benchmarks=benchmark_root,
        harness=None,
        force_rebuild=False,
        output=None,
        format="json",
        jobs=1,
        cores_per_job=2,
        build_workers=1,
        verify_workers=2,
        source="pkgs",
        output_dir=None,
    )

    with patch(
        "crsbench.evaluation.coverage.cli.coverage_command._available_coverage_cpus",
        return_value=[0],
    ):
        assert run_coverage(args) == 1


def test_run_direct_seed_timeline_pins_runtime_cpus(
    tmp_path: Path,
) -> None:
    seed_dir = tmp_path / "seeds"
    seed_dir.mkdir()
    (seed_dir / "a.bin").write_bytes(b"a")

    benchmark_root = tmp_path / "benchmarks"
    benchmark_dir = benchmark_root / "bench-a"
    benchmark_dir.mkdir(parents=True)

    engine_inits: list[dict] = []

    class _FakeEngine:
        def __init__(
            self,
            *,
            build_workers: int | None = None,
            runtime_workers: int | None = None,
            runtime_cpus: list[int] | None = None,
            source_mode: str = "pkgs",
        ):
            engine_inits.append(
                {
                    "build_workers": build_workers,
                    "runtime_workers": runtime_workers,
                    "runtime_cpus": runtime_cpus,
                    "source_mode": source_mode,
                }
            )

        def collect_timed_line_coverage(self, *args, **kwargs):
            del args, kwargs
            return [], CoverageSummary(lines_covered=0, lines_total=0)

        def cleanup(self) -> None:
            return None

    args = argparse.Namespace(
        verbose=False,
        experiment_config=None,
        experiment_dir=None,
        benchmark_path=None,
        corpus_dir=None,
        seed_dir=seed_dir,
        bucket_size_seconds=1,
        benchmark="bench-a",
        benchmarks=benchmark_root,
        harness="fuzz_target",
        force_rebuild=False,
        output=None,
        format="json",
        jobs=1,
        cores_per_job=2,
        build_workers=1,
        verify_workers=2,
        source="pkgs",
        output_dir=tmp_path / "coverage-out",
    )

    with (
        patch(
            "crsbench.evaluation.coverage.cli.coverage_command.CoverageEngine",
            _FakeEngine,
        ),
        patch(
            "crsbench.evaluation.coverage.cli.coverage_command.resolve_benchmark_path",
            return_value=benchmark_dir,
        ),
        patch(
            "crsbench.evaluation.coverage.cli.coverage_command._write_timeline_outputs"
        ),
        patch(
            "crsbench.evaluation.coverage.cli.coverage_command._available_coverage_cpus",
            return_value=[4, 5, 6, 7],
        ),
    ):
        assert _run_direct_seed_timeline(args) == 0

    assert engine_inits == [
        {
            "build_workers": 1,
            "runtime_workers": 2,
            "runtime_cpus": [4, 5],
            "source_mode": "pkgs",
        }
    ]


def test_run_direct_seed_timeline_accepts_legacy_worker_aliases(
    tmp_path: Path,
) -> None:
    seed_dir = tmp_path / "seeds"
    seed_dir.mkdir()
    (seed_dir / "a.bin").write_bytes(b"a")

    benchmark_root = tmp_path / "benchmarks"
    benchmark_dir = benchmark_root / "bench-a"
    benchmark_dir.mkdir(parents=True)

    engine_inits: list[dict] = []

    class _FakeEngine:
        def __init__(
            self,
            *,
            build_workers: int | None = None,
            runtime_workers: int | None = None,
            runtime_cpus: list[int] | None = None,
            source_mode: str = "pkgs",
        ):
            engine_inits.append(
                {
                    "build_workers": build_workers,
                    "runtime_workers": runtime_workers,
                    "runtime_cpus": runtime_cpus,
                    "source_mode": source_mode,
                }
            )

        def collect_timed_line_coverage(self, *args, **kwargs):
            del args, kwargs
            return [], CoverageSummary(lines_covered=0, lines_total=0)

        def cleanup(self) -> None:
            return None

    args = argparse.Namespace(
        verbose=False,
        experiment_config=None,
        experiment_dir=None,
        benchmark_path=None,
        corpus_dir=None,
        seed_dir=seed_dir,
        bucket_size_seconds=1,
        benchmark="bench-a",
        benchmarks=benchmark_root,
        harness="fuzz_target",
        force_rebuild=False,
        output=None,
        format="json",
        jobs=None,
        cores_per_job=None,
        build_workers=3,
        verify_workers=2,
        source="pkgs",
        output_dir=tmp_path / "coverage-out",
    )

    with (
        patch(
            "crsbench.evaluation.coverage.cli.coverage_command.CoverageEngine",
            _FakeEngine,
        ),
        patch(
            "crsbench.evaluation.coverage.cli.coverage_command.resolve_benchmark_path",
            return_value=benchmark_dir,
        ),
        patch(
            "crsbench.evaluation.coverage.cli.coverage_command._write_timeline_outputs"
        ),
        patch(
            "crsbench.evaluation.coverage.cli.coverage_command._available_coverage_cpus",
            return_value=[6, 7, 8, 9],
        ),
    ):
        assert _run_direct_seed_timeline(args) == 0

    assert engine_inits == [
        {
            "build_workers": 3,
            "runtime_workers": 2,
            "runtime_cpus": [6, 7],
            "source_mode": "pkgs",
        }
    ]
