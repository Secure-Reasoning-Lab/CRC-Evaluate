"""Integration tests for benchmark CI CLI subcommand structure.

Tests verify:
- Subcommand registration with correct parent parser composition
- Dispatch routing (no subcommand -> error, each subcommand -> correct handler)
- Args propagation through dispatch chain
- parse_cmd integration with output.py (print_results_table)
- Parent parser inheritance across all subcommands
- POV and Patch subcommand handler wiring and exit code behavior
"""

import argparse
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from crsbench.benchmark_ci.cli import add_ci_subparser, dispatch_ci


def _make_parser() -> argparse.ArgumentParser:
    """Create a parser with ci subcommand registered."""
    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers(dest="command")
    add_ci_subparser(subs)
    return parser


# --- Test subcommand registration ---


class TestSubcommandRegistration:
    """Tests for subcommand registration and parent parser composition."""

    def test_ci_subparser_has_all_subcommands(self):
        parser = _make_parser()
        args = parser.parse_args(["ci", "format", "--all"])
        assert args.ci_subcommand == "format"
        assert args.all is True

    def test_ci_subparser_format_has_no_build_workers(self):
        parser = _make_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["ci", "format", "--build-workers", "4"])

    def test_ci_subparser_format_has_parallel(self):
        parser = _make_parser()
        args = parser.parse_args(["ci", "format", "--all", "--parallel", "4"])
        assert args.parallel == 4

    def test_ci_subparser_pov_has_build_options(self):
        parser = _make_parser()
        args = parser.parse_args(["ci", "pov", "--source", "pkgs", "--all"])
        assert args.source == "pkgs"

    def test_ci_subparser_all_runs_coverage_by_default(self):
        parser = _make_parser()
        args = parser.parse_args(["ci", "all", "--all"])
        assert hasattr(args, "ci_func")


# --- Test dispatch end-to-end ---


class TestDispatchCi:
    """Tests for dispatch_ci routing and handler invocation."""

    def test_dispatch_ci_no_subcommand_returns_1(self):
        args = argparse.Namespace()
        result = dispatch_ci(args)
        assert result == 1

    def test_dispatch_ci_no_ci_func_returns_1(self):
        args = argparse.Namespace(ci_func=None)
        result = dispatch_ci(args)
        assert result == 1

    def test_dispatch_ci_calls_handler(self):
        def mock_handler(_args: argparse.Namespace) -> int:
            return 42

        args = argparse.Namespace(ci_func=mock_handler)
        result = dispatch_ci(args)
        assert result == 42

    def test_dispatch_ci_format_returns_0_when_valid(self):
        parser = _make_parser()
        args = parser.parse_args(["ci", "format", "--all"])
        with (
            patch(
                "crsbench.benchmark_ci.cli.commands.format_cmd.resolve_benchmark_paths"
            ) as mock_discover,
            patch(
                "crsbench.benchmark_ci.cli.commands.format_cmd.structural_validate"
            ) as mock_structural,
            patch(
                "crsbench.benchmark_ci.cli.commands.format_cmd.schema_validate"
            ) as mock_schema,
            patch("crsbench.benchmark_ci.cli.commands.format_cmd.print_results_table"),
        ):
            from crsbench.benchmark.packaging.validate import (
                ValidationResult as StructResult,
            )
            from crsbench.validation.errors import ValidationResult

            mock_discover.return_value = [Path("/tmp/bench1")]
            mock_structural.return_value = StructResult(valid=True)
            mock_schema.return_value = ValidationResult(is_valid=True, issues=[])
            result = dispatch_ci(args)
            assert result == 0

    @pytest.mark.parametrize(
        ("subcommand", "handler_name", "extra_args"),
        [
            ("format", "run_format", ["--all"]),
            ("pov", "run_pov", ["--all"]),
            ("patch", "run_patch", ["--all"]),
            ("coverage", "run_coverage", ["--all"]),
            ("rts", "run_rts", ["--all"]),
            ("all", "run_all", ["--all"]),
            ("parse", "run_parse", ["-d", "/tmp/nonexist"]),
        ],
    )
    def test_dispatch_ci_routes_to_correct_handler_per_subcommand(
        self, subcommand, handler_name, extra_args
    ):
        parser = _make_parser()
        args = parser.parse_args(["ci", subcommand, *extra_args])
        assert args.ci_func.__name__ == handler_name

    def test_dispatch_ci_propagates_args_to_handler(self):
        parser = _make_parser()
        args = parser.parse_args(
            ["ci", "pov", "--source", "pkgs", "--build-workers", "8", "--all"]
        )

        with patch(
            "crsbench.benchmark_ci.cli.commands.pov_cmd.run_pov"
        ) as mock_run_pov:
            mock_run_pov.return_value = 0
            args.ci_func = mock_run_pov
            dispatch_ci(args)
            mock_run_pov.assert_called_once_with(args)
            call_args = mock_run_pov.call_args[0][0]
            assert call_args.source == "pkgs"
            assert call_args.build_workers == 8


# --- Test parse subcommand (functional) ---


def _create_summary_json(tmp_path: Path) -> Path:
    """Create a valid summary.json in the given directory."""
    summary_data = {
        "summary": {"total": 1, "passed": 1, "failed": 0, "errors": 0},
        "check_mode": "default",
        "results": [
            {
                "benchmark": "test-benchmark",
                "benchmark_path": str(tmp_path / "benchmarks" / "test-benchmark"),
                "total_status": "pass",
                "total_time_seconds": 5.0,
                "format_check": {
                    "status": "pass",
                    "time_seconds": 1.0,
                    "error": "",
                    "details": {},
                    "fallback_used": False,
                },
                "pov_check": {
                    "status": "pass",
                    "time_seconds": 2.0,
                    "error": "",
                    "details": {},
                    "fallback_used": False,
                },
                "patch_check": {
                    "status": "pass",
                    "time_seconds": 2.0,
                    "error": "",
                    "details": {},
                    "fallback_used": False,
                },
                "coverage_check": None,
                "pov_inc_check": None,
                "patch_inc_check": None,
                "patch_rts_check": None,
                "patch_inc_rts_check": None,
                "coverage_inc_check": None,
                "supports_inc_build": True,
                "rts_mode": None,
                "started_at": "2026-01-01T00:00:00",
                "finished_at": "2026-01-01T00:00:05",
            }
        ],
        "started_at": "2026-01-01T00:00:00",
        "finished_at": "2026-01-01T00:00:05",
    }
    summary_path = tmp_path / "summary.json"
    summary_path.write_text(json.dumps(summary_data))
    return summary_path


class TestParseSubcommand:
    """Tests for parse subcommand functionality."""

    def test_parse_subcommand_loads_summary(self, tmp_path):
        _create_summary_json(tmp_path)
        parser = _make_parser()
        args = parser.parse_args(["ci", "parse", "--output-dir", str(tmp_path)])
        result = dispatch_ci(args)
        assert result == 0

    def test_parse_subcommand_missing_dir_returns_1(self):
        parser = _make_parser()
        args = parser.parse_args(["ci", "parse", "-d", "/tmp/nonexistent_dir_for_test"])
        result = dispatch_ci(args)
        assert result == 1

    def test_parse_subcommand_uses_print_results_table(self, tmp_path):
        _create_summary_json(tmp_path)
        parser = _make_parser()
        args = parser.parse_args(["ci", "parse", "-d", str(tmp_path), "-f", "table"])
        with patch(
            "crsbench.benchmark_ci.cli.commands.parse_cmd.print_results_table"
        ) as mock_table:
            result = dispatch_ci(args)
            assert result == 0
            mock_table.assert_called_once()

    def test_parse_subcommand_json_format(self, tmp_path):
        _create_summary_json(tmp_path)
        parser = _make_parser()
        args = parser.parse_args(["ci", "parse", "-d", str(tmp_path), "-f", "json"])
        result = dispatch_ci(args)
        assert result == 0

    def test_parse_subcommand_csv_format(self, tmp_path):
        _create_summary_json(tmp_path)
        parser = _make_parser()
        args = parser.parse_args(["ci", "parse", "-d", str(tmp_path), "-f", "csv"])
        result = dispatch_ci(args)
        assert result == 0

    def test_parse_subcommand_failed_only(self, tmp_path):
        _create_summary_json(tmp_path)
        parser = _make_parser()
        args = parser.parse_args(["ci", "parse", "-d", str(tmp_path), "--failed-only"])
        result = dispatch_ci(args)
        # All passed, so with failed-only filter: 0 results, still exit 0
        assert result == 0


# --- Test parent parser inheritance ---


class TestParentParserInheritance:
    """Tests for parent parser argument inheritance across subcommands."""

    @pytest.mark.parametrize(
        "subcommand",
        ["format", "pov", "patch", "coverage", "rts", "all"],
    )
    def test_all_subcommands_accept_benchmark_positional(self, subcommand):
        parser = _make_parser()
        args = parser.parse_args(["ci", subcommand, "benchmarks/test-project"])
        assert args.benchmark == "benchmarks/test-project"

    @pytest.mark.parametrize(
        "subcommand",
        ["format", "pov", "patch", "coverage", "rts", "all"],
    )
    def test_all_subcommands_accept_all_flag(self, subcommand):
        parser = _make_parser()
        args = parser.parse_args(["ci", subcommand, "--all"])
        assert args.all is True

    @pytest.mark.parametrize(
        "subcommand",
        ["pov", "patch", "coverage", "rts", "all"],
    )
    def test_build_subcommands_accept_source(self, subcommand):
        parser = _make_parser()
        args = parser.parse_args(["ci", subcommand, "--source", "pkgs", "--all"])
        assert args.source == "pkgs"

    @pytest.mark.parametrize(
        "subcommand",
        ["pov", "patch", "coverage", "rts", "all"],
    )
    def test_build_subcommands_accept_build_workers(self, subcommand):
        parser = _make_parser()
        args = parser.parse_args(["ci", subcommand, "--build-workers", "16", "--all"])
        assert args.build_workers == 16

    @pytest.mark.parametrize(
        "subcommand",
        ["pov", "patch", "coverage", "rts", "all"],
    )
    def test_build_subcommands_accept_verify_workers(self, subcommand):
        parser = _make_parser()
        args = parser.parse_args(["ci", subcommand, "--verify-workers", "8", "--all"])
        assert args.verify_workers == 8

    @pytest.mark.parametrize(
        "subcommand",
        ["format", "pov", "patch", "coverage", "rts", "all"],
    )
    def test_all_subcommands_accept_no_color(self, subcommand):
        parser = _make_parser()
        args = parser.parse_args(["ci", subcommand, "--no-color", "--all"])
        assert args.no_color is True

    def test_format_does_not_accept_build_workers(self):
        parser = _make_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["ci", "format", "--build-workers", "4", "--all"])


# --- Test format subcommand integration ---


class TestFormatSubcommand:
    """Integration tests for the format subcommand."""

    @patch("crsbench.benchmark_ci.cli.commands.format_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.format_cmd.schema_validate")
    @patch("crsbench.benchmark_ci.cli.commands.format_cmd.structural_validate")
    @patch("crsbench.benchmark_ci.cli.commands.format_cmd.resolve_benchmark_paths")
    def test_format_all_pass_returns_0(
        self, mock_discover, mock_structural, mock_schema, mock_table
    ):
        from crsbench.benchmark.packaging.validate import (
            ValidationResult as StructResult,
        )
        from crsbench.validation.errors import ValidationResult

        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_structural.return_value = StructResult(valid=True)
        mock_schema.return_value = ValidationResult(is_valid=True, issues=[])

        parser = _make_parser()
        args = parser.parse_args(["ci", "format", "--all"])
        result = dispatch_ci(args)

        assert result == 0
        mock_table.assert_called_once()

    @patch("crsbench.benchmark_ci.cli.commands.format_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.format_cmd.schema_validate")
    @patch("crsbench.benchmark_ci.cli.commands.format_cmd.structural_validate")
    @patch("crsbench.benchmark_ci.cli.commands.format_cmd.resolve_benchmark_paths")
    def test_format_structural_fail_returns_1(
        self, mock_discover, mock_structural, mock_schema, mock_table
    ):
        from crsbench.benchmark.packaging.validate import (
            ValidationResult as StructResult,
        )
        from crsbench.validation.errors import ValidationResult

        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_structural.return_value = StructResult(
            valid=False, errors=["Missing Dockerfile"]
        )
        mock_schema.return_value = ValidationResult(is_valid=True, issues=[])

        parser = _make_parser()
        args = parser.parse_args(["ci", "format", "--all"])
        result = dispatch_ci(args)

        assert result == 1

    @patch("crsbench.benchmark_ci.cli.commands.format_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.format_cmd.schema_validate")
    @patch("crsbench.benchmark_ci.cli.commands.format_cmd.structural_validate")
    @patch("crsbench.benchmark_ci.cli.commands.format_cmd.resolve_benchmark_paths")
    def test_format_schema_fail_returns_1(
        self, mock_discover, mock_structural, mock_schema, mock_table
    ):
        from crsbench.benchmark.packaging.validate import (
            ValidationResult as StructResult,
        )
        from crsbench.validation.errors import (
            ValidationIssue,
            ValidationResult,
            ValidationSeverity,
        )

        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_structural.return_value = StructResult(valid=True)
        mock_schema.return_value = ValidationResult(
            is_valid=False,
            issues=[
                ValidationIssue(
                    severity=ValidationSeverity.ERROR,
                    code="TEST",
                    message="bad field",
                )
            ],
        )

        parser = _make_parser()
        args = parser.parse_args(["ci", "format", "--all"])
        result = dispatch_ci(args)

        assert result == 1

    @patch("crsbench.benchmark_ci.cli.commands.format_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.format_cmd.schema_validate")
    @patch("crsbench.benchmark_ci.cli.commands.format_cmd.structural_validate")
    @patch("crsbench.benchmark_ci.cli.commands.format_cmd.resolve_benchmark_paths")
    def test_format_calls_print_results_table(
        self, mock_discover, mock_structural, mock_schema, mock_table
    ):
        from crsbench.benchmark.packaging.validate import (
            ValidationResult as StructResult,
        )
        from crsbench.benchmark_ci.models import ValidationSummary
        from crsbench.validation.errors import ValidationResult

        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_structural.return_value = StructResult(valid=True)
        mock_schema.return_value = ValidationResult(is_valid=True, issues=[])

        parser = _make_parser()
        args = parser.parse_args(["ci", "format", "--all"])
        dispatch_ci(args)

        mock_table.assert_called_once()
        call_args = mock_table.call_args
        assert isinstance(call_args[0][0], ValidationSummary)

    @patch("crsbench.benchmark_ci.cli.commands.format_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.format_cmd.schema_validate")
    @patch("crsbench.benchmark_ci.cli.commands.format_cmd.structural_validate")
    @patch("crsbench.benchmark_ci.cli.commands.format_cmd.resolve_benchmark_paths")
    def test_format_exception_returns_error_status(
        self, mock_discover, mock_structural, mock_schema, mock_table
    ):
        from crsbench.benchmark.packaging.validate import (
            ValidationResult as StructResult,
        )

        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_structural.return_value = StructResult(valid=True)
        mock_schema.side_effect = Exception("boom")

        parser = _make_parser()
        args = parser.parse_args(["ci", "format", "--all"])
        result = dispatch_ci(args)

        assert result == 1

    @patch("crsbench.benchmark_ci.cli.commands.format_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.format_cmd.schema_validate")
    @patch("crsbench.benchmark_ci.cli.commands.format_cmd.structural_validate")
    @patch("crsbench.benchmark_ci.cli.commands.format_cmd.resolve_benchmark_paths")
    def test_format_output_writes_json(
        self, mock_discover, mock_structural, mock_schema, mock_table, tmp_path
    ):
        from crsbench.benchmark.packaging.validate import (
            ValidationResult as StructResult,
        )
        from crsbench.validation.errors import ValidationResult

        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_structural.return_value = StructResult(valid=True)
        mock_schema.return_value = ValidationResult(is_valid=True, issues=[])

        output_file = tmp_path / "out.json"
        parser = _make_parser()
        args = parser.parse_args(
            ["ci", "format", "--all", "--output", str(output_file)]
        )
        result = dispatch_ci(args)

        assert result == 0
        assert output_file.exists()
        data = json.loads(output_file.read_text())
        assert "summary" in data

    @patch("crsbench.benchmark_ci.cli.commands.format_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.format_cmd.schema_validate")
    @patch("crsbench.benchmark_ci.cli.commands.format_cmd.structural_validate")
    @patch("crsbench.benchmark_ci.cli.commands.format_cmd.resolve_benchmark_paths")
    def test_format_multiple_benchmarks_mixed_results(
        self, mock_discover, mock_structural, mock_schema, mock_table
    ):
        from crsbench.benchmark.packaging.validate import (
            ValidationResult as StructResult,
        )
        from crsbench.validation.errors import (
            ValidationIssue,
            ValidationResult,
            ValidationSeverity,
        )

        mock_discover.return_value = [Path("/tmp/bench1"), Path("/tmp/bench2")]
        mock_structural.return_value = StructResult(valid=True)
        mock_schema.side_effect = [
            ValidationResult(is_valid=True, issues=[]),
            ValidationResult(
                is_valid=False,
                issues=[
                    ValidationIssue(
                        severity=ValidationSeverity.ERROR,
                        code="TEST",
                        message="missing field",
                    )
                ],
            ),
        ]

        parser = _make_parser()
        args = parser.parse_args(["ci", "format", "--all"])
        result = dispatch_ci(args)

        assert result == 1

    @patch("crsbench.benchmark_ci.cli.commands.format_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.format_cmd.schema_validate")
    @patch("crsbench.benchmark_ci.cli.commands.format_cmd.structural_validate")
    @patch("crsbench.benchmark_ci.cli.commands.format_cmd.resolve_benchmark_paths")
    def test_format_cpv_conflict_returns_1(
        self, mock_discover, mock_structural, mock_schema, mock_table, tmp_path
    ):
        from crsbench.benchmark.packaging.validate import (
            ValidationResult as StructResult,
        )
        from crsbench.validation.errors import ValidationResult

        # Create benchmark with cpv_0 under two harnesses (conflict)
        bench = tmp_path / "bench1"
        for harness in ["harness_a", "harness_b"]:
            cpv_dir = bench / ".aixcc" / harness / "cpv_0"
            (cpv_dir / "blobs").mkdir(parents=True)
            (cpv_dir / "blobs" / "pov_0.blob").write_bytes(b"x")
            (cpv_dir / "patches").mkdir()
            (cpv_dir / "patches" / "patch_0.diff").write_text("diff")
            (cpv_dir / "vuln.yaml").write_text("id: cpv_0")

        mock_discover.return_value = [bench]
        mock_structural.return_value = StructResult(valid=True)
        mock_schema.return_value = ValidationResult(is_valid=True, issues=[])

        parser = _make_parser()
        args = parser.parse_args(["ci", "format", "--all"])
        result = dispatch_ci(args)

        assert result == 1

    @patch("crsbench.benchmark_ci.cli.commands.format_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.format_cmd.schema_validate")
    @patch("crsbench.benchmark_ci.cli.commands.format_cmd.structural_validate")
    @patch("crsbench.benchmark_ci.cli.commands.format_cmd.resolve_benchmark_paths")
    def test_format_cpv_missing_files_returns_1(
        self, mock_discover, mock_structural, mock_schema, mock_table, tmp_path
    ):
        from crsbench.benchmark.packaging.validate import (
            ValidationResult as StructResult,
        )
        from crsbench.validation.errors import ValidationResult

        # Create CPV without vuln.yaml, blobs, or patches
        bench = tmp_path / "bench1"
        cpv_dir = bench / ".aixcc" / "harness_a" / "cpv_0"
        cpv_dir.mkdir(parents=True)

        mock_discover.return_value = [bench]
        mock_structural.return_value = StructResult(valid=True)
        mock_schema.return_value = ValidationResult(is_valid=True, issues=[])

        parser = _make_parser()
        args = parser.parse_args(["ci", "format", "--all"])
        result = dispatch_ci(args)

        assert result == 1

    @patch("crsbench.benchmark_ci.cli.commands.format_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.format_cmd.schema_validate")
    @patch("crsbench.benchmark_ci.cli.commands.format_cmd.structural_validate")
    @patch("crsbench.benchmark_ci.cli.commands.format_cmd.resolve_benchmark_paths")
    def test_format_cpv_complete_structure_passes(
        self, mock_discover, mock_structural, mock_schema, mock_table, tmp_path
    ):
        from crsbench.benchmark.packaging.validate import (
            ValidationResult as StructResult,
        )
        from crsbench.validation.errors import ValidationResult

        # Create complete CPV structure
        bench = tmp_path / "bench1"
        aixcc = bench / ".aixcc"
        for cpv_id in ["cpv_0", "cpv_1"]:
            cpv_dir = aixcc / "harness_a" / cpv_id
            (cpv_dir / "blobs").mkdir(parents=True)
            (cpv_dir / "blobs" / "pov_0.blob").write_bytes(b"x")
            (cpv_dir / "patches").mkdir()
            (cpv_dir / "patches" / "patch_0.diff").write_text("diff")
            (cpv_dir / "vuln.yaml").write_text("id: test")

        # meta.yaml declaring the harness and vulns
        meta_content = {
            "harness_files": [
                {
                    "name": "harness_a",
                    "vulns": [
                        {"vuln_keyword": "cpv_0"},
                        {"vuln_keyword": "cpv_1"},
                    ],
                }
            ]
        }
        import yaml

        (aixcc / "meta.yaml").write_text(yaml.dump(meta_content))

        mock_discover.return_value = [bench]
        mock_structural.return_value = StructResult(valid=True)
        mock_schema.return_value = ValidationResult(is_valid=True, issues=[])

        parser = _make_parser()
        args = parser.parse_args(["ci", "format", "--all"])
        result = dispatch_ci(args)

        assert result == 0


# --- Test POV subcommand integration ---


def _make_pov_dag_results(
    benchmark_name: str, cpv_ids: list[str], *, success: bool = True
):
    """Create DAG results for POV verification jobs."""
    from crsbench.executor.types import ExecutorResult, JobStatus

    results = {}
    status = JobStatus.SUCCESS if success else JobStatus.FAILED
    for cpv_id in cpv_ids:
        job_id = f"verify-cpv-pov:{benchmark_name}:{cpv_id}"
        results[job_id] = ExecutorResult(
            job_id=job_id,
            status=status,
            elapsed_seconds=2.0,
            error=None if success else "POV not detected",
        )
    build_id = f"build-variants:{benchmark_name}"
    results[build_id] = ExecutorResult(
        job_id=build_id,
        status=JobStatus.SUCCESS,
        elapsed_seconds=5.0,
    )
    return results


class TestPovSubcommand:
    """Integration tests for the POV subcommand."""

    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd.DAGExecutor")
    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd.discover_pov_paths")
    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd.discover_cpv_ids")
    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd.discover_harness_names")
    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd._load_project_capabilities")
    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd.resolve_benchmark_paths")
    def test_pov_all_pass_returns_0(
        self,
        mock_discover,
        mock_caps,
        mock_harness,
        mock_cpvs,
        mock_povs,
        mock_executor_cls,
        mock_table,
    ):
        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_caps.return_value = (True, None)
        mock_harness.return_value = ["fuzz_target"]
        mock_cpvs.return_value = ["cpv_0"]
        mock_povs.return_value = [Path("/tmp/pov_0.blob")]
        mock_executor_cls.return_value.execute.return_value = _make_pov_dag_results(
            "bench1", ["cpv_0"]
        )

        parser = _make_parser()
        args = parser.parse_args(["ci", "pov", "--all"])
        result = dispatch_ci(args)

        assert result == 0
        mock_table.assert_called_once()

    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd.DAGExecutor")
    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd.discover_pov_paths")
    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd.discover_cpv_ids")
    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd.discover_harness_names")
    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd._load_project_capabilities")
    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd.resolve_benchmark_paths")
    def test_pov_fail_returns_1(
        self,
        mock_discover,
        mock_caps,
        mock_harness,
        mock_cpvs,
        mock_povs,
        mock_executor_cls,
        mock_table,
    ):
        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_caps.return_value = (True, None)
        mock_harness.return_value = ["fuzz_target"]
        mock_cpvs.return_value = ["cpv_0"]
        mock_povs.return_value = [Path("/tmp/pov_0.blob")]
        mock_executor_cls.return_value.execute.return_value = _make_pov_dag_results(
            "bench1", ["cpv_0"], success=False
        )

        parser = _make_parser()
        args = parser.parse_args(["ci", "pov", "--all"])
        result = dispatch_ci(args)

        assert result == 1

    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd.DAGExecutor")
    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd.discover_pov_paths")
    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd.discover_cpv_ids")
    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd.discover_harness_names")
    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd._load_project_capabilities")
    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd.resolve_benchmark_paths")
    def test_pov_output_writes_json(
        self,
        mock_discover,
        mock_caps,
        mock_harness,
        mock_cpvs,
        mock_povs,
        mock_executor_cls,
        mock_table,
        tmp_path,
    ):
        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_caps.return_value = (True, None)
        mock_harness.return_value = ["fuzz_target"]
        mock_cpvs.return_value = ["cpv_0"]
        mock_povs.return_value = [Path("/tmp/pov_0.blob")]
        mock_executor_cls.return_value.execute.return_value = _make_pov_dag_results(
            "bench1", ["cpv_0"]
        )

        output_file = tmp_path / "out.json"
        parser = _make_parser()
        args = parser.parse_args(["ci", "pov", "--all", "--output", str(output_file)])
        result = dispatch_ci(args)

        assert result == 0
        assert output_file.exists()
        data = json.loads(output_file.read_text())
        assert "summary" in data

    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd.DAGExecutor")
    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd.discover_pov_paths")
    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd.discover_cpv_ids")
    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd.discover_harness_names")
    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd._load_project_capabilities")
    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd.resolve_benchmark_paths")
    def test_pov_no_cpvs_returns_0(
        self,
        mock_discover,
        mock_caps,
        mock_harness,
        mock_cpvs,
        mock_povs,
        mock_executor_cls,
        mock_table,
    ):
        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_caps.return_value = (True, None)
        mock_harness.return_value = ["fuzz_target"]
        mock_cpvs.return_value = []
        mock_povs.return_value = []
        # No verify jobs → empty results (only build)
        mock_executor_cls.return_value.execute.return_value = {
            "build-variants:bench1": _make_pov_dag_results("bench1", [])[
                "build-variants:bench1"
            ]
        }

        parser = _make_parser()
        args = parser.parse_args(["ci", "pov", "--all"])
        result = dispatch_ci(args)

        assert result == 0

    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd.DAGExecutor")
    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd.discover_pov_paths")
    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd.discover_cpv_ids")
    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd.discover_harness_names")
    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd._load_project_capabilities")
    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd.resolve_benchmark_paths")
    def test_pov_default_uses_inc_build(
        self,
        mock_discover,
        mock_caps,
        mock_harness,
        mock_cpvs,
        mock_povs,
        mock_executor_cls,
        mock_table,
    ):
        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_caps.return_value = (True, None)
        mock_harness.return_value = ["fuzz_target"]
        mock_cpvs.return_value = ["cpv_0"]
        mock_povs.return_value = [Path("/tmp/pov_0.blob")]
        mock_executor_cls.return_value.execute.return_value = _make_pov_dag_results(
            "bench1", ["cpv_0"]
        )

        parser = _make_parser()
        args = parser.parse_args(["ci", "pov", "--all"])
        dispatch_ci(args)

        # Check the BuildVariantsJob passed to executor
        call_args = mock_executor_cls.return_value.execute.call_args
        jobs = call_args[0][0]
        build_job = next(j for j in jobs if j.job_id == "build-variants:bench1")
        assert build_job.use_inc_build is True
        assert build_job.force_rebuild is True

    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd.DAGExecutor")
    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd.discover_pov_paths")
    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd.discover_cpv_ids")
    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd.discover_harness_names")
    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd._load_project_capabilities")
    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd.resolve_benchmark_paths")
    def test_pov_no_inc_build_flag(
        self,
        mock_discover,
        mock_caps,
        mock_harness,
        mock_cpvs,
        mock_povs,
        mock_executor_cls,
        mock_table,
    ):
        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_caps.return_value = (True, None)
        mock_harness.return_value = ["fuzz_target"]
        mock_cpvs.return_value = ["cpv_0"]
        mock_povs.return_value = [Path("/tmp/pov_0.blob")]
        mock_executor_cls.return_value.execute.return_value = _make_pov_dag_results(
            "bench1", ["cpv_0"]
        )

        parser = _make_parser()
        args = parser.parse_args(["ci", "pov", "--all", "--no-inc-build"])
        dispatch_ci(args)

        call_args = mock_executor_cls.return_value.execute.call_args
        jobs = call_args[0][0]
        build_job = next(j for j in jobs if j.job_id == "build-variants:bench1")
        assert build_job.use_inc_build is False


# --- Test Patch subcommand integration ---


def _make_patch_dag_results(
    benchmark_name: str,
    patch_keys: list[tuple[str, str]],
    *,
    success: bool = True,
    test_mode: str = "FULL",
):
    """Create DAG results for patch build + test jobs."""
    from crsbench.executor.types import ExecutorResult, JobStatus

    results = {}
    build_id = f"build-variants:{benchmark_name}"
    results[build_id] = ExecutorResult(
        job_id=build_id,
        status=JobStatus.SUCCESS,
        elapsed_seconds=5.0,
    )
    status = JobStatus.SUCCESS if success else JobStatus.FAILED
    for cpv_id, patch_id in patch_keys:
        bp_id = f"build-patch:{benchmark_name}:{cpv_id}:{patch_id}"
        results[bp_id] = ExecutorResult(
            job_id=bp_id,
            status=JobStatus.SUCCESS,
            elapsed_seconds=3.0,
        )
        tp_id = f"test-patch:{benchmark_name}:{cpv_id}:{patch_id}:{test_mode}"
        results[tp_id] = ExecutorResult(
            job_id=tp_id,
            status=status,
            elapsed_seconds=4.0,
            error=None if success else "POVs still crash",
        )
    return results


class TestPatchSubcommand:
    """Integration tests for the Patch subcommand."""

    @patch("crsbench.benchmark_ci.cli.commands.patch_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.patch_cmd.DAGExecutor")
    @patch("crsbench.benchmark_ci.cli.commands.patch_cmd.discover_pov_paths")
    @patch("crsbench.benchmark_ci.cli.commands.patch_cmd.discover_patch_paths")
    @patch("crsbench.benchmark_ci.cli.commands.patch_cmd.discover_cpv_ids")
    @patch("crsbench.benchmark_ci.cli.commands.patch_cmd.discover_harness_names")
    @patch("crsbench.benchmark_ci.cli.commands.patch_cmd._load_project_capabilities")
    @patch("crsbench.benchmark_ci.cli.commands.patch_cmd.resolve_benchmark_paths")
    def test_patch_all_pass_returns_0(
        self,
        mock_discover,
        mock_caps,
        mock_harness,
        mock_cpvs,
        mock_patches,
        mock_povs,
        mock_executor_cls,
        mock_table,
    ):
        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_caps.return_value = (True, None)
        mock_harness.return_value = ["fuzz_target"]
        mock_cpvs.return_value = ["cpv_0"]
        mock_patches.return_value = [("patch_0", Path("/tmp/patch_0.diff"))]
        mock_povs.return_value = [Path("/tmp/pov_0.blob")]
        mock_executor_cls.return_value.execute.return_value = _make_patch_dag_results(
            "bench1", [("cpv_0", "patch_0")]
        )

        parser = _make_parser()
        args = parser.parse_args(["ci", "patch", "--all"])
        result = dispatch_ci(args)

        assert result == 0
        mock_table.assert_called_once()

    @patch("crsbench.benchmark_ci.cli.commands.patch_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.patch_cmd.DAGExecutor")
    @patch("crsbench.benchmark_ci.cli.commands.patch_cmd.discover_pov_paths")
    @patch("crsbench.benchmark_ci.cli.commands.patch_cmd.discover_patch_paths")
    @patch("crsbench.benchmark_ci.cli.commands.patch_cmd.discover_cpv_ids")
    @patch("crsbench.benchmark_ci.cli.commands.patch_cmd.discover_harness_names")
    @patch("crsbench.benchmark_ci.cli.commands.patch_cmd._load_project_capabilities")
    @patch("crsbench.benchmark_ci.cli.commands.patch_cmd.resolve_benchmark_paths")
    def test_patch_fail_returns_1(
        self,
        mock_discover,
        mock_caps,
        mock_harness,
        mock_cpvs,
        mock_patches,
        mock_povs,
        mock_executor_cls,
        mock_table,
    ):
        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_caps.return_value = (True, None)
        mock_harness.return_value = ["fuzz_target"]
        mock_cpvs.return_value = ["cpv_0"]
        mock_patches.return_value = [("patch_0", Path("/tmp/patch_0.diff"))]
        mock_povs.return_value = [Path("/tmp/pov_0.blob")]
        mock_executor_cls.return_value.execute.return_value = _make_patch_dag_results(
            "bench1", [("cpv_0", "patch_0")], success=False
        )

        parser = _make_parser()
        args = parser.parse_args(["ci", "patch", "--all"])
        result = dispatch_ci(args)

        assert result == 1

    @patch("crsbench.benchmark_ci.cli.commands.patch_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.patch_cmd.DAGExecutor")
    @patch("crsbench.benchmark_ci.cli.commands.patch_cmd.discover_pov_paths")
    @patch("crsbench.benchmark_ci.cli.commands.patch_cmd.discover_patch_paths")
    @patch("crsbench.benchmark_ci.cli.commands.patch_cmd.discover_cpv_ids")
    @patch("crsbench.benchmark_ci.cli.commands.patch_cmd.discover_harness_names")
    @patch("crsbench.benchmark_ci.cli.commands.patch_cmd._load_project_capabilities")
    @patch("crsbench.benchmark_ci.cli.commands.patch_cmd.resolve_benchmark_paths")
    def test_patch_output_writes_json(
        self,
        mock_discover,
        mock_caps,
        mock_harness,
        mock_cpvs,
        mock_patches,
        mock_povs,
        mock_executor_cls,
        mock_table,
        tmp_path,
    ):
        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_caps.return_value = (True, None)
        mock_harness.return_value = ["fuzz_target"]
        mock_cpvs.return_value = ["cpv_0"]
        mock_patches.return_value = [("patch_0", Path("/tmp/patch_0.diff"))]
        mock_povs.return_value = [Path("/tmp/pov_0.blob")]
        mock_executor_cls.return_value.execute.return_value = _make_patch_dag_results(
            "bench1", [("cpv_0", "patch_0")]
        )

        output_file = tmp_path / "out.json"
        parser = _make_parser()
        args = parser.parse_args(["ci", "patch", "--all", "--output", str(output_file)])
        result = dispatch_ci(args)

        assert result == 0
        assert output_file.exists()
        data = json.loads(output_file.read_text())
        assert "summary" in data

    @patch("crsbench.benchmark_ci.cli.commands.patch_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.patch_cmd.DAGExecutor")
    @patch("crsbench.benchmark_ci.cli.commands.patch_cmd.discover_pov_paths")
    @patch("crsbench.benchmark_ci.cli.commands.patch_cmd.discover_patch_paths")
    @patch("crsbench.benchmark_ci.cli.commands.patch_cmd.discover_cpv_ids")
    @patch("crsbench.benchmark_ci.cli.commands.patch_cmd.discover_harness_names")
    @patch("crsbench.benchmark_ci.cli.commands.patch_cmd._load_project_capabilities")
    @patch("crsbench.benchmark_ci.cli.commands.patch_cmd.resolve_benchmark_paths")
    def test_patch_default_uses_inc_build(
        self,
        mock_discover,
        mock_caps,
        mock_harness,
        mock_cpvs,
        mock_patches,
        mock_povs,
        mock_executor_cls,
        mock_table,
    ):
        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_caps.return_value = (True, None)
        mock_harness.return_value = ["fuzz_target"]
        mock_cpvs.return_value = ["cpv_0"]
        mock_patches.return_value = [("patch_0", Path("/tmp/patch_0.diff"))]
        mock_povs.return_value = [Path("/tmp/pov_0.blob")]
        mock_executor_cls.return_value.execute.return_value = _make_patch_dag_results(
            "bench1", [("cpv_0", "patch_0")]
        )

        parser = _make_parser()
        args = parser.parse_args(["ci", "patch", "--all"])
        dispatch_ci(args)

        call_args = mock_executor_cls.return_value.execute.call_args
        jobs = call_args[0][0]
        build_job = next(j for j in jobs if j.job_id == "build-variants:bench1")
        assert build_job.use_inc_build is True
        assert build_job.force_rebuild is True

    @patch("crsbench.benchmark_ci.cli.commands.patch_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.patch_cmd.DAGExecutor")
    @patch("crsbench.benchmark_ci.cli.commands.patch_cmd.discover_pov_paths")
    @patch("crsbench.benchmark_ci.cli.commands.patch_cmd.discover_patch_paths")
    @patch("crsbench.benchmark_ci.cli.commands.patch_cmd.discover_cpv_ids")
    @patch("crsbench.benchmark_ci.cli.commands.patch_cmd.discover_harness_names")
    @patch("crsbench.benchmark_ci.cli.commands.patch_cmd._load_project_capabilities")
    @patch("crsbench.benchmark_ci.cli.commands.patch_cmd.resolve_benchmark_paths")
    def test_patch_no_inc_build_flag(
        self,
        mock_discover,
        mock_caps,
        mock_harness,
        mock_cpvs,
        mock_patches,
        mock_povs,
        mock_executor_cls,
        mock_table,
    ):
        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_caps.return_value = (True, None)
        mock_harness.return_value = ["fuzz_target"]
        mock_cpvs.return_value = ["cpv_0"]
        mock_patches.return_value = [("patch_0", Path("/tmp/patch_0.diff"))]
        mock_povs.return_value = [Path("/tmp/pov_0.blob")]
        mock_executor_cls.return_value.execute.return_value = _make_patch_dag_results(
            "bench1", [("cpv_0", "patch_0")]
        )

        parser = _make_parser()
        args = parser.parse_args(["ci", "patch", "--all", "--no-inc-build"])
        dispatch_ci(args)

        call_args = mock_executor_cls.return_value.execute.call_args
        jobs = call_args[0][0]
        build_job = next(j for j in jobs if j.job_id == "build-variants:bench1")
        assert build_job.use_inc_build is False


# --- Test RTS subcommand integration ---


class TestRtsSubcommand:
    """Integration tests for the RTS subcommand."""

    @patch("crsbench.benchmark_ci.cli.commands.rts_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.rts_cmd.DAGExecutor")
    @patch("crsbench.benchmark_ci.cli.commands.rts_cmd.discover_pov_paths")
    @patch("crsbench.benchmark_ci.cli.commands.rts_cmd.discover_patch_paths")
    @patch("crsbench.benchmark_ci.cli.commands.rts_cmd.discover_cpv_ids")
    @patch("crsbench.benchmark_ci.cli.commands.rts_cmd.discover_harness_names")
    @patch("crsbench.benchmark_ci.cli.commands.rts_cmd._load_project_capabilities")
    @patch("crsbench.benchmark_ci.cli.commands.rts_cmd.resolve_benchmark_paths")
    def test_rts_all_pass_returns_0(
        self,
        mock_discover,
        mock_caps,
        mock_harness,
        mock_cpvs,
        mock_patches,
        mock_povs,
        mock_executor_cls,
        mock_table,
    ):
        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_caps.return_value = (True, "jcgeks")
        mock_harness.return_value = ["fuzz_target"]
        mock_cpvs.return_value = ["cpv_0"]
        mock_patches.return_value = [("patch_0", Path("/tmp/patch_0.diff"))]
        mock_povs.return_value = [Path("/tmp/pov_0.blob")]
        mock_executor_cls.return_value.execute.return_value = _make_patch_dag_results(
            "bench1", [("cpv_0", "patch_0")], test_mode="RTS"
        )

        parser = _make_parser()
        args = parser.parse_args(["ci", "rts", "--all"])
        result = dispatch_ci(args)

        assert result == 0
        mock_table.assert_called_once()

    @patch("crsbench.benchmark_ci.cli.commands.rts_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.rts_cmd.DAGExecutor")
    @patch("crsbench.benchmark_ci.cli.commands.rts_cmd.discover_pov_paths")
    @patch("crsbench.benchmark_ci.cli.commands.rts_cmd.discover_patch_paths")
    @patch("crsbench.benchmark_ci.cli.commands.rts_cmd.discover_cpv_ids")
    @patch("crsbench.benchmark_ci.cli.commands.rts_cmd.discover_harness_names")
    @patch("crsbench.benchmark_ci.cli.commands.rts_cmd._load_project_capabilities")
    @patch("crsbench.benchmark_ci.cli.commands.rts_cmd.resolve_benchmark_paths")
    def test_rts_fail_returns_1(
        self,
        mock_discover,
        mock_caps,
        mock_harness,
        mock_cpvs,
        mock_patches,
        mock_povs,
        mock_executor_cls,
        mock_table,
    ):
        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_caps.return_value = (True, "jcgeks")
        mock_harness.return_value = ["fuzz_target"]
        mock_cpvs.return_value = ["cpv_0"]
        mock_patches.return_value = [("patch_0", Path("/tmp/patch_0.diff"))]
        mock_povs.return_value = [Path("/tmp/pov_0.blob")]
        mock_executor_cls.return_value.execute.return_value = _make_patch_dag_results(
            "bench1", [("cpv_0", "patch_0")], success=False, test_mode="RTS"
        )

        parser = _make_parser()
        args = parser.parse_args(["ci", "rts", "--all"])
        result = dispatch_ci(args)

        assert result == 1

    @patch("crsbench.benchmark_ci.cli.commands.rts_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.rts_cmd._load_project_capabilities")
    @patch("crsbench.benchmark_ci.cli.commands.rts_cmd.resolve_benchmark_paths")
    def test_rts_no_rts_mode_shows_skip(self, mock_discover, mock_caps, mock_table):
        from crsbench.benchmark_ci.models import CheckStatus

        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_caps.return_value = (True, None)  # No rts_mode

        parser = _make_parser()
        args = parser.parse_args(["ci", "rts", "--all"])
        result = dispatch_ci(args)

        # Return 0 (skip = no failure)
        assert result == 0
        # Verify SKIP in summary
        call_args = mock_table.call_args
        summary = call_args[0][0]
        assert summary.results[0].patch_rts_check.status == CheckStatus.SKIP

    @patch("crsbench.benchmark_ci.cli.commands.rts_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.rts_cmd.DAGExecutor")
    @patch("crsbench.benchmark_ci.cli.commands.rts_cmd.discover_pov_paths")
    @patch("crsbench.benchmark_ci.cli.commands.rts_cmd.discover_patch_paths")
    @patch("crsbench.benchmark_ci.cli.commands.rts_cmd.discover_cpv_ids")
    @patch("crsbench.benchmark_ci.cli.commands.rts_cmd.discover_harness_names")
    @patch("crsbench.benchmark_ci.cli.commands.rts_cmd._load_project_capabilities")
    @patch("crsbench.benchmark_ci.cli.commands.rts_cmd.resolve_benchmark_paths")
    def test_rts_output_writes_json(
        self,
        mock_discover,
        mock_caps,
        mock_harness,
        mock_cpvs,
        mock_patches,
        mock_povs,
        mock_executor_cls,
        mock_table,
        tmp_path,
    ):
        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_caps.return_value = (True, "jcgeks")
        mock_harness.return_value = ["fuzz_target"]
        mock_cpvs.return_value = ["cpv_0"]
        mock_patches.return_value = [("patch_0", Path("/tmp/patch_0.diff"))]
        mock_povs.return_value = [Path("/tmp/pov_0.blob")]
        mock_executor_cls.return_value.execute.return_value = _make_patch_dag_results(
            "bench1", [("cpv_0", "patch_0")], test_mode="RTS"
        )

        output_file = tmp_path / "out.json"
        parser = _make_parser()
        args = parser.parse_args(["ci", "rts", "--all", "--output", str(output_file)])
        result = dispatch_ci(args)

        assert result == 0
        assert output_file.exists()
        data = json.loads(output_file.read_text())
        assert "summary" in data

    @patch("crsbench.benchmark_ci.cli.commands.rts_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.rts_cmd.DAGExecutor")
    @patch("crsbench.benchmark_ci.cli.commands.rts_cmd.discover_pov_paths")
    @patch("crsbench.benchmark_ci.cli.commands.rts_cmd.discover_patch_paths")
    @patch("crsbench.benchmark_ci.cli.commands.rts_cmd.discover_cpv_ids")
    @patch("crsbench.benchmark_ci.cli.commands.rts_cmd.discover_harness_names")
    @patch("crsbench.benchmark_ci.cli.commands.rts_cmd._load_project_capabilities")
    @patch("crsbench.benchmark_ci.cli.commands.rts_cmd.resolve_benchmark_paths")
    def test_rts_uses_rts_test_mode(
        self,
        mock_discover,
        mock_caps,
        mock_harness,
        mock_cpvs,
        mock_patches,
        mock_povs,
        mock_executor_cls,
        mock_table,
    ):
        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_caps.return_value = (True, "jcgeks")
        mock_harness.return_value = ["fuzz_target"]
        mock_cpvs.return_value = ["cpv_0"]
        mock_patches.return_value = [("patch_0", Path("/tmp/patch_0.diff"))]
        mock_povs.return_value = [Path("/tmp/pov_0.blob")]
        mock_executor_cls.return_value.execute.return_value = _make_patch_dag_results(
            "bench1", [("cpv_0", "patch_0")], test_mode="RTS"
        )

        parser = _make_parser()
        args = parser.parse_args(["ci", "rts", "--all"])
        dispatch_ci(args)

        # Verify PatchVariantTestJob uses test_mode="RTS"
        call_args = mock_executor_cls.return_value.execute.call_args
        jobs = call_args[0][0]
        from crsbench.benchmark_ci.jobs.flat import PatchVariantTestJob

        test_jobs = [j for j in jobs if isinstance(j, PatchVariantTestJob)]
        assert len(test_jobs) == 1
        assert test_jobs[0].test_mode == "RTS"


# --- Test Coverage subcommand integration ---


def _make_coverage_dag_results(benchmark_name: str, *, success: bool = True):
    """Create DAG results for coverage jobs."""
    from crsbench.executor.types import ExecutorResult, JobStatus

    build_id = f"build-variants:{benchmark_name}"
    cov_id = f"collect-coverage:{benchmark_name}"
    status = JobStatus.SUCCESS if success else JobStatus.FAILED
    return {
        build_id: ExecutorResult(
            job_id=build_id, status=JobStatus.SUCCESS, elapsed_seconds=5.0
        ),
        cov_id: ExecutorResult(
            job_id=cov_id,
            status=status,
            elapsed_seconds=10.0,
            error=None if success else "Coverage failed",
        ),
    }


class TestCoverageSubcommand:
    """Integration tests for the Coverage subcommand."""

    @patch("crsbench.benchmark_ci.cli.commands.coverage_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.coverage_cmd.DAGExecutor")
    @patch("crsbench.benchmark_ci.cli.commands.coverage_cmd.discover_harness_names")
    @patch("crsbench.benchmark_ci.cli.commands.coverage_cmd._load_project_capabilities")
    @patch("crsbench.benchmark_ci.cli.commands.coverage_cmd.resolve_benchmark_paths")
    def test_coverage_all_pass_returns_0(
        self, mock_discover, mock_caps, mock_harness, mock_executor_cls, mock_table
    ):
        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_caps.return_value = (True, None)
        mock_harness.return_value = ["fuzz_target"]
        mock_executor_cls.return_value.execute.return_value = (
            _make_coverage_dag_results("bench1")
        )

        parser = _make_parser()
        args = parser.parse_args(["ci", "coverage", "--all"])
        result = dispatch_ci(args)

        assert result == 0
        mock_table.assert_called_once()

    @patch("crsbench.benchmark_ci.cli.commands.coverage_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.coverage_cmd.DAGExecutor")
    @patch("crsbench.benchmark_ci.cli.commands.coverage_cmd.discover_harness_names")
    @patch("crsbench.benchmark_ci.cli.commands.coverage_cmd._load_project_capabilities")
    @patch("crsbench.benchmark_ci.cli.commands.coverage_cmd.resolve_benchmark_paths")
    def test_coverage_fail_returns_1(
        self, mock_discover, mock_caps, mock_harness, mock_executor_cls, mock_table
    ):
        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_caps.return_value = (True, None)
        mock_harness.return_value = ["fuzz_target"]
        mock_executor_cls.return_value.execute.return_value = (
            _make_coverage_dag_results("bench1", success=False)
        )

        parser = _make_parser()
        args = parser.parse_args(["ci", "coverage", "--all"])
        result = dispatch_ci(args)

        assert result == 1

    @patch("crsbench.benchmark_ci.cli.commands.coverage_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.coverage_cmd.DAGExecutor")
    @patch("crsbench.benchmark_ci.cli.commands.coverage_cmd.discover_harness_names")
    @patch("crsbench.benchmark_ci.cli.commands.coverage_cmd._load_project_capabilities")
    @patch("crsbench.benchmark_ci.cli.commands.coverage_cmd.resolve_benchmark_paths")
    def test_coverage_output_writes_json(
        self,
        mock_discover,
        mock_caps,
        mock_harness,
        mock_executor_cls,
        mock_table,
        tmp_path,
    ):
        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_caps.return_value = (True, None)
        mock_harness.return_value = ["fuzz_target"]
        mock_executor_cls.return_value.execute.return_value = (
            _make_coverage_dag_results("bench1")
        )

        output_file = tmp_path / "out.json"
        parser = _make_parser()
        args = parser.parse_args(
            ["ci", "coverage", "--all", "--output", str(output_file)]
        )
        result = dispatch_ci(args)

        assert result == 0
        assert output_file.exists()
        data = json.loads(output_file.read_text())
        assert "summary" in data

    @patch("crsbench.benchmark_ci.cli.commands.coverage_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.coverage_cmd.DAGExecutor")
    @patch("crsbench.benchmark_ci.cli.commands.coverage_cmd.discover_harness_names")
    @patch("crsbench.benchmark_ci.cli.commands.coverage_cmd._load_project_capabilities")
    @patch("crsbench.benchmark_ci.cli.commands.coverage_cmd.resolve_benchmark_paths")
    def test_coverage_uses_all_check_mode(
        self, mock_discover, mock_caps, mock_harness, mock_executor_cls, mock_table
    ):
        from crsbench.benchmark_ci.models import CheckMode

        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_caps.return_value = (True, None)
        mock_harness.return_value = ["fuzz_target"]
        mock_executor_cls.return_value.execute.return_value = (
            _make_coverage_dag_results("bench1")
        )

        parser = _make_parser()
        args = parser.parse_args(["ci", "coverage", "--all"])
        dispatch_ci(args)

        call_args = mock_table.call_args
        assert call_args.kwargs.get("check_mode") == CheckMode.ALL

    @patch("crsbench.benchmark_ci.cli.commands.coverage_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.coverage_cmd.DAGExecutor")
    @patch("crsbench.benchmark_ci.cli.commands.coverage_cmd.discover_harness_names")
    @patch("crsbench.benchmark_ci.cli.commands.coverage_cmd._load_project_capabilities")
    @patch("crsbench.benchmark_ci.cli.commands.coverage_cmd.resolve_benchmark_paths")
    def test_coverage_default_uses_inc_build(
        self, mock_discover, mock_caps, mock_harness, mock_executor_cls, mock_table
    ):
        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_caps.return_value = (True, None)
        mock_harness.return_value = ["fuzz_target"]
        mock_executor_cls.return_value.execute.return_value = (
            _make_coverage_dag_results("bench1")
        )

        parser = _make_parser()
        args = parser.parse_args(["ci", "coverage", "--all"])
        dispatch_ci(args)

        call_args = mock_executor_cls.return_value.execute.call_args
        jobs = call_args[0][0]
        build_job = next(j for j in jobs if j.job_id == "build-variants:bench1")
        assert build_job.use_inc_build is True
        assert build_job.force_rebuild is True

    @patch("crsbench.benchmark_ci.cli.commands.coverage_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.coverage_cmd.DAGExecutor")
    @patch("crsbench.benchmark_ci.cli.commands.coverage_cmd.discover_harness_names")
    @patch("crsbench.benchmark_ci.cli.commands.coverage_cmd._load_project_capabilities")
    @patch("crsbench.benchmark_ci.cli.commands.coverage_cmd.resolve_benchmark_paths")
    def test_coverage_no_inc_build_flag(
        self, mock_discover, mock_caps, mock_harness, mock_executor_cls, mock_table
    ):
        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_caps.return_value = (True, None)
        mock_harness.return_value = ["fuzz_target"]
        mock_executor_cls.return_value.execute.return_value = (
            _make_coverage_dag_results("bench1")
        )

        parser = _make_parser()
        args = parser.parse_args(["ci", "coverage", "--all", "--no-inc-build"])
        dispatch_ci(args)

        call_args = mock_executor_cls.return_value.execute.call_args
        jobs = call_args[0][0]
        build_job = next(j for j in jobs if j.job_id == "build-variants:bench1")
        assert build_job.use_inc_build is False


# --- Test All subcommand integration ---


def _make_all_dag_results(benchmark_name: str, *, success: bool = True):
    """Create DAG results for all checks (POV + patch + coverage)."""
    from crsbench.executor.types import ExecutorResult, JobStatus

    status = JobStatus.SUCCESS if success else JobStatus.FAILED
    return {
        f"build-variants:{benchmark_name}": ExecutorResult(
            job_id=f"build-variants:{benchmark_name}",
            status=JobStatus.SUCCESS,
            elapsed_seconds=5.0,
        ),
        f"verify-cpv-pov:{benchmark_name}:cpv_0": ExecutorResult(
            job_id=f"verify-cpv-pov:{benchmark_name}:cpv_0",
            status=status,
            elapsed_seconds=2.0,
        ),
        f"build-patch:{benchmark_name}:cpv_0:patch_0": ExecutorResult(
            job_id=f"build-patch:{benchmark_name}:cpv_0:patch_0",
            status=JobStatus.SUCCESS,
            elapsed_seconds=3.0,
        ),
        f"test-patch:{benchmark_name}:cpv_0:patch_0:FULL": ExecutorResult(
            job_id=f"test-patch:{benchmark_name}:cpv_0:patch_0:FULL",
            status=status,
            elapsed_seconds=4.0,
        ),
        f"test-patch:{benchmark_name}:cpv_0:patch_0:RTS": ExecutorResult(
            job_id=f"test-patch:{benchmark_name}:cpv_0:patch_0:RTS",
            status=status,
            elapsed_seconds=3.0,
        ),
        f"collect-coverage:{benchmark_name}": ExecutorResult(
            job_id=f"collect-coverage:{benchmark_name}",
            status=status,
            elapsed_seconds=10.0,
        ),
    }


def _setup_all_cmd_mocks(
    mock_discover,
    mock_fmt,
    mock_caps,
    mock_harness,
    mock_cpvs,
    mock_patches,
    mock_povs,
    mock_executor_cls,
    mock_adapter=None,
    *,
    paths=None,
    rts_mode="jcgeks",
    supports_inc=True,
    success=True,
):
    """Common setup for all_cmd tests."""
    from crsbench.benchmark_ci.models import (
        BenchmarkValidationResult,
        CheckResult,
        CheckStatus,
    )

    if paths is None:
        paths = [Path("/tmp/bench1")]
    mock_discover.return_value = paths
    mock_caps.return_value = (supports_inc, rts_mode)
    # validate_format returns BenchmarkValidationResult with format_check
    mock_fmt.side_effect = lambda path, _source_mode: BenchmarkValidationResult(
        benchmark=path.name,
        benchmark_path=path,
        format_check=CheckResult(status=CheckStatus.PASS, time_seconds=0.1),
    )
    mock_harness.return_value = ["fuzz_target"]
    mock_cpvs.return_value = ["cpv_0"]
    mock_patches.return_value = [("patch_0", Path("/tmp/patch_0.diff"))]
    mock_povs.return_value = [Path("/tmp/pov_0.blob")]

    # Setup mock adapter for _load_benchmark_adapter
    if mock_adapter is not None:
        adapter = MagicMock()
        adapter.get_ref_commit.return_value = "abc123def456"
        adapter.get_base_commit.return_value = "base123456"
        adapter.main_repo = "https://github.com/test/repo.git"
        adapter.lang = "c"
        adapter.repo_name = None
        mock_adapter.return_value = adapter

    results = {}
    for p in paths:
        results.update(_make_all_dag_results(p.name, success=success))
    mock_executor_cls.return_value.execute.return_value = results


_ALL_CMD_PATCHES = [
    "crsbench.benchmark_ci.cli.commands.all_cmd.print_results_table",
    "crsbench.benchmark_ci.cli.commands.all_cmd.DAGExecutor",
    "crsbench.benchmark_ci.cli.commands.all_cmd.discover_pov_paths",
    "crsbench.benchmark_ci.cli.commands.all_cmd.discover_patch_paths",
    "crsbench.benchmark_ci.cli.commands.all_cmd.discover_cpv_ids",
    "crsbench.benchmark_ci.cli.commands.all_cmd.discover_harness_names",
    "crsbench.benchmark_ci.cli.commands.all_cmd._load_project_capabilities",
    "crsbench.benchmark_ci.cli.commands.all_cmd.validate_format",
    "crsbench.benchmark_ci.cli.commands.all_cmd.resolve_benchmark_paths",
]


class TestAllSubcommand:
    """Integration tests for the All subcommand."""

    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.DAGExecutor")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.discover_pov_paths")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.discover_patch_paths")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.discover_cpv_ids")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.discover_harness_names")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd._load_benchmark_adapter")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd._load_project_capabilities")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.validate_format")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.resolve_benchmark_paths")
    def test_all_pass_returns_0(
        self,
        mock_discover,
        mock_fmt,
        mock_caps,
        mock_adapter,
        mock_harness,
        mock_cpvs,
        mock_patches,
        mock_povs,
        mock_executor_cls,
        mock_table,
    ):
        _setup_all_cmd_mocks(
            mock_discover,
            mock_fmt,
            mock_caps,
            mock_harness,
            mock_cpvs,
            mock_patches,
            mock_povs,
            mock_executor_cls,
            mock_adapter,
        )

        parser = _make_parser()
        args = parser.parse_args(["ci", "all", "--all"])
        result = dispatch_ci(args)

        assert result == 0
        mock_table.assert_called_once()

    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.DAGExecutor")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.discover_pov_paths")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.discover_patch_paths")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.discover_cpv_ids")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.discover_harness_names")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd._load_benchmark_adapter")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd._load_project_capabilities")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.validate_format")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.resolve_benchmark_paths")
    def test_all_fail_returns_1(
        self,
        mock_discover,
        mock_fmt,
        mock_caps,
        mock_adapter,
        mock_harness,
        mock_cpvs,
        mock_patches,
        mock_povs,
        mock_executor_cls,
        mock_table,
    ):
        _setup_all_cmd_mocks(
            mock_discover,
            mock_fmt,
            mock_caps,
            mock_harness,
            mock_cpvs,
            mock_patches,
            mock_povs,
            mock_executor_cls,
            mock_adapter,
            success=False,
        )

        parser = _make_parser()
        args = parser.parse_args(["ci", "all", "--all"])
        result = dispatch_ci(args)

        assert result == 1

    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.DAGExecutor")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.discover_pov_paths")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.discover_patch_paths")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.discover_cpv_ids")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.discover_harness_names")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd._load_benchmark_adapter")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd._load_project_capabilities")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.validate_format")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.resolve_benchmark_paths")
    def test_all_output_writes_json(
        self,
        mock_discover,
        mock_fmt,
        mock_caps,
        mock_adapter,
        mock_harness,
        mock_cpvs,
        mock_patches,
        mock_povs,
        mock_executor_cls,
        mock_table,
        tmp_path,
    ):
        _setup_all_cmd_mocks(
            mock_discover,
            mock_fmt,
            mock_caps,
            mock_harness,
            mock_cpvs,
            mock_patches,
            mock_povs,
            mock_executor_cls,
            mock_adapter,
        )

        output_file = tmp_path / "out.json"
        parser = _make_parser()
        args = parser.parse_args(["ci", "all", "--all", "--output", str(output_file)])
        result = dispatch_ci(args)

        assert result == 0
        assert output_file.exists()
        data = json.loads(output_file.read_text())
        assert "summary" in data

    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.DAGExecutor")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.discover_pov_paths")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.discover_patch_paths")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.discover_cpv_ids")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.discover_harness_names")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd._load_benchmark_adapter")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd._load_project_capabilities")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.validate_format")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.resolve_benchmark_paths")
    def test_all_uses_all_check_mode(
        self,
        mock_discover,
        mock_fmt,
        mock_caps,
        mock_adapter,
        mock_harness,
        mock_cpvs,
        mock_patches,
        mock_povs,
        mock_executor_cls,
        mock_table,
    ):
        from crsbench.benchmark_ci.models import CheckMode

        _setup_all_cmd_mocks(
            mock_discover,
            mock_fmt,
            mock_caps,
            mock_harness,
            mock_cpvs,
            mock_patches,
            mock_povs,
            mock_executor_cls,
            mock_adapter,
        )

        parser = _make_parser()
        args = parser.parse_args(["ci", "all", "--all"])
        dispatch_ci(args)

        call_args = mock_table.call_args
        assert call_args.kwargs.get("check_mode") == CheckMode.ALL

    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.DAGExecutor")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.discover_pov_paths")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.discover_patch_paths")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.discover_cpv_ids")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.discover_harness_names")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd._load_benchmark_adapter")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd._load_project_capabilities")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.validate_format")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.resolve_benchmark_paths")
    def test_all_single_build_per_benchmark(
        self,
        mock_discover,
        mock_fmt,
        mock_caps,
        mock_adapter,
        mock_harness,
        mock_cpvs,
        mock_patches,
        mock_povs,
        mock_executor_cls,
        mock_table,
    ):
        """ci all: Creates BuildSingleVariantJob for each variant (vulnerable, allpatched, cpv)."""
        _setup_all_cmd_mocks(
            mock_discover,
            mock_fmt,
            mock_caps,
            mock_harness,
            mock_cpvs,
            mock_patches,
            mock_povs,
            mock_executor_cls,
            mock_adapter,
        )

        parser = _make_parser()
        args = parser.parse_args(["ci", "all", "--all"])
        dispatch_ci(args)

        call_args = mock_executor_cls.return_value.execute.call_args
        jobs = call_args[0][0]
        from crsbench.benchmark_ci.jobs.flat import BuildSingleVariantJob

        # 3 BuildSingleVariantJob per benchmark with 1 CPV:
        # - deltaref (vulnerable)
        # - allpatched (all patches applied)
        # - cpv0 (excludes cpv_0's patch, so cpv_0 vuln is present for POV verification)
        build_jobs = [j for j in jobs if isinstance(j, BuildSingleVariantJob)]
        assert len(build_jobs) == 3

    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.DAGExecutor")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.discover_pov_paths")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.discover_patch_paths")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.discover_cpv_ids")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.discover_harness_names")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd._load_benchmark_adapter")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd._load_project_capabilities")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.validate_format")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.resolve_benchmark_paths")
    def test_all_no_inc_support_uses_full_build(
        self,
        mock_discover,
        mock_fmt,
        mock_caps,
        mock_adapter,
        mock_harness,
        mock_cpvs,
        mock_patches,
        mock_povs,
        mock_executor_cls,
        mock_table,
    ):
        _setup_all_cmd_mocks(
            mock_discover,
            mock_fmt,
            mock_caps,
            mock_harness,
            mock_cpvs,
            mock_patches,
            mock_povs,
            mock_executor_cls,
            mock_adapter,
            supports_inc=False,
        )

        parser = _make_parser()
        args = parser.parse_args(["ci", "all", "--all"])
        dispatch_ci(args)

        call_args = mock_executor_cls.return_value.execute.call_args
        jobs = call_args[0][0]
        from crsbench.benchmark_ci.jobs.flat import BuildSingleVariantJob

        # Find any BuildSingleVariantJob for bench1
        build_job = next(
            j
            for j in jobs
            if isinstance(j, BuildSingleVariantJob) and "bench1" in j.job_id
        )
        assert build_job.use_inc_build is False

    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.DAGExecutor")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.discover_pov_paths")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.discover_patch_paths")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.discover_cpv_ids")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.discover_harness_names")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd._load_benchmark_adapter")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd._load_project_capabilities")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.validate_format")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.resolve_benchmark_paths")
    def test_all_multiple_benchmarks(
        self,
        mock_discover,
        mock_fmt,
        mock_caps,
        mock_adapter,
        mock_harness,
        mock_cpvs,
        mock_patches,
        mock_povs,
        mock_executor_cls,
        mock_table,
    ):
        _setup_all_cmd_mocks(
            mock_discover,
            mock_fmt,
            mock_caps,
            mock_harness,
            mock_cpvs,
            mock_patches,
            mock_povs,
            mock_executor_cls,
            mock_adapter,
            paths=[Path("/tmp/bench1"), Path("/tmp/bench2"), Path("/tmp/bench3")],
            supports_inc=False,
            rts_mode=None,
        )

        parser = _make_parser()
        args = parser.parse_args(["ci", "all", "--all"])
        result = dispatch_ci(args)

        assert result == 0
        call_args = mock_table.call_args
        summary = call_args[0][0]
        assert len(summary.results) == 3

    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.DAGExecutor")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.discover_pov_paths")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.discover_patch_paths")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.discover_cpv_ids")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.discover_harness_names")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd._load_benchmark_adapter")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd._load_project_capabilities")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.validate_format")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.resolve_benchmark_paths")
    def test_all_default_workers(
        self,
        mock_discover,
        mock_fmt,
        mock_caps,
        mock_adapter,
        mock_harness,
        mock_cpvs,
        mock_patches,
        mock_povs,
        mock_executor_cls,
        mock_table,
    ):
        _setup_all_cmd_mocks(
            mock_discover,
            mock_fmt,
            mock_caps,
            mock_harness,
            mock_cpvs,
            mock_patches,
            mock_povs,
            mock_executor_cls,
            mock_adapter,
        )

        parser = _make_parser()
        args = parser.parse_args(["ci", "all", "--all"])
        dispatch_ci(args)

        assert args.build_workers == 4
        assert args.verify_workers == 4

    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.DAGExecutor")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.discover_pov_paths")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.discover_patch_paths")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.discover_cpv_ids")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.discover_harness_names")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd._load_benchmark_adapter")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd._load_project_capabilities")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.validate_format")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.resolve_benchmark_paths")
    def test_all_default_uses_inc_build_and_force_rebuild(
        self,
        mock_discover,
        mock_fmt,
        mock_caps,
        mock_adapter,
        mock_harness,
        mock_cpvs,
        mock_patches,
        mock_povs,
        mock_executor_cls,
        mock_table,
    ):
        _setup_all_cmd_mocks(
            mock_discover,
            mock_fmt,
            mock_caps,
            mock_harness,
            mock_cpvs,
            mock_patches,
            mock_povs,
            mock_executor_cls,
            mock_adapter,
        )

        parser = _make_parser()
        args = parser.parse_args(["ci", "all", "--all"])
        dispatch_ci(args)

        call_args = mock_executor_cls.return_value.execute.call_args
        jobs = call_args[0][0]
        from crsbench.benchmark_ci.jobs.flat import BuildSingleVariantJob

        # Find any BuildSingleVariantJob for bench1
        build_job = next(
            j
            for j in jobs
            if isinstance(j, BuildSingleVariantJob) and "bench1" in j.job_id
        )
        # ci all: inc-build by default when project supports it
        assert build_job.use_inc_build is True
        # ci all: always force-rebuild by default
        assert build_job.force_rebuild is True

    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.DAGExecutor")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.discover_pov_paths")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.discover_patch_paths")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.discover_cpv_ids")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.discover_harness_names")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd._load_benchmark_adapter")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd._load_project_capabilities")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.validate_format")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.resolve_benchmark_paths")
    def test_all_no_inc_build_flag(
        self,
        mock_discover,
        mock_fmt,
        mock_caps,
        mock_adapter,
        mock_harness,
        mock_cpvs,
        mock_patches,
        mock_povs,
        mock_executor_cls,
        mock_table,
    ):
        _setup_all_cmd_mocks(
            mock_discover,
            mock_fmt,
            mock_caps,
            mock_harness,
            mock_cpvs,
            mock_patches,
            mock_povs,
            mock_executor_cls,
            mock_adapter,
        )

        parser = _make_parser()
        args = parser.parse_args(["ci", "all", "--all", "--no-inc-build"])
        dispatch_ci(args)

        call_args = mock_executor_cls.return_value.execute.call_args
        jobs = call_args[0][0]
        from crsbench.benchmark_ci.jobs.flat import BuildSingleVariantJob

        # Find any BuildSingleVariantJob for bench1
        build_job = next(
            j
            for j in jobs
            if isinstance(j, BuildSingleVariantJob) and "bench1" in j.job_id
        )
        assert build_job.use_inc_build is False

    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.DAGExecutor")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.discover_pov_paths")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.discover_patch_paths")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.discover_cpv_ids")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.discover_harness_names")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd._load_benchmark_adapter")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd._load_project_capabilities")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.validate_format")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.resolve_benchmark_paths")
    def test_all_calls_validate_format(
        self,
        mock_discover,
        mock_fmt,
        mock_caps,
        mock_adapter,
        mock_harness,
        mock_cpvs,
        mock_patches,
        mock_povs,
        mock_executor_cls,
        mock_table,
    ):
        _setup_all_cmd_mocks(
            mock_discover,
            mock_fmt,
            mock_caps,
            mock_harness,
            mock_cpvs,
            mock_patches,
            mock_povs,
            mock_executor_cls,
            mock_adapter,
        )

        parser = _make_parser()
        args = parser.parse_args(["ci", "all", "--all"])
        dispatch_ci(args)

        mock_fmt.assert_called_once()

    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.DAGExecutor")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.discover_pov_paths")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.discover_patch_paths")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.discover_cpv_ids")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.discover_harness_names")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd._load_benchmark_adapter")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd._load_project_capabilities")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.validate_format")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.resolve_benchmark_paths")
    def test_all_handles_mixed_sanitizers(
        self,
        mock_discover,
        mock_fmt,
        mock_caps,
        mock_adapter,
        mock_harness,
        mock_cpvs,
        mock_patches,
        mock_povs,
        mock_executor_cls,
        mock_table,
    ):
        """Test that all command handles benchmarks with mixed sanitizers across harnesses."""
        from crsbench.benchmark_ci.models import (
            BenchmarkValidationResult,
            CheckResult,
            CheckStatus,
        )

        # Setup mocks
        paths = [Path("/tmp/bench1")]
        mock_discover.return_value = paths
        mock_caps.return_value = (True, "jcgeks")  # supports_inc, rts_mode
        mock_fmt.side_effect = lambda path, _source_mode: BenchmarkValidationResult(
            benchmark=path.name,
            benchmark_path=path,
            format_check=CheckResult(status=CheckStatus.PASS, time_seconds=0.1),
        )

        # Mock two harnesses with different sanitizers
        mock_harness.return_value = ["harness_addr", "harness_undef"]
        # Return different CPVs for each harness
        mock_cpvs.side_effect = lambda _path, harness: (
            ["cpv_0"] if harness == "harness_addr" else ["cpv_1"]
        )
        mock_patches.return_value = [("patch_0", Path("/tmp/patch_0.diff"))]
        mock_povs.return_value = [Path("/tmp/pov_0.blob")]

        # Setup mock adapter with mixed sanitizers
        adapter = MagicMock()
        adapter.get_ref_commit.return_value = "abc123def456"
        adapter.get_base_commit.return_value = "base123456"
        adapter.main_repo = "https://github.com/test/repo.git"
        adapter.lang = "c"
        adapter.repo_name = None
        adapter.get_mode.return_value.value = "delta"

        # get_all_cpv_sanitizers returns both sanitizers used (supports mixed)
        adapter.get_all_cpv_sanitizers.return_value = ["address", "undefined"]

        # get_cpv_sanitizer returns per-CPV sanitizers (supports mixed sanitizers)
        def cpv_sanitizer(harness_name, cpv_id):
            if harness_name == "harness_addr" and cpv_id == "cpv_0":
                return "address"
            if harness_name == "harness_undef" and cpv_id == "cpv_1":
                return "undefined"
            return "address"

        adapter.get_cpv_sanitizer.side_effect = cpv_sanitizer
        mock_adapter.return_value = adapter

        # Mock executor results - need results for both CPVs
        from crsbench.executor.types import ExecutorResult, JobStatus

        results = {
            # Build jobs (6 total: 2 deltaref, 2 allpatched, 2 cpv - one for each sanitizer)
            "build-single:bench1-asan-deltaref": ExecutorResult(
                job_id="build-single:bench1-asan-deltaref",
                status=JobStatus.SUCCESS,
                elapsed_seconds=5.0,
            ),
            "build-single:bench1-asan-delta-allpatched": ExecutorResult(
                job_id="build-single:bench1-asan-delta-allpatched",
                status=JobStatus.SUCCESS,
                elapsed_seconds=5.0,
            ),
            "build-single:bench1-ubsan-deltaref": ExecutorResult(
                job_id="build-single:bench1-ubsan-deltaref",
                status=JobStatus.SUCCESS,
                elapsed_seconds=5.0,
            ),
            "build-single:bench1-ubsan-delta-allpatched": ExecutorResult(
                job_id="build-single:bench1-ubsan-delta-allpatched",
                status=JobStatus.SUCCESS,
                elapsed_seconds=5.0,
            ),
            "build-single:bench1-asan-delta-cpv0": ExecutorResult(
                job_id="build-single:bench1-asan-delta-cpv0",
                status=JobStatus.SUCCESS,
                elapsed_seconds=5.0,
            ),
            "build-single:bench1-ubsan-delta-cpv1": ExecutorResult(
                job_id="build-single:bench1-ubsan-delta-cpv1",
                status=JobStatus.SUCCESS,
                elapsed_seconds=5.0,
            ),
            # POV verify jobs (2 total: cpv_0, cpv_1)
            "verify-cpv-pov:bench1:cpv_0": ExecutorResult(
                job_id="verify-cpv-pov:bench1:cpv_0",
                status=JobStatus.SUCCESS,
                elapsed_seconds=2.0,
            ),
            "verify-cpv-pov:bench1:cpv_1": ExecutorResult(
                job_id="verify-cpv-pov:bench1:cpv_1",
                status=JobStatus.SUCCESS,
                elapsed_seconds=2.0,
            ),
            # Patch build jobs (2 total: cpv_0:patch_0, cpv_1:patch_0)
            "build-patch:bench1:cpv_0:patch_0": ExecutorResult(
                job_id="build-patch:bench1:cpv_0:patch_0",
                status=JobStatus.SUCCESS,
                elapsed_seconds=3.0,
            ),
            "build-patch:bench1:cpv_1:patch_0": ExecutorResult(
                job_id="build-patch:bench1:cpv_1:patch_0",
                status=JobStatus.SUCCESS,
                elapsed_seconds=3.0,
            ),
            # Patch test jobs (4 total: cpv_0:FULL, cpv_0:RTS, cpv_1:FULL, cpv_1:RTS)
            "test-patch:bench1:cpv_0:patch_0:FULL": ExecutorResult(
                job_id="test-patch:bench1:cpv_0:patch_0:FULL",
                status=JobStatus.SUCCESS,
                elapsed_seconds=4.0,
            ),
            "test-patch:bench1:cpv_0:patch_0:RTS": ExecutorResult(
                job_id="test-patch:bench1:cpv_0:patch_0:RTS",
                status=JobStatus.SUCCESS,
                elapsed_seconds=3.0,
            ),
            "test-patch:bench1:cpv_1:patch_0:FULL": ExecutorResult(
                job_id="test-patch:bench1:cpv_1:patch_0:FULL",
                status=JobStatus.SUCCESS,
                elapsed_seconds=4.0,
            ),
            "test-patch:bench1:cpv_1:patch_0:RTS": ExecutorResult(
                job_id="test-patch:bench1:cpv_1:patch_0:RTS",
                status=JobStatus.SUCCESS,
                elapsed_seconds=3.0,
            ),
        }
        mock_executor_cls.return_value.execute.return_value = results

        # Execute command
        parser = _make_parser()
        args = parser.parse_args(["ci", "all", "--all"])
        result = dispatch_ci(args)

        # Verify success
        assert result == 0

        # Verify get_cpv_sanitizer was called for each CPV
        assert adapter.get_cpv_sanitizer.call_count == 2
        adapter.get_cpv_sanitizer.assert_any_call("harness_addr", "cpv_0")
        adapter.get_cpv_sanitizer.assert_any_call("harness_undef", "cpv_1")
