"""Tests for coverage fixes on fix/coverage-tmp-workspace-bloat branch.

Covers:
1. _read_project_sanitizer — sanitizer from project.yaml
2. resolve_harness — chmod +x non-executable binaries
3. clamp_negative_to_zero for experiment_start_time
4. skip-if-done for experiment-dir resumability
5. oss_fuzz_path passed to CoverageEngine
6. relaxed run_time requirement
"""

from __future__ import annotations

import argparse
import os
import stat
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from crsbench.evaluation.coverage.cli.coverage_command import (
    _build_timeline_report,
    _run_experiment_timeline,
)
from crsbench.evaluation.coverage.engine import _CoverageBuildWorkspace
from crsbench.evaluation.coverage.timeline import normalize_seed_inputs
from crsbench.evaluation.coverage.uniafl_runtime import _read_project_sanitizer

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# 1. _read_project_sanitizer
# ---------------------------------------------------------------------------


class TestReadProjectSanitizer:
    def test_returns_address_when_no_project_yaml(self, tmp_path: Path) -> None:
        assert _read_project_sanitizer(tmp_path) == "address"

    def test_returns_address_when_sanitizers_missing(self, tmp_path: Path) -> None:
        (tmp_path / "project.yaml").write_text("language: c\n")
        assert _read_project_sanitizer(tmp_path) == "address"

    def test_returns_address_sanitizer(self, tmp_path: Path) -> None:
        (tmp_path / "project.yaml").write_text(
            "language: c\nsanitizers:\n  - address\n"
        )
        assert _read_project_sanitizer(tmp_path) == "address"

    def test_returns_undefined_sanitizer(self, tmp_path: Path) -> None:
        (tmp_path / "project.yaml").write_text(
            "language: c\nsanitizers:\n  - undefined\n"
        )
        assert _read_project_sanitizer(tmp_path) == "undefined"

    def test_returns_first_sanitizer_from_list(self, tmp_path: Path) -> None:
        (tmp_path / "project.yaml").write_text(
            "language: c\nsanitizers:\n  - undefined\n  - address\n"
        )
        assert _read_project_sanitizer(tmp_path) == "undefined"

    def test_returns_address_for_empty_sanitizers_list(self, tmp_path: Path) -> None:
        (tmp_path / "project.yaml").write_text("language: c\nsanitizers: []\n")
        assert _read_project_sanitizer(tmp_path) == "address"

    def test_returns_address_for_invalid_yaml(self, tmp_path: Path) -> None:
        (tmp_path / "project.yaml").write_text("{{invalid yaml")
        assert _read_project_sanitizer(tmp_path) == "address"

    def test_returns_address_for_non_dict_yaml(self, tmp_path: Path) -> None:
        (tmp_path / "project.yaml").write_text("just a string\n")
        assert _read_project_sanitizer(tmp_path) == "address"

    def test_returns_address_for_non_list_sanitizers(self, tmp_path: Path) -> None:
        (tmp_path / "project.yaml").write_text("language: c\nsanitizers: address\n")
        assert _read_project_sanitizer(tmp_path) == "address"


# ---------------------------------------------------------------------------
# 2. resolve_harness — chmod +x non-executable binaries
# ---------------------------------------------------------------------------


class TestResolveHarness:
    def _make_workspace(self, tmp_path: Path) -> _CoverageBuildWorkspace:
        return _CoverageBuildWorkspace(legacy_root=None, work_dir=tmp_path)

    def test_returns_harness_name_unchanged(self, tmp_path: Path) -> None:
        ws = self._make_workspace(tmp_path)
        variant = "test-variant"
        build_out = ws.get_build_output_path(variant)
        build_out.mkdir(parents=True)
        harness = build_out / "fuzz_target"
        harness.write_bytes(b"\x7fELF")
        harness.chmod(0o755)

        result = ws.resolve_harness(variant, "fuzz_target")
        assert result == "fuzz_target"

    def test_makes_non_executable_binary_executable(self, tmp_path: Path) -> None:
        ws = self._make_workspace(tmp_path)
        variant = "test-variant"
        build_out = ws.get_build_output_path(variant)
        build_out.mkdir(parents=True)
        harness = build_out / "fuzz_target"
        harness.write_bytes(b"\x7fELF")
        harness.chmod(0o644)  # not executable

        assert not os.access(harness, os.X_OK)
        ws.resolve_harness(variant, "fuzz_target")
        assert os.access(harness, os.X_OK)

    def test_preserves_existing_permissions(self, tmp_path: Path) -> None:
        ws = self._make_workspace(tmp_path)
        variant = "test-variant"
        build_out = ws.get_build_output_path(variant)
        build_out.mkdir(parents=True)
        harness = build_out / "fuzz_target"
        harness.write_bytes(b"\x7fELF")
        harness.chmod(0o640)

        ws.resolve_harness(variant, "fuzz_target")
        mode = harness.stat().st_mode
        # Original rw-r----- plus execute bits = rwxr-x--x
        assert mode & stat.S_IRUSR
        assert mode & stat.S_IWUSR
        assert mode & stat.S_IXUSR
        assert mode & stat.S_IRGRP
        assert mode & stat.S_IXGRP

    def test_no_op_when_harness_missing(self, tmp_path: Path) -> None:
        ws = self._make_workspace(tmp_path)
        variant = "test-variant"
        build_out = ws.get_build_output_path(variant)
        build_out.mkdir(parents=True)

        result = ws.resolve_harness(variant, "nonexistent")
        assert result == "nonexistent"

    def test_no_op_when_already_executable(self, tmp_path: Path) -> None:
        ws = self._make_workspace(tmp_path)
        variant = "test-variant"
        build_out = ws.get_build_output_path(variant)
        build_out.mkdir(parents=True)
        harness = build_out / "fuzz_target"
        harness.write_bytes(b"\x7fELF")
        harness.chmod(0o755)
        original_mode = harness.stat().st_mode

        ws.resolve_harness(variant, "fuzz_target")
        assert harness.stat().st_mode == original_mode


# ---------------------------------------------------------------------------
# 3. clamp_negative_to_zero for experiment_start_time
# ---------------------------------------------------------------------------


class TestClampNegativeToZero:
    def test_seeds_before_start_time_dropped_without_clamp(
        self, tmp_path: Path
    ) -> None:
        seed = tmp_path / "seed.bin"
        seed.write_bytes(b"data")
        os.utime(seed, (50.0, 50.0))  # mtime before base_time

        result = normalize_seed_inputs(
            tmp_path, base_time=100.0, clamp_negative_to_zero=False
        )
        assert len(result) == 0

    def test_seeds_before_start_time_clamped_to_zero(self, tmp_path: Path) -> None:
        seed = tmp_path / "seed.bin"
        seed.write_bytes(b"data")
        os.utime(seed, (50.0, 50.0))  # mtime before base_time

        result = normalize_seed_inputs(
            tmp_path, base_time=100.0, clamp_negative_to_zero=True
        )
        assert len(result) == 1
        assert result[0].relative_time == 0.0

    def test_seeds_after_start_time_unaffected_by_clamp(self, tmp_path: Path) -> None:
        seed = tmp_path / "seed.bin"
        seed.write_bytes(b"data")
        os.utime(seed, (150.0, 150.0))

        result = normalize_seed_inputs(
            tmp_path, base_time=100.0, clamp_negative_to_zero=True
        )
        assert len(result) == 1
        assert result[0].relative_time == pytest.approx(50.0)

    def test_experiment_start_time_origin_clamps(self, tmp_path: Path) -> None:
        """_build_timeline_report with experiment_start_time clamps negatives."""
        seed = tmp_path / "seed.bin"
        seed.write_bytes(b"data")
        os.utime(seed, (99.0, 99.0))  # 1s before base_time

        from crsbench.evaluation.coverage.models import CoverageSummary

        collected_inputs = []

        class _FakeEngine:
            def collect_timed_line_coverage(self, **kwargs):
                collected_inputs.extend(kwargs["timed_inputs"])
                return kwargs["timed_inputs"], CoverageSummary()

        _build_timeline_report(
            engine=_FakeEngine(),
            benchmark_path=tmp_path / "bench",
            harness_name="fuzz",
            seed_dir=tmp_path,
            time_origin_base=100.0,
            time_origin="experiment_start_time",
            pov_markers=[],
            timeline_duration_seconds=None,
            force_rebuild=False,
            output_dir=tmp_path / "out",
        )
        assert len(collected_inputs) == 1
        assert collected_inputs[0].relative_time == 0.0


# ---------------------------------------------------------------------------
# 4. skip-if-done for experiment-dir resumability
# ---------------------------------------------------------------------------


class TestSkipIfDone:
    def _make_trial(
        self,
        experiment_dir: Path,
        name: str,
        *,
        has_seeds: bool = True,
        has_coverage: bool = False,
    ) -> Path:
        trial_dir = experiment_dir / name
        trial_dir.mkdir(parents=True)
        (trial_dir / "metadata.json").write_text("{}")
        if has_seeds:
            seed_dir = trial_dir / "output" / "seeds"
            seed_dir.mkdir(parents=True)
            (seed_dir / "seed.bin").write_bytes(b"data")
        if has_coverage:
            cov_dir = trial_dir / "coverage"
            cov_dir.mkdir(parents=True)
            (cov_dir / "coverage_timeline.json").write_text("{}")
        return trial_dir

    def test_skips_trials_with_existing_coverage(self, tmp_path: Path) -> None:
        from crsbench.evaluation.coverage.timeline import TrialCoverageContext

        experiment_dir = tmp_path / "experiment"
        benchmark_root = tmp_path / "benchmarks"
        benchmark_dir = benchmark_root / "bench-a"
        benchmark_dir.mkdir(parents=True)

        # 2 done, 1 pending
        self._make_trial(experiment_dir, "trial-0", has_coverage=True)
        self._make_trial(experiment_dir, "trial-1", has_coverage=True)
        t2 = self._make_trial(experiment_dir, "trial-2", has_coverage=False)

        args = argparse.Namespace(
            verbose=False,
            experiment_config=None,
            experiment_dir=experiment_dir,
            benchmarks=benchmark_root,
            harness=None,
            force_rebuild=False,
            jobs=1,
            cores_per_job=1,
            source="pkgs",
            output_dir=None,
        )

        context = TrialCoverageContext(
            trial_dir=t2,
            benchmark="bench-a",
            harness="h0",
            seed_dir=t2 / "output" / "seeds",
            crs_run_start_time=100.0,
            timeline_duration_seconds=None,
            pov_markers=[],
        )

        trial_jobs_received = []

        def _fake_run_trial_jobs(**kwargs):
            trial_jobs_received.extend(kwargs["trial_jobs"])
            return len(kwargs["trial_jobs"])

        class _FakeEngine:
            def __init__(self, oss_fuzz_path=None, **kwargs):
                pass

            def cleanup(self):
                pass

        with (
            patch(
                "crsbench.evaluation.coverage.cli.coverage_command.load_trial_context",
                return_value=context,
            ),
            patch(
                "crsbench.evaluation.coverage.cli.coverage_command._validate_experiment_timeline_context",
                return_value=None,
            ),
            patch(
                "crsbench.evaluation.coverage.cli.coverage_command.resolve_benchmark_path",
                return_value=benchmark_dir,
            ),
            patch(
                "crsbench.evaluation.coverage.cli.coverage_command._run_trial_jobs",
                side_effect=_fake_run_trial_jobs,
            ),
            patch(
                "crsbench.evaluation.coverage.cli.coverage_command.ensure_oss_fuzz_root",
                return_value="/tmp/fake",
            ),
            patch(
                "crsbench.evaluation.coverage.cli.coverage_command._available_coverage_cpus",
                return_value=[0, 1],
            ),
        ):
            result = _run_experiment_timeline(args)

        assert result == 0
        # Only trial-2 should have been submitted (trial-0 and trial-1 skipped)
        assert len(trial_jobs_received) == 1
        assert trial_jobs_received[0][0] == t2

    def test_returns_zero_when_all_done(self, tmp_path: Path) -> None:
        experiment_dir = tmp_path / "experiment"
        benchmark_root = tmp_path / "benchmarks"
        (benchmark_root / "bench-a").mkdir(parents=True)

        self._make_trial(experiment_dir, "trial-0", has_coverage=True)
        self._make_trial(experiment_dir, "trial-1", has_coverage=True)

        args = argparse.Namespace(
            verbose=False,
            experiment_config=None,
            experiment_dir=experiment_dir,
            benchmarks=benchmark_root,
            harness=None,
            force_rebuild=False,
            jobs=1,
            cores_per_job=1,
            source="pkgs",
            output_dir=None,
        )

        with patch(
            "crsbench.evaluation.coverage.cli.coverage_command.ensure_oss_fuzz_root",
            return_value="/tmp/fake",
        ):
            result = _run_experiment_timeline(args)

        assert result == 0


# ---------------------------------------------------------------------------
# 5. oss_fuzz_path integration
# ---------------------------------------------------------------------------


class TestOssFuzzPathIntegration:
    def test_workspace_uses_legacy_root_when_set(self, tmp_path: Path) -> None:
        oss_fuzz = tmp_path / "oss-fuzz"
        ws = _CoverageBuildWorkspace(legacy_root=oss_fuzz, work_dir=None)
        path = ws.get_build_output_path("my-variant")
        assert str(oss_fuzz) in str(path)
        assert "my-variant" in str(path)

    def test_workspace_uses_default_root_without_legacy(self) -> None:
        ws = _CoverageBuildWorkspace(legacy_root=None, work_dir=None)
        path = ws.get_build_output_path("my-variant")
        assert ".crsbench-coverage" in str(path)

    def test_workspace_prefers_work_dir_over_legacy(self, tmp_path: Path) -> None:
        oss_fuzz = tmp_path / "oss-fuzz"
        work = tmp_path / "workdir"
        ws = _CoverageBuildWorkspace(legacy_root=oss_fuzz, work_dir=work)
        path = ws.get_build_output_path("my-variant")
        assert str(work) in str(path)


# ---------------------------------------------------------------------------
# 6. relaxed run_time requirement
# ---------------------------------------------------------------------------


class TestRelaxedRunTime:
    def test_missing_run_time_does_not_raise(self, tmp_path: Path) -> None:
        """timeline_duration_seconds=None accepted for crs_run_start_time origin."""
        seed = tmp_path / "seed.bin"
        seed.write_bytes(b"data")
        os.utime(seed, (200.0, 200.0))

        from crsbench.evaluation.coverage.models import CoverageSummary

        class _FakeEngine:
            def collect_timed_line_coverage(self, **kwargs):
                return kwargs["timed_inputs"], CoverageSummary()

        # Should not raise ValueError
        report = _build_timeline_report(
            engine=_FakeEngine(),
            benchmark_path=tmp_path / "bench",
            harness_name="fuzz",
            seed_dir=tmp_path,
            time_origin_base=100.0,
            time_origin="crs_run_start_time",
            pov_markers=[],
            timeline_duration_seconds=None,
            force_rebuild=False,
            output_dir=tmp_path / "out",
        )
        assert report.timeline_duration_seconds is None

    def test_missing_start_time_still_raises(self, tmp_path: Path) -> None:
        seed = tmp_path / "seed.bin"
        seed.write_bytes(b"data")

        class _FakeEngine:
            def collect_timed_line_coverage(self, **kwargs):
                raise AssertionError("should not be called")

        with pytest.raises(ValueError, match="crs_run_start_time"):
            _build_timeline_report(
                engine=_FakeEngine(),
                benchmark_path=tmp_path / "bench",
                harness_name="fuzz",
                seed_dir=tmp_path,
                time_origin_base=None,
                time_origin="crs_run_start_time",
                pov_markers=[],
                timeline_duration_seconds=120.0,
                force_rebuild=False,
                output_dir=tmp_path / "out",
            )
