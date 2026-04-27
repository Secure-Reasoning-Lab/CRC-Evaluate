"""CLI parser contract tests for `crsbench run`."""

import sys
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

import crsbench.run_experiment as run_experiment
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


def test_run_cli_accepts_gen_config_tui() -> None:
    args = _parse(
        [
            "crsbench",
            "gen-config-tui",
        ]
    )
    assert args.command == "gen-config-tui"


def test_run_cli_accepts_gen_config_tui_config_path() -> None:
    args = _parse(
        [
            "crsbench",
            "gen-config-tui",
            "experiment-configs/example.yaml",
        ]
    )
    assert args.command == "gen-config-tui"
    assert args.config_path == Path("experiment-configs/example.yaml")


def test_run_cli_dispatches_gen_config_tui() -> None:
    with (
        patch.object(
            run_experiment,
            "parse_arguments",
            return_value=Namespace(command="gen-config-tui"),
        ),
        patch.object(run_experiment, "run_gen_config_tui", return_value=0) as mock_run,
        pytest.raises(SystemExit) as exc_info,
    ):
        run_experiment.main()

    assert exc_info.value.code == 0
    mock_run.assert_called_once()


def test_run_gen_config_tui_passes_config_path_to_app() -> None:
    args = Namespace(
        command="gen-config-tui",
        config_path=Path("experiment-configs/example.yaml"),
    )

    with patch("crsbench.genconfig_tui.app.main", return_value=0) as mock_main:
        result = run_experiment.run_gen_config_tui(args)

    assert result == 0
    mock_main.assert_called_once_with(config_path=args.config_path)


def test_replay_povs_helpers_are_not_eagerly_imported() -> None:
    assert not hasattr(run_experiment, "add_replay_povs_subparser")
    assert not hasattr(run_experiment, "run_replay_povs")


def test_replay_povs_cli_accepts_repeated_source_dir() -> None:
    args = _parse(
        [
            "crsbench",
            "replay-povs",
            "--source-dir",
            "experiments/run-a",
            "--source-dir",
            "experiments/run-b",
            "--output",
            "out/replay",
        ]
    )

    assert args.command == "replay-povs"
    assert args.source_dirs == [
        Path("experiments/run-a"),
        Path("experiments/run-b"),
    ]
    assert args.output == Path("out/replay")


def test_replay_povs_cli_accepts_cache_root() -> None:
    args = _parse(
        [
            "crsbench",
            "replay-povs",
            "--source-dir",
            "experiments/run-a",
            "--output",
            "out/replay",
            "--cache-root",
            "/data/crsbench/replay-cache",
        ]
    )

    assert args.command == "replay-povs"
    assert args.cache_root == Path("/data/crsbench/replay-cache")


def test_replay_povs_cli_sets_handler() -> None:
    from crsbench.evaluation.replay.cli import run_replay_povs

    args = _parse(
        [
            "crsbench",
            "replay-povs",
            "--source-dir",
            "experiments/run-a",
            "--output",
            "out/replay",
        ]
    )

    assert args.func is run_replay_povs


def test_replay_povs_cli_requires_source_dir() -> None:
    with pytest.raises(SystemExit):
        _parse(
            [
                "crsbench",
                "replay-povs",
                "--output",
                "out/replay",
            ]
        )


def test_replay_povs_cli_requires_output() -> None:
    with pytest.raises(SystemExit):
        _parse(
            [
                "crsbench",
                "replay-povs",
                "--source-dir",
                "experiments/run-a",
            ]
        )


def test_replay_povs_cli_rejects_projects_root_with_sync_projects() -> None:
    with pytest.raises(SystemExit):
        _parse(
            [
                "crsbench",
                "replay-povs",
                "--source-dir",
                "experiments/run-a",
                "--output",
                "out/replay",
                "--projects-root",
                "projects",
                "--sync-projects",
            ]
        )


def test_main_dispatches_replay_povs() -> None:
    args = Namespace(command="replay-povs")

    with (
        patch.object(run_experiment, "parse_arguments", return_value=args),
        patch(
            "crsbench.evaluation.replay.cli.run_replay_povs", return_value=0
        ) as mock_replay_povs,
        pytest.raises(SystemExit) as exc_info,
    ):
        run_experiment.main()

    assert exc_info.value.code == 0
    mock_replay_povs.assert_called_once_with(args)


def test_run_replay_povs_returns_success_when_engine_completes() -> None:
    from crsbench.evaluation.replay.cli import run_replay_povs

    args = Namespace(
        source_dirs=[Path("experiments/run-a")],
        output=Path("out/replay"),
        cache_root=None,
        oss_fuzz_path=Path("third_party/oss-fuzz"),
        projects_root=Path("projects"),
        sync_projects=False,
        benchmarks=None,
        trials=None,
        jobs=1,
        per_pov_timeout=180,
        verbose=False,
    )

    with (
        patch("crsbench.evaluation.replay.cli.configure_logger"),
        patch(
            "crsbench.evaluation.replay.cli.resolve_projects_root",
            return_value=Path("/tmp/projects"),
        ),
        patch(
            "crsbench.evaluation.replay.cli.discover_source_povs",
            return_value=([], {"source_roots_processed": 1}),
        ),
        patch("crsbench.evaluation.replay.cli.ReplayEngine") as mock_engine,
        patch.object(Path, "is_dir", return_value=True),
    ):
        assert run_replay_povs(args) == 0

    mock_engine.return_value.run.assert_called_once()
