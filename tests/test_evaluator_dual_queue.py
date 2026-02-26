"""Tests for evaluator dual-queue support.

Tests that:
1. run_evaluator_main() can skip startup pre-build when pre-build is disabled
2. Supervisor creates both build and verify queues
3. Build queue has priority over verify queue
"""

import argparse
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


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
        """Evaluator skips startup pre-build by default and goes directly to supervisor."""
        from crsbench.distributed.evaluator import run_evaluator_main

        mock_supervisor.return_value = 0
        config = MagicMock()
        config.oss_fuzz_path = "/tmp/oss-fuzz"
        config.per_pov_verify_timeout = 180

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

    @patch("crsbench.distributed.evaluator.REDIS_AVAILABLE", new=True)
    @patch("crsbench.distributed.ci_supervisor.run_ci_supervisor")
    def test_defaults_build_verify_cores_to_four_in_config_mode(
        self,
        mock_supervisor: MagicMock,
    ) -> None:
        """Config mode defaults build/verify cores-per-job to 4."""
        from crsbench.distributed.evaluator import run_evaluator_main

        mock_supervisor.return_value = 0
        config = MagicMock()
        config.oss_fuzz_path = "/tmp/oss-fuzz"
        config.per_pov_verify_timeout = 180

        with (
            patch("crsbench.evaluation.verification.pov.engine.VerificationEngine"),
            patch("crsbench.distributed.evaluator_jobs.set_engine"),
        ):
            result = run_evaluator_main(config, "exp-test")

        assert result == 0
        kwargs = mock_supervisor.call_args.kwargs
        assert kwargs["build_cores_per_job"] == 4
        assert kwargs["verify_cores_per_job"] == 4


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
        config.per_pov_verify_timeout = 180

        with patch("crsbench.evaluation.verification.pov.engine.VerificationEngine"):
            run_evaluator_main(config, "exp-test")

        # ci_supervisor should receive both queue names
        call_kwargs = mock_supervisor.call_args
        from crsbench.distributed.queue import resolve_queue_names

        _, expected_build, expected_verify = resolve_queue_names("exp-test")
        assert expected_build in str(call_kwargs)
        assert expected_verify in str(call_kwargs)

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
        config.per_pov_verify_timeout = 180

        with patch("crsbench.evaluation.verification.pov.engine.VerificationEngine"):
            run_evaluator_main(config, "exp-test")

        call_kwargs = mock_supervisor.call_args[1]
        from crsbench.distributed.queue import resolve_queue_names

        _, expected_build, expected_verify = resolve_queue_names("exp-test")
        assert call_kwargs["build_queue_name"] == expected_build
        assert call_kwargs["verify_queue_name"] == expected_verify


class TestConfiglessEvaluator:
    """Tests for configless evaluator mode (registry discovery)."""

    def test_run_evaluator_configless_exists(self) -> None:
        """run_evaluator_configless function exists."""
        from crsbench.distributed.evaluator import run_evaluator_configless

        assert callable(run_evaluator_configless)

    @patch("crsbench.distributed.evaluator.REDIS_AVAILABLE", new=True)
    def test_configless_discovers_experiments(self) -> None:
        """Configless evaluator discovers experiments from registry."""
        from crsbench.distributed.evaluator import run_evaluator_configless
        from crsbench.distributed.registry import RuntimeRegistration

        reg = RuntimeRegistration(
            experiment="exp-42",
            trial_queue="crsbench_exp-42",
            build_queue="crsbench_exp-42_build",
            verify_queue="crsbench_exp-42_verify",
            benchmarks=[],
            oss_fuzz_path="/tmp/oss-fuzz",
            benchmarks_root="/tmp/benchmarks",
            per_pov_verify_timeout=180,
        )

        with (
            patch(
                "crsbench.distributed.evaluator.discover_registered_experiments",
                return_value=(MagicMock(), {"exp-42": reg}),
            ),
            patch("crsbench.evaluation.verification.pov.engine.VerificationEngine"),
            patch("crsbench.distributed.evaluator_jobs.set_engine"),
            patch("crsbench.distributed.evaluator_jobs.set_benchmarks_root"),
            patch(
                "crsbench.distributed.ci_supervisor.run_multi_queue_supervisor",
                return_value=0,
            ) as mock_supervisor,
        ):
            result = run_evaluator_configless(
                redis_host="localhost",
                build_jobs=2,
            )

        assert result == 0
        mock_supervisor.assert_called_once()
        call_kwargs = mock_supervisor.call_args[1]
        assert call_kwargs["build_queue_names"] == ["crsbench_exp-42_build"]
        assert call_kwargs["verify_queue_names"] == ["crsbench_exp-42_verify"]

    @patch("crsbench.distributed.evaluator.REDIS_AVAILABLE", new=True)
    def test_configless_resource_resolution_cli_overrides_metadata(self) -> None:
        """Configless evaluator resolves resources with CLI>metadata precedence."""
        from crsbench.distributed.evaluator import run_evaluator_configless
        from crsbench.distributed.registry import RuntimeRegistration

        reg = RuntimeRegistration(
            experiment="exp-42",
            trial_queue="crsbench_exp-42",
            build_queue="crsbench_exp-42_build",
            verify_queue="crsbench_exp-42_verify",
            benchmarks=[],
            oss_fuzz_path="/tmp/oss-fuzz",
            benchmarks_root="/tmp/benchmarks",
            per_pov_verify_timeout=180,
            evaluator_build_jobs=6,
            evaluator_build_cores_per_job=8,
            evaluator_verify_jobs=6,
            evaluator_verify_cores_per_job=8,
            evaluator_idle_timeout=123,
        )

        with (
            patch(
                "crsbench.distributed.evaluator.discover_registered_experiments",
                return_value=(MagicMock(), {"exp-42": reg}),
            ),
            patch("crsbench.evaluation.verification.pov.engine.VerificationEngine"),
            patch("crsbench.distributed.evaluator_jobs.set_engine"),
            patch("crsbench.distributed.evaluator_jobs.set_benchmarks_root"),
            patch(
                "crsbench.distributed.evaluator._enqueue_pre_builds_from_registration",
                return_value=0,
            ),
            patch(
                "crsbench.distributed.ci_supervisor.run_multi_queue_supervisor",
                return_value=0,
            ) as mock_supervisor,
        ):
            result = run_evaluator_configless(
                redis_host="localhost",
                build_jobs=2,
                build_cores_per_job=4,
                verify_jobs=3,
                verify_cores_per_job=5,
                idle_timeout=7,
            )

        assert result == 0
        kwargs = mock_supervisor.call_args.kwargs
        assert kwargs["build_jobs"] == 2
        assert kwargs["build_cores_per_job"] == 4
        assert kwargs["verify_jobs"] == 3
        assert kwargs["verify_cores_per_job"] == 5
        assert kwargs["idle_timeout"] == 7

    @patch("crsbench.distributed.evaluator.REDIS_AVAILABLE", new=True)
    def test_configless_defaults_build_verify_cores_to_four(self) -> None:
        """Configless evaluator defaults build/verify cores-per-job to 4."""
        from crsbench.distributed.evaluator import run_evaluator_configless
        from crsbench.distributed.registry import RuntimeRegistration

        reg = RuntimeRegistration(
            experiment="exp-42",
            trial_queue="crsbench_exp-42",
            build_queue="crsbench_exp-42_build",
            verify_queue="crsbench_exp-42_verify",
            benchmarks=[],
            oss_fuzz_path="/tmp/oss-fuzz",
            benchmarks_root="/tmp/benchmarks",
            per_pov_verify_timeout=180,
        )

        with (
            patch(
                "crsbench.distributed.evaluator.discover_registered_experiments",
                return_value=(MagicMock(), {"exp-42": reg}),
            ),
            patch("crsbench.evaluation.verification.pov.engine.VerificationEngine"),
            patch("crsbench.distributed.evaluator_jobs.set_engine"),
            patch("crsbench.distributed.evaluator_jobs.set_benchmarks_root"),
            patch(
                "crsbench.distributed.ci_supervisor.run_multi_queue_supervisor",
                return_value=0,
            ) as mock_supervisor,
        ):
            result = run_evaluator_configless(redis_host="localhost")

        assert result == 0
        kwargs = mock_supervisor.call_args.kwargs
        assert kwargs["build_cores_per_job"] == 4
        assert kwargs["verify_cores_per_job"] == 4

    @patch("crsbench.distributed.evaluator.REDIS_AVAILABLE", new=True)
    def test_configless_does_not_enqueue_startup_prebuilds(self) -> None:
        """Configless evaluator does not enqueue pre-builds at startup."""
        from crsbench.distributed.evaluator import run_evaluator_configless
        from crsbench.distributed.registry import RuntimeRegistration

        reg = RuntimeRegistration(
            experiment="exp-42",
            trial_queue="crsbench_exp-42",
            build_queue="crsbench_exp-42_build",
            verify_queue="crsbench_exp-42_verify",
            benchmarks=["afc-mock-full-01"],
            oss_fuzz_path="/tmp/oss-fuzz",
            benchmarks_root="/tmp/benchmarks",
            per_pov_verify_timeout=180,
        )

        with (
            patch(
                "crsbench.distributed.evaluator.discover_registered_experiments",
                return_value=(MagicMock(), {"exp-42": reg}),
            ),
            patch("crsbench.evaluation.verification.pov.engine.VerificationEngine"),
            patch("crsbench.distributed.evaluator_jobs.set_engine"),
            patch("crsbench.distributed.evaluator_jobs.set_benchmarks_root"),
            patch(
                "crsbench.distributed.evaluator._enqueue_pre_builds_from_registration",
                return_value=123,
            ) as mock_enqueue,
            patch(
                "crsbench.distributed.ci_supervisor.run_multi_queue_supervisor",
                return_value=0,
            ),
        ):
            result = run_evaluator_configless(redis_host="localhost")

        assert result == 0
        mock_enqueue.assert_not_called()

    @patch("crsbench.distributed.evaluator.REDIS_AVAILABLE", new=True)
    def test_configless_uses_first_metadata_for_conflicting_cpu_pinning(self) -> None:
        """Configless evaluator uses first metadata cores/skip and warns on conflicts."""
        from crsbench.distributed.evaluator import run_evaluator_configless
        from crsbench.distributed.registry import RuntimeRegistration

        reg1 = RuntimeRegistration(
            experiment="exp-a",
            trial_queue="crsbench_exp-a",
            build_queue="crsbench_exp-a_build",
            verify_queue="crsbench_exp-a_verify",
            benchmarks=[],
            oss_fuzz_path="/tmp/oss-fuzz",
            benchmarks_root="/tmp/benchmarks",
            per_pov_verify_timeout=180,
            evaluator_cores="32-47",
            evaluator_skip_cpus="33",
        )
        reg2 = RuntimeRegistration(
            experiment="exp-b",
            trial_queue="crsbench_exp-b",
            build_queue="crsbench_exp-b_build",
            verify_queue="crsbench_exp-b_verify",
            benchmarks=[],
            oss_fuzz_path="/tmp/oss-fuzz",
            benchmarks_root="/tmp/benchmarks",
            per_pov_verify_timeout=180,
            evaluator_cores="48-63",
            evaluator_skip_cpus="49",
        )

        with (
            patch(
                "crsbench.distributed.evaluator.discover_registered_experiments",
                return_value=(MagicMock(), {"exp-b": reg2, "exp-a": reg1}),
            ),
            patch("crsbench.evaluation.verification.pov.engine.VerificationEngine"),
            patch("crsbench.distributed.evaluator_jobs.set_engine"),
            patch("crsbench.distributed.evaluator_jobs.set_benchmarks_root"),
            patch(
                "crsbench.distributed.evaluator._enqueue_pre_builds_from_registration",
                return_value=0,
            ),
            patch(
                "crsbench.distributed.ci_supervisor.run_multi_queue_supervisor",
                return_value=0,
            ) as mock_supervisor,
            patch("crsbench.distributed.common.logger.warning") as mock_warning,
        ):
            result = run_evaluator_configless(redis_host="localhost")

        assert result == 0
        kwargs = mock_supervisor.call_args.kwargs
        assert kwargs["cores"] == "32-47"
        assert kwargs["skip_cpus"] == "33"
        assert mock_warning.called

    @patch("crsbench.distributed.evaluator.REDIS_AVAILABLE", new=True)
    def test_configless_rejects_invalid_verify_cores_metadata(self) -> None:
        """Configless evaluator fails fast on invalid verify cores metadata."""
        from crsbench.distributed.evaluator import run_evaluator_configless
        from crsbench.distributed.registry import RuntimeRegistration

        reg = RuntimeRegistration(
            experiment="exp-42",
            trial_queue="crsbench_exp-42",
            build_queue="crsbench_exp-42_build",
            verify_queue="crsbench_exp-42_verify",
            benchmarks=[],
            oss_fuzz_path="/tmp/oss-fuzz",
            benchmarks_root="/tmp/benchmarks",
            per_pov_verify_timeout=180,
            evaluator_verify_cores_per_job=0,
        )

        with (
            patch(
                "crsbench.distributed.evaluator.discover_registered_experiments",
                return_value=(MagicMock(), {"exp-42": reg}),
            ),
            patch("crsbench.evaluation.verification.pov.engine.VerificationEngine"),
            patch("crsbench.distributed.evaluator_jobs.set_engine"),
            patch("crsbench.distributed.evaluator_jobs.set_benchmarks_root"),
        ):
            result = run_evaluator_configless(redis_host="localhost")

        assert result == 1

    @patch("crsbench.distributed.evaluator.REDIS_AVAILABLE", new=True)
    def test_configless_rejects_conflicting_cpu_tag_metadata(self) -> None:
        """Configless evaluator fails when cpu_tag metadata conflicts across experiments."""
        from crsbench.distributed.evaluator import run_evaluator_configless
        from crsbench.distributed.registry import RuntimeRegistration

        reg1 = RuntimeRegistration(
            experiment="exp-a",
            trial_queue="crsbench_exp-a",
            build_queue="crsbench_exp-a_build",
            verify_queue="crsbench_exp-a_verify",
            benchmarks=[],
            oss_fuzz_path="/tmp/oss-fuzz",
            benchmarks_root="/tmp/benchmarks",
            per_pov_verify_timeout=180,
            evaluator_cpu_tag="cpu-a",
        )
        reg2 = RuntimeRegistration(
            experiment="exp-b",
            trial_queue="crsbench_exp-b",
            build_queue="crsbench_exp-b_build",
            verify_queue="crsbench_exp-b_verify",
            benchmarks=[],
            oss_fuzz_path="/tmp/oss-fuzz",
            benchmarks_root="/tmp/benchmarks",
            per_pov_verify_timeout=180,
            evaluator_cpu_tag="cpu-b",
        )

        with (
            patch(
                "crsbench.distributed.evaluator.discover_registered_experiments",
                return_value=(MagicMock(), {"exp-a": reg1, "exp-b": reg2}),
            ),
            patch("crsbench.evaluation.verification.pov.engine.VerificationEngine"),
            patch("crsbench.distributed.evaluator_jobs.set_engine"),
            patch("crsbench.distributed.evaluator_jobs.set_benchmarks_root"),
        ):
            result = run_evaluator_configless(redis_host="localhost")

        assert result == 1

    @patch("crsbench.distributed.evaluator.REDIS_AVAILABLE", new=True)
    def test_configless_normalizes_cpu_tag_metadata_before_conflict_check(self) -> None:
        """Whitespace-only cpu_tag differences are treated as equivalent."""
        from crsbench.distributed.evaluator import run_evaluator_configless
        from crsbench.distributed.registry import RuntimeRegistration

        reg1 = RuntimeRegistration(
            experiment="exp-a",
            trial_queue="crsbench_exp-a",
            build_queue="crsbench_exp-a_build",
            verify_queue="crsbench_exp-a_verify",
            benchmarks=[],
            oss_fuzz_path="/tmp/oss-fuzz",
            benchmarks_root="/tmp/benchmarks",
            per_pov_verify_timeout=180,
            evaluator_cpu_tag="x86-avx2",
        )
        reg2 = RuntimeRegistration(
            experiment="exp-b",
            trial_queue="crsbench_exp-b",
            build_queue="crsbench_exp-b_build",
            verify_queue="crsbench_exp-b_verify",
            benchmarks=[],
            oss_fuzz_path="/tmp/oss-fuzz",
            benchmarks_root="/tmp/benchmarks",
            per_pov_verify_timeout=180,
            evaluator_cpu_tag="  x86-avx2  ",
        )

        with (
            patch(
                "crsbench.distributed.evaluator.discover_registered_experiments",
                return_value=(MagicMock(), {"exp-a": reg1, "exp-b": reg2}),
            ),
            patch("crsbench.evaluation.verification.pov.engine.VerificationEngine"),
            patch("crsbench.distributed.evaluator_jobs.set_engine"),
            patch("crsbench.distributed.evaluator_jobs.set_benchmarks_root"),
            patch(
                "crsbench.distributed.ci_supervisor.run_multi_queue_supervisor",
                return_value=0,
            ) as mock_supervisor,
        ):
            result = run_evaluator_configless(redis_host="localhost")

        assert result == 0
        assert mock_supervisor.call_args.kwargs["cpu_tag"] == "x86-avx2"

    @patch("crsbench.distributed.evaluator.REDIS_AVAILABLE", new=True)
    def test_configless_rejects_negative_idle_timeout_metadata(self) -> None:
        """Configless evaluator fails fast on invalid idle timeout metadata."""
        from crsbench.distributed.evaluator import run_evaluator_configless
        from crsbench.distributed.registry import RuntimeRegistration

        reg = RuntimeRegistration(
            experiment="exp-42",
            trial_queue="crsbench_exp-42",
            build_queue="crsbench_exp-42_build",
            verify_queue="crsbench_exp-42_verify",
            benchmarks=[],
            oss_fuzz_path="/tmp/oss-fuzz",
            benchmarks_root="/tmp/benchmarks",
            per_pov_verify_timeout=180,
            evaluator_idle_timeout=-1,
        )

        with (
            patch(
                "crsbench.distributed.evaluator.discover_registered_experiments",
                return_value=(MagicMock(), {"exp-42": reg}),
            ),
            patch("crsbench.evaluation.verification.pov.engine.VerificationEngine"),
            patch("crsbench.distributed.evaluator_jobs.set_engine"),
            patch("crsbench.distributed.evaluator_jobs.set_benchmarks_root"),
        ):
            result = run_evaluator_configless(redis_host="localhost")

        assert result == 1

    @patch("crsbench.distributed.evaluator.REDIS_AVAILABLE", new=True)
    def test_configless_rejects_invalid_verify_cores_override(self) -> None:
        """Configless evaluator rejects non-positive verify core override."""
        from crsbench.distributed.evaluator import run_evaluator_configless
        from crsbench.distributed.registry import RuntimeRegistration

        reg = RuntimeRegistration(
            experiment="exp-42",
            trial_queue="crsbench_exp-42",
            build_queue="crsbench_exp-42_build",
            verify_queue="crsbench_exp-42_verify",
            benchmarks=[],
            oss_fuzz_path="/tmp/oss-fuzz",
            benchmarks_root="/tmp/benchmarks",
            per_pov_verify_timeout=180,
        )

        with patch(
            "crsbench.distributed.evaluator.discover_registered_experiments",
            return_value=(MagicMock(), {"exp-42": reg}),
        ):
            result = run_evaluator_configless(
                redis_host="localhost",
                verify_cores_per_job=0,
            )

        assert result == 1

    @patch("crsbench.distributed.evaluator.REDIS_AVAILABLE", new=False)
    def test_configless_returns_error_without_redis(self) -> None:
        """Returns error code when Redis is not available."""
        from crsbench.distributed.evaluator import run_evaluator_configless

        result = run_evaluator_configless()
        assert result == 1

    def test_enqueue_pre_builds_from_registration_exists(self) -> None:
        """_enqueue_pre_builds_from_registration helper exists."""
        from crsbench.distributed.evaluator import (
            _enqueue_pre_builds_from_registration,
        )

        assert callable(_enqueue_pre_builds_from_registration)

    @patch("rq.Queue")
    @patch("crsbench.distributed.queue.create_redis_connection")
    @patch("crsbench.distributed.ci_jobs.serialize_ci_job")
    @patch("crsbench.executor.variant_planner.VariantPlanner")
    @patch("crsbench.utils.benchmark_utils.filter_benchmarks_by_mode")
    def test_enqueue_pre_builds_normalizes_benchmarks_root_to_path(
        self,
        mock_filter: MagicMock,
        mock_planner_cls: MagicMock,
        mock_serialize: MagicMock,
        mock_create_redis: MagicMock,
        mock_queue_cls: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Configless pre-build helper should pass Path (not str) to mode filter."""
        from crsbench.distributed.evaluator import _enqueue_pre_builds_from_registration
        from crsbench.distributed.registry import RuntimeRegistration

        benchmark_name = "afc-mock-full-01"
        (tmp_path / benchmark_name).mkdir(parents=True, exist_ok=True)

        reg = RuntimeRegistration(
            experiment="exp-42",
            trial_queue="crsbench_exp-42",
            build_queue="crsbench_exp-42_build",
            verify_queue="crsbench_exp-42_verify",
            benchmarks=[benchmark_name],
            modes=["full"],
            oss_fuzz_path="/tmp/oss-fuzz",
            benchmarks_root=str(tmp_path),
            source_mode="pkgs",
            build_timeout=3600,
        )

        def _filter(names: list[str], _mode: str, root: Path) -> list[str]:
            assert isinstance(root, Path)
            return names

        mock_filter.side_effect = _filter
        mock_create_redis.return_value = MagicMock()
        mock_queue = MagicMock()
        mock_queue_cls.return_value = mock_queue
        mock_serialize.return_value = {"kind": "build"}

        mock_planner = MagicMock()
        job = MagicMock()
        job.job_id = "job-1"
        mock_planner.plan_builds.return_value = [job]
        mock_planner_cls.return_value = mock_planner

        enqueued = _enqueue_pre_builds_from_registration(
            reg,
            redis_host="localhost",
            benchmarks_root=str(tmp_path),
        )

        assert enqueued == 1
        mock_filter.assert_called_once()
        mock_planner.plan_builds.assert_called_once_with(
            tmp_path / benchmark_name,
            use_inc_build=False,
            skip_if_cached=True,
        )

    def test_evaluator_cli_configless_mode(self) -> None:
        """Evaluator CLI enters configless mode when no --ci or --experiment-config."""
        import argparse

        from crsbench.distributed.cli.evaluator_command import run_evaluator

        args = argparse.Namespace(
            experiment_config=None,
            ci=False,
            verbose=False,
            no_cpuset=True,
            cores=None,
            skip_cpus=None,
            cpu_tag=None,
            build_jobs=1,
            build_cores_per_job=None,
            verify_cores_per_job=None,
            verify_jobs=None,
            worker_name=None,
            idle_timeout=0,
            benchmarks_root=None,
        )

        with patch(
            "crsbench.distributed.evaluator.run_evaluator_configless",
            return_value=0,
        ) as mock_configless:
            result = run_evaluator(args)

        assert result == 0
        mock_configless.assert_called_once()

    def test_evaluator_cli_config_mode_uses_resources_cpu_tag(self) -> None:
        """Config-mode evaluator falls back to resources.cpu_tag when unset in evaluator block."""
        import argparse

        from crsbench.distributed.cli.evaluator_command import run_evaluator

        args = argparse.Namespace(
            experiment_config="test.yaml",
            ci=False,
            verbose=False,
            no_cpuset=True,
            cores=None,
            skip_cpus=None,
            cpu_tag=None,
            build_jobs=None,
            build_cores_per_job=None,
            verify_cores_per_job=None,
            verify_jobs=None,
            worker_name=None,
            idle_timeout=None,
            benchmarks_root=None,
        )

        with (
            patch(
                "crsbench.distributed.evaluator.run_evaluator_main", return_value=0
            ) as mock_main,
            patch("crsbench.run_experiment.load_experiment_config") as mock_load,
        ):
            mock_config = MagicMock()
            mock_config.experiment = "exp-1"
            mock_config.redis_host = "localhost"
            mock_config.evaluator = None
            mock_config.resources = MagicMock()
            mock_config.resources.cpu_tag = "x86-avx2"
            mock_load.return_value = mock_config

            result = run_evaluator(args)

        assert result == 0
        assert mock_main.call_args.kwargs["cpu_tag"] == "x86-avx2"

    def test_evaluator_cli_config_mode_whitespace_evaluator_cpu_tag_falls_back_to_resources(
        self,
    ) -> None:
        """Whitespace evaluator.cpu_tag should fall back to resources.cpu_tag."""
        import argparse

        from crsbench.distributed.cli.evaluator_command import run_evaluator

        args = argparse.Namespace(
            experiment_config="test.yaml",
            ci=False,
            verbose=False,
            no_cpuset=True,
            cores=None,
            skip_cpus=None,
            cpu_tag=None,
            build_jobs=None,
            build_cores_per_job=None,
            verify_cores_per_job=None,
            verify_jobs=None,
            worker_name=None,
            idle_timeout=None,
            benchmarks_root=None,
        )

        with (
            patch(
                "crsbench.distributed.evaluator.run_evaluator_main", return_value=0
            ) as mock_main,
            patch("crsbench.run_experiment.load_experiment_config") as mock_load,
        ):
            mock_evaluator = MagicMock()
            mock_evaluator.cpu_tag = "   "
            mock_evaluator.build_jobs = None
            mock_evaluator.build_cores_per_job = None
            mock_evaluator.verify_cores_per_job = None
            mock_evaluator.verify_jobs = None
            mock_evaluator.idle_timeout = None
            mock_evaluator.cores = None
            mock_evaluator.skip_cpus = None

            mock_config = MagicMock()
            mock_config.experiment = "exp-1"
            mock_config.redis_host = "localhost"
            mock_config.evaluator = mock_evaluator
            mock_config.resources = MagicMock()
            mock_config.resources.cpu_tag = "x86-avx2"
            mock_load.return_value = mock_config

            result = run_evaluator(args)

        assert result == 0
        assert mock_main.call_args.kwargs["cpu_tag"] == "x86-avx2"

    @patch("crsbench.distributed.evaluator.REDIS_AVAILABLE", new=True)
    def test_configless_evaluator_cpu_tag_whitespace_falls_back_to_resources_cpu_tag(
        self,
    ) -> None:
        """Whitespace evaluator_cpu_tag should defer to registration cpu_tag."""
        from crsbench.distributed.evaluator import run_evaluator_configless
        from crsbench.distributed.registry import RuntimeRegistration

        reg = RuntimeRegistration(
            experiment="exp-a",
            trial_queue="crsbench_exp-a",
            build_queue="crsbench_exp-a_build",
            verify_queue="crsbench_exp-a_verify",
            benchmarks=[],
            oss_fuzz_path="/tmp/oss-fuzz",
            benchmarks_root="/tmp/benchmarks",
            per_pov_verify_timeout=180,
            evaluator_cpu_tag="   ",
            cpu_tag="x86-avx2",
        )

        with (
            patch(
                "crsbench.distributed.evaluator.discover_registered_experiments",
                return_value=(MagicMock(), {"exp-a": reg}),
            ),
            patch("crsbench.evaluation.verification.pov.engine.VerificationEngine"),
            patch("crsbench.distributed.evaluator_jobs.set_engine"),
            patch("crsbench.distributed.evaluator_jobs.set_benchmarks_root"),
            patch(
                "crsbench.distributed.ci_supervisor.run_multi_queue_supervisor",
                return_value=0,
            ) as mock_supervisor,
        ):
            result = run_evaluator_configless(redis_host="localhost")

        assert result == 0
        assert mock_supervisor.call_args.kwargs["cpu_tag"] == "x86-avx2"


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


class TestRunEvaluatorCiMode:
    """Tests for evaluator CI mode defaults."""

    @patch("crsbench.distributed.evaluator.REDIS_AVAILABLE", new=True)
    @patch("crsbench.distributed.ci_supervisor.run_ci_supervisor")
    def test_ci_mode_defaults_build_verify_cores_to_four(
        self, mock_supervisor: MagicMock
    ) -> None:
        """CI mode defaults build/verify cores-per-job to 4."""
        from crsbench.distributed.evaluator import run_evaluator_ci_mode

        mock_supervisor.return_value = 0
        result = run_evaluator_ci_mode(redis_host="localhost")

        assert result == 0
        kwargs = mock_supervisor.call_args.kwargs
        assert kwargs["build_cores_per_job"] == 4
        assert kwargs["verify_cores_per_job"] == 4


class TestEvaluatorCliValidation:
    """Tests for evaluator CLI argument validation."""

    def test_build_jobs_rejects_zero(self) -> None:
        """--build-jobs must be >= 1."""
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="command")
        from crsbench.distributed.cli.evaluator_command import add_evaluator_subparser

        add_evaluator_subparser(subparsers)
        with pytest.raises(SystemExit):
            parser.parse_args(["evaluator", "--build-jobs", "0"])

    def test_verify_jobs_rejects_zero(self) -> None:
        """--verify-jobs must be >= 1."""
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="command")
        from crsbench.distributed.cli.evaluator_command import add_evaluator_subparser

        add_evaluator_subparser(subparsers)
        with pytest.raises(SystemExit):
            parser.parse_args(["evaluator", "--verify-jobs", "0"])

    def test_idle_timeout_rejects_negative(self) -> None:
        """--idle-timeout must be >= 0."""
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="command")
        from crsbench.distributed.cli.evaluator_command import add_evaluator_subparser

        add_evaluator_subparser(subparsers)
        with pytest.raises(SystemExit):
            parser.parse_args(["evaluator", "--idle-timeout", "-1"])
