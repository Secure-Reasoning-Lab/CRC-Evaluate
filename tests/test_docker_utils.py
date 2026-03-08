"""Tests for Docker cleanup helpers."""

import subprocess
from pathlib import Path
from unittest.mock import patch

from crsbench.utils.docker import docker_rmtree


def test_docker_rmtree_fallback_returns_true_on_success(tmp_path: Path) -> None:
    """Fallback should succeed when Docker cleanup and host delete both succeed."""
    target = tmp_path / "out"
    target.mkdir()

    with (
        patch("shutil.rmtree", side_effect=[PermissionError(), None]) as mock_rmtree,
        patch(
            "crsbench.utils.docker.run_with_timeout",
            return_value=subprocess.CompletedProcess(
                args=["docker"], returncode=0, stdout="", stderr=""
            ),
        ) as mock_run,
    ):
        assert docker_rmtree(target) is True

    assert mock_rmtree.call_count == 2
    cmd = mock_run.call_args.args[0]
    assert "; true" not in cmd[-1]


def test_docker_rmtree_fallback_returns_false_when_docker_fails(tmp_path: Path) -> None:
    """Fallback should report failure when Docker command fails."""
    target = tmp_path / "out"
    target.mkdir()

    with (
        patch("shutil.rmtree", side_effect=PermissionError()),
        patch(
            "crsbench.utils.docker.run_with_timeout",
            return_value=subprocess.CompletedProcess(
                args=["docker"], returncode=1, stdout="", stderr="rm failed"
            ),
        ),
    ):
        assert docker_rmtree(target) is False


def test_docker_rmtree_fallback_returns_false_with_residuals(tmp_path: Path) -> None:
    """Fallback should fail if host delete still fails after Docker cleanup."""
    target = tmp_path / "out"
    target.mkdir()

    with (
        patch("shutil.rmtree", side_effect=[PermissionError(), OSError("busy")]),
        patch(
            "crsbench.utils.docker.run_with_timeout",
            return_value=subprocess.CompletedProcess(
                args=["docker"], returncode=0, stdout="", stderr=""
            ),
        ),
    ):
        assert docker_rmtree(target) is False
