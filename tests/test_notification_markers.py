"""Regression coverage for notification marker collection."""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]


def _collect_notification_tests() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "uv",
            "run",
            "pytest",
            "--collect-only",
            "-q",
            "-m",
            "notification",
            "tests/test_run_experiment_distributed.py",
        ],
        cwd=ROOT_DIR,
        capture_output=True,
        text=True,
        check=False,
    )


def test_notification_collection_includes_monitor_and_bringup_regressions() -> None:
    result = _collect_notification_tests()

    assert result.returncode == 0, result.stderr
    assert (
        "test_continue_mode_monitors_existing_finished_jobs_without_reenqueue"
        in result.stdout
    )
    assert (
        "test_provider_neutral_cloud_retry_failed_refreshes_active_existing_jobs"
        in result.stdout
    )
    assert "test_cloud_fleet_bringup_runs_before_enqueue" in result.stdout
