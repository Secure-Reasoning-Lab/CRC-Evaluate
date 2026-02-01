"""Tests for evaluator dual-queue support.

Tests that:
1. run_evaluator_main() skips Phase 1 builds (no startup build phase)
2. Supervisor creates both build and verify queues
3. Build queue has priority over verify queue
"""

from unittest.mock import MagicMock, patch


class TestRunEvaluatorMain:
    """run_evaluator_main() tests."""

    @patch("crsbench.distributed.evaluator.REDIS_AVAILABLE", new=False)
    def test_returns_error_without_redis(self) -> None:
        """Returns error code when Redis is not available."""
        from crsbench.distributed.evaluator import run_evaluator_main

        config = MagicMock()
        result = run_evaluator_main(config, "exp-test")
        assert result == 1

    @patch("crsbench.distributed.evaluator.REDIS_AVAILABLE", new=True)
    @patch("crsbench.distributed.evaluator._run_evaluator_supervisor")
    @patch("crsbench.distributed.evaluator_jobs.set_build_cache")
    def test_skips_phase1_builds(
        self,
        mock_set_cache: MagicMock,
        mock_supervisor: MagicMock,
    ) -> None:
        """Evaluator skips Phase 1 builds and goes directly to supervisor."""
        from crsbench.distributed.evaluator import run_evaluator_main

        mock_supervisor.return_value = 0
        config = MagicMock()
        config.oss_fuzz_path = "/tmp/oss-fuzz"
        config.reproduce_timeout = 180

        with patch(
            "crsbench.evaluation.verification.pov.engine.VerificationEngine"
        ) as mock_engine_cls:
            mock_engine = MagicMock()
            mock_engine_cls.return_value = mock_engine

            result = run_evaluator_main(config, "exp-test")

        # Should set empty build cache (not call _build_all_variants)
        mock_set_cache.assert_called_once()
        call_args = mock_set_cache.call_args
        assert call_args[0][1] == {}  # empty built_results

        # Should call supervisor directly
        mock_supervisor.assert_called_once()
        assert result == 0

    def test_no_build_workers_parameter(self) -> None:
        """run_evaluator_main no longer has build_workers parameter."""
        import inspect

        from crsbench.distributed.evaluator import run_evaluator_main

        sig = inspect.signature(run_evaluator_main)
        assert "build_workers" not in sig.parameters


class TestEvaluatorSupervisorQueues:
    """Test _run_evaluator_supervisor dual-queue setup."""

    @patch("crsbench.distributed.evaluator.redis")
    @patch("crsbench.distributed.evaluator.rq")
    def test_creates_both_queues(
        self, mock_rq: MagicMock, mock_redis: MagicMock
    ) -> None:
        """Supervisor creates both build and verify queues."""
        from crsbench.distributed.evaluator import _run_evaluator_supervisor

        mock_conn = MagicMock()
        mock_redis.Redis.return_value = mock_conn

        # Make the queue counts return 0 and then raise to break loop
        mock_build_queue = MagicMock()
        mock_verify_queue = MagicMock()
        mock_build_queue.count = 0
        mock_verify_queue.count = 0
        mock_build_queue.name = "crsbench_test_build"
        mock_verify_queue.name = "crsbench_test_verify"

        queues_created = []

        def track_queue_creation(name, **_kwargs):
            queues_created.append(name)
            if "build" in name:
                return mock_build_queue
            return mock_verify_queue

        mock_rq.Queue.side_effect = track_queue_creation

        # Use KeyboardInterrupt to break out of the infinite loop
        def break_after_first_iteration(_seconds):
            raise KeyboardInterrupt

        with patch(
            "crsbench.distributed.evaluator.time.sleep",
            side_effect=break_after_first_iteration,
        ):
            result = _run_evaluator_supervisor(
                redis_host="localhost",
                experiment_name="test",
                max_jobs=1,
            )

        # Both queues should be created
        assert "crsbench_test_build" in queues_created
        assert "crsbench_test_verify" in queues_created
        assert result == 0  # KeyboardInterrupt returns 0

    @patch("crsbench.distributed.evaluator.redis")
    @patch("crsbench.distributed.evaluator.rq")
    def test_build_queue_priority(
        self, mock_rq: MagicMock, mock_redis: MagicMock
    ) -> None:
        """Build queue is listed first for dequeue priority."""
        from crsbench.distributed.evaluator import _run_evaluator_supervisor

        mock_conn = MagicMock()
        mock_redis.Redis.return_value = mock_conn

        mock_build_queue = MagicMock()
        mock_verify_queue = MagicMock()
        mock_build_queue.count = 1  # Has a build job
        mock_verify_queue.count = 1  # Also has a verify job
        mock_build_queue.name = "crsbench_test_build"
        mock_verify_queue.name = "crsbench_test_verify"

        def queue_factory(name, **_kwargs):
            if "build" in name:
                return mock_build_queue
            return mock_verify_queue

        mock_rq.Queue.side_effect = queue_factory

        # Track the order of queues passed to dequeue_any
        dequeue_calls = []

        def track_dequeue_any(queues, **_kwargs):
            dequeue_calls.append([q.name for q in queues])
            raise KeyboardInterrupt  # Break after first dequeue attempt

        mock_rq.Queue.dequeue_any = track_dequeue_any

        _run_evaluator_supervisor(
            redis_host="localhost",
            experiment_name="test",
            max_jobs=1,
        )

        # Build queue should be first in the dequeue list
        assert len(dequeue_calls) > 0
        assert dequeue_calls[0][0] == "crsbench_test_build"
        assert dequeue_calls[0][1] == "crsbench_test_verify"


class TestRunSingleJob:
    """Test _run_single_job() generic job execution."""

    def test_function_exists(self) -> None:
        """_run_single_job exists (renamed from _run_single_verify_job)."""
        from crsbench.distributed.evaluator import _run_single_job

        assert callable(_run_single_job)

    def test_old_function_removed(self) -> None:
        """_run_single_verify_job no longer exists."""
        import crsbench.distributed.evaluator as mod

        assert not hasattr(mod, "_run_single_verify_job")
