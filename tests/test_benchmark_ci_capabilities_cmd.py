"""Tests for benchmark ci capabilities command."""

import argparse
from pathlib import Path
from unittest.mock import patch

from crsbench.benchmark_ci.cli import add_ci_subparser
from crsbench.benchmark_ci.cli.commands.capabilities_cmd import (
    CapabilityProbeResult,
    _probe_benchmark,
    _probe_runtime_capabilities,
)


def _make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers(dest="command")
    add_ci_subparser(subs)
    return parser


def test_capabilities_subcommand_parses():
    parser = _make_parser()
    args = parser.parse_args(["ci", "capabilities", "--all"])
    assert args.ci_subcommand == "capabilities"
    assert args.all is True


def test_capabilities_subcommand_success():
    parser = _make_parser()
    args = parser.parse_args(["ci", "capabilities", "--all"])

    with (
        patch(
            "crsbench.benchmark_ci.cli.commands.capabilities_cmd.resolve_benchmark_paths"
        ) as mock_paths,
        patch(
            "crsbench.benchmark_ci.cli.commands.capabilities_cmd._probe_benchmark"
        ) as mock_probe,
    ):
        mock_paths.return_value = [Path("/tmp/bench-a")]
        mock_probe.return_value = CapabilityProbeResult(
            benchmark="bench-a",
            declared_inc_build=True,
            probed_inc_build=True,
            declared_rts_mode=None,
            probed_rts_mode=None,
            reasons=[],
            matches=True,
        )

        rc = args.ci_func(args)
        assert rc == 0


def test_capabilities_subcommand_fails_on_mismatch():
    parser = _make_parser()
    args = parser.parse_args(["ci", "capabilities", "--all"])

    with (
        patch(
            "crsbench.benchmark_ci.cli.commands.capabilities_cmd.resolve_benchmark_paths"
        ) as mock_paths,
        patch(
            "crsbench.benchmark_ci.cli.commands.capabilities_cmd._probe_benchmark"
        ) as mock_probe,
    ):
        mock_paths.return_value = [Path("/tmp/bench-a")]
        mock_probe.return_value = CapabilityProbeResult(
            benchmark="bench-a",
            declared_inc_build=True,
            probed_inc_build=False,
            declared_rts_mode="jcgeks",
            probed_rts_mode=None,
            reasons=["no inc-build image available"],
            matches=False,
        )

        rc = args.ci_func(args)
        assert rc == 1


def test_probe_runtime_capabilities_default_mode_can_pull():
    bench = Path("/tmp/bench-a")
    with (
        patch(
            "crsbench.benchmark_ci.cli.commands.capabilities_cmd._load_project_capabilities"
        ) as mock_caps,
        patch(
            "crsbench.benchmark_ci.cli.commands.capabilities_cmd._load_project_sanitizers"
        ) as mock_sanitizers,
        patch(
            "crsbench.benchmark_ci.cli.commands.capabilities_cmd.OSSFuzzInfrastructure"
        ) as mock_infra_cls,
    ):
        mock_caps.return_value = (True, None)
        mock_sanitizers.return_value = ["address"]
        mock_infra = mock_infra_cls.return_value
        mock_infra.is_inc_image_available.side_effect = [False, True]
        mock_infra.pull_inc_build_image.return_value = True
        mock_infra.is_tests_available.return_value = False

        inc, rts, _reasons = _probe_runtime_capabilities(
            bench, registry="test.registry", probe_local_only=False
        )
        assert inc is True
        assert rts is None
        mock_infra.pull_inc_build_image.assert_called_once()


def test_probe_runtime_capabilities_local_only_does_not_pull():
    bench = Path("/tmp/bench-a")
    with (
        patch(
            "crsbench.benchmark_ci.cli.commands.capabilities_cmd._load_project_capabilities"
        ) as mock_caps,
        patch(
            "crsbench.benchmark_ci.cli.commands.capabilities_cmd._load_project_sanitizers"
        ) as mock_sanitizers,
        patch(
            "crsbench.benchmark_ci.cli.commands.capabilities_cmd.OSSFuzzInfrastructure"
        ) as mock_infra_cls,
    ):
        mock_caps.return_value = (True, None)
        mock_sanitizers.return_value = ["address"]
        mock_infra = mock_infra_cls.return_value
        mock_infra.is_inc_image_available.return_value = False
        mock_infra.pull_inc_build_image.return_value = True
        mock_infra.is_tests_available.return_value = False

        inc, rts, _reasons = _probe_runtime_capabilities(
            bench, registry="test.registry", probe_local_only=True
        )
        assert inc is False
        assert rts is None
        mock_infra.pull_inc_build_image.assert_not_called()


def test_probe_benchmark_flags_rts_under_declaration():
    bench = Path("/tmp/bench-a")
    with (
        patch(
            "crsbench.benchmark_ci.cli.commands.capabilities_cmd._load_project_capabilities"
        ) as mock_caps,
        patch(
            "crsbench.benchmark_ci.cli.commands.capabilities_cmd._probe_runtime_capabilities"
        ) as mock_probe,
    ):
        mock_caps.return_value = (True, None)
        mock_probe.return_value = (
            True,
            "supported",
            ["test.sh found", "inc-build available"],
        )

        result = _probe_benchmark(
            bench,
            registry="test.registry",
            probe_local_only=True,
        )
        assert result.matches is False
        assert result.probed_rts_mode == "supported"
