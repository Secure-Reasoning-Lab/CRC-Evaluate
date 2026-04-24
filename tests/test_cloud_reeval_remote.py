from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml


def _write_submission_bundle(
    submission_dir: Path,
    *,
    source_experiment: str = "source-exp",
    remote_experiment: str = "source-exp-reeval-20260424",
) -> None:
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
    _write_submission_bundle(submission_dir)

    process = MagicMock()
    process.pid = 43210
    with patch(
        "crsbench.cloud.reeval_remote.subprocess.Popen", return_value=process
    ) as mock_popen:
        pid = launch_remote_submission(submission_dir)

    assert pid == 43210
    state = read_submission_state(submission_dir / "submission.json")
    assert state["state"] == "published"
    assert "python" in " ".join(mock_popen.call_args.args[0])
    assert "--submission-dir" in mock_popen.call_args.args[0]


def test_execute_remote_submission_materializes_workspace_and_updates_state(
    tmp_path: Path,
) -> None:
    from crsbench.cloud.reeval_remote import (
        execute_remote_submission,
        read_submission_state,
    )

    submission_dir = tmp_path / "submission"
    _write_submission_state(submission_dir)
    _write_submission_bundle(submission_dir)

    with patch(
        "crsbench.cloud.reeval_remote.subprocess.run",
        return_value=subprocess.CompletedProcess(args=["crsbench"], returncode=0),
    ) as mock_run:
        rc = execute_remote_submission(
            submission_dir,
            redis_host="localhost:6379",
            benchmarks_root=Path("/opt/crsbench/benchmarks"),
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
    assert derived_config["benchmarks_root"] == "/opt/crsbench/benchmarks"
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
    mock_run.assert_called_once()
