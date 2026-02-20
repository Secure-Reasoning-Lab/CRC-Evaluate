"""Tests that migrate, stats, ci are correctly nested under benchmark.

Verifies the CLI restructuring: these commands moved from top-level
(crsbench migrate, crsbench stats, crsbench ci) to nested under benchmark
(crsbench benchmark migrate, crsbench benchmark stats, crsbench benchmark ci).
"""

import argparse

from crsbench.benchmark.packaging.cli.benchmark_command import (
    add_benchmark_subparser,
    run_benchmark_command,
)


def _make_parser() -> argparse.ArgumentParser:
    """Create a parser with the benchmark subcommand registered."""
    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers(dest="command")
    add_benchmark_subparser(subs)
    return parser


class TestBenchmarkCiNesting:
    """Tests for 'crsbench benchmark ci' nested subcommand."""

    def test_benchmark_ci_format_parses(self):
        parser = _make_parser()
        args = parser.parse_args(["benchmark", "ci", "format", "--all"])
        assert args.ci_subcommand == "format"
        assert args.all is True

    def test_benchmark_ci_all_parses(self):
        parser = _make_parser()
        args = parser.parse_args(["benchmark", "ci", "all", "--all"])
        assert args.ci_subcommand == "all"

    def test_benchmark_ci_pov_parses(self):
        parser = _make_parser()
        args = parser.parse_args(["benchmark", "ci", "pov", "--all"])
        assert args.ci_subcommand == "pov"

    def test_benchmark_ci_patch_parses(self):
        parser = _make_parser()
        args = parser.parse_args(["benchmark", "ci", "patch", "--all"])
        assert args.ci_subcommand == "patch"

    def test_benchmark_ci_parse_parses(self):
        parser = _make_parser()
        args = parser.parse_args(
            ["benchmark", "ci", "parse", "--output-dir", "/tmp/test"]
        )
        assert args.ci_subcommand == "parse"

    def test_benchmark_ci_dispatch_routes_to_handler(self):
        parser = _make_parser()
        args = parser.parse_args(["benchmark", "ci", "format", "--all"])
        assert hasattr(args, "func")
        assert callable(args.func)

    def test_benchmark_ci_all_subcommands_registered(self):
        parser = _make_parser()
        expected = [
            "format",
            "build",
            "pov",
            "patch",
            "coverage",
            "rts",
            "all",
            "parse",
            "retry",
            "storage",
        ]
        for sub in expected:
            extra = ["--all"] if sub not in ("parse", "retry", "storage") else []
            if sub == "parse":
                extra = ["--output-dir", "/tmp/test"]
            elif sub == "retry":
                extra = ["--csv", "/tmp/test.csv"]
            elif sub == "storage":
                extra = ["--all"]
            args = parser.parse_args(["benchmark", "ci", sub, *extra])
            assert args.ci_subcommand == sub


class TestBenchmarkStatsNesting:
    """Tests for 'crsbench benchmark stats' nested subcommand."""

    def test_benchmark_stats_parses(self):
        parser = _make_parser()
        args = parser.parse_args(["benchmark", "stats", "--summary-only"])
        assert args.summary_only is True

    def test_benchmark_stats_output_parses(self):
        parser = _make_parser()
        args = parser.parse_args(["benchmark", "stats", "--output", "benchmarks.csv"])
        assert str(args.output) == "benchmarks.csv"

    def test_benchmark_stats_dispatch_routes_to_handler(self):
        parser = _make_parser()
        args = parser.parse_args(["benchmark", "stats", "--summary-only"])
        assert hasattr(args, "func")
        assert callable(args.func)
        assert args.func.__name__ == "run_stats"


class TestBenchmarkMigrateNesting:
    """Tests for 'crsbench benchmark migrate' nested subcommand."""

    def test_benchmark_migrate_atlanta_to_rfc_parses(self):
        parser = _make_parser()
        args = parser.parse_args(
            [
                "benchmark",
                "migrate",
                "atlanta-to-rfc",
                "--source-dir",
                "/tmp/src",
                "--target-dir",
                "/tmp/dst",
            ]
        )
        assert args.migrate_subcommand == "atlanta-to-rfc"

    def test_benchmark_migrate_rfc_to_atlanta_parses(self):
        parser = _make_parser()
        args = parser.parse_args(
            [
                "benchmark",
                "migrate",
                "rfc-to-atlanta",
                "--source-dir",
                "/tmp/src",
                "--target-dir",
                "/tmp/dst",
            ]
        )
        assert args.migrate_subcommand == "rfc-to-atlanta"

    def test_benchmark_migrate_dispatch_routes_to_handler(self):
        parser = _make_parser()
        args = parser.parse_args(
            [
                "benchmark",
                "migrate",
                "atlanta-to-rfc",
                "--source-dir",
                "/tmp/src",
                "--target-dir",
                "/tmp/dst",
            ]
        )
        assert hasattr(args, "func")
        assert callable(args.func)
        assert args.func.__name__ == "run_migrate"

    def test_benchmark_migrate_all_subcommands_registered(self):
        parser = _make_parser()
        subcommand_args = {
            "atlanta-to-rfc": [
                "--source-dir",
                "/tmp/src",
                "--target-dir",
                "/tmp/dst",
            ],
            "rfc-to-atlanta": [
                "--source-dir",
                "/tmp/src",
                "--target-dir",
                "/tmp/dst",
            ],
            "generate-test-sh": ["--benchmark", "test-bench"],
            "generate-vuln-yaml": [],
        }
        for sub, extra in subcommand_args.items():
            args = parser.parse_args(["benchmark", "migrate", sub, *extra])
            assert args.migrate_subcommand == sub


class TestBenchmarkCommandDispatch:
    """Tests for run_benchmark_command dispatching to nested commands."""

    def test_benchmark_command_dispatches_via_func(self):
        """Verify run_benchmark_command calls args.func(args)."""
        called = {}

        def mock_func(_args):
            called["invoked"] = True
            return 0

        args = argparse.Namespace(func=mock_func)
        result = run_benchmark_command(args)
        assert result == 0
        assert called["invoked"] is True

    def test_benchmark_command_no_func_shows_help(self):
        """Without func attr, run_benchmark_command returns help code."""
        args = argparse.Namespace()
        result = run_benchmark_command(args)
        assert result != 0 or result is None  # help handler


class TestOldTopLevelCommandsRemoved:
    """Verify migrate, stats, ci are NOT registered as top-level commands.

    We build the full parser the same way parse_arguments() does,
    but stop before calling parse_args() (which would consume sys.argv).
    """

    @staticmethod
    def _build_full_parser() -> argparse.ArgumentParser:
        """Build the full crsbench CLI parser without parsing sys.argv."""
        from crsbench.benchmark.packaging.cli import add_benchmark_subparser
        from crsbench.dataset.cli import add_dataset_subparser
        from crsbench.distributed.cli.evaluator_command import (
            add_evaluator_subparser,
        )
        from crsbench.distributed.cli.worker_command import add_worker_subparser
        from crsbench.evaluation.coverage.cli.coverage_command import (
            add_coverage_subparser,
        )
        from crsbench.evaluation.reeval.cli import add_reeval_subparser
        from crsbench.evaluation.verification.cli.patch_verify_command import (
            add_patch_verify_subparser,
        )
        from crsbench.evaluation.verification.cli.pov_verify_command import (
            add_verify_subparser,
        )
        from crsbench.reporting.cli import (
            add_dashboard_subparser,
            add_report_subparser,
        )

        parser = argparse.ArgumentParser(prog="crsbench")
        subs = parser.add_subparsers(dest="command")

        add_dataset_subparser(subs)
        add_benchmark_subparser(subs)
        add_worker_subparser(subs)
        add_evaluator_subparser(subs)
        add_verify_subparser(subs)
        add_patch_verify_subparser(subs)
        add_coverage_subparser(subs)
        add_report_subparser(subs)
        add_dashboard_subparser(subs)
        add_reeval_subparser(subs)

        return parser

    @staticmethod
    def _get_top_level_commands() -> set[str]:
        parser = TestOldTopLevelCommandsRemoved._build_full_parser()
        # Extract subparser choices from the parser
        for action in parser._subparsers._actions:
            if isinstance(action, argparse._SubParsersAction):
                return set(action.choices.keys())
        return set()

    def test_no_top_level_migrate(self):
        commands = self._get_top_level_commands()
        assert "migrate" not in commands

    def test_no_top_level_stats(self):
        commands = self._get_top_level_commands()
        assert "stats" not in commands

    def test_no_top_level_ci(self):
        commands = self._get_top_level_commands()
        assert "ci" not in commands

    def test_benchmark_is_registered(self):
        commands = self._get_top_level_commands()
        assert "benchmark" in commands
