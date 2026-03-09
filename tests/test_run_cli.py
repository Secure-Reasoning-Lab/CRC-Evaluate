"""CLI parser contract tests for `crsbench run`."""

import sys

import pytest
from crsbench.run_experiment import parse_arguments


def _parse(argv: list[str]):
    original = sys.argv[:]
    try:
        sys.argv = argv
        return parse_arguments()
    finally:
        sys.argv = original


def test_run_cli_accepts_queue_modes() -> None:
    for mode in ("fresh", "continue", "quit"):
        args = _parse(
            [
                "crsbench",
                "run",
                "--experiment-config",
                "experiment-configs/afc-final-bugfinding/atlantis-multilang-given_fuzzer-default-full-given-fuzzer-run.yaml",
                "--queue-mode",
                mode,
            ]
        )
        assert args.command == "run"
        assert args.queue_mode == mode


def test_run_cli_rejects_legacy_queue_mode_values() -> None:
    with pytest.raises(SystemExit):
        _parse(
            [
                "crsbench",
                "run",
                "--experiment-config",
                "experiment-configs/afc-final-bugfinding/atlantis-multilang-given_fuzzer-default-full-given-fuzzer-run.yaml",
                "--queue-mode",
                "resume",
            ]
        )


def test_run_cli_retry_failed_flag_parsing() -> None:
    args = _parse(
        [
            "crsbench",
            "run",
            "--experiment-config",
            "experiment-configs/afc-final-bugfinding/atlantis-multilang-given_fuzzer-default-full-given-fuzzer-run.yaml",
            "--retry-failed",
        ]
    )
    assert args.command == "run"
    assert args.retry_failed is True
