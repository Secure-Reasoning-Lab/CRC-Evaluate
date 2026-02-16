"""Tests for the CI dual-queue supervisor.

Tests that:
1. run_ci_supervisor creates both build and verify queues
2. Build queue has priority over verify queue
3. Per-queue concurrency limits are enforced
4. Worker and evaluator adapters exist and are callable
5. Continuous vs non-continuous exit behavior
"""

from unittest.mock import MagicMock, patch

import pytest

rq = pytest.importorskip("rq")


def _make_mock_queue(name: str, count: int = 0, deferred: int = 0):
    """Create a mock RQ queue with configurable counts."""
    q = MagicMock()
    q.name = name
    q.count = count
    q.deferred_job_registry = MagicMock()
    q.deferred_job_registry.count = deferred
    return q


def _patch_supervisor():
    """Return a context manager that patches create_redis_connection."""
    return patch(
        "crsbench.distributed.queue.create_redis_connection",
        return_value=MagicMock(),
    )


class TestCiSupervisorQueues:
    """Test run_ci_supervisor dual-queue setup."""

    def test_creates_both_queues(self) -> None:
        """Supervisor creates both build and verify queues."""
        from crsbench.distributed.ci_supervisor import run_ci_supervisor

        mock_build_queue = _make_mock_queue("crsbench_ci_build")
        mock_verify_queue = _make_mock_queue("crsbench_ci_verify")

        queues_created = []

        def track_queue_creation(name, **_kwargs):
            queues_created.append(name)
            if "build" in name:
                return mock_build_queue
            return mock_verify_queue

        with (
            _patch_supervisor(),
            patch("crsbench.distributed.ci_supervisor.rq") as mock_rq,
            patch(
                "crsbench.distributed.ci_supervisor.time.sleep",
                side_effect=KeyboardInterrupt,
            ),
        ):
            mock_rq.Queue.side_effect = track_queue_creation
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

    def test_build_queue_priority(self) -> None:
        """Build queue is listed first for dequeue priority."""
        from crsbench.distributed.ci_supervisor import run_ci_supervisor

        mock_build_queue = _make_mock_queue("crsbench_ci_build", count=1)
        mock_verify_queue = _make_mock_queue("crsbench_ci_verify", count=1)

        def queue_factory(name, **_kwargs):
            if "build" in name:
                return mock_build_queue
            return mock_verify_queue

        dequeue_calls = []

        def track_dequeue_any(queues, **_kwargs):
            dequeue_calls.append([q.name for q in queues])
            raise KeyboardInterrupt

        with (
            _patch_supervisor(),
            patch("crsbench.distributed.ci_supervisor.rq") as mock_rq,
        ):
            mock_rq.Queue.side_effect = queue_factory
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

    def test_respects_per_queue_capacity(self) -> None:
        """Only queues with remaining capacity are included in dequeue."""
        from crsbench.distributed.ci_supervisor import run_ci_supervisor

        mock_build_queue = _make_mock_queue("crsbench_ci_build", count=1)
        mock_verify_queue = _make_mock_queue("crsbench_ci_verify", count=1)

        def queue_factory(name, **_kwargs):
            if "build" in name:
                return mock_build_queue
            return mock_verify_queue

        dequeue_iteration = [0]
        dequeue_calls = []

        mock_job = MagicMock()
        mock_job.id = "test-job-12345678"
        mock_job.meta = {}
        mock_job.get_status.return_value = "queued"
        mock_process = MagicMock()
        mock_process.pid = 12345
        mock_process.is_alive.return_value = True

        def track_dequeue_any(queues, **_kwargs):
            dequeue_calls.append([q.name for q in queues])
            dequeue_iteration[0] += 1
            if dequeue_iteration[0] >= 2:
                raise KeyboardInterrupt
            return (mock_job, mock_build_queue)

        with (
            _patch_supervisor(),
            patch("crsbench.distributed.ci_supervisor.rq") as mock_rq,
            patch(
                "crsbench.distributed.ci_supervisor.multiprocessing.Process"
            ) as mock_proc_cls,
        ):
            mock_rq.Queue.side_effect = queue_factory
            mock_rq.Queue.dequeue_any = track_dequeue_any
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

        assert len(dequeue_calls) >= 2
        assert "crsbench_ci_build" in dequeue_calls[0]
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


class TestSupervisorExitCondition:
    """Test continuous vs non-continuous exit behavior."""

    def _run_supervisor(
        self,
        build_count: int = 0,
        verify_count: int = 0,
        build_deferred: int = 0,
        verify_deferred: int = 0,
        *,
        continuous: bool = True,
        max_iterations: int = 5,
    ) -> tuple[int, int]:
        """Run supervisor and return (exit_code, loop_iterations).

        Uses time.sleep side_effect to count iterations and break the loop.
        """
        from crsbench.distributed.ci_supervisor import run_ci_supervisor

        mock_build_queue = _make_mock_queue(
            "crsbench_ci_build", build_count, build_deferred
        )
        mock_verify_queue = _make_mock_queue(
            "crsbench_ci_verify", verify_count, verify_deferred
        )

        def queue_factory(name, **_kwargs):
            if "build" in name:
                return mock_build_queue
            return mock_verify_queue

        iterations = [0]

        def count_and_break(_seconds):
            iterations[0] += 1
            if iterations[0] >= max_iterations:
                raise KeyboardInterrupt

        with (
            _patch_supervisor(),
            patch("crsbench.distributed.ci_supervisor.rq") as mock_rq,
            patch(
                "crsbench.distributed.ci_supervisor.time.sleep",
                side_effect=count_and_break,
            ),
        ):
            mock_rq.Queue.side_effect = queue_factory
            result = run_ci_supervisor(
                redis_host="localhost",
                build_queue_name="crsbench_ci_build",
                verify_queue_name="crsbench_ci_verify",
                worker_name="test-worker",
                build_jobs=2,
                build_cores_per_job=1,
                verify_jobs=8,
                job_runner=lambda _h, _n, _j: None,
                continuous=continuous,
            )

        return result, iterations[0]

    def test_non_continuous_exits_when_empty(self) -> None:
        """Non-continuous mode exits immediately when all queues are empty."""
        result, iterations = self._run_supervisor(continuous=False)
        assert result == 0
        # Should exit before reaching sleep (0 iterations)
        assert iterations == 0

    def test_non_continuous_waits_for_deferred(self) -> None:
        """Non-continuous mode does NOT exit while deferred jobs exist."""
        result, iterations = self._run_supervisor(verify_deferred=3, continuous=False)
        # Should loop until KeyboardInterrupt (5 iterations)
        assert result == 0
        assert iterations == 5

    def test_non_continuous_waits_for_queued(self) -> None:
        """Non-continuous mode does NOT exit while queued jobs exist."""
        from crsbench.distributed.ci_supervisor import run_ci_supervisor

        mock_build_queue = _make_mock_queue("crsbench_ci_build")
        mock_verify_queue = _make_mock_queue("crsbench_ci_verify", count=2)

        def queue_factory(name, **_kwargs):
            if "build" in name:
                return mock_build_queue
            return mock_verify_queue

        iterations = [0]

        def count_and_break(_seconds):
            iterations[0] += 1
            if iterations[0] >= 5:
                raise KeyboardInterrupt

        def dequeue_noop(_queues, **_kwargs):
            # Simulate no job ready yet — returns None
            return None

        with (
            _patch_supervisor(),
            patch("crsbench.distributed.ci_supervisor.rq") as mock_rq,
            patch(
                "crsbench.distributed.ci_supervisor.time.sleep",
                side_effect=count_and_break,
            ),
        ):
            mock_rq.Queue.side_effect = queue_factory
            mock_rq.Queue.dequeue_any = dequeue_noop
            result = run_ci_supervisor(
                redis_host="localhost",
                build_queue_name="crsbench_ci_build",
                verify_queue_name="crsbench_ci_verify",
                worker_name="test-worker",
                build_jobs=2,
                build_cores_per_job=1,
                verify_jobs=8,
                job_runner=lambda _h, _n, _j: None,
                continuous=False,
            )

        assert result == 0
        assert iterations[0] == 5

    def test_continuous_does_not_exit_when_empty(self) -> None:
        """Continuous mode keeps running even when all queues are empty."""
        result, iterations = self._run_supervisor(continuous=True)
        # Should loop until KeyboardInterrupt (5 iterations)
        assert result == 0
        assert iterations == 5

    def test_default_is_continuous(self) -> None:
        """Default mode is continuous (does not exit when empty)."""
        result, iterations = self._run_supervisor()
        assert iterations == 5  # Ran until KeyboardInterrupt


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
