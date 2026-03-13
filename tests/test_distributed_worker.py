"""Tests for distributed worker lock functionality."""

import argparse
import multiprocessing
import os
import tempfile
import time
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from crsbench.distributed.worker import worker_lock


class TestWorkerLock:
    """Tests for worker_lock context manager."""

    def test_lock_can_be_acquired_once(self):
        """Lock should be successfully acquired when not held."""
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_dir = Path(tmpdir)
            with patch("crsbench.distributed.worker.LOCK_DIR", lock_dir):
                with worker_lock("test-worker"):
                    # Lock acquired successfully
                    expected_lock_path = lock_dir / "crsbench-worker-test-worker.lock"
                    assert expected_lock_path.exists()

    def test_lock_prevents_concurrent_acquisition(self):
        """Lock should prevent concurrent acquisition in the same process."""
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_dir = Path(tmpdir)
            with patch("crsbench.distributed.worker.LOCK_DIR", lock_dir):
                with worker_lock("test-worker"):
                    # Try to acquire lock again - should fail
                    with pytest.raises(BlockingIOError):
                        with worker_lock("test-worker"):
                            pass  # Should never reach here

    def test_lock_released_after_context_exit(self):
        """Lock should be released when context manager exits."""
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_dir = Path(tmpdir)
            with patch("crsbench.distributed.worker.LOCK_DIR", lock_dir):
                with worker_lock("test-worker"):
                    pass  # Lock held here

                # Lock should be released now
                with worker_lock("test-worker"):
                    pass  # Should succeed

    def test_lock_released_on_exception(self):
        """Lock should be released even if exception occurs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_dir = Path(tmpdir)
            with patch("crsbench.distributed.worker.LOCK_DIR", lock_dir):
                try:
                    with worker_lock("test-worker"):
                        raise ValueError("Test exception")
                except ValueError:
                    pass

                # Lock should be released despite exception
                with worker_lock("test-worker"):
                    pass  # Should succeed

    def test_lock_file_location_from_env_var(self):
        """Lock file location should be configurable via environment variable."""
        with tempfile.TemporaryDirectory() as tmpdir:
            custom_lock_dir = str(Path(tmpdir))

            # Reload the module to pick up the env var
            with patch.dict(os.environ, {"CRSBENCH_WORKER_LOCK_DIR": custom_lock_dir}):
                from importlib import reload

                from crsbench.distributed import worker as worker_module

                reload(worker_module)

                with worker_module.worker_lock("test-worker"):
                    expected_lock_path = (
                        Path(custom_lock_dir) / "crsbench-worker-test-worker.lock"
                    )
                    assert expected_lock_path.exists()

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
            nested_dir = Path(tmpdir) / "nested" / "dirs"
            with patch("crsbench.distributed.worker.LOCK_DIR", nested_dir):
                with worker_lock("test-worker"):
                    expected_lock_path = nested_dir / "crsbench-worker-test-worker.lock"
                    assert expected_lock_path.exists()
                    assert expected_lock_path.parent.exists()

    def test_multiple_workers_different_names_can_run_concurrently(self):
        """Workers with different names should be able to run concurrently."""
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_dir = Path(tmpdir)
            with patch("crsbench.distributed.worker.LOCK_DIR", lock_dir):
                # Both locks should be acquirable simultaneously
                with worker_lock("worker-0"):
                    lock_0 = lock_dir / "crsbench-worker-worker-0.lock"
                    assert lock_0.exists()

                    # Different worker name should not conflict
                    with worker_lock("worker-1"):
                        lock_1 = lock_dir / "crsbench-worker-worker-1.lock"
                        assert lock_1.exists()
                        # Both locks should exist simultaneously
                        assert lock_0.exists()
                        assert lock_1.exists()


class TestWorkerContinuousMode:
    """Regression tests for continuous worker process behavior."""

    def test_run_single_worker_continuous_uses_single_loop_runner(self):
        """Continuous child worker should not recursively spawn more workers."""
        from crsbench.distributed.worker import _run_single_worker

        with (
            patch(
                "crsbench.distributed.worker._run_worker_continuous"
            ) as mock_single_loop,
            patch("crsbench.distributed.worker.run_worker_continuous") as mock_spawn,
        ):
            _run_single_worker(
                redis_host="localhost",
                experiment_name="exp",
                worker_name="worker-0",
                continuous=True,
                queue_name="crsbench_exp",
                log_level="INFO",
            )

            mock_single_loop.assert_called_once_with(
                "localhost", "exp", "worker-0", "crsbench_exp"
            )
            mock_spawn.assert_not_called()

    @patch("crsbench.distributed.worker.REDIS_AVAILABLE", new=True)
    def test_worker_main_rejects_invalid_cli_redis_host(self):
        """Worker main should fail when explicit redis_host normalizes to None."""
        from crsbench.distributed.worker import main

        result = main(redis_host="none")
        assert result == 1

    @patch("crsbench.distributed.worker.REDIS_AVAILABLE", new=True)
    def test_worker_main_rejects_invalid_env_redis_host(self):
        """Worker main should fail when CRSBENCH_REDIS_HOST is invalid."""
        from crsbench.distributed.worker import main

        with patch.dict("os.environ", {"CRSBENCH_REDIS_HOST": "   "}, clear=False):
            result = main(redis_host=None)
        assert result == 1


class TestWorkerStdinDetachment:
    """Tests for detaching stdin in all worker execution paths."""

    def test_run_worker_detaches_stdin_before_running_rq_worker(self):
        """Burst worker path should detach stdin before RQ work starts."""
        from crsbench.distributed.worker import _run_worker

        queue = MagicMock()
        queue.count = 0
        queue.deferred_job_registry.count = 0
        worker = MagicMock()
        fake_rq = types.SimpleNamespace(
            Queue=MagicMock(return_value=queue),
            Worker=MagicMock(return_value=worker),
        )

        with (
            patch.dict("sys.modules", {"rq": fake_rq}),
            patch(
                "crsbench.distributed.worker._detach_stdin_to_devnull"
            ) as mock_detach,
            patch(
                "crsbench.distributed.queue.create_redis_connection",
                return_value=MagicMock(),
            ),
        ):
            _run_worker("localhost", "exp", "worker-0", "crsbench_trial")

        mock_detach.assert_called_once_with()

    def test_run_worker_continuous_detaches_stdin_before_running_rq_worker(self):
        """Continuous worker path should detach stdin before polling."""
        from crsbench.distributed.worker import _run_worker_continuous

        queue = MagicMock()
        worker = MagicMock()
        fake_rq = types.SimpleNamespace(
            Queue=MagicMock(return_value=queue),
            Worker=MagicMock(return_value=worker),
        )

        with (
            patch.dict("sys.modules", {"rq": fake_rq}),
            patch(
                "crsbench.distributed.worker._detach_stdin_to_devnull"
            ) as mock_detach,
            patch(
                "crsbench.distributed.queue.create_redis_connection",
                return_value=MagicMock(),
            ),
        ):
            _run_worker_continuous("localhost", "exp", "worker-0", "crsbench_trial")

        mock_detach.assert_called_once_with()

    @patch("crsbench.distributed.worker.REDIS_AVAILABLE", new=True)
    def test_main_cpuset_supervisor_detaches_stdin_before_launch(self):
        """Cpuset worker mode should detach stdin before starting supervisor."""
        from crsbench.distributed.worker import main

        with (
            patch(
                "crsbench.distributed.worker._detach_stdin_to_devnull"
            ) as mock_detach,
            patch(
                "crsbench.distributed.ci_supervisor.run_ci_supervisor",
                return_value=0,
            ) as mock_supervisor,
        ):
            result = main(
                redis_host="localhost",
                experiment_name="exp",
                worker_name="worker-0",
                num_workers=2,
                use_cpuset=True,
            )

        assert result == 0
        mock_detach.assert_called_once_with()
        mock_supervisor.assert_called_once()

    @patch("crsbench.distributed.worker.REDIS_AVAILABLE", new=True)
    def test_run_worker_continuous_cpuset_detaches_stdin_before_launch(self):
        """Continuous cpuset mode should detach stdin before starting supervisor."""
        from crsbench.distributed.worker import run_worker_continuous

        with (
            patch(
                "crsbench.distributed.worker._detach_stdin_to_devnull"
            ) as mock_detach,
            patch(
                "crsbench.distributed.ci_supervisor.run_ci_supervisor",
                return_value=0,
            ) as mock_supervisor,
        ):
            run_worker_continuous(
                redis_host="localhost",
                experiment_name="exp",
                worker_name="worker-0",
                num_workers=2,
                use_cpuset=True,
            )

        mock_detach.assert_called_once_with()
        mock_supervisor.assert_called_once()

    @patch("crsbench.distributed.worker.REDIS_AVAILABLE", new=True)
    def test_spawn_workers_standard_multiworker_detaches_stdin_before_spawn(self):
        """Standard multi-worker mode should detach stdin before spawning children."""
        from crsbench.distributed.worker import _spawn_workers

        with (
            patch("crsbench.distributed.worker.os.open", return_value=42) as mock_open,
            patch("crsbench.distributed.worker.os.dup2") as mock_dup2,
            patch("crsbench.distributed.worker.os.close") as mock_close,
            patch("crsbench.distributed.worker._mp_ctx.Process") as mock_process,
        ):
            mock_proc = MagicMock()
            mock_proc.exitcode = 0
            mock_process.return_value = mock_proc
            result = _spawn_workers(
                redis_host="localhost",
                experiment_name="exp",
                worker_name="worker-0",
                num_workers=2,
                queue_name="crsbench_trial",
                continuous=False,
            )

        assert result == 0
        mock_open.assert_called_once_with(os.devnull, os.O_RDONLY)
        mock_dup2.assert_called_once_with(42, 0)
        mock_close.assert_called_once_with(42)
        assert mock_process.call_count == 2


class TestConfiglessWorker:
    """Tests for configless worker mode (registry discovery)."""

    def test_run_worker_configless_exists(self):
        """run_worker_configless function exists and is callable."""
        from crsbench.distributed.worker import run_worker_configless

        assert callable(run_worker_configless)

    @patch("crsbench.distributed.worker.REDIS_AVAILABLE", new=True)
    def test_configless_supervisor_detaches_stdin_before_launch(self):
        """Configless supervisor mode should detach stdin before queue polling."""
        from crsbench.distributed.registry import RuntimeRegistration
        from crsbench.distributed.worker import run_worker_configless

        reg = RuntimeRegistration(
            experiment="exp-42",
            trial_queue="crsbench_exp-42",
            build_queue="crsbench_exp-42_build",
            verify_queue="crsbench_exp-42_verify",
        )

        with (
            patch(
                "crsbench.distributed.worker.discover_registered_experiments",
                return_value=(MagicMock(), {"exp-42": reg}),
            ),
            patch(
                "crsbench.distributed.worker._detach_stdin_to_devnull"
            ) as mock_detach,
            patch(
                "crsbench.distributed.ci_supervisor.run_multi_queue_supervisor",
                return_value=0,
            ) as mock_supervisor,
        ):
            result = run_worker_configless(redis_host="localhost")

        assert result == 0
        mock_detach.assert_called_once_with()
        mock_supervisor.assert_called_once()

    def test_run_worker_configless_rejects_none_redis_host(self):
        from crsbench.distributed.worker import run_worker_configless

        result = run_worker_configless(redis_host="none")
        assert result == 1

    def test_configless_discovers_from_registry(self):
        """Configless worker discovers queues from registry."""
        from crsbench.distributed.registry import RuntimeRegistration
        from crsbench.distributed.worker import run_worker_configless

        reg = RuntimeRegistration(
            experiment="exp-42",
            trial_queue="crsbench_exp-42",
            build_queue="crsbench_exp-42_build",
            verify_queue="crsbench_exp-42_verify",
        )

        mock_conn = MagicMock()

        with (
            patch(
                "crsbench.distributed.worker.discover_registered_experiments",
                return_value=(mock_conn, {"exp-42": reg}),
            ),
            patch(
                "crsbench.distributed.ci_supervisor.run_multi_queue_supervisor",
                return_value=0,
            ) as mock_supervisor,
        ):
            result = run_worker_configless(
                redis_host="localhost",
                use_cpuset=False,
                continuous=False,
            )

        assert result == 0
        mock_supervisor.assert_called_once()

    def test_configless_polls_when_no_experiments(self):
        """Configless worker polls when registry is initially empty."""
        from crsbench.distributed.worker import run_worker_configless

        with (
            patch(
                "crsbench.distributed.worker.discover_registered_experiments",
                side_effect=RuntimeError("No experiments found after 12 attempts"),
            ),
            patch("rq.Queue") as mock_rq_queue,
            patch("rq.Worker") as mock_rq_worker,
        ):
            mock_rq_queue.return_value = MagicMock()
            mock_rq_worker.return_value = MagicMock()

            result = run_worker_configless(
                redis_host="localhost",
                use_cpuset=False,
                continuous=False,
            )

        assert result == 1

    def test_worker_cli_config_mode(self):
        """Worker CLI uses main() when --experiment-config is given."""
        import argparse

        from crsbench.distributed.cli.worker_command import run_worker

        args = argparse.Namespace(
            experiment_config="test.yaml",
            verbose=False,
            continuous=False,
            worker_name=None,
            no_cpuset=True,
            cores=None,
            skip_cpus=None,
            cpu_tag=None,
            jobs=None,
            cores_per_job=None,
        )

        with (
            patch(
                "crsbench.distributed.worker.main",
                return_value=0,
            ) as mock_main,
            patch(
                "crsbench.run_experiment.load_experiment_config",
            ) as mock_load,
        ):
            mock_config = MagicMock()
            mock_config.worker = None
            mock_config.benchmarks = None
            mock_config.experiment = "default"
            mock_config.redis_host = "localhost"
            mock_config.resources = MagicMock()
            mock_config.resources.cpu_tag = "x86-avx2"
            mock_load.return_value = mock_config

            with patch.dict(
                "os.environ", {"CRSBENCH_QUEUE_MODEL": "flat"}, clear=False
            ):
                result = run_worker(args)

        assert result == 0
        mock_main.assert_called_once()
        assert mock_main.call_args.kwargs["queue_name"] == "crsbench_trial"
        assert mock_main.call_args.kwargs["cpu_tag"] == "x86-avx2"

    def test_worker_cli_config_mode_whitespace_worker_cpu_tag_falls_back_to_resources(
        self,
    ):
        """Whitespace worker.cpu_tag should fall back to resources.cpu_tag."""
        import argparse

        from crsbench.distributed.cli.worker_command import run_worker

        args = argparse.Namespace(
            experiment_config="test.yaml",
            verbose=False,
            continuous=False,
            worker_name=None,
            no_cpuset=True,
            cores=None,
            skip_cpus=None,
            cpu_tag=None,
            jobs=None,
            cores_per_job=None,
        )

        with (
            patch(
                "crsbench.distributed.worker.main",
                return_value=0,
            ) as mock_main,
            patch(
                "crsbench.run_experiment.load_experiment_config",
            ) as mock_load,
        ):
            mock_worker = MagicMock()
            mock_worker.cpu_tag = "   "
            mock_worker.worker_name = None
            mock_worker.jobs = 1
            mock_worker.cores_per_job = None
            mock_worker.continuous = False
            mock_worker.minimum_disk_size = "10GB"
            mock_worker.disk_check_interval = 60
            mock_worker.cpuset = None
            mock_worker.skip_cpuset = None
            mock_worker.redis_host = None

            mock_config = MagicMock()
            mock_config.worker = mock_worker
            mock_config.benchmarks = None
            mock_config.experiment = "default"
            mock_config.redis_host = "localhost"
            mock_config.resources = MagicMock()
            mock_config.resources.cpu_tag = "x86-avx2"
            mock_load.return_value = mock_config

            result = run_worker(args)

        assert result == 0
        assert mock_main.call_args.kwargs["cpu_tag"] == "x86-avx2"

    def test_worker_cli_config_mode_leaves_cores_per_job_unset_when_not_configured(
        self,
    ):
        """Config-mode worker should not inject a CPU-per-job default."""
        import argparse

        from crsbench.distributed.cli.worker_command import run_worker

        args = argparse.Namespace(
            experiment_config="test.yaml",
            verbose=False,
            continuous=False,
            worker_name=None,
            no_cpuset=True,
            cores=None,
            skip_cpus=None,
            cpu_tag=None,
            jobs=None,
            cores_per_job=None,
        )

        with (
            patch(
                "crsbench.distributed.worker.main",
                return_value=0,
            ) as mock_main,
            patch(
                "crsbench.run_experiment.load_experiment_config",
            ) as mock_load,
        ):
            mock_worker = MagicMock()
            mock_worker.cpu_tag = None
            mock_worker.worker_name = None
            mock_worker.jobs = 1
            mock_worker.cores_per_job = None
            mock_worker.continuous = False
            mock_worker.minimum_disk_size = "10GB"
            mock_worker.disk_check_interval = 60
            mock_worker.cpuset = None
            mock_worker.skip_cpuset = None
            mock_worker.redis_host = None

            mock_resources = MagicMock()
            mock_resources.cores_per_trial = None
            mock_resources.cpu_tag = None

            mock_config = MagicMock()
            mock_config.worker = mock_worker
            mock_config.benchmarks = None
            mock_config.experiment = "default"
            mock_config.redis_host = "localhost"
            mock_config.resources = mock_resources
            mock_load.return_value = mock_config

            result = run_worker(args)

        assert result == 0
        assert mock_main.call_args.kwargs["cores_per_job"] is None

    def test_worker_cli_config_mode_does_not_inherit_trial_cores_for_worker_width(
        self,
    ):
        """Config-mode worker width stays unset even if trial resource fallback exists."""
        import argparse

        from crsbench.distributed.cli.worker_command import run_worker

        args = argparse.Namespace(
            experiment_config="test.yaml",
            verbose=False,
            continuous=False,
            worker_name=None,
            no_cpuset=True,
            cores=None,
            skip_cpus=None,
            cpu_tag=None,
            jobs=None,
            cores_per_job=None,
        )

        with (
            patch(
                "crsbench.distributed.worker.main",
                return_value=0,
            ) as mock_main,
            patch(
                "crsbench.run_experiment.load_experiment_config",
            ) as mock_load,
        ):
            mock_worker = MagicMock()
            mock_worker.cpu_tag = None
            mock_worker.worker_name = None
            mock_worker.jobs = 1
            mock_worker.cores_per_job = None
            mock_worker.continuous = False
            mock_worker.minimum_disk_size = "10GB"
            mock_worker.disk_check_interval = 60
            mock_worker.cpuset = None
            mock_worker.skip_cpuset = None
            mock_worker.redis_host = None

            mock_resources = MagicMock()
            mock_resources.cores_per_trial = 8
            mock_resources.cpu_tag = None

            mock_config = MagicMock()
            mock_config.worker = mock_worker
            mock_config.benchmarks = None
            mock_config.experiment = "default"
            mock_config.redis_host = "localhost"
            mock_config.resources = mock_resources
            mock_load.return_value = mock_config

            result = run_worker(args)

        assert result == 0
        assert mock_main.call_args.kwargs["cores_per_job"] is None

    def test_worker_cli_config_mode_rejects_none_redis_host(self):
        """Config-mode worker should fail fast when redis host normalizes to None."""
        from crsbench.distributed.cli.worker_command import run_worker

        args = argparse.Namespace(
            experiment_config="test.yaml",
            verbose=False,
            continuous=False,
            worker_name=None,
            no_cpuset=True,
            cores=None,
            skip_cpus=None,
            cpu_tag=None,
            jobs=None,
            cores_per_job=None,
        )

        with (
            patch("crsbench.distributed.worker.main", return_value=0) as mock_main,
            patch("crsbench.run_experiment.load_experiment_config") as mock_load,
            patch.dict("os.environ", {"CRSBENCH_REDIS_HOST": "none"}, clear=False),
        ):
            mock_worker = MagicMock()
            mock_worker.cpu_tag = None
            mock_worker.worker_name = None
            mock_worker.jobs = 1
            mock_worker.cores_per_job = None
            mock_worker.continuous = False
            mock_worker.minimum_disk_size = "10GB"
            mock_worker.disk_check_interval = 60
            mock_worker.cpuset = None
            mock_worker.skip_cpuset = None
            mock_worker.redis_host = " none "

            mock_config = MagicMock()
            mock_config.worker = mock_worker
            mock_config.benchmarks = None
            mock_config.experiment = "default"
            mock_config.redis_host = None
            mock_config.resources = None
            mock_load.return_value = mock_config

            result = run_worker(args)

        assert result == 1
        mock_main.assert_not_called()

    def test_worker_cli_configless_rejects_empty_env_redis_host(self):
        """Configless worker should fail fast when env redis host is unset/none."""
        from crsbench.distributed.cli.worker_command import run_worker

        args = argparse.Namespace(
            experiment_config=None,
            experiment_name=None,
            verbose=False,
            continuous=False,
            worker_name=None,
            no_cpuset=True,
            cores=None,
            skip_cpus=None,
            cpu_tag=None,
            jobs=None,
            cores_per_job=None,
        )

        with (
            patch(
                "crsbench.distributed.worker.run_worker_configless", return_value=0
            ) as mock_configless,
            patch.dict("os.environ", {"CRSBENCH_REDIS_HOST": "   "}, clear=False),
        ):
            result = run_worker(args)

        assert result == 1
        mock_configless.assert_not_called()

    def test_worker_cli_experiment_name_mode_pins_worker_to_one_queue(self):
        """Cloud workers should target one experiment instead of configless discovery."""
        from crsbench.distributed.cli.worker_command import run_worker

        args = argparse.Namespace(
            experiment_config=None,
            experiment_name="exp-cloud-42",
            verbose=False,
            continuous=None,
            worker_name="gce-worker-001",
            no_cpuset=True,
            cores=None,
            skip_cpus=None,
            cpu_tag="c3",
            jobs=3,
            cores_per_job=6,
        )

        with (
            patch(
                "crsbench.distributed.worker.run_worker_continuous",
                return_value=None,
            ) as mock_continuous,
            patch(
                "crsbench.distributed.worker.run_worker_configless",
                return_value=0,
            ) as mock_configless,
            patch.dict(
                "os.environ",
                {"CRSBENCH_REDIS_HOST": "redis.internal:6380"},
                clear=False,
            ),
        ):
            result = run_worker(args)

        assert result == 0
        mock_configless.assert_not_called()
        mock_continuous.assert_called_once()
        assert mock_continuous.call_args.kwargs["experiment_name"] == "exp-cloud-42"

    def test_worker_parser_accepts_experiment_name_mode(self):
        """Worker CLI should expose an experiment-name mode for cloud bootstrap."""
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="command")
        from crsbench.distributed.cli.worker_command import add_worker_subparser

        add_worker_subparser(subparsers)
        args = parser.parse_args(["worker", "--experiment-name", "exp-cloud-42"])

        assert args.experiment_name == "exp-cloud-42"

    @patch("crsbench.distributed.worker.REDIS_AVAILABLE", new=True)
    def test_configless_worker_cpu_tag_whitespace_falls_back_to_resources_cpu_tag(
        self,
    ):
        """Whitespace worker_cpu_tag should defer to registration cpu_tag."""
        from crsbench.distributed.registry import RuntimeRegistration
        from crsbench.distributed.worker import run_worker_configless

        reg = RuntimeRegistration(
            experiment="exp-a",
            trial_queue="crsbench_exp-a",
            build_queue="crsbench_exp-a_build",
            verify_queue="crsbench_exp-a_verify",
            worker_cpu_tag="   ",
            cpu_tag="x86-avx2",
        )

        with (
            patch(
                "crsbench.distributed.worker.discover_registered_experiments",
                return_value=(MagicMock(), {"exp-a": reg}),
            ),
            patch(
                "crsbench.distributed.ci_supervisor.run_multi_queue_supervisor",
                return_value=0,
            ) as mock_supervisor,
        ):
            result = run_worker_configless(redis_host="localhost")

        assert result == 0
        assert mock_supervisor.call_args.kwargs["cpu_tag"] == "x86-avx2"

    def test_configless_cpuset_uses_cli_then_metadata_profile(self):
        """Configless cpuset worker resolves jobs/cores_per_job from CLI>metadata."""
        from crsbench.distributed.registry import RuntimeRegistration
        from crsbench.distributed.worker import run_worker_configless

        reg = RuntimeRegistration(
            experiment="exp-42",
            trial_queue="crsbench_exp-42",
            build_queue="crsbench_exp-42_build",
            verify_queue="crsbench_exp-42_verify",
            worker_jobs=6,
            worker_cores_per_job=8,
        )

        with (
            patch(
                "crsbench.distributed.worker.discover_registered_experiments",
                return_value=(MagicMock(), {"exp-42": reg}),
            ),
            patch(
                "crsbench.distributed.ci_supervisor.run_multi_queue_supervisor",
                return_value=0,
            ) as mock_supervisor,
        ):
            result = run_worker_configless(
                redis_host="localhost",
                use_cpuset=True,
                continuous=True,
                jobs_override=None,
                cores_per_job_override=None,
            )

        assert result == 0
        kwargs = mock_supervisor.call_args.kwargs
        assert kwargs["build_jobs"] == 6
        assert kwargs["build_cores_per_job"] == 8

        with (
            patch(
                "crsbench.distributed.worker.discover_registered_experiments",
                return_value=(MagicMock(), {"exp-42": reg}),
            ),
            patch(
                "crsbench.distributed.ci_supervisor.run_multi_queue_supervisor",
                return_value=0,
            ) as mock_supervisor,
        ):
            result = run_worker_configless(
                redis_host="localhost",
                use_cpuset=True,
                continuous=True,
                jobs_override=2,
                cores_per_job_override=4,
            )

        assert result == 0
        kwargs = mock_supervisor.call_args.kwargs
        assert kwargs["build_jobs"] == 2
        assert kwargs["build_cores_per_job"] == 4

    def test_configless_cpuset_leaves_cores_per_job_unset_without_metadata(self):
        """Configless cpuset worker should not inject a default cores_per_job."""
        from crsbench.distributed.registry import RuntimeRegistration
        from crsbench.distributed.worker import run_worker_configless

        reg = RuntimeRegistration(
            experiment="exp-42",
            trial_queue="crsbench_exp-42",
            build_queue="crsbench_exp-42_build",
            verify_queue="crsbench_exp-42_verify",
        )

        with (
            patch(
                "crsbench.distributed.worker.discover_registered_experiments",
                return_value=(MagicMock(), {"exp-42": reg}),
            ),
            patch(
                "crsbench.distributed.ci_supervisor.run_multi_queue_supervisor",
                return_value=0,
            ) as mock_supervisor,
        ):
            result = run_worker_configless(
                redis_host="localhost",
                use_cpuset=True,
                continuous=True,
            )

        assert result == 0
        kwargs = mock_supervisor.call_args.kwargs
        assert kwargs["build_jobs"] == 1
        assert kwargs["build_cores_per_job"] is None

    def test_configless_cpuset_uses_cli_cpu_pinning_only(self):
        """Configless worker uses CLI cpuset/skip-cpuset (no metadata pinning)."""
        from crsbench.distributed.registry import RuntimeRegistration
        from crsbench.distributed.worker import run_worker_configless

        reg1 = RuntimeRegistration(
            experiment="exp-a",
            trial_queue="crsbench_exp-a",
            build_queue="crsbench_exp-a_build",
            verify_queue="crsbench_exp-a_verify",
        )
        reg2 = RuntimeRegistration(
            experiment="exp-b",
            trial_queue="crsbench_exp-b",
            build_queue="crsbench_exp-b_build",
            verify_queue="crsbench_exp-b_verify",
        )

        with (
            patch(
                "crsbench.distributed.worker.discover_registered_experiments",
                return_value=(MagicMock(), {"exp-b": reg2, "exp-a": reg1}),
            ),
            patch(
                "crsbench.distributed.ci_supervisor.run_multi_queue_supervisor",
                return_value=0,
            ) as mock_supervisor,
        ):
            result = run_worker_configless(
                redis_host="localhost",
                use_cpuset=True,
                continuous=True,
                cores="8-15",
                skip_cpus="9",
            )

        assert result == 0
        kwargs = mock_supervisor.call_args.kwargs
        assert kwargs["cores"] == "8-15"
        assert kwargs["skip_cpus"] == "9"

    @patch("crsbench.distributed.worker.REDIS_AVAILABLE", new=True)
    def test_configless_rejects_conflicting_cpu_tag_metadata(self):
        """Configless worker fails when cpu_tag metadata conflicts across experiments."""
        from crsbench.distributed.registry import RuntimeRegistration
        from crsbench.distributed.worker import run_worker_configless

        reg1 = RuntimeRegistration(
            experiment="exp-a",
            trial_queue="crsbench_exp-a",
            build_queue="crsbench_exp-a_build",
            verify_queue="crsbench_exp-a_verify",
            worker_cpu_tag="cpu-a",
        )
        reg2 = RuntimeRegistration(
            experiment="exp-b",
            trial_queue="crsbench_exp-b",
            build_queue="crsbench_exp-b_build",
            verify_queue="crsbench_exp-b_verify",
            worker_cpu_tag="cpu-b",
        )

        with patch(
            "crsbench.distributed.worker.discover_registered_experiments",
            return_value=(MagicMock(), {"exp-a": reg1, "exp-b": reg2}),
        ):
            result = run_worker_configless(redis_host="localhost")

        assert result == 1

    @patch("crsbench.distributed.worker.REDIS_AVAILABLE", new=True)
    def test_configless_normalizes_cpu_tag_metadata_before_conflict_check(self):
        """Whitespace-only cpu_tag differences are treated as equivalent."""
        from crsbench.distributed.registry import RuntimeRegistration
        from crsbench.distributed.worker import run_worker_configless

        reg1 = RuntimeRegistration(
            experiment="exp-a",
            trial_queue="crsbench_exp-a",
            build_queue="crsbench_exp-a_build",
            verify_queue="crsbench_exp-a_verify",
            worker_cpu_tag="x86-avx2",
        )
        reg2 = RuntimeRegistration(
            experiment="exp-b",
            trial_queue="crsbench_exp-b",
            build_queue="crsbench_exp-b_build",
            verify_queue="crsbench_exp-b_verify",
            worker_cpu_tag="  x86-avx2  ",
        )

        with (
            patch(
                "crsbench.distributed.worker.discover_registered_experiments",
                return_value=(MagicMock(), {"exp-a": reg1, "exp-b": reg2}),
            ),
            patch(
                "crsbench.distributed.ci_supervisor.run_multi_queue_supervisor",
                return_value=0,
            ) as mock_supervisor,
        ):
            result = run_worker_configless(redis_host="localhost")

        assert result == 0
        assert mock_supervisor.call_args.kwargs["cpu_tag"] == "x86-avx2"

    @patch("crsbench.distributed.worker.REDIS_AVAILABLE", new=True)
    def test_configless_rejects_invalid_worker_jobs_metadata(self):
        """Configless worker fails fast on invalid worker.jobs metadata."""
        from crsbench.distributed.registry import RuntimeRegistration
        from crsbench.distributed.worker import run_worker_configless

        reg = RuntimeRegistration(
            experiment="exp-42",
            trial_queue="crsbench_exp-42",
            build_queue="crsbench_exp-42_build",
            verify_queue="crsbench_exp-42_verify",
            worker_jobs=0,
        )

        with patch(
            "crsbench.distributed.worker.discover_registered_experiments",
            return_value=(MagicMock(), {"exp-42": reg}),
        ):
            result = run_worker_configless(redis_host="localhost")

        assert result == 1

    @patch("crsbench.distributed.worker.REDIS_AVAILABLE", new=True)
    def test_configless_rejects_invalid_worker_cores_per_job_metadata(self):
        """Configless worker fails fast on invalid worker.cores_per_job metadata."""
        from crsbench.distributed.registry import RuntimeRegistration
        from crsbench.distributed.worker import run_worker_configless

        reg = RuntimeRegistration(
            experiment="exp-42",
            trial_queue="crsbench_exp-42",
            build_queue="crsbench_exp-42_build",
            verify_queue="crsbench_exp-42_verify",
            worker_cores_per_job=0,
        )

        with patch(
            "crsbench.distributed.worker.discover_registered_experiments",
            return_value=(MagicMock(), {"exp-42": reg}),
        ):
            result = run_worker_configless(redis_host="localhost")

        assert result == 1

    @patch("crsbench.distributed.worker.REDIS_AVAILABLE", new=True)
    def test_configless_rejects_invalid_jobs_override(self):
        """Configless worker rejects non-positive CLI override values."""
        from crsbench.distributed.registry import RuntimeRegistration
        from crsbench.distributed.worker import run_worker_configless

        reg = RuntimeRegistration(
            experiment="exp-42",
            trial_queue="crsbench_exp-42",
            build_queue="crsbench_exp-42_build",
            verify_queue="crsbench_exp-42_verify",
        )

        with patch(
            "crsbench.distributed.worker.discover_registered_experiments",
            return_value=(MagicMock(), {"exp-42": reg}),
        ):
            result = run_worker_configless(redis_host="localhost", jobs_override=0)

        assert result == 1

    @patch("crsbench.distributed.worker.REDIS_AVAILABLE", new=True)
    def test_configless_refresh_skips_incompatible_resource_profile(self):
        """Refresh should not adopt experiments requiring incompatible worker profile."""
        from crsbench.distributed.registry import RuntimeRegistration
        from crsbench.distributed.worker import run_worker_configless

        initial_reg = RuntimeRegistration(
            experiment="exp-a",
            trial_queue="crsbench_exp-a",
            build_queue="crsbench_exp-a_build",
            verify_queue="crsbench_exp-a_verify",
            worker_jobs=1,
            worker_cores_per_job=4,
            worker_cpu_tag="x86-avx2",
        )
        compatible_reg = RuntimeRegistration(
            experiment="exp-b",
            trial_queue="crsbench_exp-b",
            build_queue="crsbench_exp-b_build",
            verify_queue="crsbench_exp-b_verify",
            worker_jobs=1,
            worker_cores_per_job=4,
            worker_cpu_tag="x86-avx2",
        )
        incompatible_reg = RuntimeRegistration(
            experiment="exp-c",
            trial_queue="crsbench_exp-c",
            build_queue="crsbench_exp-c_build",
            verify_queue="crsbench_exp-c_verify",
            worker_jobs=2,
            worker_cores_per_job=4,
            worker_cpu_tag="x86-avx2",
        )

        def _run_supervisor(**kwargs):
            refresher = kwargs["queue_refresher"]
            with patch("crsbench.distributed.registry.RegistryClient") as mock_registry:
                mock_client = MagicMock()
                mock_client.list_experiments.return_value = {
                    "exp-a": initial_reg,
                    "exp-b": compatible_reg,
                    "exp-c": incompatible_reg,
                }
                mock_registry.return_value = mock_client
                refreshed_build, refreshed_verify = refresher(MagicMock())
            assert refreshed_build == ["crsbench_exp-a", "crsbench_exp-b"]
            assert refreshed_verify == []
            return 0

        with (
            patch(
                "crsbench.distributed.worker.discover_registered_experiments",
                return_value=(MagicMock(), {"exp-a": initial_reg}),
            ),
            patch(
                "crsbench.distributed.ci_supervisor.run_multi_queue_supervisor",
                side_effect=_run_supervisor,
            ),
            patch("crsbench.distributed.worker.logger.warning") as mock_warning,
        ):
            result = run_worker_configless(redis_host="localhost")

        assert result == 0
        assert any(
            "incompatible worker resource profile" in str(call.args[0])
            for call in mock_warning.call_args_list
        )

    @patch("crsbench.distributed.worker.REDIS_AVAILABLE", new=True)
    def test_configless_refresh_keeps_untagged_experiment_with_tagged_worker(self):
        """Untagged experiments should remain compatible during refresh."""
        from crsbench.distributed.registry import RuntimeRegistration
        from crsbench.distributed.worker import run_worker_configless

        initial_reg = RuntimeRegistration(
            experiment="exp-a",
            trial_queue="crsbench_exp-a",
            build_queue="crsbench_exp-a_build",
            verify_queue="crsbench_exp-a_verify",
            worker_jobs=1,
            worker_cores_per_job=4,
            worker_cpu_tag="x86-avx2",
        )
        untagged_reg = RuntimeRegistration(
            experiment="exp-b",
            trial_queue="crsbench_exp-b",
            build_queue="crsbench_exp-b_build",
            verify_queue="crsbench_exp-b_verify",
            worker_jobs=1,
            worker_cores_per_job=4,
            worker_cpu_tag=None,
        )

        def _run_supervisor(**kwargs):
            refresher = kwargs["queue_refresher"]
            with patch("crsbench.distributed.registry.RegistryClient") as mock_registry:
                mock_client = MagicMock()
                mock_client.list_experiments.return_value = {
                    "exp-a": initial_reg,
                    "exp-b": untagged_reg,
                }
                mock_registry.return_value = mock_client
                refreshed_build, refreshed_verify = refresher(MagicMock())
            assert refreshed_build == ["crsbench_exp-a", "crsbench_exp-b"]
            assert refreshed_verify == []
            return 0

        with (
            patch(
                "crsbench.distributed.worker.discover_registered_experiments",
                return_value=(MagicMock(), {"exp-a": initial_reg}),
            ),
            patch(
                "crsbench.distributed.ci_supervisor.run_multi_queue_supervisor",
                side_effect=_run_supervisor,
            ),
        ):
            result = run_worker_configless(redis_host="localhost")

        assert result == 0


class TestMetadataValidationHelpers:
    """Unit tests for configless metadata validation helpers."""

    def test_collect_validated_int_metadata_rejects_boolean(self):
        """Boolean metadata values should not pass integer validation."""
        from types import SimpleNamespace

        from crsbench.distributed.common import collect_validated_int_metadata

        reg = SimpleNamespace(experiment="exp-42", worker_jobs=True)
        with pytest.raises(RuntimeError):
            collect_validated_int_metadata(
                registrations=[reg],
                attr_name="worker_jobs",
                field_name="worker.jobs",
                minimum=1,
            )


class TestWorkerCliValidation:
    """Tests for worker CLI argument validation."""

    def test_jobs_rejects_zero(self):
        """--jobs must be >= 1."""
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="command")
        from crsbench.distributed.cli.worker_command import add_worker_subparser

        add_worker_subparser(subparsers)
        with pytest.raises(SystemExit):
            parser.parse_args(["worker", "--jobs", "0"])
