from __future__ import annotations

import json
import subprocess
import sys
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
import yaml

if TYPE_CHECKING:
    from pathlib import Path


def _write_submission_bundle(
    submission_dir: Path,
    *,
    source_experiment: str = "source-exp",
    remote_experiment: str = "source-exp-reeval-20260424",
    source_runtime_revision: str | None = None,
) -> None:
    from crsbench.cloud.reeval_compat import discover_git_revision

    if source_runtime_revision is None:
        source_runtime_revision = discover_git_revision()

    bundle_dir = submission_dir / "bundle"
    (bundle_dir / "config").mkdir(parents=True)
    (
        bundle_dir / "trials" / "bugbench__ensemble" / "trial-1" / "output" / "povs"
    ).mkdir(parents=True)
    (bundle_dir / "config" / "source-config.yaml").write_text(
        yaml.safe_dump(
            {
                "experiment": source_experiment,
                "experiment_filestore": "/tmp/ignored-filestore",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (
        bundle_dir / "trials" / "bugbench__ensemble" / "trial-1" / "metadata.json"
    ).write_text(
        json.dumps(
            {
                "timestamp": "2026-04-24T00:00:00",
                "trial_num": 1,
                "crs": "ensemble",
                "benchmark": "bugbench",
                "harness": "fuzz_bug",
                "mode": "bug_finding",
                "source": {"path": "/src", "commit": "abc123"},
            }
        ),
        encoding="utf-8",
    )
    (
        bundle_dir
        / "trials"
        / "bugbench__ensemble"
        / "trial-1"
        / "output"
        / "povs"
        / "pov.bin"
    ).write_text(
        "pov",
        encoding="utf-8",
    )
    (bundle_dir / "manifest.json").write_text(
        json.dumps(
            {
                "bundle_id": "bundle-123",
                "source_experiment_name": source_experiment,
                "remote_experiment_name": remote_experiment,
                "selected_trial_count": 1,
                "skipped_trial_count": 0,
                "compatibility": {
                    "benchmark_names": ["bugbench"],
                    "source_mode": "pkgs",
                    "source_runtime_revision": source_runtime_revision,
                },
                "selected_trials": [
                    {
                        "relative_path": "bugbench__ensemble/trial-1",
                        "benchmark": "bugbench",
                        "harness": "fuzz_bug",
                        "mode": "bug_finding",
                        "trial_num": 1,
                    }
                ],
                "skipped_trials": [],
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def _write_submission_state(
    submission_dir: Path,
    *,
    remote_experiment: str = "source-exp-reeval-20260424",
) -> None:
    submission_dir.mkdir(parents=True, exist_ok=True)
    (submission_dir / "submission.json").write_text(
        json.dumps(
            {
                "state": "uploading",
                "source_experiment_name": "source-exp",
                "remote_experiment_name": remote_experiment,
                "bundle_path": str(submission_dir / "bundle"),
                "workspace_path": str(submission_dir / "workspace"),
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def test_launch_remote_submission_marks_published_and_starts_runner(
    tmp_path: Path,
) -> None:
    from crsbench.cloud.reeval_remote import (
        launch_remote_submission,
        read_submission_state,
    )

    submission_dir = tmp_path / "submission"
    _write_submission_state(submission_dir)
    _write_submission_bundle(submission_dir, source_runtime_revision="source-rev")

    process = MagicMock()
    process.pid = 43210
    with patch(
        "crsbench.cloud.reeval_remote.subprocess.Popen", return_value=process
    ) as mock_popen:
        pid = launch_remote_submission(submission_dir)

    assert pid == 43210
    state = read_submission_state(submission_dir / "submission.json")
    assert state["state"] == "published"
    assert state["runner_pid"] == 43210
    assert mock_popen.call_args.args[0][0] == sys.executable
    assert "--submission-dir" in mock_popen.call_args.args[0]


def test_launch_remote_submission_persists_published_state_before_spawning_runner(
    tmp_path: Path,
) -> None:
    from crsbench.cloud.reeval_remote import launch_remote_submission

    submission_dir = tmp_path / "submission"
    _write_submission_state(submission_dir)
    _write_submission_bundle(submission_dir)

    observed_state: dict[str, str | None] = {"state": None}
    process = MagicMock()
    process.pid = 43210

    def _spawn_side_effect(*_args, **_kwargs):
        payload = json.loads(
            (submission_dir / "submission.json").read_text(encoding="utf-8")
        )
        observed_state["state"] = payload.get("state")
        return process

    with patch(
        "crsbench.cloud.reeval_remote.subprocess.Popen",
        side_effect=_spawn_side_effect,
    ):
        launch_remote_submission(submission_dir)

    assert observed_state["state"] == "published"


def test_launch_remote_submission_marks_failed_when_runner_spawn_fails(
    tmp_path: Path,
) -> None:
    from crsbench.cloud.reeval_remote import (
        launch_remote_submission,
        read_submission_state,
    )

    submission_dir = tmp_path / "submission"
    _write_submission_state(submission_dir)
    _write_submission_bundle(submission_dir)

    with patch(
        "crsbench.cloud.reeval_remote.subprocess.Popen",
        side_effect=OSError("spawn failed"),
    ):
        with pytest.raises(OSError, match="spawn failed"):
            launch_remote_submission(submission_dir)

    state = read_submission_state(submission_dir / "submission.json")
    assert state["state"] == "failed"
    summary = json.loads((submission_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["state"] == "failed"
    assert summary["error"] == "spawn failed"


def test_execute_remote_submission_materializes_workspace_and_updates_state(
    tmp_path: Path,
) -> None:
    from crsbench.cloud.reeval_compat import discover_git_revision
    from crsbench.cloud.reeval_remote import (
        execute_remote_submission,
        read_submission_state,
    )

    submission_dir = tmp_path / "submission"
    _write_submission_state(submission_dir)
    _write_submission_bundle(submission_dir)
    stale_file = (
        submission_dir
        / "workspace"
        / "source-exp-reeval-20260424"
        / "bugbench__ensemble"
        / "trial-1"
        / "stale.txt"
    )
    stale_file.parent.mkdir(parents=True, exist_ok=True)
    stale_file.write_text("stale", encoding="utf-8")
    benchmarks_root = tmp_path / "benchmarks"
    (benchmarks_root / "bugbench").mkdir(parents=True)

    with (
        patch(
            "crsbench.cloud.reeval_remote._discover_git_revision",
            return_value=discover_git_revision(),
        ),
        patch(
            "crsbench.cloud.reeval_remote.subprocess.run",
            return_value=subprocess.CompletedProcess(args=["crsbench"], returncode=0),
        ) as mock_run,
    ):
        rc = execute_remote_submission(
            submission_dir,
            redis_host="localhost:6379",
            benchmarks_root=benchmarks_root,
        )

    assert rc == 0
    state = read_submission_state(submission_dir / "submission.json")
    assert state["state"] == "succeeded"
    derived_config = yaml.safe_load(
        (submission_dir / "workspace" / "experiment-config.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert derived_config["experiment"] == "source-exp-reeval-20260424"
    assert derived_config["redis_host"] == "localhost:6379"
    assert derived_config["runtime"]["redis"]["host"] == "localhost:6379"
    assert derived_config["benchmarks_root"] == str(benchmarks_root)
    assert (
        submission_dir
        / "workspace"
        / "source-exp-reeval-20260424"
        / "bugbench__ensemble"
        / "trial-1"
        / "output"
        / "povs"
        / "pov.bin"
    ).is_file()
    summary = json.loads((submission_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["bundle_id"] == "bundle-123"
    assert summary["reeval_exit_code"] == 0
    assert not stale_file.exists()
    mock_run.assert_called_once()
    assert mock_run.call_args.args[0][:3] == [
        sys.executable,
        "-m",
        "crsbench.run_experiment",
    ]


def test_execute_remote_submission_fails_on_runtime_revision_mismatch(
    tmp_path: Path,
) -> None:
    from crsbench.cloud.reeval_remote import (
        execute_remote_submission,
        read_submission_state,
    )

    submission_dir = tmp_path / "submission"
    _write_submission_state(submission_dir)
    _write_submission_bundle(submission_dir)
    benchmarks_root = tmp_path / "benchmarks"
    (benchmarks_root / "bugbench").mkdir(parents=True)

    with (
        patch(
            "crsbench.cloud.reeval_remote._discover_git_revision",
            return_value="remote-rev",
        ),
        patch("crsbench.cloud.reeval_remote.subprocess.run") as mock_run,
    ):
        rc = execute_remote_submission(
            submission_dir,
            redis_host="localhost:6379",
            benchmarks_root=benchmarks_root,
        )

    assert rc == 1
    mock_run.assert_not_called()
    state = read_submission_state(submission_dir / "submission.json")
    assert state["state"] == "failed"
    summary = json.loads((submission_dir / "summary.json").read_text(encoding="utf-8"))
    assert "runtime revision" in summary["error"]


def test_execute_remote_submission_marks_failed_on_runner_exception(
    tmp_path: Path,
) -> None:
    from crsbench.cloud.reeval_compat import discover_git_revision
    from crsbench.cloud.reeval_remote import (
        execute_remote_submission,
        read_submission_state,
    )

    submission_dir = tmp_path / "submission"
    _write_submission_state(submission_dir)
    _write_submission_bundle(submission_dir)
    benchmarks_root = tmp_path / "benchmarks"
    (benchmarks_root / "bugbench").mkdir(parents=True)

    with (
        patch(
            "crsbench.cloud.reeval_remote._discover_git_revision",
            return_value=discover_git_revision(),
        ),
        patch(
            "crsbench.cloud.reeval_remote.subprocess.run",
            side_effect=RuntimeError("reeval boom"),
        ),
    ):
        rc = execute_remote_submission(
            submission_dir,
            benchmarks_root=benchmarks_root,
        )

    assert rc == 1
    state = read_submission_state(submission_dir / "submission.json")
    assert state["state"] == "failed"
    summary = json.loads((submission_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["state"] == "failed"
    assert summary["error"] == "reeval boom"
