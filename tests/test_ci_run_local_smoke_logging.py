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


def test_run_checks_scopes_blank_apprise_env_to_pytest_command() -> None:
    result = _run_bash(
        f"""
        export CRSBENCH_RUN_LOCAL_SOURCE_ONLY=1
        export CRSBENCH_NOTIFY_APPRISE_URLS=discord://token/channel
        export CRSBENCH_NOTIFY_APPRISE_TITLE="Bench Alerts"
        export CRSBENCH_NOTIFY_APPRISE_TAG=ops
        source "{SCRIPT_PATH}"
        just() {{ return 0; }}
        uv() {{
            if [ "$1" = "run" ] && [ "$2" = "pytest" ]; then
                printf 'pytest_urls=<%s>;pytest_title=<%s>;pytest_tag=<%s>;args=<%s>\\n' "$CRSBENCH_NOTIFY_APPRISE_URLS" "$CRSBENCH_NOTIFY_APPRISE_TITLE" "$CRSBENCH_NOTIFY_APPRISE_TAG" "$*"
            fi
            return 0
        }}
        run_stage() {{ :; }}
        success() {{ :; }}
        fail() {{ printf 'fail:<%s>\\n' "$1"; return 1; }}
        run_checks
        printf 'shell_urls=<%s>;shell_title=<%s>;shell_tag=<%s>' "$CRSBENCH_NOTIFY_APPRISE_URLS" "$CRSBENCH_NOTIFY_APPRISE_TITLE" "$CRSBENCH_NOTIFY_APPRISE_TAG"
        """
    )

    assert result.returncode == 0
    assert (
        "pytest_urls=<>;pytest_title=<>;pytest_tag=<>;args=<run pytest tests/ -v -n auto -m not integration and not notification>"
        in result.stdout
    )
    assert (
        "shell_urls=<discord://token/channel>;shell_title=<Bench Alerts>;shell_tag=<ops>"
        in result.stdout
    )


def test_run_checks_excludes_notification_marker() -> None:
    result = _run_bash(
        f"""
        export CRSBENCH_RUN_LOCAL_SOURCE_ONLY=1
        source "{SCRIPT_PATH}"
        declare -f run_checks
        """
    )

    assert result.returncode == 0
    assert '-m "not integration and not notification"' in result.stdout


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


def test_cleanup_smoke_bg_logging_ignores_failed_logger(tmp_path: Path) -> None:
    stream_dir = tmp_path / "stream-dir"
    fifo_path = stream_dir / "stream"

    result = _run_bash(
        f"""
        export CRSBENCH_RUN_LOCAL_SOURCE_ONLY=1
        source "{SCRIPT_PATH}"
        mkdir -p "{stream_dir}"
        mkfifo "{fifo_path}"
        bash -lc 'exit 1' &
        SMOKE_BG_LOGGER_PID=$!
        SMOKE_BG_STREAM_DIR="{stream_dir}"
        SMOKE_BG_STREAM_FIFO="{fifo_path}"
        cleanup_smoke_bg_logging
        [ ! -e "{fifo_path}" ]
        [ ! -d "{stream_dir}" ]
        """
    )

    assert result.returncode == 0


def test_cleanup_stale_smoke_state_uses_runner_temp_and_keeps_unrelated_dirs(
    tmp_path: Path,
) -> None:
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    stale_bugfinding = runner_temp / "crsbench-smoke-bugfinding-old"
    stale_bugfixing = runner_temp / "crsbench-smoke-bugfixing-old"
    stale_stream = runner_temp / "crsbench-smoke-stream-old"
    unrelated = runner_temp / "leave-alone"
    stale_bugfinding.mkdir()
    stale_bugfixing.mkdir()
    stale_stream.mkdir()
    unrelated.mkdir()

    result = _run_bash(
        f"""
        export CRSBENCH_RUN_LOCAL_SOURCE_ONLY=1
        export RUNNER_TEMP="{runner_temp}"
        export SMOKE_CLEAN_STALE=1
        source "{SCRIPT_PATH}"
        cleanup_stale_smoke_state
        """
    )

    assert result.returncode == 0
    assert not stale_bugfinding.exists()
    assert not stale_bugfixing.exists()
    assert not stale_stream.exists()
    assert unrelated.exists()
    assert (runner_temp / "crsbench-smoke-workspaces").exists()


def test_run_smoke_suite_verify_failure_cleans_workspace_and_marker(
    tmp_path: Path,
) -> None:
    marker_dir = tmp_path / "markers"
    workspace = tmp_path / "workspace"
    marker_dir.mkdir()
    (workspace / "experiment-data").mkdir(parents=True)
    (marker_dir / "bugfinding").write_text(str(workspace))

    result = _run_bash(
        f"""
        export CRSBENCH_RUN_LOCAL_SOURCE_ONLY=1
        export SMOKE_WORKSPACE_DIR="{marker_dir}"
        source "{SCRIPT_PATH}"
        run_stage() {{ :; }}
        smoke_skip_verification_for_suite() {{ echo 0; }}
        _smoke_verify_summary() {{ :; }}
        run_smoke_logged_command() {{
            : > "$1"
            return 7
        }}
        run_smoke_suite_verify bugfinding "Verify bugfinding"
        """
    )

    assert result.returncode == 1
    assert not workspace.exists()
    assert not (marker_dir / "bugfinding").exists()


def test_run_smoke_suite_run_failure_cleans_workspace_and_marker(
    tmp_path: Path,
) -> None:
    runner_temp = tmp_path / "runner-temp"
    base_config = tmp_path / "smoke-config.yaml"
    runner_temp.mkdir()
    base_config.write_text("experiment: smoke\n")

    result = _run_bash(
        f"""
        export CRSBENCH_RUN_LOCAL_SOURCE_ONLY=1
        export RUNNER_TEMP="{runner_temp}"
        export CRSBENCH_REDIS_HOST=localhost:6379
        source "{SCRIPT_PATH}"
        run_stage() {{ :; }}
        success() {{ :; }}
        sleep() {{
            if [ "$1" = "3" ]; then
                return 0
            fi
            command sleep "$@"
        }}
        smoke_config_for_suite() {{ printf '%s\\n' "{base_config}"; }}
        smoke_skip_verification_for_suite() {{ echo 0; }}
        render_smoke_config() {{
            : > "$3"
            return 0
        }}
        start_smoke_logged_command_bg() {{
            bash -lc 'trap "exit 0" TERM; while :; do sleep 10; done' &
            SMOKE_BG_PID=$!
            SMOKE_BG_LOGGER_PID=""
        }}
        stop_worker_process() {{
            kill "$1" 2>/dev/null || true
            wait "$1" 2>/dev/null || true
            return 0
        }}
        run_smoke_logged_command() {{
            return 7
        }}
        run_smoke_suite_run bugfinding "Run bugfinding"
        """
    )

    assert result.returncode == 1
    assert not list(runner_temp.glob("crsbench-smoke-bugfinding-*"))
    assert not (runner_temp / "crsbench-smoke-workspaces" / "bugfinding").exists()
