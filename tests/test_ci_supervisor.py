"""Tests for the CI dual-queue supervisor.

Tests that:
1. run_ci_supervisor creates both build and verify queues
2. Build queue has priority over verify queue
3. Per-queue concurrency limits are enforced
4. Worker and evaluator adapters exist and are callable
"""

from unittest.mock import MagicMock, patch


class TestCiSupervisorQueues:
    """Test run_ci_supervisor dual-queue setup."""

    @patch("crsbench.distributed.ci_supervisor.redis")
    @patch("crsbench.distributed.ci_supervisor.rq")
    def test_creates_both_queues(
        self, mock_rq: MagicMock, mock_redis: MagicMock
    ) -> None:
        """Supervisor creates both build and verify queues."""
        from crsbench.distributed.ci_supervisor import run_ci_supervisor

        mock_conn = MagicMock()
        mock_redis.Redis.return_value = mock_conn

        mock_build_queue = MagicMock()
        mock_verify_queue = MagicMock()
        mock_build_queue.count = 0
        mock_verify_queue.count = 0
        mock_build_queue.name = "crsbench_ci_build"
        mock_verify_queue.name = "crsbench_ci_verify"

        queues_created = []

        def track_queue_creation(name, **_kwargs):
            queues_created.append(name)
            if "build" in name:
                return mock_build_queue
            return mock_verify_queue

        mock_rq.Queue.side_effect = track_queue_creation

        def break_loop(_seconds):
            raise KeyboardInterrupt

        with patch(
            "crsbench.distributed.ci_supervisor.time.sleep",
            side_effect=break_loop,
        ):
            result = run_ci_supervisor(
                redis_host="localhost",
                build_queue_name="crsbench_ci_build",
                verify_queue_name="crsbench_ci_verify",
                worker_name="test-worker",
                build_jobs=2,
                build_cores_per_job=4,
                verify_jobs=8,
                job_runner=lambda _h, _n, _j: None,
            )

        assert "crsbench_ci_build" in queues_created
        assert "crsbench_ci_verify" in queues_created
        assert result == 0

    @patch("crsbench.distributed.ci_supervisor.redis")
    @patch("crsbench.distributed.ci_supervisor.rq")
    def test_build_queue_priority(
        self, mock_rq: MagicMock, mock_redis: MagicMock
    ) -> None:
        """Build queue is listed first for dequeue priority."""
        from crsbench.distributed.ci_supervisor import run_ci_supervisor

        mock_conn = MagicMock()
        mock_redis.Redis.return_value = mock_conn

        mock_build_queue = MagicMock()
        mock_verify_queue = MagicMock()
        mock_build_queue.count = 1
        mock_verify_queue.count = 1
        mock_build_queue.name = "crsbench_ci_build"
        mock_verify_queue.name = "crsbench_ci_verify"

        def queue_factory(name, **_kwargs):
            if "build" in name:
                return mock_build_queue
            return mock_verify_queue

        mock_rq.Queue.side_effect = queue_factory

        dequeue_calls = []

        def track_dequeue_any(queues, **_kwargs):
            dequeue_calls.append([q.name for q in queues])
            raise KeyboardInterrupt

        mock_rq.Queue.dequeue_any = track_dequeue_any

        run_ci_supervisor(
            redis_host="localhost",
            build_queue_name="crsbench_ci_build",
            verify_queue_name="crsbench_ci_verify",
            worker_name="test-worker",
            build_jobs=2,
            build_cores_per_job=4,
            verify_jobs=8,
            job_runner=lambda _h, _n, _j: None,
        )

        assert len(dequeue_calls) > 0
        assert dequeue_calls[0][0] == "crsbench_ci_build"
        assert dequeue_calls[0][1] == "crsbench_ci_verify"

    @patch("crsbench.distributed.ci_supervisor.redis")
    @patch("crsbench.distributed.ci_supervisor.rq")
    def test_respects_per_queue_capacity(
        self, mock_rq: MagicMock, mock_redis: MagicMock
    ) -> None:
        """Only queues with remaining capacity are included in dequeue."""
        from crsbench.distributed.ci_supervisor import run_ci_supervisor

        mock_conn = MagicMock()
        mock_redis.Redis.return_value = mock_conn

        mock_build_queue = MagicMock()
        mock_verify_queue = MagicMock()
        mock_build_queue.count = 1
        mock_verify_queue.count = 1
        mock_build_queue.name = "crsbench_ci_build"
        mock_verify_queue.name = "crsbench_ci_verify"

        def queue_factory(name, **_kwargs):
            if "build" in name:
                return mock_build_queue
            return mock_verify_queue

        mock_rq.Queue.side_effect = queue_factory

        # First dequeue returns a build job, second should skip build
        # (build_jobs=1, so after one build, only verify has capacity)
        dequeue_iteration = [0]
        dequeue_calls = []

        mock_job = MagicMock()
        mock_job.id = "test-job-12345678"
        mock_job.meta = {}
        mock_process = MagicMock()
        mock_process.pid = 12345
        mock_process.is_alive.return_value = True

        def track_dequeue_any(queues, **_kwargs):
            dequeue_calls.append([q.name for q in queues])
            dequeue_iteration[0] += 1
            if dequeue_iteration[0] >= 2:
                raise KeyboardInterrupt
            return (mock_job, mock_build_queue)

        mock_rq.Queue.dequeue_any = track_dequeue_any

        with patch(
            "crsbench.distributed.ci_supervisor.multiprocessing.Process"
        ) as mock_proc_cls:
            mock_proc_cls.return_value = mock_process
            run_ci_supervisor(
                redis_host="localhost",
                build_queue_name="crsbench_ci_build",
                verify_queue_name="crsbench_ci_verify",
                worker_name="test-worker",
                build_jobs=1,
                build_cores_per_job=1,
                verify_jobs=8,
                job_runner=lambda _h, _n, _j: None,
            )

        # First dequeue: both queues have capacity
        assert len(dequeue_calls) >= 2
        assert "crsbench_ci_build" in dequeue_calls[0]
        # Second dequeue: build at capacity (1/1), only verify should remain
        assert "crsbench_ci_build" not in dequeue_calls[1]
        assert "crsbench_ci_verify" in dequeue_calls[1]


class TestWorkerCiAdapter:
    """Test that the worker CI adapter exists and works."""

    def test_ci_job_runner_exists(self) -> None:
        """_ci_job_runner adapter exists in worker module."""
        from crsbench.distributed.worker import _ci_job_runner

        assert callable(_ci_job_runner)

    def test_ci_job_runner_signature(self) -> None:
        """_ci_job_runner has the expected 3-arg signature."""
        import inspect

        from crsbench.distributed.worker import _ci_job_runner

        sig = inspect.signature(_ci_job_runner)
        params = list(sig.parameters.keys())
        assert params == ["redis_host", "child_name", "job_id"]


class TestEvaluatorCiAdapter:
    """Test that the evaluator CI adapter exists and works."""

    def test_evaluator_job_runner_exists(self) -> None:
        """_evaluator_job_runner adapter exists in evaluator module."""
        from crsbench.distributed.evaluator import _evaluator_job_runner

        assert callable(_evaluator_job_runner)

    def test_evaluator_job_runner_signature(self) -> None:
        """_evaluator_job_runner has the expected 3-arg signature."""
        import inspect

        from crsbench.distributed.evaluator import _evaluator_job_runner

        sig = inspect.signature(_evaluator_job_runner)
        params = list(sig.parameters.keys())
        assert params == ["redis_host", "_child_name", "job_id"]


class TestWorkerCommandCiFlags:
    """Test worker CLI flag validation."""

    def test_ci_queue_choice_available(self) -> None:
        """--queue accepts 'ci' as a valid choice."""
        import argparse

        from crsbench.distributed.cli.worker_command import (
            add_worker_subparser,
        )

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        add_worker_subparser(subparsers)

        # Should not raise
        args = parser.parse_args(["worker", "--queue", "ci"])
        assert args.queue == "ci"

    def test_ci_build_queue_choice_removed(self) -> None:
        """--queue no longer accepts 'ci-build'."""
        import argparse

        import pytest
        from crsbench.distributed.cli.worker_command import (
            add_worker_subparser,
        )

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        add_worker_subparser(subparsers)

        with pytest.raises(SystemExit):
            parser.parse_args(["worker", "--queue", "ci-build"])

    def test_build_jobs_flag_exists(self) -> None:
        """--build-jobs flag is available."""
        import argparse

        from crsbench.distributed.cli.worker_command import (
            add_worker_subparser,
        )

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        add_worker_subparser(subparsers)

        args = parser.parse_args(["worker", "--build-jobs", "32"])
        assert args.build_jobs == 32

    def test_build_cores_per_job_default(self) -> None:
        """--build-cores-per-job defaults to 1."""
        import argparse

        from crsbench.distributed.cli.worker_command import (
            add_worker_subparser,
        )

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        add_worker_subparser(subparsers)

        args = parser.parse_args(["worker"])
        assert args.build_cores_per_job == 1

    def test_verify_jobs_flag_exists(self) -> None:
        """--verify-jobs flag is available."""
        import argparse

        from crsbench.distributed.cli.worker_command import (
            add_worker_subparser,
        )

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        add_worker_subparser(subparsers)

        args = parser.parse_args(["worker", "--verify-jobs", "128"])
        assert args.verify_jobs == 128


class TestEvaluatorCommandCiFlags:
    """Test evaluator CLI flag changes."""

    def test_build_jobs_flag_exists(self) -> None:
        """--build-jobs flag is available on evaluator."""
        import argparse

        from crsbench.distributed.cli.evaluator_command import (
            add_evaluator_subparser,
        )

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        add_evaluator_subparser(subparsers)

        args = parser.parse_args(
            [
                "evaluator",
                "--experiment-config",
                "test.yaml",
                "--experiment-name",
                "exp1",
                "--build-jobs",
                "16",
            ]
        )
        assert args.build_jobs == 16

    def test_build_cores_per_job_default(self) -> None:
        """--build-cores-per-job defaults to 1 on evaluator."""
        import argparse

        from crsbench.distributed.cli.evaluator_command import (
            add_evaluator_subparser,
        )

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        add_evaluator_subparser(subparsers)

        args = parser.parse_args(
            [
                "evaluator",
                "--experiment-config",
                "test.yaml",
                "--experiment-name",
                "exp1",
            ]
        )
        assert args.build_cores_per_job == 1

    def test_old_build_workers_removed(self) -> None:
        """--build-workers flag no longer exists."""
        import argparse

        import pytest
        from crsbench.distributed.cli.evaluator_command import (
            add_evaluator_subparser,
        )

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        add_evaluator_subparser(subparsers)

        with pytest.raises(SystemExit):
            parser.parse_args(
                [
                    "evaluator",
                    "--experiment-config",
                    "test.yaml",
                    "--experiment-name",
                    "exp1",
                    "--build-workers",
                    "4",
                ]
            )

    def test_old_verify_workers_removed(self) -> None:
        """--verify-workers flag no longer exists."""
        import argparse

        import pytest
        from crsbench.distributed.cli.evaluator_command import (
            add_evaluator_subparser,
        )

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        add_evaluator_subparser(subparsers)

        with pytest.raises(SystemExit):
            parser.parse_args(
                [
                    "evaluator",
                    "--experiment-config",
                    "test.yaml",
                    "--experiment-name",
                    "exp1",
                    "--verify-workers",
                    "8",
                ]
            )


class TestHelperFunctions:
    """Test internal helper functions."""

    def test_next_worker_num_finds_lowest(self) -> None:
        """_next_worker_num finds the lowest unused number."""
        from crsbench.distributed.ci_supervisor import _next_worker_num

        assert _next_worker_num(set(), 10) == 1
        assert _next_worker_num({1}, 10) == 2
        assert _next_worker_num({1, 2, 3}, 10) == 4
        assert _next_worker_num({1, 3}, 10) == 2

    def test_next_worker_num_overflow(self) -> None:
        """_next_worker_num returns max+1 when all numbers are used."""
        from crsbench.distributed.ci_supervisor import _next_worker_num

        assert _next_worker_num({1, 2, 3}, 3) == 4
