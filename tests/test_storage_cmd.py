"""Tests for benchmark CI storage output behavior."""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path
from unittest.mock import MagicMock, patch

from crsbench.benchmark_ci.cli.commands.storage_cmd import run_storage
from crsbench.benchmark_ci.storage import StorageMetrics


def test_run_storage_does_not_force_terminal_when_color_enabled() -> None:
    args = Namespace(
        benchmark=None,
        benchmarks=None,
        benchmark_suite=None,
        all=False,
        filter=None,
        project_image_prefix="crsbench",
        no_color=False,
    )

    with (
        patch(
            "crsbench.benchmark_ci.cli.commands.storage_cmd.resolve_benchmark_paths",
            return_value=[Path("/tmp/bench-a")],
        ),
        patch(
            "crsbench.benchmark_ci.cli.commands.storage_cmd.ensure_oss_fuzz_root",
            return_value="/tmp/oss-fuzz",
        ),
        patch(
            "crsbench.benchmark_ci.cli.commands.storage_cmd.collect_benchmark_storage",
            return_value=StorageMetrics(
                build_artifacts_bytes=1,
                docker_image_bytes=2,
                git_bytes=3,
            ),
        ),
        patch("crsbench.benchmark_ci.cli.commands.storage_cmd.Console") as console_cls,
    ):
        console = MagicMock()
        console_cls.return_value = console
        exit_code = run_storage(args)

    assert exit_code == 0
    console_cls.assert_called_once_with(no_color=False)
