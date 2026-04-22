"""Tests for the CI dual-queue supervisor.

Tests that:
1. run_ci_supervisor creates both build and verify queues
2. Runnable jobs are selected through the fair scheduler
3. Per-queue concurrency limits are enforced
4. Worker and evaluator adapters exist and are callable
5. Continuous vs non-continuous exit behavior
"""

from types import SimpleNamespace
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

    def test_no_longer_uses_ordered_dequeue_priority(self) -> None:
        """Supervisor should not call legacy ordered `dequeue_any()` anymore."""
        from crsbench.distributed.ci_supervisor import run_ci_supervisor

        mock_build_queue = _make_mock_queue("crsbench_ci_build", count=1)
        mock_verify_queue = _make_mock_queue("crsbench_ci_verify", count=1)

        def queue_factory(name, **_kwargs):
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
            mock_rq.Queue.side_effect = queue_factory
            mock_rq.Queue.dequeue_any.side_effect = AssertionError(
                "ordered dequeue_any should not be used"
            )
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

        assert mock_rq.Queue.dequeue_any.call_count == 0

    def test_respects_per_queue_capacity(self) -> None:
        """Once a build slot fills, only verify capacity remains eligible."""
        from crsbench.distributed.ci_supervisor import run_ci_supervisor

        mock_build_queue = _make_mock_queue("crsbench_ci_build", count=1)
        mock_verify_queue = _make_mock_queue("crsbench_ci_verify", count=1)

        def queue_factory(name, **_kwargs):
            if "build" in name:
                return mock_build_queue
            return mock_verify_queue

        selector_calls = []

        mock_job = MagicMock()
        mock_job.id = "test-job-12345678"
        mock_job.meta = {}
        mock_job.get_status.return_value = "queued"
        mock_process = MagicMock()
        mock_process.pid = 12345
        mock_process.is_alive.return_value = True

        def select_fair_job(**kwargs):
            selector_calls.append(
                (kwargs["build_has_capacity"], kwargs["verify_has_capacity"])
            )
            if len(selector_calls) >= 2:
                raise KeyboardInterrupt
            return (mock_job, mock_build_queue)

        with (
            _patch_supervisor(),
            patch("crsbench.distributed.ci_supervisor.rq") as mock_rq,
            patch(
                "crsbench.distributed.ci_supervisor._select_fair_job",
                side_effect=select_fair_job,
            ),
            patch(
                "crsbench.distributed.ci_supervisor._mp_ctx.Process"
            ) as mock_proc_cls,
        ):
            mock_rq.Queue.side_effect = queue_factory
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

        assert selector_calls[0] == (True, True)
        assert selector_calls[1] == (False, True)

    def test_select_fair_job_rotates_between_trial_owners(self) -> None:
        """Fair helper should rotate build claims across distinct trial owners."""
        from crsbench.distributed.ci_supervisor import _select_fair_job
        from crsbench.distributed.evaluator_scheduler import (
            SCHEDULER_OWNER_KEY_META,
            FairSchedulerState,
        )

        build_q1 = _make_mock_queue("crsbench_ci_build_a", count=2)
        build_q1.key = "rq:queue:crsbench_ci_build_a"
        build_q1.get_job_ids.side_effect = [["job-a1", "job-a2"], ["job-a2"]]
        build_q2 = _make_mock_queue("crsbench_ci_build_b", count=1)
        build_q2.key = "rq:queue:crsbench_ci_build_b"
        build_q2.get_job_ids.side_effect = [["job-b1"], ["job-b1"]]

        jobs = {
            "job-a1": SimpleNamespace(
                id="job-a1",
                meta={SCHEDULER_OWNER_KEY_META: "trial::exp::trial-a"},
            ),
            "job-a2": SimpleNamespace(
                id="job-a2",
                meta={SCHEDULER_OWNER_KEY_META: "trial::exp::trial-a"},
            ),
            "job-b1": SimpleNamespace(
                id="job-b1",
                meta={SCHEDULER_OWNER_KEY_META: "trial::exp::trial-b"},
            ),
        }

        redis_conn = MagicMock()
        redis_conn.eval.return_value = 1
        state = FairSchedulerState(next_queue_class="build")

        with patch("crsbench.distributed.ci_supervisor.rq") as mock_rq:
            mock_rq.job.Job.fetch_many.side_effect = lambda ids, **_kwargs: [
                jobs[job_id] for job_id in ids
            ]
            mock_rq.job.Job.fetch.side_effect = lambda job_id, **_kwargs: jobs[job_id]

            first = _select_fair_job(
                redis_conn=redis_conn,
                build_queues=[build_q1, build_q2],
                verify_queues=[],
                build_has_capacity=True,
                verify_has_capacity=False,
                state=state,
            )
            second = _select_fair_job(
                redis_conn=redis_conn,
                build_queues=[build_q1, build_q2],
                verify_queues=[],
                build_has_capacity=True,
                verify_has_capacity=False,
                state=state,
            )

        assert first is not None
        assert second is not None
        assert first[0].id == "job-a1"
        assert second[0].id == "job-b1"

    def test_select_fair_job_alternates_build_and_verify_classes(self) -> None:
        """Fair helper should alternate class claims when both remain runnable."""
        from crsbench.distributed.ci_supervisor import _select_fair_job
        from crsbench.distributed.evaluator_scheduler import (
            SCHEDULER_OWNER_KEY_META,
            FairSchedulerState,
        )

        build_queue = _make_mock_queue("crsbench_ci_build", count=2)
        build_queue.key = "rq:queue:crsbench_ci_build"
        build_queue.get_job_ids.side_effect = [["build-1", "build-2"], ["build-2"]]
        verify_queue = _make_mock_queue("crsbench_ci_verify", count=2)
        verify_queue.key = "rq:queue:crsbench_ci_verify"
        verify_queue.get_job_ids.side_effect = [
            ["verify-1", "verify-2"],
            ["verify-1", "verify-2"],
        ]

        jobs = {
            "build-1": SimpleNamespace(
                id="build-1",
                meta={SCHEDULER_OWNER_KEY_META: "trial::exp::trial-a"},
            ),
            "build-2": SimpleNamespace(
                id="build-2",
                meta={SCHEDULER_OWNER_KEY_META: "trial::exp::trial-a"},
            ),
            "verify-1": SimpleNamespace(
                id="verify-1",
                meta={SCHEDULER_OWNER_KEY_META: "trial::exp::trial-b"},
            ),
            "verify-2": SimpleNamespace(
                id="verify-2",
                meta={SCHEDULER_OWNER_KEY_META: "trial::exp::trial-b"},
            ),
        }

        redis_conn = MagicMock()
        redis_conn.eval.return_value = 1
        state = FairSchedulerState(next_queue_class="build")

        with patch("crsbench.distributed.ci_supervisor.rq") as mock_rq:
            mock_rq.job.Job.fetch_many.side_effect = lambda ids, **_kwargs: [
                jobs[job_id] for job_id in ids
            ]
            mock_rq.job.Job.fetch.side_effect = lambda job_id, **_kwargs: jobs[job_id]

            first = _select_fair_job(
                redis_conn=redis_conn,
                build_queues=[build_queue],
                verify_queues=[verify_queue],
                build_has_capacity=True,
                verify_has_capacity=True,
                state=state,
            )
            second = _select_fair_job(
                redis_conn=redis_conn,
                build_queues=[build_queue],
                verify_queues=[verify_queue],
                build_has_capacity=True,
                verify_has_capacity=True,
                state=state,
            )

        assert first is not None
        assert second is not None
        assert first[1].name == "crsbench_ci_build"
        assert second[1].name == "crsbench_ci_verify"

    def test_cpu_tag_mismatch_applies_short_backoff(self) -> None:
        """Mismatch path should back off briefly to avoid hot-loop churn."""
        from crsbench.distributed.ci_supervisor import run_ci_supervisor

        mock_build_queue = _make_mock_queue("crsbench_ci_build", count=1)
        mock_verify_queue = _make_mock_queue("crsbench_ci_verify", count=0)

        def queue_factory(name, **_kwargs):
            if "build" in name:
                return mock_build_queue
            return mock_verify_queue

        mock_job = MagicMock()
        mock_job.id = "test-job-12345678"
        mock_job.meta = {"cpu_tag": "arm-neon"}
        mock_job.get_status.return_value = "queued"

        sleep_calls: list[float] = []

        def record_sleep(seconds: float):
            sleep_calls.append(seconds)

        with (
            _patch_supervisor(),
            patch("crsbench.distributed.ci_supervisor.rq") as mock_rq,
            patch(
                "crsbench.distributed.ci_supervisor._select_fair_job",
                side_effect=[(mock_job, mock_build_queue), KeyboardInterrupt],
            ),
            patch(
                "crsbench.distributed.ci_supervisor.time.sleep",
                side_effect=record_sleep,
            ),
        ):
            mock_rq.Queue.side_effect = queue_factory
            run_ci_supervisor(
                redis_host="localhost",
                build_queue_name="crsbench_ci_build",
                verify_queue_name="crsbench_ci_verify",
                worker_name="test-worker",
                build_jobs=1,
                build_cores_per_job=1,
                verify_jobs=0,
                job_runner=lambda _h, _n, _j: None,
                cpu_tag="x86-avx2",
            )

        assert 0.05 in sleep_calls

    def test_non_continuous_cpu_tag_mismatch_exits_without_livelock(self) -> None:
        """Non-continuous mode should stop after repeated cpu_tag mismatch requeues."""
        from crsbench.distributed.ci_supervisor import (
            CPU_TAG_MISMATCH_EXIT_CODE,
            NON_CONTINUOUS_CPU_MISMATCH_LIMIT,
            run_ci_supervisor,
        )

        mock_build_queue = _make_mock_queue("crsbench_ci_build", count=1)
        mock_verify_queue = _make_mock_queue("crsbench_ci_verify", count=0)

        def queue_factory(name, **_kwargs):
            if "build" in name:
                return mock_build_queue
            return mock_verify_queue

        mock_job = MagicMock()
        mock_job.id = "test-job-12345678"
        mock_job.meta = {"cpu_tag": "arm-neon"}
        mock_job.get_status.return_value = "queued"

        with (
            _patch_supervisor(),
            patch("crsbench.distributed.ci_supervisor.rq") as mock_rq,
            patch(
                "crsbench.distributed.ci_supervisor._select_fair_job",
                return_value=(mock_job, mock_build_queue),
            ) as mock_select,
            patch("crsbench.distributed.ci_supervisor.time.sleep"),
        ):
            mock_rq.Queue.side_effect = queue_factory
            result = run_ci_supervisor(
                redis_host="localhost",
                build_queue_name="crsbench_ci_build",
                verify_queue_name="crsbench_ci_verify",
                worker_name="test-worker",
                build_jobs=1,
                build_cores_per_job=1,
                verify_jobs=0,
                job_runner=lambda _h, _n, _j: None,
                cpu_tag="x86-avx2",
                continuous=False,
            )

        assert result == CPU_TAG_MISMATCH_EXIT_CODE
        assert mock_select.call_count == NON_CONTINUOUS_CPU_MISMATCH_LIMIT
        assert (
            mock_build_queue.enqueue_job.call_count == NON_CONTINUOUS_CPU_MISMATCH_LIMIT
        )


class TestWorkerTrialAdapter:
    """Test that the worker trial adapter exists and works."""

    def test_trial_job_runner_exists(self) -> None:
        """_trial_job_runner adapter exists in worker module."""
        from crsbench.distributed.worker import _trial_job_runner

        assert callable(_trial_job_runner)

    def test_trial_job_runner_signature(self) -> None:
        """_trial_job_runner has the expected 3-arg signature."""
        import inspect

        from crsbench.distributed.worker import _trial_job_runner

        sig = inspect.signature(_trial_job_runner)
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
        assert params == ["redis_host", "child_name", "job_id"]


class TestCiCliFlags:
    """Test CLI flag changes for worker and evaluator."""

    def test_queue_flag_removed(self) -> None:
        """Worker no longer has --queue flag."""
        import argparse

        from crsbench.distributed.cli.worker_command import (
            add_worker_subparser,
        )

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        add_worker_subparser(subparsers)
        args = parser.parse_args(["worker"])
        assert not hasattr(args, "queue")

    def test_build_jobs_removed_from_worker(self) -> None:
        """Worker no longer has --build-jobs flag."""
        import argparse

        from crsbench.distributed.cli.worker_command import (
            add_worker_subparser,
        )

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        add_worker_subparser(subparsers)
        args = parser.parse_args(["worker"])
        assert not hasattr(args, "build_jobs")

    def test_verify_jobs_removed_from_worker(self) -> None:
        """Worker no longer has --verify-jobs flag."""
        import argparse

        from crsbench.distributed.cli.worker_command import (
            add_worker_subparser,
        )

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        add_worker_subparser(subparsers)
        args = parser.parse_args(["worker"])
        assert not hasattr(args, "verify_jobs")

    def test_worker_defaults_to_continuous(self) -> None:
        """Worker defaults to continuous mode unless explicitly disabled."""
        import argparse

        from crsbench.distributed.cli.worker_command import (
            add_worker_subparser,
        )

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        add_worker_subparser(subparsers)
        args = parser.parse_args(["worker"])
        assert args.continuous is None

    def test_worker_accepts_no_continuous(self) -> None:
        """Worker exposes an explicit one-shot mode override."""
        import argparse

        from crsbench.distributed.cli.worker_command import (
            add_worker_subparser,
        )

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        add_worker_subparser(subparsers)
        args = parser.parse_args(["worker", "--no-continuous"])
        assert args.continuous is False

    def test_evaluator_ci_flag(self) -> None:
        """Evaluator accepts --ci flag."""
        import argparse

        from crsbench.distributed.cli.evaluator_command import (
            add_evaluator_subparser,
        )

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        add_evaluator_subparser(subparsers)
        args = parser.parse_args(["evaluator", "--ci"])
        assert args.ci is True

    def test_evaluator_experiment_config_optional(self) -> None:
        """Evaluator --experiment-config is optional (for --ci mode)."""
        import argparse

        from crsbench.distributed.cli.evaluator_command import (
            add_evaluator_subparser,
        )

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        add_evaluator_subparser(subparsers)
        args = parser.parse_args(["evaluator", "--ci"])
        assert args.experiment_config is None


class TestEvaluatorCommandCiFlags:
    """Test evaluator CLI flag changes."""

    def test_jobs_flag_exists(self) -> None:
        """Unified --jobs flag is available on evaluator."""
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
                "--jobs",
                "16",
            ]
        )
        assert args.jobs == 16

    def test_cores_per_job_default(self) -> None:
        """Unified --cores-per-job is unset by parser and resolved later."""
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
            ]
        )
        assert args.cores_per_job is None

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
                "--build-jobs",
                "16",
            ]
        )
        assert args.build_jobs == 16

    def test_build_cores_per_job_default(self) -> None:
        """--build-cores-per-job is unset by parser and resolved later."""
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
            ]
        )
        assert args.build_cores_per_job is None

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

        with (
            _patch_supervisor(),
            patch("crsbench.distributed.ci_supervisor.rq") as mock_rq,
            patch(
                "crsbench.distributed.ci_supervisor.time.sleep",
                side_effect=count_and_break,
            ),
        ):
            mock_rq.Queue.side_effect = queue_factory
            mock_build_queue.get_job_ids.return_value = []
            mock_verify_queue.get_job_ids.return_value = []
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

    def test_idle_timeout_does_not_exit_when_build_work_reappears(self) -> None:
        """Idle timeout should not trigger while build queue has work."""
        from crsbench.distributed.ci_supervisor import run_ci_supervisor

        build_q = _make_mock_queue("crsbench_ci_build", count=0)
        verify_q = _make_mock_queue("crsbench_ci_verify", count=0)

        def queue_factory(name, **_kwargs):
            return build_q if "build" in name else verify_q

        iterations = [0]

        def bump_and_interrupt(_seconds):
            iterations[0] += 1
            # Simulate new build work after initial backlog drain.
            build_q.count = 1
            if iterations[0] >= 6:
                raise KeyboardInterrupt

        time_counter = [0]

        def fake_time():
            time_counter[0] += 1
            return float(time_counter[0])

        with (
            _patch_supervisor(),
            patch("crsbench.distributed.ci_supervisor.rq") as mock_rq,
            patch(
                "crsbench.distributed.ci_supervisor.time.sleep",
                side_effect=bump_and_interrupt,
            ),
            patch(
                "crsbench.distributed.ci_supervisor.time.time", side_effect=fake_time
            ),
        ):
            mock_rq.Queue.side_effect = queue_factory
            build_q.get_job_ids.return_value = []
            verify_q.get_job_ids.return_value = []
            result = run_ci_supervisor(
                redis_host="localhost",
                build_queue_name="crsbench_ci_build",
                verify_queue_name="crsbench_ci_verify",
                worker_name="test-worker",
                build_jobs=1,
                build_cores_per_job=1,
                verify_jobs=1,
                job_runner=lambda _h, _n, _j: None,
                continuous=True,
                idle_timeout=5,
            )

        assert result == 0
        assert iterations[0] == 6

    def test_exception_path_terminates_workers(self) -> None:
        """Unexpected supervisor errors should terminate active workers."""
        from crsbench.distributed.ci_supervisor import run_ci_supervisor

        mock_build_queue = _make_mock_queue("crsbench_ci_build", count=1)
        mock_verify_queue = _make_mock_queue("crsbench_ci_verify", count=0)

        def queue_factory(name, **_kwargs):
            if "build" in name:
                return mock_build_queue
            return mock_verify_queue

        with (
            _patch_supervisor(),
            patch("crsbench.distributed.ci_supervisor.rq") as mock_rq,
            patch(
                "crsbench.distributed.ci_supervisor._select_fair_job",
                side_effect=RuntimeError("boom"),
            ),
            patch(
                "crsbench.distributed.ci_supervisor._terminate_all"
            ) as mock_terminate,
        ):
            mock_rq.Queue.side_effect = queue_factory
            result = run_ci_supervisor(
                redis_host="localhost",
                build_queue_name="crsbench_ci_build",
                verify_queue_name="crsbench_ci_verify",
                worker_name="test-worker",
                build_jobs=1,
                build_cores_per_job=1,
                verify_jobs=1,
                job_runner=lambda _h, _n, _j: None,
            )

        assert result == 3
        assert mock_terminate.call_count == 2

    def test_exception_path_cleans_active_worker_resources(self) -> None:
        """Exception after spawn should cleanup process, CPUs, and cgroup."""
        from pathlib import Path

        from crsbench.distributed.ci_supervisor import run_ci_supervisor

        mock_build_queue = _make_mock_queue("crsbench_ci_build", count=1)
        mock_verify_queue = _make_mock_queue("crsbench_ci_verify", count=0)

        def queue_factory(name, **_kwargs):
            if "build" in name:
                return mock_build_queue
            return mock_verify_queue

        mock_job = MagicMock()
        mock_job.id = "test-job-12345678"
        mock_job.meta = {}
        mock_job.get_status.return_value = "queued"

        mock_process = MagicMock()
        mock_process.pid = 43210
        # reap check, start-race check, terminate loop check, force-kill check
        mock_process.is_alive.side_effect = [True, True, True, True]

        mock_pool = MagicMock()
        mock_pool.allocate.return_value = [0]
        mock_pool.total_cpus = 1

        with (
            _patch_supervisor(),
            patch("crsbench.distributed.ci_supervisor.rq") as mock_rq,
            patch(
                "crsbench.distributed.ci_supervisor._select_fair_job",
                side_effect=[(mock_job, mock_build_queue), RuntimeError("boom")],
            ),
            patch(
                "crsbench.distributed.ci_supervisor._mp_ctx.Process"
            ) as mock_proc_cls,
            patch("crsbench.utils.cpu_pool.CPUPool", return_value=mock_pool),
            patch(
                "crsbench.utils.cgroup.run_preflight_checks",
                return_value=Path("/sys/fs/cgroup/crsbench"),
            ),
            patch("crsbench.utils.cgroup.setup_cgroup_hierarchy"),
            patch("crsbench.utils.cgroup.cleanup_stale_cgroups", return_value=0),
            patch(
                "crsbench.utils.cgroup.create_cgroup",
                return_value=Path("/sys/fs/cgroup/crsbench/build-1"),
            ),
            patch(
                "crsbench.utils.cgroup.cgroup_path_for_docker",
                return_value="crsbench/build-1",
            ),
            patch("crsbench.utils.cgroup.cleanup_cgroup", return_value=True) as mock_cg,
            patch(
                "crsbench.distributed.worker._terminate_process_tree"
            ) as mock_tree_kill,
            patch("crsbench.distributed.ci_supervisor._cleanup_stale_rq_workers"),
        ):
            mock_rq.Queue.side_effect = queue_factory
            mock_proc_cls.return_value = mock_process
            result = run_ci_supervisor(
                redis_host="localhost",
                build_queue_name="crsbench_ci_build",
                verify_queue_name="crsbench_ci_verify",
                worker_name="test-worker",
                build_jobs=2,
                build_cores_per_job=1,
                verify_jobs=1,
                job_runner=lambda _h, _n, _j: None,
                use_cpuset=True,
                use_cgroups=True,
            )

        assert result == 3
        mock_tree_kill.assert_called_once_with(43210)
        mock_pool.release.assert_called_once_with([0])
        mock_cg.assert_called_with(Path("/sys/fs/cgroup/crsbench/build-1"), force=True)

    def test_early_child_exit_requeues_job_at_front(self) -> None:
        """If child exits before active registration, requeue job at front."""
        from crsbench.distributed.ci_supervisor import run_ci_supervisor

        mock_build_queue = _make_mock_queue("crsbench_ci_build", count=1)
        mock_verify_queue = _make_mock_queue("crsbench_ci_verify", count=0)

        def queue_factory(name, **_kwargs):
            if "build" in name:
                return mock_build_queue
            return mock_verify_queue

        mock_job = MagicMock()
        mock_job.id = "test-job-12345678"
        mock_job.meta = {}
        # Child exits before touching RQ state: still queued -> requeue is required.
        mock_job.get_status.return_value = "queued"

        mock_process = MagicMock()
        mock_process.pid = 12345
        mock_process.is_alive.return_value = False

        with (
            _patch_supervisor(),
            patch("crsbench.distributed.ci_supervisor.rq") as mock_rq,
            patch(
                "crsbench.distributed.ci_supervisor._select_fair_job",
                side_effect=[(mock_job, mock_build_queue), KeyboardInterrupt],
            ),
            patch(
                "crsbench.distributed.ci_supervisor._mp_ctx.Process"
            ) as mock_proc_cls,
        ):
            mock_rq.Queue.side_effect = queue_factory
            mock_proc_cls.return_value = mock_process
            result = run_ci_supervisor(
                redis_host="localhost",
                build_queue_name="crsbench_ci_build",
                verify_queue_name="crsbench_ci_verify",
                worker_name="test-worker",
                build_jobs=1,
                build_cores_per_job=1,
                verify_jobs=0,
                job_runner=lambda _h, _n, _j: None,
            )

        assert result == 0
        mock_build_queue.enqueue_job.assert_called_once_with(mock_job, at_front=True)


class TestMultiQueueSupervisor:
    """Tests for run_multi_queue_supervisor with multiple queue lists."""

    def test_function_exists(self) -> None:
        """run_multi_queue_supervisor exists and is importable."""
        from crsbench.distributed.ci_supervisor import run_multi_queue_supervisor

        assert callable(run_multi_queue_supervisor)

    def test_creates_multiple_queues(self) -> None:
        """Multi-queue supervisor creates queue objects for all names."""
        from crsbench.distributed.ci_supervisor import run_multi_queue_supervisor

        queues_created = []

        def track_queue_creation(name, **_kwargs):
            queues_created.append(name)
            return _make_mock_queue(name)

        with (
            _patch_supervisor(),
            patch("crsbench.distributed.ci_supervisor.rq") as mock_rq,
            patch(
                "crsbench.distributed.ci_supervisor.time.sleep",
                side_effect=KeyboardInterrupt,
            ),
        ):
            mock_rq.Queue.side_effect = track_queue_creation
            result = run_multi_queue_supervisor(
                redis_host="localhost",
                build_queue_names=["crsbench_exp1_build", "crsbench_exp2_build"],
                verify_queue_names=["crsbench_exp1_verify", "crsbench_exp2_verify"],
                worker_name="test-worker",
                build_jobs=2,
                build_cores_per_job=1,
                verify_jobs=4,
                job_runner=lambda _h, _n, _j: None,
            )

        assert "crsbench_exp1_build" in queues_created
        assert "crsbench_exp2_build" in queues_created
        assert "crsbench_exp1_verify" in queues_created
        assert "crsbench_exp2_verify" in queues_created
        assert result == 0

    def test_multi_queue_no_longer_uses_ordered_dequeue_priority(self) -> None:
        """Multi-queue supervisor should not call legacy ordered dequeue."""
        from crsbench.distributed.ci_supervisor import run_multi_queue_supervisor

        build_q1 = _make_mock_queue("crsbench_exp1_build", count=1)
        build_q2 = _make_mock_queue("crsbench_exp2_build", count=1)
        verify_q1 = _make_mock_queue("crsbench_exp1_verify", count=1)
        verify_q2 = _make_mock_queue("crsbench_exp2_verify", count=1)

        queue_map = {
            "crsbench_exp1_build": build_q1,
            "crsbench_exp2_build": build_q2,
            "crsbench_exp1_verify": verify_q1,
            "crsbench_exp2_verify": verify_q2,
        }

        def queue_factory(name, **_kwargs):
            return queue_map[name]

        with (
            _patch_supervisor(),
            patch("crsbench.distributed.ci_supervisor.rq") as mock_rq,
            patch(
                "crsbench.distributed.ci_supervisor.time.sleep",
                side_effect=KeyboardInterrupt,
            ),
        ):
            mock_rq.Queue.side_effect = queue_factory
            mock_rq.Queue.dequeue_any.side_effect = AssertionError(
                "ordered dequeue_any should not be used"
            )
            run_multi_queue_supervisor(
                redis_host="localhost",
                build_queue_names=["crsbench_exp1_build", "crsbench_exp2_build"],
                verify_queue_names=["crsbench_exp1_verify", "crsbench_exp2_verify"],
                worker_name="test-worker",
                build_jobs=2,
                build_cores_per_job=1,
                verify_jobs=4,
                job_runner=lambda _h, _n, _j: None,
            )

        assert mock_rq.Queue.dequeue_any.call_count == 0

    def test_non_continuous_exits_when_all_empty(self) -> None:
        """Non-continuous mode exits when all queues across experiments are empty."""
        from crsbench.distributed.ci_supervisor import run_multi_queue_supervisor

        build_q = _make_mock_queue("crsbench_exp1_build")
        verify_q = _make_mock_queue("crsbench_exp1_verify")

        def queue_factory(name, **_kwargs):
            if "build" in name:
                return build_q
            return verify_q

        iterations = [0]

        def count_and_break(_seconds):
            iterations[0] += 1
            if iterations[0] >= 5:
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
            result = run_multi_queue_supervisor(
                redis_host="localhost",
                build_queue_names=["crsbench_exp1_build"],
                verify_queue_names=["crsbench_exp1_verify"],
                worker_name="test-worker",
                build_jobs=1,
                build_cores_per_job=1,
                verify_jobs=1,
                job_runner=lambda _h, _n, _j: None,
                continuous=False,
            )

        # Should exit immediately (0 iterations) since all queues are empty
        assert result == 0
        assert iterations[0] == 0

    def test_non_continuous_cpu_tag_mismatch_exits_without_livelock(self) -> None:
        """Multi-queue non-continuous mode should stop repeated mismatch requeues."""
        from crsbench.distributed.ci_supervisor import (
            CPU_TAG_MISMATCH_EXIT_CODE,
            NON_CONTINUOUS_CPU_MISMATCH_LIMIT,
            run_multi_queue_supervisor,
        )

        build_q = _make_mock_queue("crsbench_exp1_build", count=1)
        verify_q = _make_mock_queue("crsbench_exp1_verify", count=0)

        def queue_factory(name, **_kwargs):
            return build_q if "build" in name else verify_q

        mock_job = MagicMock()
        mock_job.id = "test-job-12345678"
        mock_job.meta = {"cpu_tag": "arm-neon"}
        mock_job.get_status.return_value = "queued"

        with (
            _patch_supervisor(),
            patch("crsbench.distributed.ci_supervisor.rq") as mock_rq,
            patch(
                "crsbench.distributed.ci_supervisor._select_fair_job",
                return_value=(mock_job, build_q),
            ) as mock_select,
            patch("crsbench.distributed.ci_supervisor.time.sleep"),
        ):
            mock_rq.Queue.side_effect = queue_factory
            result = run_multi_queue_supervisor(
                redis_host="localhost",
                build_queue_names=["crsbench_exp1_build"],
                verify_queue_names=["crsbench_exp1_verify"],
                worker_name="test-worker",
                build_jobs=1,
                build_cores_per_job=1,
                verify_jobs=0,
                job_runner=lambda _h, _n, _j: None,
                cpu_tag="x86-avx2",
                continuous=False,
            )

        assert result == CPU_TAG_MISMATCH_EXIT_CODE
        assert mock_select.call_count == NON_CONTINUOUS_CPU_MISMATCH_LIMIT
        assert build_q.enqueue_job.call_count == NON_CONTINUOUS_CPU_MISMATCH_LIMIT

    def test_accepts_empty_verify_list(self) -> None:
        """Multi-queue supervisor works with empty verify queue list (worker mode)."""
        from crsbench.distributed.ci_supervisor import run_multi_queue_supervisor

        def queue_factory(name, **_kwargs):
            return _make_mock_queue(name)

        with (
            _patch_supervisor(),
            patch("crsbench.distributed.ci_supervisor.rq") as mock_rq,
            patch(
                "crsbench.distributed.ci_supervisor.time.sleep",
                side_effect=KeyboardInterrupt,
            ),
        ):
            mock_rq.Queue.side_effect = queue_factory
            result = run_multi_queue_supervisor(
                redis_host="localhost",
                build_queue_names=["crsbench_exp_trial"],
                verify_queue_names=[],
                worker_name="test-worker",
                build_jobs=1,
                build_cores_per_job=1,
                verify_jobs=0,
                job_runner=lambda _h, _n, _j: None,
            )

        assert result == 0

    def test_idle_timeout_does_not_exit_when_build_work_reappears(self) -> None:
        """Multi-queue idle timeout should not trigger with pending build work."""
        from crsbench.distributed.ci_supervisor import run_multi_queue_supervisor

        build_q = _make_mock_queue("crsbench_exp_build", count=0)
        verify_q = _make_mock_queue("crsbench_exp_verify", count=0)

        def queue_factory(name, **_kwargs):
            return build_q if "build" in name else verify_q

        iterations = [0]

        def bump_and_interrupt(_seconds):
            iterations[0] += 1
            build_q.count = 1
            if iterations[0] >= 6:
                raise KeyboardInterrupt

        time_counter = [0]

        def fake_time():
            time_counter[0] += 1
            return float(time_counter[0])

        with (
            _patch_supervisor(),
            patch("crsbench.distributed.ci_supervisor.rq") as mock_rq,
            patch(
                "crsbench.distributed.ci_supervisor.time.sleep",
                side_effect=bump_and_interrupt,
            ),
            patch(
                "crsbench.distributed.ci_supervisor.time.time", side_effect=fake_time
            ),
        ):
            mock_rq.Queue.side_effect = queue_factory
            build_q.get_job_ids.return_value = []
            verify_q.get_job_ids.return_value = []
            result = run_multi_queue_supervisor(
                redis_host="localhost",
                build_queue_names=["crsbench_exp_build"],
                verify_queue_names=["crsbench_exp_verify"],
                worker_name="test-worker",
                build_jobs=1,
                build_cores_per_job=1,
                verify_jobs=1,
                job_runner=lambda _h, _n, _j: None,
                continuous=True,
                idle_timeout=5,
            )

        assert result == 0
        assert iterations[0] == 6

    def test_exception_path_terminates_workers(self) -> None:
        """Unexpected multi-queue errors should terminate active workers."""
        from crsbench.distributed.ci_supervisor import run_multi_queue_supervisor

        build_q = _make_mock_queue("crsbench_exp_build", count=1)
        verify_q = _make_mock_queue("crsbench_exp_verify", count=0)

        def queue_factory(name, **_kwargs):
            return build_q if "build" in name else verify_q

        with (
            _patch_supervisor(),
            patch("crsbench.distributed.ci_supervisor.rq") as mock_rq,
            patch(
                "crsbench.distributed.ci_supervisor._select_fair_job",
                side_effect=RuntimeError("boom"),
            ),
            patch(
                "crsbench.distributed.ci_supervisor._terminate_all"
            ) as mock_terminate,
        ):
            mock_rq.Queue.side_effect = queue_factory
            result = run_multi_queue_supervisor(
                redis_host="localhost",
                build_queue_names=["crsbench_exp_build"],
                verify_queue_names=["crsbench_exp_verify"],
                worker_name="test-worker",
                build_jobs=1,
                build_cores_per_job=1,
                verify_jobs=1,
                job_runner=lambda _h, _n, _j: None,
            )

        assert result == 3
        assert mock_terminate.call_count == 2

    def test_exception_path_cleans_active_worker_resources(self) -> None:
        """Multi-queue exception after spawn should cleanup active worker resources."""
        from pathlib import Path

        from crsbench.distributed.ci_supervisor import run_multi_queue_supervisor

        build_q = _make_mock_queue("crsbench_exp_build", count=1)
        verify_q = _make_mock_queue("crsbench_exp_verify", count=0)

        def queue_factory(name, **_kwargs):
            return build_q if "build" in name else verify_q

        mock_job = MagicMock()
        mock_job.id = "test-job-12345678"
        mock_job.meta = {}
        mock_job.get_status.return_value = "queued"

        mock_process = MagicMock()
        mock_process.pid = 12321
        # reap check, start-race check, terminate loop check, force-kill check
        mock_process.is_alive.side_effect = [True, True, True, True]

        mock_pool = MagicMock()
        mock_pool.allocate.return_value = [0]
        mock_pool.total_cpus = 1

        with (
            _patch_supervisor(),
            patch("crsbench.distributed.ci_supervisor.rq") as mock_rq,
            patch(
                "crsbench.distributed.ci_supervisor._select_fair_job",
                side_effect=[(mock_job, build_q), RuntimeError("boom")],
            ),
            patch(
                "crsbench.distributed.ci_supervisor._mp_ctx.Process"
            ) as mock_proc_cls,
            patch("crsbench.utils.cpu_pool.CPUPool", return_value=mock_pool),
            patch(
                "crsbench.utils.cgroup.run_preflight_checks",
                return_value=Path("/sys/fs/cgroup/crsbench"),
            ),
            patch("crsbench.utils.cgroup.setup_cgroup_hierarchy"),
            patch("crsbench.utils.cgroup.cleanup_stale_cgroups", return_value=0),
            patch(
                "crsbench.utils.cgroup.create_cgroup",
                return_value=Path("/sys/fs/cgroup/crsbench/build-1"),
            ),
            patch(
                "crsbench.utils.cgroup.cgroup_path_for_docker",
                return_value="crsbench/build-1",
            ),
            patch("crsbench.utils.cgroup.cleanup_cgroup", return_value=True) as mock_cg,
            patch(
                "crsbench.distributed.worker._terminate_process_tree"
            ) as mock_tree_kill,
            patch("crsbench.distributed.ci_supervisor._cleanup_stale_rq_workers"),
        ):
            mock_rq.Queue.side_effect = queue_factory
            mock_proc_cls.return_value = mock_process
            result = run_multi_queue_supervisor(
                redis_host="localhost",
                build_queue_names=["crsbench_exp_build"],
                verify_queue_names=["crsbench_exp_verify"],
                worker_name="test-worker",
                build_jobs=2,
                build_cores_per_job=1,
                verify_jobs=1,
                job_runner=lambda _h, _n, _j: None,
                use_cpuset=True,
                use_cgroups=True,
            )

        assert result == 3
        mock_tree_kill.assert_called_once_with(12321)
        mock_pool.release.assert_called_once_with([0])
        mock_cg.assert_called_with(Path("/sys/fs/cgroup/crsbench/build-1"), force=True)


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

    def test_progress_milestones_crossed_reports_all_boundaries(self) -> None:
        """Progress milestone helper should report each crossed multiple."""
        from crsbench.distributed.ci_supervisor import _progress_milestones_crossed

        assert _progress_milestones_crossed(0, 49, interval=50) == []
        assert _progress_milestones_crossed(49, 50, interval=50) == [50]
        assert _progress_milestones_crossed(49, 101, interval=50) == [50, 100]
        assert _progress_milestones_crossed(100, 100, interval=50) == []

    def test_log_handled_job_progress_logs_at_threshold(self) -> None:
        """Handled-job progress log should fire when a milestone is crossed."""
        from crsbench.distributed.ci_supervisor import _log_handled_job_progress

        with patch("crsbench.distributed.ci_supervisor.logger.info") as mock_info:
            _log_handled_job_progress(
                previous_total=49,
                build_handled=30,
                verify_handled=20,
                build_active=1,
                verify_active=2,
                build_queued=7,
                verify_queued=9,
                interval=50,
            )

        mock_info.assert_called_once()
        assert "Handled 50 jobs so far" in mock_info.call_args.args[0]
        assert "build=30" in mock_info.call_args.args[0]
        assert "verify=20" in mock_info.call_args.args[0]

    def test_check_disk_space_in_ci_supervisor(self) -> None:
        """check_disk_space is importable from ci_supervisor."""
        from pathlib import Path

        from crsbench.distributed.ci_supervisor import check_disk_space

        result = check_disk_space(Path("/tmp"))
        assert isinstance(result, int)
        assert result > 0

    def test_safe_cwd_falls_back_to_root_on_enoent(self) -> None:
        """_safe_cwd should not propagate ENOENT from Path.cwd()."""
        from pathlib import Path

        from crsbench.distributed.ci_supervisor import _safe_cwd

        with patch.object(Path, "cwd", side_effect=FileNotFoundError("cwd missing")):
            assert _safe_cwd() == Path("/")

    def test_check_disk_space_uses_safe_cwd_for_relative_paths(self) -> None:
        """Relative paths should still resolve when cwd cannot be read."""
        from pathlib import Path

        from crsbench.distributed.ci_supervisor import check_disk_space

        with patch.object(Path, "cwd", side_effect=FileNotFoundError("cwd missing")):
            result = check_disk_space(Path("relative/missing/path"))
            assert isinstance(result, int)
            assert result > 0

    def test_matches_cpu_tag_strict_for_untagged_workers(self) -> None:
        """Untagged workers should not execute tagged jobs."""
        from crsbench.distributed.ci_supervisor import _matches_cpu_tag

        tagged_job = MagicMock()
        tagged_job.meta = {"cpu_tag": "x86-avx2"}
        untagged_job = MagicMock()
        untagged_job.meta = {}

        assert _matches_cpu_tag(untagged_job, None) is True
        assert _matches_cpu_tag(tagged_job, None) is False

    def test_matches_cpu_tag_for_tagged_workers(self) -> None:
        """Tagged workers execute untagged + matching tagged jobs only."""
        from crsbench.distributed.ci_supervisor import _matches_cpu_tag

        untagged_job = MagicMock()
        untagged_job.meta = {}
        matching_job = MagicMock()
        matching_job.meta = {"cpu_tag": "x86-avx2"}
        mismatching_job = MagicMock()
        mismatching_job.meta = {"cpu_tag": "arm-neon"}

        assert _matches_cpu_tag(untagged_job, "x86-avx2") is True
        assert _matches_cpu_tag(matching_job, "x86-avx2") is True
        assert _matches_cpu_tag(mismatching_job, "x86-avx2") is False

    def test_force_cleanup_deferred_cgroups_clears_successes(self) -> None:
        """Force cleanup should remove successful paths and keep failures only."""
        from pathlib import Path

        from crsbench.distributed.ci_supervisor import _force_cleanup_deferred_cgroups

        deferred = [Path("/cg/a"), Path("/cg/b")]

        def _cleanup(path: Path, force: bool) -> bool:
            assert force is True
            if str(path).endswith("/b"):
                raise RuntimeError("still busy")
            return True

        with patch("crsbench.utils.cgroup.cleanup_cgroup", side_effect=_cleanup):
            _force_cleanup_deferred_cgroups(deferred)

        assert deferred == [Path("/cg/b")]

    def test_force_cleanup_deferred_cgroups_keeps_false_results(self) -> None:
        """False return from cleanup_cgroup should keep path deferred."""
        from pathlib import Path

        from crsbench.distributed.ci_supervisor import _force_cleanup_deferred_cgroups

        deferred = [Path("/cg/a"), Path("/cg/b")]

        def _cleanup(path: Path, force: bool) -> bool:
            assert force is True
            return not str(path).endswith("/b")

        with patch("crsbench.utils.cgroup.cleanup_cgroup", side_effect=_cleanup):
            _force_cleanup_deferred_cgroups(deferred)

        assert deferred == [Path("/cg/b")]
