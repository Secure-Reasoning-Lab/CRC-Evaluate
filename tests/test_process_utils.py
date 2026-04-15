"""Regression tests for crsbench.evaluation.process_utils.

Specifically pins the GH #182 fix: run_with_graceful_timeout must kill the
entire child process group (not just the direct child) when a timeout
fires, so grandchildren that hold stdout/stderr pipes open (notably
``docker`` CLI wrappers spawned by oss-crs) do not block
``process.communicate()`` past the grace period.
"""

from __future__ import annotations

import os
import sys
import time

import pytest
from crsbench.evaluation.process_utils import run_with_graceful_timeout


class TestRunWithGracefulTimeoutHappyPath:
    """Tests for normal (no timeout) execution."""

    def test_returns_stdout_stderr_rc_and_flag(self) -> None:
        stdout, stderr, rc, timed_out = run_with_graceful_timeout(
            ["echo", "hello"], timeout=10
        )
        assert "hello" in stdout
        assert stderr == ""
        assert rc == 0
        assert timed_out is False

    def test_captures_stderr(self) -> None:
        stdout, stderr, rc, timed_out = run_with_graceful_timeout(
            ["bash", "-c", "echo err >&2"], timeout=10
        )
        assert "err" in stderr
        assert rc == 0
        assert timed_out is False

    def test_returns_nonzero_exit_code(self) -> None:
        _stdout, _stderr, rc, timed_out = run_with_graceful_timeout(
            ["false"], timeout=10
        )
        assert rc != 0
        assert timed_out is False

    def test_cwd_parameter(self, tmp_path) -> None:
        stdout, _stderr, rc, _timed_out = run_with_graceful_timeout(
            ["pwd"], timeout=10, cwd=tmp_path
        )
        assert rc == 0
        assert str(tmp_path) in stdout


class TestRunWithGracefulTimeoutKillsProcessGroup:
    """GH #182 regression tests: timeout must kill the entire process group."""

    def test_sleep_timeout_reports_timed_out(self) -> None:
        stdout, stderr, rc, timed_out = run_with_graceful_timeout(
            ["sleep", "60"], timeout=1, grace_period=1
        )
        assert timed_out is True
        # stdout/stderr should be strings regardless of whether the child
        # wrote anything
        assert isinstance(stdout, str)
        assert isinstance(stderr, str)

    def test_timeout_captures_output_before_sleep(self) -> None:
        """Output written before the sleep must still be returned."""
        stdout, _stderr, _rc, timed_out = run_with_graceful_timeout(
            ["bash", "-c", "echo marker_before_sleep; sleep 60"],
            timeout=2,
            grace_period=1,
        )
        assert timed_out is True
        assert "marker_before_sleep" in stdout

    def test_timeout_kills_grandchild_process(self) -> None:
        """Spawn a shell that backgrounds a grandchild and prints its PID.

        This is the load-bearing GH #182 regression test: without
        ``start_new_session=True`` + ``os.killpg``, the grandchild would
        survive the shell's termination and keep running, holding pipes
        open and potentially leaking file descriptors. With the fix, the
        entire process group dies together.
        """
        script = (
            "echo $$ ; "  # shell PID
            "sleep 300 & "  # background grandchild
            "echo $! ; "  # grandchild PID
            "wait"
        )

        stdout, _stderr, _rc, timed_out = run_with_graceful_timeout(
            ["bash", "-c", script], timeout=2, grace_period=1
        )

        assert timed_out is True, "Expected timeout to fire"

        lines = [ln.strip() for ln in stdout.strip().splitlines() if ln.strip()]
        assert len(lines) >= 2, (
            f"Expected at least 2 PID lines in stdout; got {lines!r}"
        )
        shell_pid = int(lines[0])
        child_pid = int(lines[1])

        # Give the OS a beat to reap the process group after communicate().
        time.sleep(0.5)

        for pid in (shell_pid, child_pid):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                continue  # expected: process was killed
            # If we reach here, the process is still alive. Try to clean
            # it up so a failing test does not leak background processes.
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass
            pytest.fail(
                f"Process {pid} survived run_with_graceful_timeout; "
                f"process-group kill did not propagate to grandchildren. "
                f"This is the GH #182 leak path."
            )

    def test_timeout_does_not_hang_when_grandchild_holds_pipes_open(
        self,
    ) -> None:
        """The grace-period wait must not block forever.

        Before the GH #182 fix, ``process.terminate()`` only signalled
        the direct child. A grandchild that inherited stdout/stderr via
        its parent could keep the pipes open and block
        ``process.communicate(timeout=grace_period)`` until the grace
        period's own timeout fired. With the process-group kill, the
        grandchild dies immediately and communicate() returns.
        """
        # Spawn a shell that redirects stdout/stderr to a background
        # sleeper and then sleeps itself. The background sleeper holds
        # the pipes open after the direct shell is signalled.
        script = (
            "exec 3>&1 ; "  # save stdout
            "echo started ; "  # ensure parent flushes before sleep
            "(sleep 600) & "  # background grandchild keeps inherited fds
            "sleep 600"
        )

        wall_start = time.monotonic()
        stdout, _stderr, _rc, timed_out = run_with_graceful_timeout(
            ["bash", "-c", script], timeout=1, grace_period=10
        )
        wall_elapsed = time.monotonic() - wall_start

        assert timed_out is True
        assert "started" in stdout
        # With the process-group kill, teardown should complete well
        # within `timeout + grace_period`. Budget generously to avoid
        # flakes on slow CI.
        assert wall_elapsed < 15.0, (
            f"run_with_graceful_timeout took {wall_elapsed:.1f}s; expected "
            f"< 15s. This suggests the grace-period wait is hanging, which "
            f"is the GH #182 pipe-hang path."
        )


class TestRunWithGracefulTimeoutEarlyStop:
    """Tests for stop_event-triggered early termination."""

    def test_stop_event_terminates_quickly(self) -> None:
        import threading

        stop = threading.Event()

        def _set_stop_soon() -> None:
            time.sleep(0.5)
            stop.set()

        threading.Thread(target=_set_stop_soon, daemon=True).start()

        wall_start = time.monotonic()
        _stdout, _stderr, _rc, timed_out = run_with_graceful_timeout(
            ["sleep", "60"],
            timeout=30,
            grace_period=1,
            stop_event=stop,
        )
        wall_elapsed = time.monotonic() - wall_start

        assert timed_out is True
        assert wall_elapsed < 5.0, f"Early stop took {wall_elapsed:.1f}s; expected < 5s"


class TestProcessGroupIntegration:
    """Verify that the child is actually started in its own process group."""

    def test_child_runs_in_new_session(self) -> None:
        """PGID of the child must differ from the Python runner's PGID.

        This is the precondition for ``os.killpg`` to work on the whole
        child tree without signalling the test runner itself.
        """
        # Print both the child's PGID and the parent's PGID.
        # We compare them via stdout; the child's PGID should equal its
        # own PID when started with start_new_session=True.
        code = "import os, sys; print(f'pid={os.getpid()} pgid={os.getpgid(0)}')"

        stdout, _stderr, rc, _timed_out = run_with_graceful_timeout(
            [sys.executable, "-c", code], timeout=10
        )
        assert rc == 0
        assert "pid=" in stdout
        assert "pgid=" in stdout

        # Parse the pid / pgid from the output.
        # Format: "pid=<int> pgid=<int>"
        parts = dict(kv.split("=", 1) for kv in stdout.strip().split() if "=" in kv)
        child_pid = int(parts["pid"])
        child_pgid = int(parts["pgid"])

        # start_new_session=True makes the child its own group leader,
        # so its PGID equals its PID.
        assert child_pgid == child_pid, (
            f"Child should be its own process group leader. "
            f"pid={child_pid}, pgid={child_pgid}. "
            f"This means start_new_session=True was not applied, and "
            f"os.killpg() on the grace-period path would signal the test "
            f"runner's own group instead of the child's tree."
        )
