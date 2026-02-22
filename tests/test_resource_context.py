"""Tests for ResourceContext cgroup lifecycle and compose adapter LLM integration.

Covers:
- ResourceContext env var management and cgroup lifecycle (mock-based)
- Graceful degradation when cgroup v2 is unavailable
- Compose adapter external_litellm configuration
- run_oss_crs_run env var passthrough to subprocess
"""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from crsbench.evaluation.adapter import OssCrsAdapter

FACTORY_ARGS = {
    "crs_config_name": "test-crs",
    "oss_fuzz_path": Path("/tmp/fake/oss-fuzz"),
    "registry_dir": Path("/tmp/fake/registry"),
    "benchmarks_root": Path("/tmp/fake/benchmarks"),
    "crs_configs_dir": Path("/tmp/fake/configs"),
}


# ---------------------------------------------------------------------------
# ResourceContext tests
# ---------------------------------------------------------------------------


class TestResourceContext:
    """Tests for ResourceContext cgroup lifecycle and env var management."""

    def test_no_cgroups_sets_no_env_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ResourceContext with use_cgroups=False should not set env vars."""
        monkeypatch.delenv("OSS_FUZZ_CGROUP_PARENT", raising=False)
        monkeypatch.delenv("OSS_FUZZ_CPUSET_CPUS", raising=False)

        from crsbench.evaluation.resource_context import ResourceContext

        with ResourceContext(trial_name="test-trial", cpuset="0-3"):
            assert "OSS_FUZZ_CGROUP_PARENT" not in os.environ
            assert "OSS_FUZZ_CPUSET_CPUS" not in os.environ

    def test_cgroup_path_is_none_when_disabled(self) -> None:
        """cgroup_path should be None when cgroups are not used."""
        from crsbench.evaluation.resource_context import ResourceContext

        ctx = ResourceContext(trial_name="test-trial")
        with ctx:
            assert ctx.cgroup_path is None

    def test_env_vars_cleaned_on_exit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Env vars should be cleaned up after context exit."""
        # Pre-set env vars to verify they get cleaned
        monkeypatch.setenv("OSS_FUZZ_CGROUP_PARENT", "leftover-value")
        monkeypatch.setenv("OSS_FUZZ_CPUSET_CPUS", "0-1")

        from crsbench.evaluation.resource_context import ResourceContext

        with ResourceContext(trial_name="test-trial"):
            pass

        assert "OSS_FUZZ_CGROUP_PARENT" not in os.environ
        assert "OSS_FUZZ_CPUSET_CPUS" not in os.environ

    def test_cgroup_creation_with_mock(self) -> None:
        """Verify cgroup lifecycle with fully mocked cgroup functions.

        Tests that run_preflight_checks, create_cgroup, cgroup_path_for_docker,
        and cleanup_cgroup are called correctly through the ResourceContext flow.
        """
        from crsbench.evaluation.resource_context import ResourceContext

        mock_cgroup_path = Path("/sys/fs/cgroup/crsbench/test-trial")

        with (
            patch("crsbench.utils.cgroup.run_preflight_checks") as mock_preflight,
            patch("crsbench.utils.cgroup.setup_cgroup_hierarchy"),
            patch("crsbench.utils.cgroup.create_cgroup") as mock_create,
            patch("crsbench.utils.cgroup.cgroup_path_for_docker") as mock_docker_path,
            patch("crsbench.utils.cgroup.cleanup_cgroup") as mock_cleanup,
        ):
            mock_preflight.return_value = Path("/sys/fs/cgroup/crsbench")
            mock_create.return_value = mock_cgroup_path
            mock_docker_path.return_value = "crsbench/test-trial"

            ctx = ResourceContext(
                trial_name="test-trial",
                cpuset="0-3",
                memory_bytes=8 * 1024**3,
                use_cgroups=True,
            )

            with ctx:
                mock_preflight.assert_called_once()
                mock_create.assert_called_once_with(
                    Path("/sys/fs/cgroup/crsbench"),
                    "test-trial",
                    cpuset="0-3",
                    memory_bytes=8 * 1024**3,
                )
                mock_docker_path.assert_called_once_with(mock_cgroup_path)
                assert ctx.cgroup_path == mock_cgroup_path
                assert os.environ.get("OSS_FUZZ_CGROUP_PARENT") == "crsbench/test-trial"
                assert os.environ.get("OSS_FUZZ_CPUSET_CPUS") == "0-3"

            # After exit: cleanup called, env vars cleaned
            mock_cleanup.assert_called_once_with(mock_cgroup_path, force=True)
            assert "OSS_FUZZ_CGROUP_PARENT" not in os.environ
            assert "OSS_FUZZ_CPUSET_CPUS" not in os.environ

    def test_cgroup_creation_with_mock_direct(self) -> None:
        """Direct mock test for cgroup lifecycle without patching __enter__."""
        from crsbench.evaluation.resource_context import ResourceContext

        mock_cgroup_path = Path("/sys/fs/cgroup/crsbench/test-trial-2")

        with (
            patch("crsbench.utils.cgroup.run_preflight_checks") as mock_preflight,
            patch("crsbench.utils.cgroup.setup_cgroup_hierarchy"),
            patch("crsbench.utils.cgroup.create_cgroup") as mock_create,
            patch("crsbench.utils.cgroup.cgroup_path_for_docker") as mock_docker_path,
            patch("crsbench.utils.cgroup.cleanup_cgroup") as mock_cleanup,
        ):
            mock_preflight.return_value = Path("/sys/fs/cgroup/crsbench")
            mock_create.return_value = mock_cgroup_path
            mock_docker_path.return_value = "crsbench/test-trial-2"

            ctx = ResourceContext(
                trial_name="test-trial-2",
                cpuset="4-7",
                memory_bytes=4 * 1024**3,
                use_cgroups=True,
            )

            with ctx:
                mock_preflight.assert_called_once()
                mock_create.assert_called_once()
                assert ctx.cgroup_path == mock_cgroup_path
                assert os.environ.get("OSS_FUZZ_CPUSET_CPUS") == "4-7"

            mock_cleanup.assert_called_once_with(mock_cgroup_path, force=True)
            assert "OSS_FUZZ_CGROUP_PARENT" not in os.environ
            assert "OSS_FUZZ_CPUSET_CPUS" not in os.environ

    def test_graceful_degradation_on_cgroup_error(self) -> None:
        """CgroupError during preflight should not propagate.

        Verifies: no exception, cgroup_path is None, context enters/exits cleanly,
        and a warning is logged about degradation.
        """
        from crsbench.utils.cgroup import CgroupError

        with (
            patch(
                "crsbench.utils.cgroup.run_preflight_checks",
                side_effect=CgroupError("cgroup v2 not mounted"),
            ),
            patch("crsbench.evaluation.resource_context.logger") as mock_logger,
        ):
            from crsbench.evaluation.resource_context import ResourceContext

            ctx = ResourceContext(
                trial_name="test-fail",
                cpuset="0-3",
                use_cgroups=True,
            )

            with ctx:
                assert ctx.cgroup_path is None

            # Verify warning was logged about cgroup unavailability
            mock_logger.warning.assert_called_once()
            warning_args = mock_logger.warning.call_args
            assert "unavailable" in str(warning_args).lower()

    def test_env_vars_set_with_cpuset(self) -> None:
        """Verify OSS_FUZZ_CPUSET_CPUS is set to the provided cpuset."""
        from crsbench.evaluation.resource_context import ResourceContext

        with (
            patch("crsbench.utils.cgroup.run_preflight_checks") as mock_preflight,
            patch("crsbench.utils.cgroup.setup_cgroup_hierarchy"),
            patch("crsbench.utils.cgroup.create_cgroup") as mock_create,
            patch("crsbench.utils.cgroup.cgroup_path_for_docker") as mock_docker_path,
            patch("crsbench.utils.cgroup.cleanup_cgroup"),
        ):
            mock_preflight.return_value = Path("/sys/fs/cgroup/crsbench")
            mock_create.return_value = Path("/sys/fs/cgroup/crsbench/cpuset-test")
            mock_docker_path.return_value = "crsbench/cpuset-test"

            ctx = ResourceContext(
                trial_name="cpuset-test",
                cpuset="4-7",
                use_cgroups=True,
            )

            with ctx:
                assert os.environ.get("OSS_FUZZ_CPUSET_CPUS") == "4-7"

        assert "OSS_FUZZ_CPUSET_CPUS" not in os.environ

    def test_env_vars_property(self) -> None:
        """Verify the env_vars property returns the correct dict."""
        from crsbench.evaluation.resource_context import ResourceContext

        with (
            patch("crsbench.utils.cgroup.run_preflight_checks") as mock_preflight,
            patch("crsbench.utils.cgroup.setup_cgroup_hierarchy"),
            patch("crsbench.utils.cgroup.create_cgroup") as mock_create,
            patch("crsbench.utils.cgroup.cgroup_path_for_docker") as mock_docker_path,
            patch("crsbench.utils.cgroup.cleanup_cgroup"),
        ):
            mock_preflight.return_value = Path("/sys/fs/cgroup/crsbench")
            mock_create.return_value = Path("/sys/fs/cgroup/crsbench/env-test")
            mock_docker_path.return_value = "crsbench/env-test"

            ctx = ResourceContext(
                trial_name="env-test",
                cpuset="0-15",
                use_cgroups=True,
            )

            with ctx:
                env = ctx.env_vars
                assert env["OSS_FUZZ_CGROUP_PARENT"] == "crsbench/env-test"
                assert env["OSS_FUZZ_CPUSET_CPUS"] == "0-15"

        # After exit, env_vars dict should be empty
        assert ctx.env_vars == {}


# ---------------------------------------------------------------------------
# Compose adapter LLM integration tests
# ---------------------------------------------------------------------------


class TestComposeLLMIntegration:
    """Tests for compose adapter external_litellm configuration and wiring."""

    def test_oss_crs_adapter_bugfind_accepts_external_litellm_config(self) -> None:
        """Configure with external_litellm should set internal state (bug-finding)."""
        adapter = OssCrsAdapter(**FACTORY_ARGS, mode="bug-finding")
        adapter.configure(
            {
                "external_litellm": True,
                "litellm_url": "http://litellm:4000",
                "litellm_api_key": "sk-test-key-123",
            }
        )
        assert adapter._external_litellm is True
        assert adapter._litellm_url == "http://litellm:4000"
        assert adapter._litellm_api_key == "sk-test-key-123"

    def test_oss_crs_adapter_bugfix_accepts_external_litellm_config(self) -> None:
        """Configure with external_litellm should set internal state (bug-fixing)."""
        adapter = OssCrsAdapter(**FACTORY_ARGS, mode="bug-fixing")
        adapter.configure(
            {
                "external_litellm": True,
                "litellm_url": "http://litellm:4000",
                "litellm_api_key": "sk-bugfix-key",
            }
        )
        assert adapter._external_litellm is True
        assert adapter._litellm_url == "http://litellm:4000"
        assert adapter._litellm_api_key == "sk-bugfix-key"

    @patch("crsbench.evaluation.adapter.compose_common.run_with_graceful_timeout")
    def test_run_oss_crs_run_passes_env_when_external_litellm(
        self, mock_run: MagicMock
    ) -> None:
        """New interface: no run env injection, compose handles LiteLLM routing."""
        mock_run.return_value = ("stdout", "stderr", 0, False)

        from crsbench.evaluation.adapter.compose_common import run_oss_crs_run

        run_oss_crs_run(
            compose_file=Path("/tmp/compose.yaml"),
            work_dir=Path("/tmp/work"),
            target_proj_path=Path("/tmp/proj"),
            target_harness="fuzz_test",
            timeout=3600,
            external_litellm=True,
            litellm_url="http://litellm:4000",
            litellm_api_key="sk-test-key",
        )

        mock_run.assert_called_once()
        call_kwargs = mock_run.call_args
        env = call_kwargs.kwargs.get("env") or call_kwargs[1].get("env")
        assert env is None

    @patch("crsbench.evaluation.adapter.compose_common.run_with_graceful_timeout")
    def test_run_oss_crs_run_no_env_when_external_litellm_false(
        self, mock_run: MagicMock
    ) -> None:
        """When external_litellm=False, env should be None."""
        mock_run.return_value = ("stdout", "stderr", 0, False)

        from crsbench.evaluation.adapter.compose_common import run_oss_crs_run

        run_oss_crs_run(
            compose_file=Path("/tmp/compose.yaml"),
            work_dir=Path("/tmp/work"),
            target_proj_path=Path("/tmp/proj"),
            target_harness="fuzz_test",
            timeout=3600,
            external_litellm=False,
        )

        mock_run.assert_called_once()
        call_kwargs = mock_run.call_args
        env = call_kwargs.kwargs.get("env") or call_kwargs[1].get("env")
        assert env is None

    @patch("crsbench.evaluation.adapter.compose_common.run_with_graceful_timeout")
    def test_run_oss_crs_run_no_env_when_url_missing(self, mock_run: MagicMock) -> None:
        """When external_litellm=True but url/key missing, run still has no env."""
        mock_run.return_value = ("stdout", "stderr", 0, False)

        from crsbench.evaluation.adapter.compose_common import run_oss_crs_run

        run_oss_crs_run(
            compose_file=Path("/tmp/compose.yaml"),
            work_dir=Path("/tmp/work"),
            target_proj_path=Path("/tmp/proj"),
            target_harness="fuzz_test",
            timeout=3600,
            external_litellm=True,
            litellm_url=None,
            litellm_api_key=None,
        )

        mock_run.assert_called_once()
        call_kwargs = mock_run.call_args
        env = call_kwargs.kwargs.get("env") or call_kwargs[1].get("env")
        assert env is None
