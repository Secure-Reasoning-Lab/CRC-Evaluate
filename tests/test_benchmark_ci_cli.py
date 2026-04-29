"""Unit tests for benchmark CI CLI infrastructure modules.

Tests common_args, discovery, and output modules.
"""

import argparse
from pathlib import Path

import pytest
from crsbench.benchmark_ci.cli.common_args import (
    create_benchmark_selection_parent,
    create_build_options_parent,
    create_output_options_parent,
)
from crsbench.benchmark_ci.cli.discovery import (
    discover_benchmarks,
    get_benchmarks_root,
    load_benchmark_suite,
    resolve_benchmark_paths,
)
from crsbench.benchmark_ci.cli.output import (
    format_status,
    print_results_table,
)
from crsbench.benchmark_ci.models import (
    BenchmarkValidationResult,
    CheckMode,
    CheckResult,
    CheckStatus,
    ValidationSummary,
)

# ===== Tests for common_args.py =====


class TestBenchmarkSelectionParent:
    def test_all_flag(self):
        parser = create_benchmark_selection_parent()
        ns = parser.parse_args(["--all"])
        assert ns.all is True
        assert ns.benchmark is None

    def test_positional(self):
        parser = create_benchmark_selection_parent()
        ns = parser.parse_args(["benchmarks/my-project"])
        assert ns.benchmark == "benchmarks/my-project"
        assert ns.all is False

    def test_filter(self):
        parser = create_benchmark_selection_parent()
        ns = parser.parse_args(["--all", "--filter", "afc-*"])
        assert ns.filter == "afc-*"
        assert ns.all is True

    def test_filter_short(self):
        parser = create_benchmark_selection_parent()
        ns = parser.parse_args(["--all", "-f", "sanity-*"])
        assert ns.filter == "sanity-*"


class TestBuildOptionsParent:
    def test_defaults(self):
        parser = create_build_options_parent()
        ns = parser.parse_args([])
        assert ns.source == "pkgs"
        assert ns.exit_on_error is False

    def test_source_pkgs(self):
        parser = create_build_options_parent()
        ns = parser.parse_args(["--source", "pkgs"])
        assert ns.source == "pkgs"

    def test_exit_on_error(self):
        parser = create_build_options_parent()
        ns = parser.parse_args(["--exit-on-error"])
        assert ns.exit_on_error is True


class TestOutputOptionsParent:
    def test_defaults(self):
        parser = create_output_options_parent()
        ns = parser.parse_args([])
        assert ns.output is None
        assert ns.output_dir is None
        assert ns.no_color is False

    def test_no_color(self):
        parser = create_output_options_parent()
        ns = parser.parse_args(["--no-color"])
        assert ns.no_color is True

    def test_output_path(self):
        parser = create_output_options_parent()
        ns = parser.parse_args(["--output", "results.json"])
        assert ns.output == Path("results.json")

    def test_output_dir(self):
        parser = create_output_options_parent()
        ns = parser.parse_args(["--output-dir", "/tmp/results"])
        assert ns.output_dir == Path("/tmp/results")


class TestParentsComposition:
    def test_compose_without_conflict(self):
        """All three parent parsers compose without argparse conflicts."""
        parser = argparse.ArgumentParser(
            parents=[
                create_benchmark_selection_parent(),
                create_build_options_parent(),
                create_output_options_parent(),
            ]
        )
        ns = parser.parse_args(["--all", "--source", "pkgs", "--no-color"])
        assert ns.all is True
        assert ns.source == "pkgs"
        assert ns.no_color is True

    def test_compose_with_positional(self):
        """Positional arg works in composed parser."""
        parser = argparse.ArgumentParser(
            parents=[
                create_benchmark_selection_parent(),
                create_build_options_parent(),
                create_output_options_parent(),
            ]
        )
        ns = parser.parse_args(["benchmarks/test", "-o", "out.json"])
        assert ns.benchmark == "benchmarks/test"
        assert ns.output == Path("out.json")


# ===== Tests for discovery.py =====


class TestGetBenchmarksRoot:
    def test_from_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BENCHMARKS_ROOT", str(tmp_path))
        result = get_benchmarks_root()
        assert result == tmp_path

    def test_from_env_overrides_walk(self, tmp_path, monkeypatch):
        """Env var takes precedence over directory walking."""
        monkeypatch.setenv("BENCHMARKS_ROOT", str(tmp_path))
        result = get_benchmarks_root()
        assert result == tmp_path


class TestDiscoverBenchmarks:
    def test_finds_aixcc_dirs(self, tmp_path):
        # Create dirs with .aixcc marker
        (tmp_path / "project-a" / ".aixcc").mkdir(parents=True)
        (tmp_path / "project-b" / ".aixcc").mkdir(parents=True)
        # Create dir without .aixcc
        (tmp_path / "not-a-benchmark").mkdir()

        result = discover_benchmarks(tmp_path)
        assert len(result) == 2
        names = [p.name for p in result]
        assert "project-a" in names
        assert "project-b" in names
        assert "not-a-benchmark" not in names

    def test_skips_hidden(self, tmp_path):
        (tmp_path / ".hidden" / ".aixcc").mkdir(parents=True)
        (tmp_path / "visible" / ".aixcc").mkdir(parents=True)

        result = discover_benchmarks(tmp_path)
        assert len(result) == 1
        assert result[0].name == "visible"

    def test_applies_filter(self, tmp_path):
        (tmp_path / "afc-project-1" / ".aixcc").mkdir(parents=True)
        (tmp_path / "afc-project-2" / ".aixcc").mkdir(parents=True)
        (tmp_path / "sanity-mock" / ".aixcc").mkdir(parents=True)

        result = discover_benchmarks(tmp_path, filter_pattern="afc-*")
        assert len(result) == 2
        assert all(p.name.startswith("afc-") for p in result)

    def test_returns_sorted(self, tmp_path):
        (tmp_path / "zebra" / ".aixcc").mkdir(parents=True)
        (tmp_path / "alpha" / ".aixcc").mkdir(parents=True)
        (tmp_path / "middle" / ".aixcc").mkdir(parents=True)

        result = discover_benchmarks(tmp_path)
        names = [p.name for p in result]
        assert names == ["alpha", "middle", "zebra"]

    def test_empty_dir(self, tmp_path):
        result = discover_benchmarks(tmp_path)
        assert result == []

    def test_no_filter_returns_all(self, tmp_path):
        (tmp_path / "a" / ".aixcc").mkdir(parents=True)
        (tmp_path / "b" / ".aixcc").mkdir(parents=True)

        result = discover_benchmarks(tmp_path, filter_pattern=None)
        assert len(result) == 2


class TestResolveBenchmarkPaths:
    def test_all_flag(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BENCHMARKS_ROOT", str(tmp_path))
        (tmp_path / "proj-1" / ".aixcc").mkdir(parents=True)
        (tmp_path / "proj-2" / ".aixcc").mkdir(parents=True)

        result = resolve_benchmark_paths(all_benchmarks=True)
        assert len(result) == 2

    def test_all_with_filter(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BENCHMARKS_ROOT", str(tmp_path))
        (tmp_path / "afc-1" / ".aixcc").mkdir(parents=True)
        (tmp_path / "afc-2" / ".aixcc").mkdir(parents=True)
        (tmp_path / "other" / ".aixcc").mkdir(parents=True)

        result = resolve_benchmark_paths(all_benchmarks=True, filter_pattern="afc-*")
        assert len(result) == 2

    def test_bare_name(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BENCHMARKS_ROOT", str(tmp_path))
        (tmp_path / "my-project").mkdir()

        result = resolve_benchmark_paths("my-project")
        assert len(result) == 1
        assert result[0] == tmp_path / "my-project"

    def test_absolute_path(self, tmp_path):
        project_dir = tmp_path / "my-project"
        project_dir.mkdir()

        result = resolve_benchmark_paths(str(project_dir))
        assert len(result) == 1
        assert result[0] == project_dir

    def test_relative_path(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BENCHMARKS_ROOT", str(tmp_path))
        project_dir = tmp_path / "benchmarks" / "my-project"
        project_dir.mkdir(parents=True)
        monkeypatch.chdir(tmp_path)

        result = resolve_benchmark_paths("benchmarks/my-project")
        assert len(result) == 1
        assert result[0] == project_dir

    def test_no_args_exits(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BENCHMARKS_ROOT", str(tmp_path))
        with pytest.raises(SystemExit) as exc_info:
            resolve_benchmark_paths()
        assert exc_info.value.code == 1

    def test_nonexistent_path_exits(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BENCHMARKS_ROOT", str(tmp_path))
        with pytest.raises(SystemExit) as exc_info:
            resolve_benchmark_paths("nonexistent-project")
        assert exc_info.value.code == 1

    def test_all_no_matches_exits(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BENCHMARKS_ROOT", str(tmp_path))
        # Empty dir, no benchmarks
        with pytest.raises(SystemExit) as exc_info:
            resolve_benchmark_paths(all_benchmarks=True)
        assert exc_info.value.code == 1

    def test_benchmark_suite(self, tmp_path, monkeypatch):
        benchmarks_root = tmp_path / "benchmarks"
        suites_root = tmp_path / "benchmark-suites"
        monkeypatch.setenv("BENCHMARKS_ROOT", str(benchmarks_root))
        monkeypatch.setenv("BENCHMARK_SUITES_ROOT", str(suites_root))

        (benchmarks_root / "afc-one").mkdir(parents=True)
        (benchmarks_root / "afc-two").mkdir(parents=True)
        suites_root.mkdir()
        (suites_root / "sample.yaml").write_text(
            "Name: sample\n"
            "Description: Sample suite\n"
            "Release date: 01.01.2026\n"
            "benchmark_list:\n"
            "  - afc-one\n"
            "  - afc-two\n"
        )

        result = resolve_benchmark_paths(benchmark_suite="sample")
        assert result == [benchmarks_root / "afc-one", benchmarks_root / "afc-two"]


class TestLoadBenchmarkSuite:
    def test_explicit_suites_root(self, tmp_path):
        suites_root = tmp_path / "benchmark-suites"
        suites_root.mkdir()
        (suites_root / "custom.yaml").write_text(
            "Name: custom\n"
            "Description: Custom suite\n"
            "Release date: 01.01.2026\n"
            "benchmark_list:\n"
            "  - afc-one\n"
            "  - atlanta-two\n"
        )

        result = load_benchmark_suite("custom", suites_root=suites_root)
        assert result == ["afc-one", "atlanta-two"]


# ===== Tests for output.py =====


class TestFormatStatus:
    def test_none_returns_dash(self):
        result = format_status(None)
        assert result == "[dim]-[/dim]"

    def test_pass(self):
        check = CheckResult(status=CheckStatus.PASS, time_seconds=120.0)
        result = format_status(check)
        assert "[green]" in result
        assert "PASS(2m)" in result

    def test_pass_fallback(self):
        check = CheckResult(
            status=CheckStatus.PASS, time_seconds=480.0, fallback_used=True
        )
        result = format_status(check)
        assert "[yellow]" in result
        assert "PASS-FB" in result

    def test_fail(self):
        check = CheckResult(status=CheckStatus.FAIL, time_seconds=5.0)
        result = format_status(check)
        assert result == "[red]FAIL[/red]"

    def test_error(self):
        check = CheckResult(status=CheckStatus.ERROR, time_seconds=0.0, error="timeout")
        result = format_status(check)
        assert result == "[red]ERR[/red]"

    def test_skip(self):
        check = CheckResult.skip("not needed")
        result = format_status(check)
        assert result == "[dim]SKIP[/dim]"

    def test_skip_returns_skip(self):
        check = CheckResult.skip("not needed")
        result = format_status(check)
        assert result == "[dim]SKIP[/dim]"

    def test_pass_verify_only(self):
        """Verify-only columns show time without V: prefix."""
        check = CheckResult(
            status=CheckStatus.PASS, time_seconds=30.0, verify_time=30.0
        )
        result = format_status(check)
        assert "[green]" in result
        assert "PASS(30s)" in result

    def test_pass_build_only(self):
        """Build-only columns show time without B: prefix."""
        check = CheckResult(
            status=CheckStatus.PASS,
            time_seconds=120.0,
            build_time=120.0,
        )
        result = format_status(check)
        assert "[green]" in result
        assert "PASS(2m)" in result

    def test_pass_fallback_with_verify_time(self):
        check = CheckResult(
            status=CheckStatus.PASS,
            time_seconds=30.0,
            verify_time=30.0,
            fallback_used=True,
        )
        result = format_status(check)
        assert "[yellow]" in result
        assert "PASS-FB(30s)" in result


class TestPrintResultsTable:
    def _make_summary(self) -> ValidationSummary:
        """Create a sample ValidationSummary for testing."""
        summary = ValidationSummary()
        summary.add_result(
            BenchmarkValidationResult(
                benchmark="test-project-1",
                benchmark_path=Path("/tmp/test-project-1"),
                format_check=CheckResult(status=CheckStatus.PASS, time_seconds=1.0),
                pov_check=CheckResult(status=CheckStatus.PASS, time_seconds=60.0),
                patch_check=CheckResult(status=CheckStatus.FAIL, time_seconds=30.0),
            )
        )
        summary.add_result(
            BenchmarkValidationResult(
                benchmark="test-project-2",
                benchmark_path=Path("/tmp/test-project-2"),
                format_check=CheckResult(status=CheckStatus.PASS, time_seconds=0.5),
                pov_check=CheckResult(status=CheckStatus.PASS, time_seconds=120.0),
                patch_check=CheckResult(status=CheckStatus.PASS, time_seconds=180.0),
            )
        )
        return summary

    def test_default_mode(self, capsys):
        summary = self._make_summary()
        print_results_table(summary, no_color=True)
        captured = capsys.readouterr()
        assert "Benchmark Validation Report" in captured.out
        assert "test-project-1" in captured.out
        assert "test-project-2" in captured.out
        assert "FAIL" in captured.out
        assert "Summary:" in captured.out
        assert "1 passed" in captured.out
        assert "1 failed" in captured.out

    def test_inc_mode(self, capsys):
        summary = ValidationSummary()
        summary.add_result(
            BenchmarkValidationResult(
                benchmark="inc-test",
                benchmark_path=Path("/tmp/inc-test"),
                format_check=CheckResult(status=CheckStatus.PASS, time_seconds=1.0),
                pov_check=CheckResult.skip("inc mode"),
                patch_check=CheckResult.skip("inc mode"),
                pov_inc_check=CheckResult(status=CheckStatus.PASS, time_seconds=90.0),
                patch_inc_check=CheckResult(
                    status=CheckStatus.PASS, time_seconds=200.0
                ),
                supports_inc_build=True,
            )
        )
        print_results_table(summary, check_mode=CheckMode.INC, no_color=True)
        captured = capsys.readouterr()
        assert "inc-test" in captured.out
        assert "POV(inc)" in captured.out
        # Column may be truncated due to table width, check for partial match
        assert "Patch(inc)" in captured.out or "Patch(i" in captured.out

    def test_rts_mode(self, capsys):
        summary = ValidationSummary()
        summary.add_result(
            BenchmarkValidationResult(
                benchmark="rts-test",
                benchmark_path=Path("/tmp/rts-test"),
                format_check=CheckResult(status=CheckStatus.PASS, time_seconds=1.0),
                pov_check=CheckResult.skip("rts mode"),
                patch_check=CheckResult.skip("rts mode"),
                patch_rts_check=CheckResult(
                    status=CheckStatus.PASS, time_seconds=150.0
                ),
                rts_mode="jcgeks",
            )
        )
        print_results_table(summary, check_mode=CheckMode.RTS, no_color=True)
        captured = capsys.readouterr()
        assert "rts-test" in captured.out
        assert "Patch(rts)" in captured.out
        assert "jcgeks" in captured.out

    def test_all_mode(self, capsys, monkeypatch):
        # Force wide terminal so Rich doesn't truncate column headers
        monkeypatch.setenv("COLUMNS", "200")
        summary = ValidationSummary()
        summary.add_result(
            BenchmarkValidationResult(
                benchmark="all-test",
                benchmark_path=Path("/tmp/all-test"),
                format_check=CheckResult(status=CheckStatus.PASS, time_seconds=1.0),
                pov_check=CheckResult(status=CheckStatus.PASS, time_seconds=60.0),
                patch_check=CheckResult(status=CheckStatus.PASS, time_seconds=120.0),
                patch_rts_check=CheckResult(
                    status=CheckStatus.PASS, time_seconds=150.0
                ),
                supports_inc_build=True,
                rts_mode="jcgeks",
            )
        )
        print_results_table(summary, check_mode=CheckMode.ALL, no_color=True)
        captured = capsys.readouterr()
        assert "all-test" in captured.out
        assert "Fmt" in captured.out
        # Split POV columns
        assert "V:Bld" in captured.out
        assert "V:POV" in captured.out
        assert "V:VAR" in captured.out  # POV variants (pov_1+)
        # Split patch columns
        assert "P:Bld" in captured.out
        assert "P:POV" in captured.out
        assert "P:VAR" in captured.out  # Patch POV variants (pov_1+)
        assert "P:UT" in captured.out
        assert "Cov" in captured.out

    def test_no_color_strips_markup(self, capsys):
        summary = self._make_summary()
        print_results_table(summary, no_color=True)
        captured = capsys.readouterr()
        # Rich with no_color should not contain markup brackets
        assert "[green]" not in captured.out
        assert "[red]" not in captured.out
