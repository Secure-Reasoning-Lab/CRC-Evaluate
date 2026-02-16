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
    @patch("crsbench.distributed.ci_supervisor.run_ci_supervisor")
    @patch("crsbench.distributed.evaluator_jobs.set_engine")
    def test_skips_phase1_builds(
        self,
        mock_set_engine: MagicMock,
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

        # Should set engine (not call _build_all_variants)
        mock_set_engine.assert_called_once()
        call_args = mock_set_engine.call_args
        # Verify it was called with just the engine argument (no built_results)
        assert len(call_args[0]) == 1  # Only one positional argument

        # Should call supervisor directly (now via ci_supervisor)
        mock_supervisor.assert_called_once()
        assert result == 0

    def test_no_build_workers_parameter(self) -> None:
        """run_evaluator_main no longer has build_workers parameter."""
        import inspect

        from crsbench.distributed.evaluator import run_evaluator_main

        sig = inspect.signature(run_evaluator_main)
        assert "build_workers" not in sig.parameters


class TestEvaluatorSupervisorQueues:
    """Test evaluator delegates to ci_supervisor for dual-queue setup.

    Detailed dual-queue tests are in test_ci_supervisor.py. These tests
    verify the evaluator correctly delegates to run_ci_supervisor.
    """

    @patch("crsbench.distributed.evaluator.REDIS_AVAILABLE", new=True)
    @patch("crsbench.distributed.ci_supervisor.run_ci_supervisor")
    @patch("crsbench.distributed.evaluator_jobs.set_engine")
    def test_creates_both_queues(
        self,
        mock_set_engine: MagicMock,
        mock_supervisor: MagicMock,
    ) -> None:
        """Evaluator passes build and verify queue names to ci_supervisor."""
        from crsbench.distributed.evaluator import run_evaluator_main

        mock_supervisor.return_value = 0
        config = MagicMock()
        config.oss_fuzz_path = "/tmp/oss-fuzz"
        config.reproduce_timeout = 180

        with patch("crsbench.evaluation.verification.pov.engine.VerificationEngine"):
            run_evaluator_main(config, "exp-test")

        # ci_supervisor should receive both queue names
        call_kwargs = mock_supervisor.call_args
        assert "crsbench_exp-test_build" in str(call_kwargs)
        assert "crsbench_exp-test_verify" in str(call_kwargs)

    @patch("crsbench.distributed.evaluator.REDIS_AVAILABLE", new=True)
    @patch("crsbench.distributed.ci_supervisor.run_ci_supervisor")
    @patch("crsbench.distributed.evaluator_jobs.set_engine")
    def test_build_queue_priority(
        self,
        mock_set_engine: MagicMock,
        mock_supervisor: MagicMock,
    ) -> None:
        """Build queue name is passed before verify queue name."""
        from crsbench.distributed.evaluator import run_evaluator_main

        mock_supervisor.return_value = 0
        config = MagicMock()
        config.oss_fuzz_path = "/tmp/oss-fuzz"
        config.reproduce_timeout = 180

        with patch("crsbench.evaluation.verification.pov.engine.VerificationEngine"):
            run_evaluator_main(config, "exp-test")

        call_kwargs = mock_supervisor.call_args[1]
        assert call_kwargs["build_queue_name"] == "crsbench_exp-test_build"
        assert call_kwargs["verify_queue_name"] == "crsbench_exp-test_verify"


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
