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
from unittest.mock import patch

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

    def test_ci_subparser_format_has_no_build_options(self):
        parser = _make_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["ci", "format", "--source", "pkgs"])

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
                "crsbench.benchmark_ci.cli.commands.format_cmd.format_validate"
            ) as mock_validate,
            patch("crsbench.benchmark_ci.cli.commands.format_cmd.print_results_table"),
        ):
            from crsbench.validation.errors import ValidationResult

            mock_discover.return_value = [Path("/tmp/bench1")]
            mock_validate.return_value = ValidationResult(is_valid=True, issues=[])
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
    @patch("crsbench.benchmark_ci.cli.commands.format_cmd.format_validate")
    @patch("crsbench.benchmark_ci.cli.commands.format_cmd.resolve_benchmark_paths")
    def test_format_all_pass_returns_0(self, mock_discover, mock_validate, mock_table):
        from crsbench.validation.errors import ValidationResult

        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_validate.return_value = ValidationResult(is_valid=True, issues=[])

        parser = _make_parser()
        args = parser.parse_args(["ci", "format", "--all"])
        result = dispatch_ci(args)

        assert result == 0
        mock_table.assert_called_once()

    @patch("crsbench.benchmark_ci.cli.commands.format_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.format_cmd.format_validate")
    @patch("crsbench.benchmark_ci.cli.commands.format_cmd.resolve_benchmark_paths")
    def test_format_fail_returns_1(self, mock_discover, mock_validate, mock_table):
        from crsbench.validation.errors import (
            ValidationIssue,
            ValidationResult,
            ValidationSeverity,
        )

        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_validate.return_value = ValidationResult(
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
    @patch("crsbench.benchmark_ci.cli.commands.format_cmd.format_validate")
    @patch("crsbench.benchmark_ci.cli.commands.format_cmd.resolve_benchmark_paths")
    def test_format_calls_print_results_table(
        self, mock_discover, mock_validate, mock_table
    ):
        from crsbench.benchmark_ci.models import ValidationSummary
        from crsbench.validation.errors import ValidationResult

        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_validate.return_value = ValidationResult(is_valid=True, issues=[])

        parser = _make_parser()
        args = parser.parse_args(["ci", "format", "--all"])
        dispatch_ci(args)

        mock_table.assert_called_once()
        call_args = mock_table.call_args
        assert isinstance(call_args[0][0], ValidationSummary)

    @patch("crsbench.benchmark_ci.cli.commands.format_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.format_cmd.format_validate")
    @patch("crsbench.benchmark_ci.cli.commands.format_cmd.resolve_benchmark_paths")
    def test_format_exception_returns_error_status(
        self, mock_discover, mock_validate, mock_table
    ):
        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_validate.side_effect = Exception("boom")

        parser = _make_parser()
        args = parser.parse_args(["ci", "format", "--all"])
        result = dispatch_ci(args)

        assert result == 1

    @patch("crsbench.benchmark_ci.cli.commands.format_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.format_cmd.format_validate")
    @patch("crsbench.benchmark_ci.cli.commands.format_cmd.resolve_benchmark_paths")
    def test_format_output_writes_json(
        self, mock_discover, mock_validate, mock_table, tmp_path
    ):
        from crsbench.validation.errors import ValidationResult

        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_validate.return_value = ValidationResult(is_valid=True, issues=[])

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
    @patch("crsbench.benchmark_ci.cli.commands.format_cmd.format_validate")
    @patch("crsbench.benchmark_ci.cli.commands.format_cmd.resolve_benchmark_paths")
    def test_format_multiple_benchmarks_mixed_results(
        self, mock_discover, mock_validate, mock_table
    ):
        from crsbench.validation.errors import (
            ValidationIssue,
            ValidationResult,
            ValidationSeverity,
        )

        mock_discover.return_value = [Path("/tmp/bench1"), Path("/tmp/bench2")]
        mock_validate.side_effect = [
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


# --- Test POV subcommand integration ---


class TestPovSubcommand:
    """Integration tests for the POV subcommand."""

    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd._load_project_capabilities")
    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd.BenchmarkValidator")
    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd.resolve_benchmark_paths")
    def test_pov_all_pass_returns_0(
        self, mock_discover, mock_validator_cls, mock_caps, mock_table
    ):
        from crsbench.benchmark_ci.models import CheckResult, CheckStatus

        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_caps.return_value = (True, None)
        mock_validator = mock_validator_cls.return_value
        mock_validator.validate_povs.return_value = CheckResult(
            status=CheckStatus.PASS, time_seconds=5.0
        )

        parser = _make_parser()
        args = parser.parse_args(["ci", "pov", "--all"])
        result = dispatch_ci(args)

        assert result == 0
        mock_table.assert_called_once()

    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd._load_project_capabilities")
    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd.BenchmarkValidator")
    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd.resolve_benchmark_paths")
    def test_pov_fail_returns_1(
        self, mock_discover, mock_validator_cls, mock_caps, mock_table
    ):
        from crsbench.benchmark_ci.models import CheckResult, CheckStatus

        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_caps.return_value = (True, None)
        mock_validator = mock_validator_cls.return_value
        mock_validator.validate_povs.return_value = CheckResult(
            status=CheckStatus.FAIL,
            time_seconds=3.0,
            details={"failures": ["cpv_0/pov_0.blob: NOT_VULNERABLE"]},
        )

        parser = _make_parser()
        args = parser.parse_args(["ci", "pov", "--all"])
        result = dispatch_ci(args)

        assert result == 1

    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd._load_project_capabilities")
    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd.BenchmarkValidator")
    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd.resolve_benchmark_paths")
    def test_pov_output_writes_json(
        self, mock_discover, mock_validator_cls, mock_caps, mock_table, tmp_path
    ):
        from crsbench.benchmark_ci.models import CheckResult, CheckStatus

        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_caps.return_value = (True, None)
        mock_validator = mock_validator_cls.return_value
        mock_validator.validate_povs.return_value = CheckResult(
            status=CheckStatus.PASS, time_seconds=5.0
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
    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd._load_project_capabilities")
    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd.BenchmarkValidator")
    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd.resolve_benchmark_paths")
    def test_pov_exception_returns_error(
        self, mock_discover, mock_validator_cls, mock_caps, mock_table
    ):
        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_caps.return_value = (True, None)
        mock_validator = mock_validator_cls.return_value
        mock_validator.validate_povs.side_effect = Exception("docker failed")

        parser = _make_parser()
        args = parser.parse_args(["ci", "pov", "--all"])
        result = dispatch_ci(args)

        assert result == 1

    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd._load_project_capabilities")
    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd.BenchmarkValidator")
    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd.resolve_benchmark_paths")
    def test_pov_default_uses_inc_build(
        self, mock_discover, mock_validator_cls, mock_caps, mock_table
    ):
        from crsbench.benchmark_ci.models import CheckResult, CheckStatus

        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_caps.return_value = (True, None)
        mock_validator = mock_validator_cls.return_value
        mock_validator.validate_povs.return_value = CheckResult(
            status=CheckStatus.PASS, time_seconds=5.0
        )

        parser = _make_parser()
        args = parser.parse_args(["ci", "pov", "--all"])
        dispatch_ci(args)

        calls = mock_validator.validate_povs.call_args_list
        # Default: inc-build is used
        assert calls[0].kwargs.get("use_inc_build") is True
        # Default: no force_rebuild for standalone commands
        assert calls[0].kwargs.get("force_rebuild") is False

    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd._load_project_capabilities")
    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd.BenchmarkValidator")
    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd.resolve_benchmark_paths")
    def test_pov_no_inc_build_flag(
        self, mock_discover, mock_validator_cls, mock_caps, mock_table
    ):
        from crsbench.benchmark_ci.models import CheckResult, CheckStatus

        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_caps.return_value = (True, None)
        mock_validator = mock_validator_cls.return_value
        mock_validator.validate_povs.return_value = CheckResult(
            status=CheckStatus.PASS, time_seconds=5.0
        )

        parser = _make_parser()
        args = parser.parse_args(["ci", "pov", "--all", "--no-inc-build"])
        dispatch_ci(args)

        calls = mock_validator.validate_povs.call_args_list
        # --no-inc-build: standard call uses full build
        assert calls[0].kwargs.get("use_inc_build") is False

    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd._load_project_capabilities")
    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd.BenchmarkValidator")
    @patch("crsbench.benchmark_ci.cli.commands.pov_cmd.resolve_benchmark_paths")
    def test_pov_force_rebuild_flag(
        self, mock_discover, mock_validator_cls, mock_caps, mock_table
    ):
        from crsbench.benchmark_ci.models import CheckResult, CheckStatus

        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_caps.return_value = (True, None)
        mock_validator = mock_validator_cls.return_value
        mock_validator.validate_povs.return_value = CheckResult(
            status=CheckStatus.PASS, time_seconds=5.0
        )

        parser = _make_parser()
        args = parser.parse_args(["ci", "pov", "--all", "--force-rebuild"])
        dispatch_ci(args)

        calls = mock_validator.validate_povs.call_args_list
        # --force-rebuild: force_rebuild=True
        assert calls[0].kwargs.get("force_rebuild") is True


# --- Test Patch subcommand integration ---


class TestPatchSubcommand:
    """Integration tests for the Patch subcommand."""

    @patch("crsbench.benchmark_ci.cli.commands.patch_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.patch_cmd._load_project_capabilities")
    @patch("crsbench.benchmark_ci.cli.commands.patch_cmd.BenchmarkValidator")
    @patch("crsbench.benchmark_ci.cli.commands.patch_cmd.resolve_benchmark_paths")
    def test_patch_all_pass_returns_0(
        self, mock_discover, mock_validator_cls, mock_caps, mock_table
    ):
        from crsbench.benchmark_ci.models import CheckResult, CheckStatus

        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_caps.return_value = (True, None)
        mock_validator = mock_validator_cls.return_value
        mock_validator.validate_patches.return_value = CheckResult(
            status=CheckStatus.PASS, time_seconds=8.0
        )

        parser = _make_parser()
        args = parser.parse_args(["ci", "patch", "--all"])
        result = dispatch_ci(args)

        assert result == 0
        mock_table.assert_called_once()

    @patch("crsbench.benchmark_ci.cli.commands.patch_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.patch_cmd._load_project_capabilities")
    @patch("crsbench.benchmark_ci.cli.commands.patch_cmd.BenchmarkValidator")
    @patch("crsbench.benchmark_ci.cli.commands.patch_cmd.resolve_benchmark_paths")
    def test_patch_fail_returns_1(
        self, mock_discover, mock_validator_cls, mock_caps, mock_table
    ):
        from crsbench.benchmark_ci.models import CheckResult, CheckStatus

        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_caps.return_value = (True, None)
        mock_validator = mock_validator_cls.return_value
        mock_validator.validate_patches.return_value = CheckResult(
            status=CheckStatus.FAIL,
            time_seconds=10.0,
            details={"failures": ["cpv_0: test.sh returned non-zero"]},
        )

        parser = _make_parser()
        args = parser.parse_args(["ci", "patch", "--all"])
        result = dispatch_ci(args)

        assert result == 1

    @patch("crsbench.benchmark_ci.cli.commands.patch_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.patch_cmd._load_project_capabilities")
    @patch("crsbench.benchmark_ci.cli.commands.patch_cmd.BenchmarkValidator")
    @patch("crsbench.benchmark_ci.cli.commands.patch_cmd.resolve_benchmark_paths")
    def test_patch_output_writes_json(
        self, mock_discover, mock_validator_cls, mock_caps, mock_table, tmp_path
    ):
        from crsbench.benchmark_ci.models import CheckResult, CheckStatus

        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_caps.return_value = (True, None)
        mock_validator = mock_validator_cls.return_value
        mock_validator.validate_patches.return_value = CheckResult(
            status=CheckStatus.PASS, time_seconds=8.0
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
    @patch("crsbench.benchmark_ci.cli.commands.patch_cmd._load_project_capabilities")
    @patch("crsbench.benchmark_ci.cli.commands.patch_cmd.BenchmarkValidator")
    @patch("crsbench.benchmark_ci.cli.commands.patch_cmd.resolve_benchmark_paths")
    def test_patch_default_uses_inc_build(
        self, mock_discover, mock_validator_cls, mock_caps, mock_table
    ):
        from crsbench.benchmark_ci.models import CheckResult, CheckStatus

        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_caps.return_value = (True, None)
        mock_validator = mock_validator_cls.return_value
        mock_validator.validate_patches.return_value = CheckResult(
            status=CheckStatus.PASS, time_seconds=8.0
        )

        parser = _make_parser()
        args = parser.parse_args(["ci", "patch", "--all"])
        dispatch_ci(args)

        calls = mock_validator.validate_patches.call_args_list
        # Default: inc-build is used
        assert calls[0].kwargs.get("use_inc_build") is True
        # Default: no force_rebuild for standalone commands
        assert calls[0].kwargs.get("force_rebuild") is False

    @patch("crsbench.benchmark_ci.cli.commands.patch_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.patch_cmd._load_project_capabilities")
    @patch("crsbench.benchmark_ci.cli.commands.patch_cmd.BenchmarkValidator")
    @patch("crsbench.benchmark_ci.cli.commands.patch_cmd.resolve_benchmark_paths")
    def test_patch_no_inc_build_flag(
        self, mock_discover, mock_validator_cls, mock_caps, mock_table
    ):
        from crsbench.benchmark_ci.models import CheckResult, CheckStatus

        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_caps.return_value = (True, None)
        mock_validator = mock_validator_cls.return_value
        mock_validator.validate_patches.return_value = CheckResult(
            status=CheckStatus.PASS, time_seconds=8.0
        )

        parser = _make_parser()
        args = parser.parse_args(["ci", "patch", "--all", "--no-inc-build"])
        dispatch_ci(args)

        calls = mock_validator.validate_patches.call_args_list
        # --no-inc-build: full build
        assert calls[0].kwargs.get("use_inc_build") is False

    @patch("crsbench.benchmark_ci.cli.commands.patch_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.patch_cmd._load_project_capabilities")
    @patch("crsbench.benchmark_ci.cli.commands.patch_cmd.BenchmarkValidator")
    @patch("crsbench.benchmark_ci.cli.commands.patch_cmd.resolve_benchmark_paths")
    def test_patch_force_rebuild_flag(
        self, mock_discover, mock_validator_cls, mock_caps, mock_table
    ):
        from crsbench.benchmark_ci.models import CheckResult, CheckStatus

        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_caps.return_value = (True, None)
        mock_validator = mock_validator_cls.return_value
        mock_validator.validate_patches.return_value = CheckResult(
            status=CheckStatus.PASS, time_seconds=8.0
        )

        parser = _make_parser()
        args = parser.parse_args(["ci", "patch", "--all", "--force-rebuild"])
        dispatch_ci(args)

        calls = mock_validator.validate_patches.call_args_list
        assert calls[0].kwargs.get("force_rebuild") is True


# --- Test RTS subcommand integration ---


class TestRtsSubcommand:
    """Integration tests for the RTS subcommand."""

    @patch("crsbench.benchmark_ci.cli.commands.rts_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.rts_cmd._load_project_capabilities")
    @patch("crsbench.benchmark_ci.cli.commands.rts_cmd.BenchmarkValidator")
    @patch("crsbench.benchmark_ci.cli.commands.rts_cmd.resolve_benchmark_paths")
    def test_rts_all_pass_returns_0(
        self, mock_discover, mock_validator_cls, mock_caps, mock_table
    ):
        from crsbench.benchmark_ci.models import CheckResult, CheckStatus

        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_caps.return_value = (True, "jcgeks")
        mock_validator = mock_validator_cls.return_value
        mock_validator.validate_patches.return_value = CheckResult(
            status=CheckStatus.PASS, time_seconds=8.0
        )

        parser = _make_parser()
        args = parser.parse_args(["ci", "rts", "--all"])
        result = dispatch_ci(args)

        assert result == 0
        mock_table.assert_called_once()

    @patch("crsbench.benchmark_ci.cli.commands.rts_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.rts_cmd._load_project_capabilities")
    @patch("crsbench.benchmark_ci.cli.commands.rts_cmd.BenchmarkValidator")
    @patch("crsbench.benchmark_ci.cli.commands.rts_cmd.resolve_benchmark_paths")
    def test_rts_fail_returns_1(
        self, mock_discover, mock_validator_cls, mock_caps, mock_table
    ):
        from crsbench.benchmark_ci.models import CheckResult, CheckStatus

        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_caps.return_value = (True, "jcgeks")
        mock_validator = mock_validator_cls.return_value
        mock_validator.validate_patches.return_value = CheckResult(
            status=CheckStatus.FAIL,
            time_seconds=10.0,
            details={"failures": ["cpv_0: rts test failed"]},
        )

        parser = _make_parser()
        args = parser.parse_args(["ci", "rts", "--all"])
        result = dispatch_ci(args)

        assert result == 1

    @patch("crsbench.benchmark_ci.cli.commands.rts_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.rts_cmd._load_project_capabilities")
    @patch("crsbench.benchmark_ci.cli.commands.rts_cmd.BenchmarkValidator")
    @patch("crsbench.benchmark_ci.cli.commands.rts_cmd.resolve_benchmark_paths")
    def test_rts_no_rts_mode_shows_skip(
        self, mock_discover, mock_validator_cls, mock_caps, mock_table
    ):
        from crsbench.benchmark_ci.models import CheckStatus

        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_caps.return_value = (True, None)  # No rts_mode
        mock_validator = mock_validator_cls.return_value

        parser = _make_parser()
        args = parser.parse_args(["ci", "rts", "--all"])
        result = dispatch_ci(args)

        # validate_patches NOT called
        mock_validator.validate_patches.assert_not_called()
        # Return 0 (skip = no failure)
        assert result == 0
        # Verify SKIP in summary
        call_args = mock_table.call_args
        summary = call_args[0][0]
        assert summary.results[0].patch_rts_check.status == CheckStatus.SKIP

    @patch("crsbench.benchmark_ci.cli.commands.rts_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.rts_cmd._load_project_capabilities")
    @patch("crsbench.benchmark_ci.cli.commands.rts_cmd.BenchmarkValidator")
    @patch("crsbench.benchmark_ci.cli.commands.rts_cmd.resolve_benchmark_paths")
    def test_rts_exception_returns_1(
        self, mock_discover, mock_validator_cls, mock_caps, mock_table
    ):
        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_caps.return_value = (True, "jcgeks")
        mock_validator = mock_validator_cls.return_value
        mock_validator.validate_patches.side_effect = Exception("rts check failed")

        parser = _make_parser()
        args = parser.parse_args(["ci", "rts", "--all"])
        result = dispatch_ci(args)

        assert result == 1

    @patch("crsbench.benchmark_ci.cli.commands.rts_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.rts_cmd._load_project_capabilities")
    @patch("crsbench.benchmark_ci.cli.commands.rts_cmd.BenchmarkValidator")
    @patch("crsbench.benchmark_ci.cli.commands.rts_cmd.resolve_benchmark_paths")
    def test_rts_output_writes_json(
        self, mock_discover, mock_validator_cls, mock_caps, mock_table, tmp_path
    ):
        from crsbench.benchmark_ci.models import CheckResult, CheckStatus

        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_caps.return_value = (True, "jcgeks")
        mock_validator = mock_validator_cls.return_value
        mock_validator.validate_patches.return_value = CheckResult(
            status=CheckStatus.PASS, time_seconds=8.0
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
    @patch("crsbench.benchmark_ci.cli.commands.rts_cmd._load_project_capabilities")
    @patch("crsbench.benchmark_ci.cli.commands.rts_cmd.BenchmarkValidator")
    @patch("crsbench.benchmark_ci.cli.commands.rts_cmd.resolve_benchmark_paths")
    def test_rts_uses_unit_test_mode_rts(
        self, mock_discover, mock_validator_cls, mock_caps, mock_table
    ):
        from crsbench.benchmark_ci.models import CheckResult, CheckStatus
        from crsbench.evaluation.verification.models import UnitTestMode

        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_caps.return_value = (True, "jcgeks")
        mock_validator = mock_validator_cls.return_value
        mock_validator.validate_patches.return_value = CheckResult(
            status=CheckStatus.PASS, time_seconds=8.0
        )

        parser = _make_parser()
        args = parser.parse_args(["ci", "rts", "--all"])
        dispatch_ci(args)

        # validate_patches called twice: build_only + RTS verify
        assert mock_validator.validate_patches.call_count == 2
        calls = mock_validator.validate_patches.call_args_list
        # First call: build_only
        assert calls[0].kwargs.get("build_only") is True
        # Second call: RTS mode
        assert calls[1].kwargs.get("test_mode") == UnitTestMode.RTS


# --- Test Coverage subcommand integration ---


class TestCoverageSubcommand:
    """Integration tests for the Coverage subcommand."""

    @patch("crsbench.benchmark_ci.cli.commands.coverage_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.coverage_cmd.BenchmarkValidator")
    @patch("crsbench.benchmark_ci.cli.commands.coverage_cmd.resolve_benchmark_paths")
    def test_coverage_all_pass_returns_0(
        self, mock_discover, mock_validator_cls, mock_table
    ):
        from crsbench.benchmark_ci.models import CheckResult, CheckStatus

        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_validator = mock_validator_cls.return_value
        mock_validator.validate_coverage.return_value = CheckResult(
            status=CheckStatus.PASS, time_seconds=10.0
        )

        parser = _make_parser()
        args = parser.parse_args(["ci", "coverage", "--all"])
        result = dispatch_ci(args)

        assert result == 0
        mock_table.assert_called_once()

    @patch("crsbench.benchmark_ci.cli.commands.coverage_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.coverage_cmd.BenchmarkValidator")
    @patch("crsbench.benchmark_ci.cli.commands.coverage_cmd.resolve_benchmark_paths")
    def test_coverage_fail_returns_1(
        self, mock_discover, mock_validator_cls, mock_table
    ):
        from crsbench.benchmark_ci.models import CheckResult, CheckStatus

        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_validator = mock_validator_cls.return_value
        mock_validator.validate_coverage.return_value = CheckResult(
            status=CheckStatus.FAIL,
            time_seconds=10.0,
            error="No coverage results generated",
        )

        parser = _make_parser()
        args = parser.parse_args(["ci", "coverage", "--all"])
        result = dispatch_ci(args)

        assert result == 1

    @patch("crsbench.benchmark_ci.cli.commands.coverage_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.coverage_cmd.BenchmarkValidator")
    @patch("crsbench.benchmark_ci.cli.commands.coverage_cmd.resolve_benchmark_paths")
    def test_coverage_exception_returns_1(
        self, mock_discover, mock_validator_cls, mock_table
    ):
        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_validator = mock_validator_cls.return_value
        mock_validator.validate_coverage.side_effect = Exception(
            "coverage engine failed"
        )

        parser = _make_parser()
        args = parser.parse_args(["ci", "coverage", "--all"])
        result = dispatch_ci(args)

        assert result == 1

    @patch("crsbench.benchmark_ci.cli.commands.coverage_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.coverage_cmd.BenchmarkValidator")
    @patch("crsbench.benchmark_ci.cli.commands.coverage_cmd.resolve_benchmark_paths")
    def test_coverage_output_writes_json(
        self, mock_discover, mock_validator_cls, mock_table, tmp_path
    ):
        from crsbench.benchmark_ci.models import CheckResult, CheckStatus

        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_validator = mock_validator_cls.return_value
        mock_validator.validate_coverage.return_value = CheckResult(
            status=CheckStatus.PASS, time_seconds=10.0
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
    @patch("crsbench.benchmark_ci.cli.commands.coverage_cmd.BenchmarkValidator")
    @patch("crsbench.benchmark_ci.cli.commands.coverage_cmd.resolve_benchmark_paths")
    def test_coverage_calls_validate_coverage(
        self, mock_discover, mock_validator_cls, mock_table
    ):
        from crsbench.benchmark_ci.models import CheckResult, CheckStatus

        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_validator = mock_validator_cls.return_value
        mock_validator.validate_coverage.return_value = CheckResult(
            status=CheckStatus.PASS, time_seconds=10.0
        )

        parser = _make_parser()
        args = parser.parse_args(["ci", "coverage", "--all"])
        dispatch_ci(args)

        mock_validator.validate_coverage.assert_called_once()

    @patch("crsbench.benchmark_ci.cli.commands.coverage_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.coverage_cmd.BenchmarkValidator")
    @patch("crsbench.benchmark_ci.cli.commands.coverage_cmd.resolve_benchmark_paths")
    def test_coverage_uses_default_check_mode(
        self, mock_discover, mock_validator_cls, mock_table
    ):
        from crsbench.benchmark_ci.models import CheckMode, CheckResult, CheckStatus

        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_validator = mock_validator_cls.return_value
        mock_validator.validate_coverage.return_value = CheckResult(
            status=CheckStatus.PASS, time_seconds=10.0
        )

        parser = _make_parser()
        args = parser.parse_args(["ci", "coverage", "--all"])
        dispatch_ci(args)

        call_args = mock_table.call_args
        assert call_args.kwargs.get("check_mode") == CheckMode.ALL

    @patch("crsbench.benchmark_ci.cli.commands.coverage_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.coverage_cmd.BenchmarkValidator")
    @patch("crsbench.benchmark_ci.cli.commands.coverage_cmd.resolve_benchmark_paths")
    def test_coverage_default_uses_inc_build(
        self, mock_discover, mock_validator_cls, mock_table
    ):
        from crsbench.benchmark_ci.models import CheckResult, CheckStatus

        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_validator = mock_validator_cls.return_value
        mock_validator.validate_coverage.return_value = CheckResult(
            status=CheckStatus.PASS, time_seconds=10.0
        )

        parser = _make_parser()
        args = parser.parse_args(["ci", "coverage", "--all"])
        dispatch_ci(args)

        calls = mock_validator.validate_coverage.call_args_list
        # Default: inc-build is used
        assert calls[0].kwargs.get("use_inc_build") is True
        # Default: no force_rebuild for standalone commands
        assert calls[0].kwargs.get("force_rebuild") is False

    @patch("crsbench.benchmark_ci.cli.commands.coverage_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.coverage_cmd.BenchmarkValidator")
    @patch("crsbench.benchmark_ci.cli.commands.coverage_cmd.resolve_benchmark_paths")
    def test_coverage_no_inc_build_flag(
        self, mock_discover, mock_validator_cls, mock_table
    ):
        from crsbench.benchmark_ci.models import CheckResult, CheckStatus

        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_validator = mock_validator_cls.return_value
        mock_validator.validate_coverage.return_value = CheckResult(
            status=CheckStatus.PASS, time_seconds=10.0
        )

        parser = _make_parser()
        args = parser.parse_args(["ci", "coverage", "--all", "--no-inc-build"])
        dispatch_ci(args)

        calls = mock_validator.validate_coverage.call_args_list
        assert calls[0].kwargs.get("use_inc_build") is False

    @patch("crsbench.benchmark_ci.cli.commands.coverage_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.coverage_cmd.BenchmarkValidator")
    @patch("crsbench.benchmark_ci.cli.commands.coverage_cmd.resolve_benchmark_paths")
    def test_coverage_force_rebuild_flag(
        self, mock_discover, mock_validator_cls, mock_table
    ):
        from crsbench.benchmark_ci.models import CheckResult, CheckStatus

        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_validator = mock_validator_cls.return_value
        mock_validator.validate_coverage.return_value = CheckResult(
            status=CheckStatus.PASS, time_seconds=10.0
        )

        parser = _make_parser()
        args = parser.parse_args(["ci", "coverage", "--all", "--force-rebuild"])
        dispatch_ci(args)

        calls = mock_validator.validate_coverage.call_args_list
        assert calls[0].kwargs.get("force_rebuild") is True


# --- Test All subcommand integration ---


class TestAllSubcommand:
    """Integration tests for the All subcommand."""

    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd._load_project_capabilities")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.BenchmarkValidator")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.format_validate")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.resolve_benchmark_paths")
    def test_all_pass_returns_0(
        self, mock_discover, mock_fmt, mock_validator_cls, mock_caps, mock_table
    ):
        from crsbench.benchmark_ci.models import CheckResult, CheckStatus
        from crsbench.validation.errors import ValidationResult

        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_caps.return_value = (True, "jcgeks")
        mock_fmt.return_value = ValidationResult(is_valid=True, issues=[])
        mock_validator = mock_validator_cls.return_value
        mock_validator.validate_povs.return_value = CheckResult(
            status=CheckStatus.PASS, time_seconds=5.0
        )
        mock_validator.validate_patches.return_value = CheckResult(
            status=CheckStatus.PASS, time_seconds=8.0
        )

        parser = _make_parser()
        args = parser.parse_args(["ci", "all", "--all"])
        result = dispatch_ci(args)

        assert result == 0
        mock_table.assert_called_once()

    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd._load_project_capabilities")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.BenchmarkValidator")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.format_validate")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.resolve_benchmark_paths")
    def test_all_fail_returns_1(
        self, mock_discover, mock_fmt, mock_validator_cls, mock_caps, mock_table
    ):
        from crsbench.benchmark_ci.models import CheckResult, CheckStatus
        from crsbench.validation.errors import ValidationResult

        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_caps.return_value = (True, None)
        mock_fmt.return_value = ValidationResult(is_valid=True, issues=[])
        mock_validator = mock_validator_cls.return_value
        mock_validator.validate_povs.return_value = CheckResult(
            status=CheckStatus.FAIL,
            time_seconds=5.0,
            details={"failures": ["cpv_0/pov_0: NOT_VULNERABLE"]},
        )
        mock_validator.validate_patches.return_value = CheckResult(
            status=CheckStatus.PASS, time_seconds=8.0
        )

        parser = _make_parser()
        args = parser.parse_args(["ci", "all", "--all"])
        result = dispatch_ci(args)

        assert result == 1

    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd._load_project_capabilities")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.BenchmarkValidator")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.format_validate")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.resolve_benchmark_paths")
    def test_all_exception_returns_1(
        self, mock_discover, mock_fmt, mock_validator_cls, mock_caps, mock_table
    ):
        from crsbench.validation.errors import ValidationResult

        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_caps.return_value = (True, None)
        mock_fmt.return_value = ValidationResult(is_valid=True, issues=[])
        mock_validator = mock_validator_cls.return_value
        mock_validator.validate_povs.side_effect = Exception("docker failed")

        parser = _make_parser()
        args = parser.parse_args(["ci", "all", "--all"])
        result = dispatch_ci(args)

        assert result == 1

    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd._load_project_capabilities")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.BenchmarkValidator")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.format_validate")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.resolve_benchmark_paths")
    def test_all_output_writes_json(
        self,
        mock_discover,
        mock_fmt,
        mock_validator_cls,
        mock_caps,
        mock_table,
        tmp_path,
    ):
        from crsbench.benchmark_ci.models import CheckResult, CheckStatus
        from crsbench.validation.errors import ValidationResult

        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_caps.return_value = (True, None)
        mock_fmt.return_value = ValidationResult(is_valid=True, issues=[])
        mock_validator = mock_validator_cls.return_value
        mock_validator.validate_povs.return_value = CheckResult(
            status=CheckStatus.PASS, time_seconds=5.0
        )
        mock_validator.validate_patches.return_value = CheckResult(
            status=CheckStatus.PASS, time_seconds=8.0
        )
        mock_validator.validate_coverage.return_value = CheckResult(
            status=CheckStatus.PASS, time_seconds=10.0
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
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd._load_project_capabilities")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.BenchmarkValidator")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.format_validate")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.resolve_benchmark_paths")
    def test_all_uses_all_check_mode(
        self, mock_discover, mock_fmt, mock_validator_cls, mock_caps, mock_table
    ):
        from crsbench.benchmark_ci.models import CheckMode, CheckResult, CheckStatus
        from crsbench.validation.errors import ValidationResult

        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_caps.return_value = (True, None)
        mock_fmt.return_value = ValidationResult(is_valid=True, issues=[])
        mock_validator = mock_validator_cls.return_value
        mock_validator.validate_povs.return_value = CheckResult(
            status=CheckStatus.PASS, time_seconds=5.0
        )
        mock_validator.validate_patches.return_value = CheckResult(
            status=CheckStatus.PASS, time_seconds=8.0
        )

        parser = _make_parser()
        args = parser.parse_args(["ci", "all", "--all"])
        dispatch_ci(args)

        call_args = mock_table.call_args
        assert call_args.kwargs.get("check_mode") == CheckMode.ALL

    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd._load_project_capabilities")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.BenchmarkValidator")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.format_validate")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.resolve_benchmark_paths")
    def test_all_always_runs_coverage(
        self, mock_discover, mock_fmt, mock_validator_cls, mock_caps, mock_table
    ):
        from crsbench.benchmark_ci.models import CheckResult, CheckStatus
        from crsbench.validation.errors import ValidationResult

        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_caps.return_value = (True, None)
        mock_fmt.return_value = ValidationResult(is_valid=True, issues=[])
        mock_validator = mock_validator_cls.return_value
        mock_validator.validate_povs.return_value = CheckResult(
            status=CheckStatus.PASS, time_seconds=5.0
        )
        mock_validator.validate_patches.return_value = CheckResult(
            status=CheckStatus.PASS, time_seconds=8.0
        )
        mock_validator.validate_coverage.return_value = CheckResult(
            status=CheckStatus.PASS, time_seconds=10.0
        )

        parser = _make_parser()
        args = parser.parse_args(["ci", "all", "--all"])
        dispatch_ci(args)

        mock_validator.validate_coverage.assert_called_once()

    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd._load_project_capabilities")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.BenchmarkValidator")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.format_validate")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.resolve_benchmark_paths")
    def test_all_no_inc_support_uses_full_build(
        self, mock_discover, mock_fmt, mock_validator_cls, mock_caps, mock_table
    ):
        from crsbench.benchmark_ci.models import CheckResult, CheckStatus
        from crsbench.validation.errors import ValidationResult

        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_caps.return_value = (False, None)  # No inc-build support
        mock_fmt.return_value = ValidationResult(is_valid=True, issues=[])
        mock_validator = mock_validator_cls.return_value
        mock_validator.validate_povs.return_value = CheckResult(
            status=CheckStatus.PASS, time_seconds=5.0
        )
        mock_validator.validate_patches.return_value = CheckResult(
            status=CheckStatus.PASS, time_seconds=8.0
        )
        mock_validator.validate_coverage.return_value = CheckResult(
            status=CheckStatus.PASS, time_seconds=3.0
        )

        parser = _make_parser()
        args = parser.parse_args(["ci", "all", "--all"])
        dispatch_ci(args)

        # validate_povs called once via DAG (single build mode)
        assert mock_validator.validate_povs.call_count == 1
        # POV uses full build (supports_inc=False means effective_inc=False)
        pov_call = mock_validator.validate_povs.call_args_list[0]
        assert pov_call.kwargs.get("use_inc_build") is False

    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd._load_project_capabilities")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.BenchmarkValidator")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.format_validate")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.resolve_benchmark_paths")
    def test_all_runs_format_pov_patch_coverage_via_dag(
        self, mock_discover, mock_fmt, mock_validator_cls, mock_caps, mock_table
    ):
        from crsbench.benchmark_ci.models import CheckResult, CheckStatus
        from crsbench.validation.errors import ValidationResult

        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_caps.return_value = (True, "jcgeks")
        mock_fmt.return_value = ValidationResult(is_valid=True, issues=[])
        mock_validator = mock_validator_cls.return_value
        mock_validator.validate_povs.return_value = CheckResult(
            status=CheckStatus.PASS, time_seconds=5.0
        )
        mock_validator.validate_patches.return_value = CheckResult(
            status=CheckStatus.PASS, time_seconds=8.0
        )
        mock_validator.validate_coverage.return_value = CheckResult(
            status=CheckStatus.PASS, time_seconds=3.0
        )

        parser = _make_parser()
        args = parser.parse_args(["ci", "all", "--all"])
        dispatch_ci(args)

        # format_validate called
        mock_fmt.assert_called_once()
        # validate_povs called once (single build mode via DAG)
        assert mock_validator.validate_povs.call_count == 1
        # validate_patches: build_only + verify + RTS = 3 calls
        assert mock_validator.validate_patches.call_count == 3
        # validate_coverage called once
        mock_validator.validate_coverage.assert_called_once()

    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd._load_project_capabilities")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.BenchmarkValidator")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.format_validate")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.resolve_benchmark_paths")
    def test_all_parallel_runs_multiple_benchmarks(
        self, mock_discover, mock_fmt, mock_validator_cls, mock_caps, mock_table
    ):
        from crsbench.benchmark_ci.models import CheckResult, CheckStatus
        from crsbench.validation.errors import ValidationResult

        mock_discover.return_value = [
            Path("/tmp/bench1"),
            Path("/tmp/bench2"),
            Path("/tmp/bench3"),
        ]
        mock_caps.return_value = (False, None)
        mock_fmt.return_value = ValidationResult(is_valid=True, issues=[])
        mock_validator = mock_validator_cls.return_value
        mock_validator.validate_povs.return_value = CheckResult(
            status=CheckStatus.PASS, time_seconds=5.0
        )
        mock_validator.validate_patches.return_value = CheckResult(
            status=CheckStatus.PASS, time_seconds=8.0
        )
        mock_validator.validate_coverage.return_value = CheckResult(
            status=CheckStatus.PASS, time_seconds=3.0
        )

        parser = _make_parser()
        args = parser.parse_args(["ci", "all", "--all"])
        result = dispatch_ci(args)

        assert result == 0
        # All 3 benchmarks processed (parallel via ThreadPoolExecutor)
        call_args = mock_table.call_args
        summary = call_args[0][0]
        assert len(summary.results) == 3

    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd._load_project_capabilities")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.BenchmarkValidator")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.format_validate")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.resolve_benchmark_paths")
    def test_all_parallel_default_is_sequential(
        self, mock_discover, mock_fmt, mock_validator_cls, mock_caps, mock_table
    ):
        from crsbench.benchmark_ci.models import CheckResult, CheckStatus
        from crsbench.validation.errors import ValidationResult

        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_caps.return_value = (False, None)
        mock_fmt.return_value = ValidationResult(is_valid=True, issues=[])
        mock_validator = mock_validator_cls.return_value
        mock_validator.validate_povs.return_value = CheckResult(
            status=CheckStatus.PASS, time_seconds=5.0
        )
        mock_validator.validate_patches.return_value = CheckResult(
            status=CheckStatus.PASS, time_seconds=8.0
        )
        mock_validator.validate_coverage.return_value = CheckResult(
            status=CheckStatus.PASS, time_seconds=3.0
        )

        parser = _make_parser()
        # Single benchmark runs sequentially (no ThreadPoolExecutor overhead)
        args = parser.parse_args(["ci", "all", "--all"])
        result = dispatch_ci(args)

        assert result == 0
        assert args.build_workers == 4  # default build workers
        assert args.verify_workers == 4  # default verify workers

    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd._load_project_capabilities")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.BenchmarkValidator")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.format_validate")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.resolve_benchmark_paths")
    def test_all_default_uses_inc_build(
        self, mock_discover, mock_fmt, mock_validator_cls, mock_caps, mock_table
    ):
        from crsbench.benchmark_ci.models import CheckResult, CheckStatus
        from crsbench.validation.errors import ValidationResult

        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_caps.return_value = (True, "jcgeks")
        mock_fmt.return_value = ValidationResult(is_valid=True, issues=[])
        mock_validator = mock_validator_cls.return_value
        mock_validator.validate_povs.return_value = CheckResult(
            status=CheckStatus.PASS, time_seconds=5.0
        )
        mock_validator.validate_patches.return_value = CheckResult(
            status=CheckStatus.PASS, time_seconds=8.0
        )
        mock_validator.validate_coverage.return_value = CheckResult(
            status=CheckStatus.PASS, time_seconds=3.0
        )

        parser = _make_parser()
        args = parser.parse_args(["ci", "all", "--all"])
        dispatch_ci(args)

        # Standard POV call: uses inc-build by default
        pov_calls = mock_validator.validate_povs.call_args_list
        assert pov_calls[0].kwargs.get("use_inc_build") is True
        # Standard Patch build_only call: uses inc-build
        patch_calls = mock_validator.validate_patches.call_args_list
        assert patch_calls[0].kwargs.get("use_inc_build") is True
        # CI all always force-rebuilds
        assert pov_calls[0].kwargs.get("force_rebuild") is True

    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.print_results_table")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd._load_project_capabilities")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.BenchmarkValidator")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.format_validate")
    @patch("crsbench.benchmark_ci.cli.commands.all_cmd.resolve_benchmark_paths")
    def test_all_no_inc_build_flag(
        self, mock_discover, mock_fmt, mock_validator_cls, mock_caps, mock_table
    ):
        from crsbench.benchmark_ci.models import CheckResult, CheckStatus
        from crsbench.validation.errors import ValidationResult

        mock_discover.return_value = [Path("/tmp/bench1")]
        mock_caps.return_value = (True, "jcgeks")
        mock_fmt.return_value = ValidationResult(is_valid=True, issues=[])
        mock_validator = mock_validator_cls.return_value
        mock_validator.validate_povs.return_value = CheckResult(
            status=CheckStatus.PASS, time_seconds=5.0
        )
        mock_validator.validate_patches.return_value = CheckResult(
            status=CheckStatus.PASS, time_seconds=8.0
        )
        mock_validator.validate_coverage.return_value = CheckResult(
            status=CheckStatus.PASS, time_seconds=3.0
        )

        parser = _make_parser()
        args = parser.parse_args(["ci", "all", "--all", "--no-inc-build"])
        dispatch_ci(args)

        # With --no-inc-build, all checks use full build (single build mode)
        pov_calls = mock_validator.validate_povs.call_args_list
        assert pov_calls[0].kwargs.get("use_inc_build") is False
        # Coverage also uses full build
        cov_calls = mock_validator.validate_coverage.call_args_list
        assert cov_calls[0].kwargs.get("use_inc_build") is False
