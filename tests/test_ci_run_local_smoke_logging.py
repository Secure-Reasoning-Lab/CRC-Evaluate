"""Tests for CI smoke log streaming helpers."""

from __future__ import annotations

import subprocess
from pathlib import Path

SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "ci-tests" / "run-local.sh"
)


def _run_bash(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-lc", script],
        capture_output=True,
        text=True,
        check=False,
    )


def test_run_smoke_logged_command_streams_prefixed_output(tmp_path: Path) -> None:
    log_path = tmp_path / "smoke.log"

    result = _run_bash(
        f"""
        export CRSBENCH_RUN_LOCAL_SOURCE_ONLY=1
        export SMOKE_STREAM_LOGS=1
        source "{SCRIPT_PATH}"
        run_smoke_logged_command "{log_path}" "smoke:bugfinding:run" \
            bash -lc 'printf "hello\\n"; printf "err\\n" >&2'
        """
    )

    assert result.returncode == 0
    combined = result.stdout + result.stderr
    assert "[smoke:bugfinding:run] hello" in combined
    assert "[smoke:bugfinding:run] err" in combined
    assert log_path.read_text() == "hello\nerr\n"


def test_load_smoke_env_file_exports_missing_vars(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("CRSBENCH_SMOKE_FROM_ENV=loaded\n")

    result = _run_bash(
        f"""
        export CRSBENCH_RUN_LOCAL_SOURCE_ONLY=1
        export CRSBENCH_RUN_LOCAL_ENV_FILE="{env_file}"
        source "{SCRIPT_PATH}"
        load_smoke_env_file
        printf '%s' "$CRSBENCH_SMOKE_FROM_ENV"
        """
    )

    assert result.returncode == 0
    assert result.stdout == "loaded"


def test_load_smoke_env_file_preserves_existing_env(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("CRSBENCH_SMOKE_FROM_ENV=file-value\n")

    result = _run_bash(
        f"""
        export CRSBENCH_RUN_LOCAL_SOURCE_ONLY=1
        export CRSBENCH_RUN_LOCAL_ENV_FILE="{env_file}"
        export CRSBENCH_SMOKE_FROM_ENV=existing-value
        source "{SCRIPT_PATH}"
        load_smoke_env_file
        printf '%s' "$CRSBENCH_SMOKE_FROM_ENV"
        """
    )

    assert result.returncode == 0
    assert result.stdout == "existing-value"


def test_run_smoke_logged_command_can_be_quiet(tmp_path: Path) -> None:
    log_path = tmp_path / "smoke.log"

    result = _run_bash(
        f"""
        export CRSBENCH_RUN_LOCAL_SOURCE_ONLY=1
        export SMOKE_STREAM_LOGS=0
        source "{SCRIPT_PATH}"
        run_smoke_logged_command "{log_path}" "smoke:bugfixing:run" \
            bash -lc 'printf "only-log\\n"'
        """
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""
    assert log_path.read_text() == "only-log\n"


def test_run_smoke_logged_command_falls_back_without_stream_support(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "smoke.log"

    result = _run_bash(
        f"""
        export CRSBENCH_RUN_LOCAL_SOURCE_ONLY=1
        export SMOKE_STREAM_LOGS=1
        source "{SCRIPT_PATH}"
        smoke_can_stream_logs() {{
            return 1
        }}
        run_smoke_logged_command "{log_path}" "smoke:bugfinding:run" \
            bash -lc 'printf "fallback\\n"'
        """
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""
    assert log_path.read_text() == "fallback\n"


def test_start_smoke_logged_command_bg_streams_and_stops(tmp_path: Path) -> None:
    log_path = tmp_path / "worker.log"

    result = _run_bash(
        f"""
        export CRSBENCH_RUN_LOCAL_SOURCE_ONLY=1
        export SMOKE_STREAM_LOGS=1
        source "{SCRIPT_PATH}"
        start_smoke_logged_command_bg "{log_path}" "smoke:bugfinding:worker" \
            bash -lc 'printf "worker-ready\\n"; trap "exit 0" TERM; while :; do sleep 1; done'
        worker_pid="$SMOKE_BG_PID"
        sleep 2
        stop_worker_process "$worker_pid" "worker[test]"
        """
    )

    assert result.returncode == 0
    combined = result.stdout + result.stderr
    assert "[smoke:bugfinding:worker] worker-ready" in combined
    assert "warning:" not in combined
    assert "worker-ready" in log_path.read_text()


def test_run_smoke_logged_command_preserves_failure_exit_code(tmp_path: Path) -> None:
    log_path = tmp_path / "smoke.log"

    result = _run_bash(
        f"""
        export CRSBENCH_RUN_LOCAL_SOURCE_ONLY=1
        export SMOKE_STREAM_LOGS=1
        source "{SCRIPT_PATH}"
        run_smoke_logged_command "{log_path}" "smoke:bugfinding:run" \
            bash -lc 'printf "before-fail\\n"; exit 7'
        """
    )

    assert result.returncode == 7
    combined = result.stdout + result.stderr
    assert "[smoke:bugfinding:run] before-fail" in combined
    assert log_path.read_text() == "before-fail\n"
