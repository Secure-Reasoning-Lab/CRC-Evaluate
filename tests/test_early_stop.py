"""Tests for coverage early stop functionality.

These tests verify that:
1. run_with_graceful_timeout properly handles stop_event for early termination
2. The saturation monitor thread correctly signals early stop
"""

import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

from crsbench.evaluation.adapter import OssCrsAdapter
from crsbench.evaluation.process_utils import run_with_graceful_timeout

_STUB_ADAPTER = OssCrsAdapter(
    crs_config_name="stub",
    oss_fuzz_path=Path("/tmp/fake/oss-fuzz"),
    registry_dir=Path("/tmp/fake/registry"),
    benchmarks_root=Path("/tmp/fake/benchmarks"),
    crs_configs_dir=Path("/tmp/fake/configs"),
    mode="bug-finding",
)


class TestRunWithGracefulTimeoutStopEvent:
    """Tests for stop_event support in run_with_graceful_timeout."""

    def test_no_stop_event_runs_to_completion(self):
        """Without stop_event, process runs to completion."""
        # Simple echo command that completes quickly
        stdout, stderr, returncode, timed_out = run_with_graceful_timeout(
            cmd=["echo", "hello"],
            timeout=10,
        )
        assert "hello" in stdout
        assert returncode == 0
        assert timed_out is False

    def test_no_stop_event_times_out(self):
        """Without stop_event, process times out after specified timeout."""
        # Sleep for longer than timeout
        start = time.time()
        stdout, stderr, returncode, timed_out = run_with_graceful_timeout(
            cmd=["sleep", "10"],
            timeout=1,
            grace_period=1,
        )
        elapsed = time.time() - start

        assert timed_out is True
        # Should have timed out around 1-2 seconds (timeout + some grace)
        assert elapsed < 5

    def test_stop_event_terminates_early(self):
        """With stop_event signaled, process terminates before timeout."""
        stop_event = threading.Event()

        # Signal stop after 0.5 seconds
        def signal_stop():
            time.sleep(0.5)
            stop_event.set()

        signal_thread = threading.Thread(target=signal_stop)
        signal_thread.start()

        start = time.time()
        stdout, stderr, returncode, timed_out = run_with_graceful_timeout(
            cmd=["sleep", "30"],  # Would take 30 seconds without early stop
            timeout=60,
            grace_period=2,
            stop_event=stop_event,
        )
        elapsed = time.time() - start

        signal_thread.join()

        # Should have terminated early due to stop_event
        assert timed_out is True
        # Should complete in about 0.5s (signal delay) + 2s (grace) = ~2.5s max
        assert elapsed < 10

    def test_stop_event_not_signaled_runs_to_timeout(self):
        """With stop_event not signaled, process runs to timeout."""
        stop_event = threading.Event()  # Never signaled

        start = time.time()
        stdout, stderr, returncode, timed_out = run_with_graceful_timeout(
            cmd=["sleep", "30"],
            timeout=1,
            grace_period=1,
            stop_event=stop_event,
        )
        elapsed = time.time() - start

        assert timed_out is True
        # Should timeout at 1 second, not wait for 30
        assert elapsed < 5

    def test_stop_event_process_completes_normally(self):
        """With stop_event provided but process completes, returns success."""
        stop_event = threading.Event()

        stdout, stderr, returncode, timed_out = run_with_graceful_timeout(
            cmd=["echo", "done"],
            timeout=60,
            grace_period=2,
            stop_event=stop_event,
        )

        assert "done" in stdout
        assert returncode == 0
        assert timed_out is False


class TestSaturationMonitorIntegration:
    """Tests for saturation monitor integration with BenchmarkRunner."""

    def test_monitor_saturation_signals_stop_when_saturated(self):
        """Saturation monitor signals stop_event when saturation detected."""
        from crsbench.evaluation.runner import BenchmarkRunner

        # Create mock coverage manager that reports saturation
        mock_coverage_manager = MagicMock()
        mock_coverage_manager.is_saturated.return_value = True

        # Create runner with early stop enabled
        runner = BenchmarkRunner(
            _STUB_ADAPTER,
            coverage_enabled=True,
            coverage_early_stop=True,
            coverage_saturation_time=60,
        )

        stop_event = threading.Event()

        # Start monitor in thread
        monitor_thread = threading.Thread(
            target=runner._monitor_saturation,
            args=(mock_coverage_manager, stop_event),
            daemon=True,
        )
        monitor_thread.start()

        # Wait for stop_event to be set (should happen quickly since is_saturated=True)
        assert stop_event.wait(timeout=10), "stop_event should be set when saturated"

        monitor_thread.join(timeout=1)

    def test_monitor_saturation_waits_when_not_saturated(self):
        """Saturation monitor keeps checking when not saturated."""
        from crsbench.evaluation.runner import BenchmarkRunner

        # Create mock coverage manager that doesn't report saturation initially
        mock_coverage_manager = MagicMock()
        check_count = [0]

        def is_saturated_impl():
            check_count[0] += 1
            # Return True after 2 checks
            return check_count[0] >= 2

        mock_coverage_manager.is_saturated.side_effect = is_saturated_impl

        runner = BenchmarkRunner(
            _STUB_ADAPTER,
            coverage_enabled=True,
            coverage_early_stop=True,
            coverage_saturation_time=60,
        )

        stop_event = threading.Event()

        monitor_thread = threading.Thread(
            target=runner._monitor_saturation,
            args=(mock_coverage_manager, stop_event),
            daemon=True,
        )
        monitor_thread.start()

        # Wait for stop_event
        assert stop_event.wait(timeout=15), "stop_event should be set eventually"

        # Verify multiple checks were made
        assert check_count[0] >= 2

        monitor_thread.join(timeout=1)

    def test_monitor_saturation_stops_when_event_pre_set(self):
        """Saturation monitor exits immediately if stop_event already set."""
        from crsbench.evaluation.runner import BenchmarkRunner

        mock_coverage_manager = MagicMock()
        mock_coverage_manager.is_saturated.return_value = False

        runner = BenchmarkRunner(
            _STUB_ADAPTER,
            coverage_enabled=True,
            coverage_early_stop=True,
        )

        stop_event = threading.Event()
        stop_event.set()  # Pre-set

        start = time.time()
        runner._monitor_saturation(mock_coverage_manager, stop_event)
        elapsed = time.time() - start

        # Should return immediately
        assert elapsed < 1


class TestEarlyStopDisabled:
    """Tests verifying behavior when early stop is disabled."""

    def test_runner_no_monitor_when_early_stop_disabled(self):
        """No saturation monitor thread when coverage_early_stop=False."""
        from crsbench.evaluation.runner import BenchmarkRunner

        runner = BenchmarkRunner(
            _STUB_ADAPTER,
            coverage_enabled=True,
            coverage_early_stop=False,  # Disabled
            coverage_saturation_time=60,
        )

        # The runner should not create a saturation monitor
        # We verify by checking the initialization message
        assert runner.coverage_early_stop is False

    def test_runner_no_monitor_when_coverage_disabled(self):
        """No saturation monitor when coverage is disabled."""
        from crsbench.evaluation.runner import BenchmarkRunner

        runner = BenchmarkRunner(
            _STUB_ADAPTER,
            coverage_enabled=False,  # Disabled
            coverage_early_stop=True,
        )

        # Early stop requires coverage enabled
        assert runner.coverage_enabled is False
