from argparse import Namespace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from crsbench.evaluation.replay.cli import run_replay_povs


def _args(
    *,
    source_dirs: list[Path],
    output: Path,
    oss_fuzz_path: Path | None = None,
    projects_root: Path | None = None,
    sync_projects: bool = False,
    benchmarks: list[str] | None = None,
    trials: list[str] | None = None,
    jobs: int = 1,
    group_jobs: int = 1,
    resume: bool = False,
    per_pov_timeout: int = 180,
    verbose: bool = False,
) -> Namespace:
    return Namespace(
        source_dirs=source_dirs,
        output=output,
        oss_fuzz_path=oss_fuzz_path,
        projects_root=projects_root,
        sync_projects=sync_projects,
        benchmarks=benchmarks,
        trials=trials,
        jobs=jobs,
        group_jobs=group_jobs,
        resume=resume,
        per_pov_timeout=per_pov_timeout,
        verbose=verbose,
    )


def test_run_replay_povs_rejects_missing_source_dirs(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="must exist and be directories"):
        run_replay_povs(
            _args(
                source_dirs=[tmp_path / "missing-source"],
                output=tmp_path / "replay-out",
            )
        )


def test_run_replay_povs_rejects_output_nested_under_source_dir(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "exp-a"
    source_dir.mkdir()

    with pytest.raises(SystemExit, match="outside every source experiment tree"):
        run_replay_povs(
            _args(
                source_dirs=[source_dir],
                output=source_dir / "replay-out",
            )
        )


def test_run_replay_povs_wires_discovery_projects_and_engine(tmp_path: Path) -> None:
    source_dir = tmp_path / "exp-a"
    source_dir.mkdir()
    output_dir = tmp_path / "replay-out"
    oss_fuzz_path = tmp_path / "oss-fuzz"
    projects_root = tmp_path / "latest-projects"
    records = [object()]
    engine = MagicMock()

    with (
        patch("crsbench.evaluation.replay.cli.configure_logger") as mock_logger,
        patch(
            "crsbench.evaluation.replay.cli.resolve_projects_root",
            return_value=projects_root.resolve(),
        ) as mock_resolve_projects,
        patch(
            "crsbench.evaluation.replay.cli.ensure_oss_fuzz_root",
            return_value=str(oss_fuzz_path),
        ) as mock_ensure_oss_fuzz_root,
        patch(
            "crsbench.evaluation.replay.cli.discover_source_povs",
            return_value=(records, {"source_roots_processed": 1}),
        ) as mock_discover,
        patch(
            "crsbench.evaluation.replay.cli.ReplayEngine",
            return_value=engine,
        ) as mock_engine,
    ):
        exit_code = run_replay_povs(
            _args(
                source_dirs=[source_dir],
                output=output_dir,
                jobs=4,
                group_jobs=2,
                per_pov_timeout=12,
                benchmarks=["bench-a"],
                trials=["trial-1"],
                verbose=True,
            )
        )

    assert exit_code == 0
    mock_logger.assert_called_once_with(level="DEBUG")
    mock_ensure_oss_fuzz_root.assert_called_once_with()
    mock_resolve_projects.assert_called_once()
    mock_discover.assert_called_once_with(
        [source_dir.resolve()],
        benchmark_filters={"bench-a"},
        trial_filters={"trial-1"},
    )
    mock_engine.assert_called_once_with(
        oss_fuzz_path=oss_fuzz_path.resolve(),
        projects_root=projects_root.resolve(),
        output_dir=output_dir.resolve(),
        jobs=4,
        group_jobs=2,
        resume=False,
        per_pov_timeout=12,
    )
    engine.run.assert_called_once_with(
        records,
        discovery_stats={"source_roots_processed": 1},
        source_dirs=[source_dir.resolve()],
    )


def test_run_replay_povs_uses_explicit_oss_fuzz_path(tmp_path: Path) -> None:
    source_dir = tmp_path / "exp-a"
    source_dir.mkdir()
    output_dir = tmp_path / "replay-out"
    explicit_oss_fuzz = tmp_path / "oss-fuzz"
    projects_root = tmp_path / "latest-projects"
    engine = MagicMock()

    with (
        patch("crsbench.evaluation.replay.cli.configure_logger"),
        patch(
            "crsbench.evaluation.replay.cli.resolve_projects_root",
            return_value=projects_root.resolve(),
        ),
        patch(
            "crsbench.evaluation.replay.cli.discover_source_povs",
            return_value=([], {"source_roots_processed": 1}),
        ),
        patch(
            "crsbench.evaluation.replay.cli.ReplayEngine",
            return_value=engine,
        ) as mock_engine,
        patch("crsbench.evaluation.replay.cli.ensure_oss_fuzz_root") as mock_ensure,
    ):
        exit_code = run_replay_povs(
            _args(
                source_dirs=[source_dir],
                output=output_dir,
                oss_fuzz_path=explicit_oss_fuzz,
            )
        )

    assert exit_code == 0
    mock_ensure.assert_not_called()
    mock_engine.assert_called_once_with(
        oss_fuzz_path=explicit_oss_fuzz.resolve(),
        projects_root=projects_root.resolve(),
        output_dir=output_dir.resolve(),
        jobs=1,
        group_jobs=1,
        resume=False,
        per_pov_timeout=180,
    )


def test_run_replay_povs_rejects_non_positive_group_jobs(tmp_path: Path) -> None:
    source_dir = tmp_path / "exp-a"
    source_dir.mkdir()

    with pytest.raises(SystemExit, match="group-jobs must be at least 1"):
        run_replay_povs(
            _args(
                source_dirs=[source_dir],
                output=tmp_path / "replay-out",
                group_jobs=0,
            )
        )
