"""Tests for crsbench.utils.subprocess_utils.

Tests the process-group-aware timeout handling that prevents orphaned
Docker containers from blocking communicate() forever.
"""

import os
import subprocess
import sys
import time

import pytest
from crsbench.utils.subprocess_utils import (
    docker_kill_orphans,
    run_with_timeout,
)

_NON_UTF8_SCRIPT = (
    "import sys; "
    'sys.stdout.buffer.write(b"stdout:\\xff\\n"); '
    "sys.stdout.flush(); "
    'sys.stderr.buffer.write(b"stderr:\\xfe\\n"); '
    "sys.stderr.flush()"
)
_NON_UTF8_SLEEP_SCRIPT = _NON_UTF8_SCRIPT + "; import time; time.sleep(5)"


class TestRunWithTimeoutNormalExecution:
    """Tests for normal (no timeout) execution."""

    def test_returns_completed_process(self):
        result = run_with_timeout(["echo", "hello"], timeout=10)
        assert isinstance(result, subprocess.CompletedProcess)
        assert result.returncode == 0
        assert result.stdout.strip() == "hello"

    def test_captures_stderr(self):
        result = run_with_timeout(
            ["bash", "-c", "echo err >&2"],
            timeout=10,
        )
        assert "err" in result.stderr

    def test_returns_nonzero_exit_code(self):
        result = run_with_timeout(["false"], timeout=10)
        assert result.returncode != 0

    def test_cwd_parameter(self, tmp_path):
        result = run_with_timeout(["pwd"], timeout=10, cwd=str(tmp_path))
        assert result.stdout.strip() == str(tmp_path)

    def test_binary_mode(self):
        result = run_with_timeout(
            ["echo", "binary"],
            timeout=10,
            text=False,
        )
        assert isinstance(result.stdout, bytes)
        assert b"binary" in result.stdout

    def test_text_mode(self):
        result = run_with_timeout(
            ["echo", "text"],
            timeout=10,
            text=True,
        )
        assert isinstance(result.stdout, str)
        assert "text" in result.stdout

    def test_text_mode_replaces_non_utf8_output(self):
        result = run_with_timeout(
            [sys.executable, "-c", _NON_UTF8_SCRIPT],
            timeout=10,
            text=True,
        )
        assert isinstance(result.stdout, str)
        assert isinstance(result.stderr, str)
        assert "stdout:" in result.stdout
        assert "\ufffd" in result.stdout
        assert "stderr:" in result.stderr
        assert "\ufffd" in result.stderr

    def test_capture_output_false(self):
        result = run_with_timeout(
            ["echo", "hidden"],
            timeout=10,
            capture_output=False,
        )
        assert result.returncode == 0
        assert result.stdout is None
        assert result.stderr is None


class TestRunWithTimeoutKillsProcessGroup:
    """Tests that timeout kills the entire process group."""

    def test_timeout_raises_timeout_expired(self):
        with pytest.raises(subprocess.TimeoutExpired):
            run_with_timeout(["sleep", "60"], timeout=1)

    def test_timeout_kills_child_process(self):
        """Verify that the child process is dead after timeout."""
        try:
            run_with_timeout(["sleep", "60"], timeout=1)
        except subprocess.TimeoutExpired:
            pass
        # Give OS a moment to clean up
        time.sleep(0.2)

    def test_timeout_expired_has_stdout(self):
        """Verify TimeoutExpired has stdout/stderr attached."""
        with pytest.raises(subprocess.TimeoutExpired) as exc_info:
            run_with_timeout(
                ["bash", "-c", "echo before_timeout; sleep 60"],
                timeout=2,
            )
        # stdout should be captured even on timeout
        assert exc_info.value.output is not None
        assert "before_timeout" in exc_info.value.output

    def test_timeout_expired_has_stderr(self):
        """Verify TimeoutExpired has stderr attached."""
        with pytest.raises(subprocess.TimeoutExpired) as exc_info:
            run_with_timeout(
                ["bash", "-c", "echo err_before >&2; sleep 60"],
                timeout=2,
            )
        assert exc_info.value.stderr is not None
        assert "err_before" in exc_info.value.stderr

    def test_process_group_kill_on_timeout(self):
        """Verify entire process group is killed, not just the direct child.

        Spawns a shell that creates a grandchild (sleep). On timeout,
        the entire group should be killed, including the grandchild.
        """
        # Create a script that spawns a background process and writes its PID
        script = (
            "echo $$;"  # Print shell PID
            "sleep 300 &"  # Background grandchild
            "echo $!;"  # Print grandchild PID
            "wait"  # Wait for grandchild
        )

        try:
            run_with_timeout(["bash", "-c", script], timeout=2)
        except subprocess.TimeoutExpired as exc:
            # Parse PIDs from output
            lines = exc.output.strip().splitlines()
            assert len(lines) >= 2, f"Expected 2 PIDs, got: {lines}"
            shell_pid = int(lines[0])
            child_pid = int(lines[1])

            # Give OS time to clean up
            time.sleep(0.5)

            # Both processes should be dead
            for pid in (shell_pid, child_pid):
                try:
                    os.kill(pid, 0)  # Check if process exists
                    pytest.fail(f"Process {pid} should be dead but is still alive")
                except ProcessLookupError:
                    pass  # Expected: process is dead

    def test_binary_mode_timeout(self):
        """Verify binary mode works correctly with timeout."""
        with pytest.raises(subprocess.TimeoutExpired) as exc_info:
            run_with_timeout(
                ["bash", "-c", "echo binary_data; sleep 60"],
                timeout=2,
                text=False,
            )
        assert isinstance(exc_info.value.output, bytes)
        assert b"binary_data" in exc_info.value.output

    def test_timeout_expired_replaces_non_utf8_output_in_text_mode(self):
        with pytest.raises(subprocess.TimeoutExpired) as exc_info:
            run_with_timeout(
                [sys.executable, "-c", _NON_UTF8_SLEEP_SCRIPT],
                timeout=1,
                text=True,
            )
        assert isinstance(exc_info.value.output, str)
        assert isinstance(exc_info.value.stderr, str)
        assert "stdout:" in exc_info.value.output
        assert "\ufffd" in exc_info.value.output
        assert "stderr:" in exc_info.value.stderr
        assert "\ufffd" in exc_info.value.stderr


class TestDockerKillOrphans:
    """Tests for docker_kill_orphans utility."""

    def test_no_matching_containers(self):
        """Should return 0 when no containers match."""
        # Use a prefix unlikely to match any real containers
        result = docker_kill_orphans("crsbench_test_nonexistent_prefix_xyz")
        assert result == 0

    def test_returns_integer(self):
        result = docker_kill_orphans("test_prefix")
        assert isinstance(result, int)
