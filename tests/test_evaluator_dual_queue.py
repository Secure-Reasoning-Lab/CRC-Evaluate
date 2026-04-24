"""Tests for evaluator dual-queue support.

Tests that:
1. run_evaluator_main() can skip startup pre-build when pre-build is disabled
2. Supervisor creates both build and verify queues
3. Evaluator passes distinct build and verify queue names to the supervisor
"""

import argparse
import builtins
import importlib
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestRunEvaluatorMain:
    """run_evaluator_main() tests."""

    def test_importing_evaluator_does_not_eagerly_import_dispatcher_warmup(
        self,
    ) -> None:
        """Shared/configless imports must not pull dispatcher-only warmup code."""
        sys.modules.pop("crsbench.distributed.evaluator", None)
        sys.modules.pop("crsbench.distributed.evaluator_warmup", None)

        importlib.import_module("crsbench.distributed.evaluator")

        assert "crsbench.distributed.evaluator_warmup" not in sys.modules

    def test_importing_evaluator_without_rq_keeps_import_safe(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Optional dispatcher deps must not break shared-mode evaluator import."""
        original_import = builtins.__import__

        def _guarded_import(
            name: str,
            globals: dict[str, object] | None = None,
            locals: dict[str, object] | None = None,
            fromlist: tuple[str, ...] = (),
            level: int = 0,
        ) -> object:
            if name == "rq" or name.startswith("rq."):
                raise ImportError("rq unavailable for test")
            return original_import(name, globals, locals, fromlist, level)

        monkeypatch.setattr(builtins, "__import__", _guarded_import)
        sys.modules.pop("crsbench.distributed.evaluator", None)
        sys.modules.pop("crsbench.distributed.evaluator_warmup", None)
        sys.modules.pop("crsbench.distributed.queue", None)
        sys.modules.pop("rq", None)
        sys.modules.pop("rq.job", None)

        evaluator = importlib.import_module("crsbench.distributed.evaluator")

        assert evaluator.REDIS_AVAILABLE is False
        assert "crsbench.distributed.evaluator_warmup" not in sys.modules

    @patch("crsbench.distributed.evaluator.REDIS_AVAILABLE", new=False)
    def test_returns_error_without_redis(self) -> None:
        """Returns error code when Redis is not available."""
        from crsbench.distributed.evaluator import run_evaluator_main

        config = MagicMock()
        result = run_evaluator_main(config, "exp-test")
        assert result == 1

    @patch("crsbench.distributed.evaluator.REDIS_AVAILABLE", new=True)
    @patch("crsbench.distributed.ci_supervisor.run_ci_supervisor")
    @patch("crsbench.distributed.evaluator.start_presence_thread")
    @patch("crsbench.distributed.evaluator_jobs.set_engine")
    def test_skips_phase1_builds(
        self,
        mock_set_engine: MagicMock,
        mock_start_presence_thread: MagicMock,
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
        mock_start_presence_thread.assert_not_called()
        assert result == 0

    @patch("crsbench.distributed.evaluator.REDIS_AVAILABLE", new=True)
    @patch("crsbench.distributed.ci_supervisor.run_ci_supervisor")
    @patch("crsbench.distributed.evaluator.start_dispatcher_warmup_thread")
    @patch("crsbench.distributed.evaluator.start_dispatcher_thread")
    @patch("crsbench.distributed.evaluator.create_redis_connection")
    @patch("crsbench.distributed.evaluator.start_presence_thread")
    @patch("crsbench.distributed.evaluator_jobs.set_engine")
    def test_uses_local_queues_in_dispatcher_mode(
        self,
        mock_set_engine: MagicMock,
        mock_start_presence_thread: MagicMock,
        mock_create_redis_connection: MagicMock,
        mock_start_dispatcher_thread: MagicMock,
        mock_start_dispatcher_warmup_thread: MagicMock,
        mock_supervisor: MagicMock,
        monkeypatch,
    ) -> None:
        """Dispatcher mode should route evaluator runtime to local queues."""
        from crsbench.distributed.evaluator import run_evaluator_main

        mock_supervisor.return_value = 0
        presence_stop = MagicMock()
        presence_thread = MagicMock()
        dispatcher_stop = MagicMock()
        dispatcher_thread = MagicMock()
        warmup_stop = MagicMock()
        warmup_thread = MagicMock()
        mock_start_presence_thread.return_value = (presence_stop, presence_thread)
        mock_create_redis_connection.return_value = MagicMock()
        mock_start_dispatcher_thread.return_value = (dispatcher_stop, dispatcher_thread)
        mock_start_dispatcher_warmup_thread.return_value = (warmup_stop, warmup_thread)
        monkeypatch.setenv("CRSBENCH_EVALUATOR_ROUTING_MODEL", "dispatcher")
        config = MagicMock()
        config.oss_fuzz_path = "/tmp/oss-fuzz"
        config.per_pov_verify_timeout = 180

        with patch("crsbench.evaluation.verification.pov.engine.VerificationEngine"):
            result = run_evaluator_main(config, "exp-test", worker_name="eval-1")

        assert result == 0
        kwargs = mock_supervisor.call_args.kwargs
        assert kwargs["build_queue_name"] == "crsbench_exp-test_eval-1_build"
        assert kwargs["verify_queue_name"] == "crsbench_exp-test_eval-1_verify"
        assert kwargs["worker_name"] == "eval-1"
        mock_start_presence_thread.assert_called_once_with(
            redis_host="localhost",
            experiment_name="exp-test",
            evaluator_id="eval-1",
            worker_name="eval-1",
        )
        mock_create_redis_connection.assert_called_once_with("localhost")
        mock_start_dispatcher_thread.assert_called_once()
        mock_start_dispatcher_warmup_thread.assert_called_once()
        warmup_kwargs = mock_start_dispatcher_warmup_thread.call_args.kwargs
        assert warmup_kwargs["build_jobs"] == 1
        assert warmup_kwargs["build_queue_name"] == "crsbench_exp-test_eval-1_build"
        presence_stop.set.assert_called_once()
        presence_thread.join.assert_called_once_with(timeout=1)
        dispatcher_stop.set.assert_called_once()
        dispatcher_thread.join.assert_called_once_with(timeout=1)
        warmup_stop.set.assert_called_once()
        warmup_thread.join.assert_called_once_with(timeout=1)

    @patch("crsbench.distributed.evaluator.REDIS_AVAILABLE", new=True)
    @patch("crsbench.distributed.evaluator._enqueue_pre_builds")
    @patch("crsbench.distributed.ci_supervisor.run_ci_supervisor")
    @patch("crsbench.distributed.evaluator.start_dispatcher_warmup_thread")
    @patch("crsbench.distributed.evaluator.start_dispatcher_thread")
    @patch("crsbench.distributed.evaluator.create_redis_connection")
    @patch("crsbench.distributed.evaluator.start_presence_thread")
    @patch("crsbench.distributed.evaluator_jobs.set_engine")
    def test_dispatcher_mode_uses_warmup_thread_instead_of_legacy_prebuilds(
        self,
        mock_set_engine: MagicMock,
        mock_start_presence_thread: MagicMock,
        mock_create_redis_connection: MagicMock,
        mock_start_dispatcher_thread: MagicMock,
        mock_start_dispatcher_warmup_thread: MagicMock,
        mock_supervisor: MagicMock,
        mock_enqueue_pre_builds: MagicMock,
        monkeypatch,
    ) -> None:
        from crsbench.distributed.evaluator import run_evaluator_main

        mock_supervisor.return_value = 0
        mock_start_presence_thread.return_value = (MagicMock(), MagicMock())
        mock_create_redis_connection.return_value = MagicMock()
        mock_start_dispatcher_thread.return_value = (MagicMock(), MagicMock())
        mock_start_dispatcher_warmup_thread.return_value = (MagicMock(), MagicMock())
        monkeypatch.setenv("CRSBENCH_EVALUATOR_ROUTING_MODEL", "dispatcher")
        config = MagicMock()
        config.oss_fuzz_path = "/tmp/oss-fuzz"
        config.per_pov_verify_timeout = 180

        with patch("crsbench.evaluation.verification.pov.engine.VerificationEngine"):
            result = run_evaluator_main(
                config,
                "exp-test",
                worker_name="eval-1",
                build_jobs=2,
            )

        assert result == 0
        mock_start_dispatcher_warmup_thread.assert_called_once()
        assert mock_start_dispatcher_warmup_thread.call_args.kwargs["build_jobs"] == 2
        mock_enqueue_pre_builds.assert_not_called()

    def test_no_build_workers_parameter(self) -> None:
        """run_evaluator_main no longer has build_workers parameter."""
        import inspect

        from crsbench.distributed.evaluator import run_evaluator_main

        sig = inspect.signature(run_evaluator_main)
        assert "build_workers" not in sig.parameters

    @patch("crsbench.distributed.evaluator.REDIS_AVAILABLE", new=True)
    @patch("crsbench.distributed.ci_supervisor.run_ci_supervisor")
    def test_leaves_build_verify_cores_unset_in_config_mode(
        self,
        mock_supervisor: MagicMock,
    ) -> None:
        """Config mode should not inject build/verify CPU-per-job defaults."""
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
        assert kwargs["build_cores_per_job"] is None
        assert kwargs["verify_cores_per_job"] is None
        assert kwargs["progress_log_every_jobs"] == 50

    @patch("crsbench.distributed.evaluator.REDIS_AVAILABLE", new=True)
    @patch("crsbench.distributed.evaluator._report_cloud_runtime_state")
    @patch("crsbench.distributed.ci_supervisor.run_ci_supervisor")
    def test_reports_cloud_readiness_before_starting_supervisor(
        self,
        mock_supervisor: MagicMock,
        mock_report_state: MagicMock,
    ) -> None:
        """Cloud-managed evaluators should promote readiness before gating trials."""
        from crsbench.distributed.evaluator import run_evaluator_main

        mock_supervisor.return_value = 0
        config = MagicMock()
        config.oss_fuzz_path = "/tmp/oss-fuzz"
        config.per_pov_verify_timeout = 180

        with (
            patch("crsbench.evaluation.verification.pov.engine.VerificationEngine"),
            patch("crsbench.distributed.evaluator_jobs.set_engine"),
        ):
            result = run_evaluator_main(
                config,
                "exp-test",
                redis_host="redis.internal:6379",
            )

        assert result == 0
        assert mock_report_state.call_args_list == [
            (
                ("redis.internal:6379",),
                {
                    "state": "registering",
                    "detail": "Preparing evaluator runtime for build and verify queues",
                },
            ),
            (
                ("redis.internal:6379",),
                {
                    "state": "ready",
                    "detail": "Evaluator supervisor managing build and verify queues",
                },
            ),
        ]

    @patch("crsbench.distributed.evaluator.REDIS_AVAILABLE", new=True)
    def test_rejects_invalid_redis_host(self) -> None:
        """run_evaluator_main should reject invalid redis host values."""
        from crsbench.distributed.evaluator import run_evaluator_main

        config = MagicMock()
        result = run_evaluator_main(config, "exp-test", redis_host=" none ")
        assert result == 1


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
    def test_passes_queue_names_by_role(
        self,
        mock_set_engine: MagicMock,
        mock_supervisor: MagicMock,
    ) -> None:
        """Evaluator passes build and verify queue names by explicit role."""
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
    def test_configless_rejects_conflicting_inc_image_settings(self) -> None:
        """Configless evaluator fails when inc-image settings conflict."""
        from crsbench.distributed.evaluator import run_evaluator_configless
        from crsbench.distributed.registry import RuntimeRegistration

        reg1 = RuntimeRegistration(
            experiment="exp-a",
            trial_queue="crsbench_exp-a",
            build_queue="crsbench_exp-a_build",
            verify_queue="crsbench_exp-a_verify",
            benchmarks=[],
            benchmarks_root="/tmp/benchmarks",
            per_pov_verify_timeout=180,
            inc_image_policy="auto",
        )
        reg2 = RuntimeRegistration(
            experiment="exp-b",
            trial_queue="crsbench_exp-b",
            build_queue="crsbench_exp-b_build",
            verify_queue="crsbench_exp-b_verify",
            benchmarks=[],
            benchmarks_root="/tmp/benchmarks",
            per_pov_verify_timeout=180,
            inc_image_policy="build_only",
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
    def test_configless_rejects_dispatcher_mode(self, monkeypatch) -> None:
        """Dispatcher routing is not supported in configless evaluator mode."""
        from crsbench.distributed.evaluator import run_evaluator_configless

        monkeypatch.setenv("CRSBENCH_EVALUATOR_ROUTING_MODEL", "dispatcher")

        with patch(
            "crsbench.distributed.evaluator.discover_registered_experiments",
            side_effect=AssertionError("configless discovery should be rejected"),
        ):
            result = run_evaluator_configless(redis_host="redis")

        assert result == 1

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
        assert call_kwargs["progress_log_every_jobs"] == 50

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
    def test_configless_leaves_build_verify_cores_unset_for_supervisor(self) -> None:
        """Configless evaluator leaves build/verify cores unset for supervisor sizing."""
        from crsbench.distributed.evaluator import run_evaluator_configless
        from crsbench.distributed.registry import RuntimeRegistration

        reg = RuntimeRegistration(
            experiment="exp-42",
            trial_queue="crsbench_exp-42",
            build_queue="crsbench_exp-42_build",
            verify_queue="crsbench_exp-42_verify",
            benchmarks=[],
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
        assert kwargs["build_cores_per_job"] is None
        assert kwargs["verify_cores_per_job"] is None

    @patch("crsbench.distributed.evaluator.REDIS_AVAILABLE", new=True)
    def test_configless_sizes_verify_cpu_width_from_cli_verify_jobs(self) -> None:
        """Explicit verify_jobs must drive auto-sized verify CPU width."""
        from crsbench.distributed.evaluator import run_evaluator_configless
        from crsbench.distributed.registry import RuntimeRegistration

        reg = RuntimeRegistration(
            experiment="exp-42",
            trial_queue="crsbench_exp-42",
            build_queue="crsbench_exp-42_build",
            verify_queue="crsbench_exp-42_verify",
            benchmarks=[],
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
                "crsbench.distributed.evaluator.auto_cores_per_job",
                side_effect=[4, 2],
            ) as mock_auto_cores_per_job,
            patch(
                "crsbench.distributed.ci_supervisor.run_multi_queue_supervisor",
                return_value=0,
            ),
        ):
            result = run_evaluator_configless(
                redis_host="localhost",
                use_cpuset=True,
                build_jobs=2,
                verify_jobs=3,
            )

        assert result == 0
        assert mock_auto_cores_per_job.call_args_list[0].args[0] == 2
        assert mock_auto_cores_per_job.call_args_list[1].args[0] == 3

    @patch("crsbench.distributed.evaluator.REDIS_AVAILABLE", new=True)
    def test_configless_refresh_keeps_cli_verify_jobs_override_compatible(
        self,
    ) -> None:
        """Refresh must not reject experiments when CLI verify_jobs is lower than auto(build_jobs)."""
        from crsbench.distributed.evaluator import run_evaluator_configless
        from crsbench.distributed.registry import RuntimeRegistration

        reg = RuntimeRegistration(
            experiment="exp-42",
            trial_queue="crsbench_exp-42",
            build_queue="crsbench_exp-42_build",
            verify_queue="crsbench_exp-42_verify",
            benchmarks=[],
            benchmarks_root="/tmp/benchmarks",
            per_pov_verify_timeout=180,
            evaluator_build_jobs=2,
        )

        def _run_supervisor(**kwargs):
            refresher = kwargs["queue_refresher"]
            with patch("crsbench.distributed.registry.RegistryClient") as mock_registry:
                mock_client = MagicMock()
                mock_client.list_experiments.return_value = {"exp-42": reg}
                mock_registry.return_value = mock_client
                refreshed_build, refreshed_verify = refresher(MagicMock())
            assert refreshed_build == ["crsbench_exp-42_build"]
            assert refreshed_verify == ["crsbench_exp-42_verify"]
            return 0

        with (
            patch(
                "crsbench.distributed.evaluator.discover_registered_experiments",
                return_value=(MagicMock(), {"exp-42": reg}),
            ),
            patch("crsbench.evaluation.verification.pov.engine.VerificationEngine"),
            patch("crsbench.distributed.evaluator_jobs.set_engine"),
            patch("crsbench.distributed.evaluator_jobs.set_benchmarks_root"),
            patch(
                "crsbench.distributed.evaluator.auto_cores_per_job",
                side_effect=[4, 2],
            ),
            patch(
                "crsbench.distributed.ci_supervisor.run_multi_queue_supervisor",
                side_effect=_run_supervisor,
            ),
        ):
            result = run_evaluator_configless(
                redis_host="localhost",
                use_cpuset=True,
                build_jobs=2,
                verify_jobs=3,
            )

        assert result == 0

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
    def test_configless_uses_cli_cpu_pinning_only(self) -> None:
        """Configless evaluator uses CLI cpuset/skip-cpuset (no metadata pinning)."""
        from crsbench.distributed.evaluator import run_evaluator_configless
        from crsbench.distributed.registry import RuntimeRegistration

        reg1 = RuntimeRegistration(
            experiment="exp-a",
            trial_queue="crsbench_exp-a",
            build_queue="crsbench_exp-a_build",
            verify_queue="crsbench_exp-a_verify",
            benchmarks=[],
            benchmarks_root="/tmp/benchmarks",
            per_pov_verify_timeout=180,
        )
        reg2 = RuntimeRegistration(
            experiment="exp-b",
            trial_queue="crsbench_exp-b",
            build_queue="crsbench_exp-b_build",
            verify_queue="crsbench_exp-b_verify",
            benchmarks=[],
            benchmarks_root="/tmp/benchmarks",
            per_pov_verify_timeout=180,
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
        ):
            result = run_evaluator_configless(
                redis_host="localhost",
                cores="32-47",
                skip_cpus="33",
            )

        assert result == 0
        kwargs = mock_supervisor.call_args.kwargs
        assert kwargs["cores"] == "32-47"
        assert kwargs["skip_cpus"] == "33"

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

    @patch("crsbench.distributed.evaluator.REDIS_AVAILABLE", new=True)
    def test_configless_refresh_skips_incompatible_resource_profile(self) -> None:
        """Refresh should not adopt experiments requiring incompatible evaluator profile."""
        from crsbench.distributed.evaluator import run_evaluator_configless
        from crsbench.distributed.registry import RuntimeRegistration

        initial_reg = RuntimeRegistration(
            experiment="exp-a",
            trial_queue="crsbench_exp-a",
            build_queue="crsbench_exp-a_build",
            verify_queue="crsbench_exp-a_verify",
            benchmarks=[],
            benchmarks_root="/tmp/benchmarks",
            per_pov_verify_timeout=180,
            evaluator_verify_cores_per_job=4,
            evaluator_cpu_tag="x86-avx2",
        )
        compatible_reg = RuntimeRegistration(
            experiment="exp-b",
            trial_queue="crsbench_exp-b",
            build_queue="crsbench_exp-b_build",
            verify_queue="crsbench_exp-b_verify",
            benchmarks=[],
            benchmarks_root="/tmp/benchmarks",
            per_pov_verify_timeout=180,
            evaluator_verify_cores_per_job=4,
            evaluator_cpu_tag="x86-avx2",
        )
        incompatible_reg = RuntimeRegistration(
            experiment="exp-c",
            trial_queue="crsbench_exp-c",
            build_queue="crsbench_exp-c_build",
            verify_queue="crsbench_exp-c_verify",
            benchmarks=[],
            benchmarks_root="/tmp/benchmarks",
            per_pov_verify_timeout=180,
            evaluator_verify_cores_per_job=8,
            evaluator_cpu_tag="x86-avx2",
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
            assert refreshed_build == ["crsbench_exp-a_build", "crsbench_exp-b_build"]
            assert refreshed_verify == [
                "crsbench_exp-a_verify",
                "crsbench_exp-b_verify",
            ]
            return 0

        with (
            patch(
                "crsbench.distributed.evaluator.discover_registered_experiments",
                return_value=(MagicMock(), {"exp-a": initial_reg}),
            ),
            patch("crsbench.evaluation.verification.pov.engine.VerificationEngine"),
            patch("crsbench.distributed.evaluator_jobs.set_engine"),
            patch("crsbench.distributed.evaluator_jobs.set_benchmarks_root"),
            patch(
                "crsbench.distributed.ci_supervisor.run_multi_queue_supervisor",
                side_effect=_run_supervisor,
            ),
            patch("crsbench.distributed.evaluator.logger.warning") as mock_warning,
        ):
            result = run_evaluator_configless(redis_host="localhost")

        assert result == 0
        assert any(
            "incompatible evaluator resource profile" in str(call.args[0])
            for call in mock_warning.call_args_list
        )

    @patch("crsbench.distributed.evaluator.REDIS_AVAILABLE", new=True)
    def test_configless_refresh_keeps_untagged_experiment_with_tagged_evaluator(
        self,
    ) -> None:
        """Untagged experiments should remain compatible during evaluator refresh."""
        from crsbench.distributed.evaluator import run_evaluator_configless
        from crsbench.distributed.registry import RuntimeRegistration

        initial_reg = RuntimeRegistration(
            experiment="exp-a",
            trial_queue="crsbench_exp-a",
            build_queue="crsbench_exp-a_build",
            verify_queue="crsbench_exp-a_verify",
            benchmarks=[],
            benchmarks_root="/tmp/benchmarks",
            per_pov_verify_timeout=180,
            evaluator_verify_cores_per_job=4,
            evaluator_cpu_tag="x86-avx2",
        )
        untagged_reg = RuntimeRegistration(
            experiment="exp-b",
            trial_queue="crsbench_exp-b",
            build_queue="crsbench_exp-b_build",
            verify_queue="crsbench_exp-b_verify",
            benchmarks=[],
            benchmarks_root="/tmp/benchmarks",
            per_pov_verify_timeout=180,
            evaluator_verify_cores_per_job=4,
            evaluator_cpu_tag=None,
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
            assert refreshed_build == ["crsbench_exp-a_build", "crsbench_exp-b_build"]
            assert refreshed_verify == [
                "crsbench_exp-a_verify",
                "crsbench_exp-b_verify",
            ]
            return 0

        with (
            patch(
                "crsbench.distributed.evaluator.discover_registered_experiments",
                return_value=(MagicMock(), {"exp-a": initial_reg}),
            ),
            patch("crsbench.evaluation.verification.pov.engine.VerificationEngine"),
            patch("crsbench.distributed.evaluator_jobs.set_engine"),
            patch("crsbench.distributed.evaluator_jobs.set_benchmarks_root"),
            patch(
                "crsbench.distributed.ci_supervisor.run_multi_queue_supervisor",
                side_effect=_run_supervisor,
            ),
        ):
            result = run_evaluator_configless(redis_host="localhost")

        assert result == 0

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
            use_inc_build=True,
            skip_if_cached=True,
            inc_image_policy="auto",
            inc_image_registry="ghcr.io/team-atlanta/crsbench",
            inc_image_max_pull_bytes=10 * 1024 * 1024 * 1024,
            inc_image_pull_timeout=300,
            local_image_prefix="crsbench",
        )

    @patch("rq.Queue")
    @patch("crsbench.distributed.queue.create_redis_connection")
    @patch("crsbench.distributed.ci_jobs.serialize_ci_job")
    @patch("crsbench.executor.variant_planner.VariantPlanner")
    @patch("crsbench.utils.benchmark_utils.filter_benchmarks_by_mode")
    def test_enqueue_pre_builds_from_registration_propagates_inc_image_settings(
        self,
        mock_filter: MagicMock,
        mock_planner_cls: MagicMock,
        mock_serialize: MagicMock,
        mock_create_redis: MagicMock,
        mock_queue_cls: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Configless pre-build helper forwards registration inc-image settings."""
        from crsbench.distributed.evaluator import _enqueue_pre_builds_from_registration
        from crsbench.distributed.registry import RuntimeRegistration

        benchmark_name = "afc-mock-full-02"
        (tmp_path / benchmark_name).mkdir(parents=True, exist_ok=True)

        reg = RuntimeRegistration(
            experiment="exp-43",
            trial_queue="crsbench_exp-43",
            build_queue="crsbench_exp-43_build",
            verify_queue="crsbench_exp-43_verify",
            benchmarks=[benchmark_name],
            modes=["full"],
            benchmarks_root=str(tmp_path),
            source_mode="pkgs",
            build_timeout=3600,
            inc_image_policy="pull_only",
            inc_image_registry="ghcr.io/example/custom",
            inc_image_max_pull_bytes=123456,
            inc_image_pull_timeout_sec=77,
            local_image_prefix="custom-prefix",
        )

        mock_filter.side_effect = lambda names, _mode, _root: names
        mock_create_redis.return_value = MagicMock()
        mock_queue_cls.return_value = MagicMock()
        mock_serialize.return_value = {"kind": "build"}

        mock_planner = MagicMock()
        job = MagicMock()
        job.job_id = "job-2"
        mock_planner.plan_builds.return_value = [job]
        mock_planner_cls.return_value = mock_planner

        enqueued = _enqueue_pre_builds_from_registration(
            reg,
            redis_host="localhost",
            benchmarks_root=str(tmp_path),
        )

        assert enqueued == 1
        mock_planner.plan_builds.assert_called_once_with(
            tmp_path / benchmark_name,
            use_inc_build=True,
            skip_if_cached=True,
            inc_image_policy="pull_only",
            inc_image_registry="ghcr.io/example/custom",
            inc_image_max_pull_bytes=123456,
            inc_image_pull_timeout=77,
            local_image_prefix="custom-prefix",
        )

    @patch("rq.Queue")
    @patch("crsbench.distributed.queue.create_redis_connection")
    @patch("crsbench.distributed.ci_jobs.serialize_ci_job")
    @patch("crsbench.executor.variant_planner.VariantPlanner")
    @patch("crsbench.distributed.evaluator.filter_benchmarks_by_mode")
    def test_enqueue_pre_builds_propagates_inc_image_settings(
        self,
        mock_filter: MagicMock,
        mock_planner_cls: MagicMock,
        mock_serialize: MagicMock,
        mock_create_redis: MagicMock,
        mock_queue_cls: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Config-mode pre-build helper forwards resolved inc-image settings."""
        from types import SimpleNamespace

        from crsbench.distributed.evaluator import _enqueue_pre_builds

        benchmark_name = "afc-mock-full-03"
        (tmp_path / benchmark_name).mkdir(parents=True, exist_ok=True)

        config = SimpleNamespace(
            benchmarks_root=tmp_path,
            mode=SimpleNamespace(value="full"),
            resources=SimpleNamespace(cpu_tag=None),
            inc_image_policy="pull_only",
            inc_image_registry="ghcr.io/example/custom",
            inc_image_max_pull_bytes=123456,
            inc_image_pull_timeout_sec=77,
            project_image_prefix="custom-prefix",
            get_benchmark_list=lambda: [benchmark_name],
        )

        mock_filter.side_effect = lambda names, _mode, _root: names
        mock_create_redis.return_value = MagicMock()
        mock_queue_cls.return_value = MagicMock()
        mock_serialize.return_value = {"kind": "build"}

        mock_planner = MagicMock()
        job = MagicMock()
        job.job_id = "job-3"
        mock_planner.plan_builds.return_value = [job]
        mock_planner_cls.return_value = mock_planner

        enqueued = _enqueue_pre_builds(
            config,
            experiment_name="exp-44",
            redis_host="localhost",
            inc_image_policy="build_only",
            inc_image_registry="ghcr.io/example/resolved",
            inc_image_max_pull_bytes=654321,
            inc_image_pull_timeout=91,
            local_image_prefix="resolved-prefix",
        )

        assert enqueued == 1
        mock_planner.plan_builds.assert_called_once_with(
            tmp_path / benchmark_name,
            use_inc_build=True,
            skip_if_cached=True,
            inc_image_policy="build_only",
            inc_image_registry="ghcr.io/example/resolved",
            inc_image_max_pull_bytes=654321,
            inc_image_pull_timeout=91,
            local_image_prefix="resolved-prefix",
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

    def test_evaluator_cli_config_mode_derives_verify_jobs_when_unset(self) -> None:
        """Config mode derives verify_jobs from build concurrency when fully unset."""
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
            build_jobs=2,
            build_cores_per_job=8,
            verify_cores_per_job=2,
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
            mock_config.resources = None
            mock_load.return_value = mock_config

            result = run_evaluator(args)

        assert result == 0
        assert mock_main.call_args.kwargs["verify_jobs"] == 8

    def test_evaluator_cli_config_mode_unified_cli_drives_both_queues(self) -> None:
        """Unified evaluator CLI flags apply to both build and verify by default."""
        import argparse

        from crsbench.distributed.cli.evaluator_command import run_evaluator

        args = argparse.Namespace(
            experiment_config="test.yaml",
            ci=False,
            verbose=False,
            cpuset=None,
            skip_cpuset=None,
            cpu_tag=None,
            jobs=3,
            cores_per_job=5,
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
            mock_config.resources = None
            mock_load.return_value = mock_config

            result = run_evaluator(args)

        assert result == 0
        kwargs = mock_main.call_args.kwargs
        assert kwargs["build_jobs"] == 3
        assert kwargs["build_cores_per_job"] == 5
        assert kwargs["verify_jobs"] == 3
        assert kwargs["verify_cores_per_job"] == 5

    def test_evaluator_cli_config_mode_prefers_unified_jobs_for_verify_fallback(
        self,
    ) -> None:
        """Config mode uses evaluator.jobs for verify_jobs when verify split override is unset."""
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
            verify_cores_per_job=1,
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
            mock_evaluator.jobs = 3
            mock_evaluator.cores_per_job = 4
            mock_evaluator.build_jobs = None
            mock_evaluator.build_cores_per_job = None
            mock_evaluator.verify_jobs = None
            mock_evaluator.verify_cores_per_job = None
            mock_evaluator.idle_timeout = None
            mock_evaluator.cpuset = None
            mock_evaluator.skip_cpuset = None
            mock_evaluator.cpu_tag = None

            mock_config = MagicMock()
            mock_config.experiment = "exp-1"
            mock_config.redis_host = "localhost"
            mock_config.evaluator = mock_evaluator
            mock_config.resources = None
            mock_load.return_value = mock_config

            result = run_evaluator(args)

        assert result == 0
        assert mock_main.call_args.kwargs["verify_jobs"] == 3

    def test_evaluator_cli_config_mode_prefers_verify_split_override_over_unified_jobs(
        self,
    ) -> None:
        """Config mode uses evaluator.verify_jobs before evaluator.jobs fallback."""
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
            mock_evaluator.jobs = 3
            mock_evaluator.cores_per_job = 4
            mock_evaluator.build_jobs = None
            mock_evaluator.build_cores_per_job = None
            mock_evaluator.verify_jobs = 7
            mock_evaluator.verify_cores_per_job = None
            mock_evaluator.idle_timeout = None
            mock_evaluator.cpuset = None
            mock_evaluator.skip_cpuset = None
            mock_evaluator.cpu_tag = None

            mock_config = MagicMock()
            mock_config.experiment = "exp-1"
            mock_config.redis_host = "localhost"
            mock_config.evaluator = mock_evaluator
            mock_config.resources = None
            mock_load.return_value = mock_config

            result = run_evaluator(args)

        assert result == 0
        assert mock_main.call_args.kwargs["verify_jobs"] == 7

    def test_evaluator_cli_config_mode_uses_env_redis_host_fallback(self) -> None:
        """Config-mode evaluator should honor CRSBENCH_REDIS_HOST fallback."""
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
            patch.dict("os.environ", {"CRSBENCH_REDIS_HOST": "redis-env"}, clear=False),
        ):
            mock_config = MagicMock()
            mock_config.experiment = "exp-1"
            mock_config.redis_host = None
            mock_config.evaluator = None
            mock_config.resources = None
            mock_load.return_value = mock_config

            result = run_evaluator(args)

        assert result == 0
        assert mock_main.call_args.kwargs["redis_host"] == "redis-env"

    def test_evaluator_cli_config_mode_rejects_empty_env_redis_host(self) -> None:
        """Config-mode evaluator should reject explicitly empty env redis host."""
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
            patch.dict("os.environ", {"CRSBENCH_REDIS_HOST": ""}, clear=False),
        ):
            mock_config = MagicMock()
            mock_config.experiment = "exp-1"
            mock_config.redis_host = None
            mock_config.evaluator = None
            mock_config.resources = None
            mock_load.return_value = mock_config

            result = run_evaluator(args)

        assert result == 1
        mock_main.assert_not_called()

    def test_evaluator_cli_config_mode_blank_config_redis_uses_env_fallback(
        self,
    ) -> None:
        """Blank config redis_host should defer to env redis host."""
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
            patch.dict("os.environ", {"CRSBENCH_REDIS_HOST": "redis-env"}, clear=False),
        ):
            mock_config = MagicMock()
            mock_config.experiment = "exp-1"
            mock_config.redis_host = "   "
            mock_config.evaluator = None
            mock_config.resources = None
            mock_load.return_value = mock_config

            result = run_evaluator(args)

        assert result == 0
        assert mock_main.call_args.kwargs["redis_host"] == "redis-env"

    def test_evaluator_cli_config_mode_env_redis_overrides_config_value(self) -> None:
        """Cloud runtime env redis host should override config-mode placeholder values."""
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
            patch.dict(
                "os.environ",
                {"CRSBENCH_REDIS_HOST": "10.202.0.17:6379"},
                clear=False,
            ),
        ):
            mock_config = MagicMock()
            mock_config.experiment = "exp-1"
            mock_config.redis_host = "redis-server:6379"
            mock_config.evaluator = None
            mock_config.resources = None
            mock_load.return_value = mock_config

            result = run_evaluator(args)

        assert result == 0
        assert mock_main.call_args.kwargs["redis_host"] == "10.202.0.17:6379"

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
            mock_evaluator.cpuset = None
            mock_evaluator.skip_cpuset = None

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

    def test_evaluator_cli_configless_rejects_none_env_redis_host(self) -> None:
        """Configless evaluator should fail fast on empty/none redis host."""
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

        with (
            patch(
                "crsbench.distributed.evaluator.run_evaluator_configless",
                return_value=0,
            ) as mock_configless,
            patch.dict("os.environ", {"CRSBENCH_REDIS_HOST": "none"}, clear=False),
        ):
            result = run_evaluator(args)

        assert result == 1
        mock_configless.assert_not_called()

    def test_evaluator_cli_ci_mode_rejects_none_env_redis_host(self) -> None:
        """CI evaluator should fail fast on empty/none redis host."""
        import argparse

        from crsbench.distributed.cli.evaluator_command import run_evaluator

        args = argparse.Namespace(
            experiment_config=None,
            ci=True,
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

        with (
            patch(
                "crsbench.distributed.evaluator.run_evaluator_ci_mode", return_value=0
            ) as mock_ci,
            patch.dict("os.environ", {"CRSBENCH_REDIS_HOST": "   "}, clear=False),
        ):
            result = run_evaluator(args)

        assert result == 1
        mock_ci.assert_not_called()


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


class TestIncImageRuntimeSettings:
    """Tests for inc-image runtime settings normalization."""

    def test_resolve_settings_sets_none_pull_cap_when_unset(self) -> None:
        """No pull-cap input should resolve to None."""
        from crsbench.distributed.evaluator import _resolve_inc_image_runtime_settings

        (
            _policy,
            _registry,
            resolved_max_pull_bytes,
            _pull_timeout,
            _local_prefix,
        ) = _resolve_inc_image_runtime_settings(
            policy="auto",
            registry="ghcr.io/team-atlanta/crsbench",
            max_pull_bytes=None,
            pull_timeout_sec=300,
            local_prefix="crsbench",
        )
        assert resolved_max_pull_bytes is None


class TestRunEvaluatorCiMode:
    """Tests for evaluator CI mode defaults."""

    @patch("crsbench.distributed.evaluator.run_evaluator_configless")
    def test_ci_mode_uses_configless_compat_alias(
        self, mock_configless: MagicMock
    ) -> None:
        """CI mode should delegate to configless with legacy alias enabled."""
        from crsbench.distributed.evaluator import run_evaluator_ci_mode

        mock_configless.return_value = 0
        result = run_evaluator_ci_mode(redis_host="localhost")

        assert result == 0
        kwargs = mock_configless.call_args.kwargs
        assert kwargs["redis_host"] == "localhost"
        assert kwargs["legacy_ci_alias"] is True

    def test_legacy_ci_alias_leaves_build_verify_cores_unset(self) -> None:
        """Configless legacy CI alias leaves build/verify CPU width unset."""
        from crsbench.distributed.evaluator import run_evaluator_configless

        with (
            patch("crsbench.distributed.evaluator.REDIS_AVAILABLE", new=True),
            patch("crsbench.evaluation.verification.pov.engine.VerificationEngine"),
            patch("crsbench.distributed.evaluator_jobs.set_engine"),
            patch("crsbench.distributed.evaluator_jobs.set_benchmarks_root"),
            patch(
                "crsbench.distributed.ci_supervisor.run_multi_queue_supervisor"
            ) as mock_supervisor,
        ):
            mock_supervisor.return_value = 0
            result = run_evaluator_configless(
                redis_host="localhost", legacy_ci_alias=True
            )

        assert result == 0
        kwargs = mock_supervisor.call_args.kwargs
        assert kwargs["build_cores_per_job"] is None
        assert kwargs["verify_cores_per_job"] is None

    def test_run_evaluator_configless_rejects_none_redis_host(self) -> None:
        from crsbench.distributed.evaluator import run_evaluator_configless

        result = run_evaluator_configless(redis_host="none")
        assert result == 1

    def test_run_evaluator_ci_mode_rejects_none_redis_host(self) -> None:
        from crsbench.distributed.evaluator import run_evaluator_ci_mode

        result = run_evaluator_ci_mode(redis_host="   ")
        assert result == 1


class TestEvaluatorCliValidation:
    """Tests for evaluator CLI argument validation."""

    def test_jobs_rejects_zero(self) -> None:
        """--jobs must be >= 1."""
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="command")
        from crsbench.distributed.cli.evaluator_command import add_evaluator_subparser

        add_evaluator_subparser(subparsers)
        with pytest.raises(SystemExit):
            parser.parse_args(["evaluator", "--jobs", "0"])

    def test_cli_configless_rejects_dispatcher_mode(self, monkeypatch) -> None:
        """Dispatcher routing should require --experiment-config at the CLI."""
        from crsbench.distributed.cli.evaluator_command import run_evaluator

        monkeypatch.setenv("CRSBENCH_EVALUATOR_ROUTING_MODEL", "dispatcher")
        args = argparse.Namespace(
            experiment_config=None,
            ci=False,
            verbose=False,
            cpuset=None,
            skip_cpuset=None,
            cpu_tag=None,
            jobs=None,
            cores_per_job=None,
            build_jobs=None,
            build_cores_per_job=None,
            verify_cores_per_job=None,
            verify_jobs=None,
            worker_name=None,
            idle_timeout=None,
            benchmarks_root=None,
        )

        with patch(
            "crsbench.distributed.evaluator.run_evaluator_configless",
            side_effect=AssertionError("dispatcher configless should be rejected"),
        ) as mock_configless:
            result = run_evaluator(args)

        assert result == 1
        mock_configless.assert_not_called()

    def test_cli_configless_mode_keeps_routing_env_unset(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Configless evaluator CLI should not inject the focused dispatcher default."""
        from crsbench.distributed.cli.evaluator_command import run_evaluator
        from crsbench.distributed.queue import EVALUATOR_ROUTING_MODEL_ENV

        monkeypatch.delenv(EVALUATOR_ROUTING_MODEL_ENV, raising=False)
        args = argparse.Namespace(
            experiment_config=None,
            ci=False,
            verbose=False,
            cpuset=None,
            skip_cpuset=None,
            cpu_tag=None,
            jobs=None,
            cores_per_job=None,
            build_jobs=None,
            build_cores_per_job=None,
            verify_cores_per_job=None,
            verify_jobs=None,
            worker_name=None,
            idle_timeout=None,
            benchmarks_root=None,
        )

        def _assert_configless(*_args, **_kwargs) -> int:
            assert os.environ.get(EVALUATOR_ROUTING_MODEL_ENV) is None
            return 0

        with (
            patch(
                "crsbench.distributed.common.normalize_redis_host",
                side_effect=lambda value: str(value).strip() or None,
            ),
            patch(
                "crsbench.distributed.evaluator.run_evaluator_configless",
                side_effect=_assert_configless,
            ) as mock_configless,
            patch.dict("os.environ", {"CRSBENCH_REDIS_HOST": "localhost"}, clear=False),
        ):
            result = run_evaluator(args)

        assert result == 0
        assert os.environ.get(EVALUATOR_ROUTING_MODEL_ENV) is None
        mock_configless.assert_called_once()

    def test_cli_config_mode_defaults_dispatcher_when_env_unset(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Focused evaluator mode should default to dispatcher routing."""
        from crsbench.distributed.cli.evaluator_command import run_evaluator
        from crsbench.distributed.queue import (
            EVALUATOR_ROUTING_MODEL_ENV,
            ROUTING_MODEL_DISPATCHER,
        )

        monkeypatch.delenv(EVALUATOR_ROUTING_MODEL_ENV, raising=False)
        args = argparse.Namespace(
            experiment_config="test.yaml",
            ci=False,
            verbose=False,
            cpuset=None,
            skip_cpuset=None,
            cpu_tag=None,
            jobs=None,
            cores_per_job=None,
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
                "crsbench.distributed.common.normalize_redis_host",
                side_effect=lambda value: str(value).strip() or None,
            ),
            patch(
                "crsbench.run_experiment.load_experiment_config",
                return_value=MagicMock(
                    experiment="exp-test",
                    redis_host="localhost",
                    evaluator=None,
                ),
            ),
            patch(
                "crsbench.distributed.evaluator.run_evaluator_main",
                side_effect=lambda *_args, **_kwargs: (
                    0
                    if os.environ.get(EVALUATOR_ROUTING_MODEL_ENV)
                    == ROUTING_MODEL_DISPATCHER
                    else 1
                ),
            ) as mock_run,
            patch.dict("os.environ", {"CRSBENCH_REDIS_HOST": "localhost"}, clear=False),
        ):
            result = run_evaluator(args)

        assert result == 0
        assert os.environ.get(EVALUATOR_ROUTING_MODEL_ENV) is None
        mock_run.assert_called_once()

    def test_cores_per_job_rejects_zero(self) -> None:
        """--cores-per-job must be >= 1."""
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="command")
        from crsbench.distributed.cli.evaluator_command import add_evaluator_subparser

        add_evaluator_subparser(subparsers)
        with pytest.raises(SystemExit):
            parser.parse_args(["evaluator", "--cores-per-job", "0"])

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
