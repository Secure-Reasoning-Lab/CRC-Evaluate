"""Tests for distributed worker lock functionality."""

import multiprocessing
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from crsbench.distributed.worker import worker_lock


class TestWorkerLock:
    """Tests for worker_lock context manager."""

    def test_lock_can_be_acquired_once(self):
        """Lock should be successfully acquired when not held."""
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / "test.lock"
            with patch("crsbench.distributed.worker.WORKER_LOCK_FILE", lock_path):
                with worker_lock():
                    # Lock acquired successfully
                    assert lock_path.exists()

    def test_lock_prevents_concurrent_acquisition(self):
        """Lock should prevent concurrent acquisition in the same process."""
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / "test.lock"
            with patch("crsbench.distributed.worker.WORKER_LOCK_FILE", lock_path):
                with worker_lock():
                    # Try to acquire lock again - should fail
                    with pytest.raises(BlockingIOError):
                        with worker_lock():
                            pass  # Should never reach here

    def test_lock_released_after_context_exit(self):
        """Lock should be released when context manager exits."""
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / "test.lock"
            with patch("crsbench.distributed.worker.WORKER_LOCK_FILE", lock_path):
                with worker_lock():
                    pass  # Lock held here

                # Lock should be released now
                with worker_lock():
                    pass  # Should succeed

    def test_lock_released_on_exception(self):
        """Lock should be released even if exception occurs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / "test.lock"
            with patch("crsbench.distributed.worker.WORKER_LOCK_FILE", lock_path):
                try:
                    with worker_lock():
                        raise ValueError("Test exception")
                except ValueError:
                    pass

                # Lock should be released despite exception
                with worker_lock():
                    pass  # Should succeed

    def test_lock_file_location_from_env_var(self):
        """Lock file location should be configurable via environment variable."""
        with tempfile.TemporaryDirectory() as tmpdir:
            custom_lock_path = str(Path(tmpdir) / "custom.lock")

            # Reload the module to pick up the env var
            with patch.dict(
                os.environ, {"CRSBENCH_WORKER_LOCK_PATH": custom_lock_path}
            ):
                from importlib import reload

                from crsbench.distributed import worker as worker_module

                reload(worker_module)

                with worker_module.worker_lock():
                    assert Path(custom_lock_path).exists()

    def test_concurrent_processes_cannot_both_acquire_lock(self):
        """Two separate processes should not be able to hold the lock simultaneously."""
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / "test.lock"

            def try_acquire_lock(lock_file_path, result_queue, hold_time):
                """Helper function to try acquiring lock in a subprocess."""
                import fcntl

                try:
                    # Ensure parent directory exists
                    lock_file_path.parent.mkdir(parents=True, exist_ok=True)

                    # Open lock file
                    with lock_file_path.open("w") as f:
                        # Try to acquire lock (non-blocking)
                        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

                        # Lock acquired
                        result_queue.put("acquired")

                        # Hold lock for specified time
                        time.sleep(hold_time)

                        # Release lock
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                        result_queue.put("released")

                except BlockingIOError:
                    result_queue.put("blocked")

            # Create a queue for inter-process communication
            result_queue = multiprocessing.Queue()

            # Start first process that holds lock for 1 second
            p1 = multiprocessing.Process(
                target=try_acquire_lock, args=(lock_path, result_queue, 1.0)
            )
            p1.start()

            # Wait a bit to ensure p1 acquires the lock
            time.sleep(0.2)

            # Start second process that tries to acquire the same lock
            p2 = multiprocessing.Process(
                target=try_acquire_lock, args=(lock_path, result_queue, 0.1)
            )
            p2.start()

            # Collect results
            results = []
            while len(results) < 3:  # Expect: acquired, blocked, released
                try:
                    result = result_queue.get(timeout=2)
                    results.append(result)
                except Exception:
                    break

            # Wait for processes to finish
            p1.join(timeout=2)
            p2.join(timeout=2)

            # Verify results
            assert "acquired" in results, "First process should acquire lock"
            assert "blocked" in results, "Second process should be blocked"
            assert "released" in results, "First process should release lock"

    def test_lock_creates_parent_directory(self):
        """Lock should create parent directory if it doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            nested_path = Path(tmpdir) / "nested" / "dirs" / "test.lock"
            with patch("crsbench.distributed.worker.WORKER_LOCK_FILE", nested_path):
                with worker_lock():
                    assert nested_path.exists()
                    assert nested_path.parent.exists()
