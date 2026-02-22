"""E2E integration tests for bug-finding evaluation through OssCrsAdapter.

Exercises run_crs_trial() end-to-end with OssCrsAdapter (mode=bug-finding),
mocking only at the subprocess boundary. Validates the complete lifecycle:
  config deserialization -> adapter dispatch -> prepare/build-target/run
  -> collect_results -> TrialResult + metadata.json + .success marker.

Requirements covered: EVAL-01
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml
from crsbench.distributed.jobs import run_crs_trial
from crsbench.evaluation.results import TrialResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_bugfind_config_dict(tmp_path: Path, **overrides: Any) -> dict[str, Any]:
    """Create ExperimentConfig dict for bug-finding compose E2E tests.

    The default values satisfy all ExperimentConfig validators (max_total_time
    > build_timeout + run_timeout + verify_timeout, benchmarks_root exists, etc.).
    """
    base: dict[str, Any] = {
        "experiment": "e2e-bugfind-test",
        "trials": 1,
        "mode": "full",
        "adapter": "oss-crs",
        "max_total_time": 86400,
        "difficulty_level": 1,
        "experiment_filestore": str(tmp_path / "filestore"),
        "report_filestore": str(tmp_path / "report"),
        "crses": ["test-crs"],
        "benchmarks": ["test-project"],
        "oss_fuzz_path": str(tmp_path / "oss-fuzz"),
        "registry_dir": str(tmp_path / "registry"),
        "benchmarks_root": str(tmp_path / "benchmarks"),
        "crs_configs_dir": str(tmp_path / "crs-configs"),
        "snapshot_period": 0,
        "skip_verification": True,
        "skip_litellm": True,
        "build_timeout": 60,
        "run_timeout": 10,
        "crs_compose": {
            "docker_registry": "ghcr.io/test",
        },
    }
    base.update(overrides)
    return base


def _create_submit_dir(
    work_dir: Path,
    crs_name: str,
    harness_name: str,
    pov_filenames: list[str] | None = None,
) -> Path:
    """Create a SUBMIT_DIR structure with optional mock POV files.

    Returns the harness-level SUBMIT_DIR path.
    """
    submit = (
        work_dir
        / "crs_compose"
        / "abc123hash"
        / "address"
        / "runs"
        / "run-0"
        / "crs"
        / crs_name
        / "target_img"
        / "SUBMIT_DIR"
        / harness_name
    )
    pov_dir = submit / "povs"
    pov_dir.mkdir(parents=True)

    for name in pov_filenames or ["crash-001", "crash-002"]:
        (pov_dir / name).write_bytes(b"\xde\xad" * 4)

    return submit


def _run_side_effect_factory(crs_name: str, harness_name: str) -> Any:
    """Create a side_effect that populates SUBMIT_DIR when run is called."""

    def _side_effect(*args: Any, **_kwargs: Any) -> tuple[str, str, int, bool]:
        cmd = args[0]
        work_dir_idx = cmd.index("--work-dir") + 1
        work_dir = Path(cmd[work_dir_idx])
        _create_submit_dir(work_dir, crs_name, harness_name)
        return ("output", "", 0, False)

    return _side_effect


# ---------------------------------------------------------------------------
# Fixture: full filesystem environment
# ---------------------------------------------------------------------------


@pytest.fixture
def e2e_bugfind_env(tmp_path: Path) -> Path:
    """Create complete filesystem environment for bug-finding E2E tests."""
    # 1. Benchmark directory with project.yaml + .aixcc/meta.yaml
    benchmark = tmp_path / "benchmarks" / "test-project"
    benchmark.mkdir(parents=True)
    (benchmark / "project.yaml").write_text(
        yaml.dump(
            {
                "main_repo": "https://github.com/test/project.git",
                "repo_name": "project",
                "language": "c",
                "fuzzing_engines": ["libfuzzer"],
                "sanitizers": ["address"],
            }
        )
    )
    aixcc = benchmark / ".aixcc"
    aixcc.mkdir()
    (aixcc / "meta.yaml").write_text(
        yaml.dump(
            {
                "harness_files": [
                    {"name": "fuzz_target", "path": "/src/project/fuzz_target.c"},
                ],
                "full_mode": {
                    "base_commit": "abc123def456789012345678901234567890abcd",
                },
                "cpvs": [
                    {
                        "cpv_id": "cpv_0",
                        "harness": "fuzz_target",
                        "sanitizer": "address",
                    },
                ],
            }
        )
    )

    # 2. CRS registry with oss-crs YAML entry
    registry = tmp_path / "registry"
    registry.mkdir(parents=True)
    (registry / "test-crs.yaml").write_text(
        yaml.dump(
            {
                "type": "bug-finding",
                "source": {
                    "url": "https://github.com/test/crs.git",
                    "ref": "main",
                },
            }
        )
    )

    # 3. CRS configs directory (get_crs_registry_name reads config-resource.yaml)
    crs_configs = tmp_path / "crs-configs" / "test-crs"
    crs_configs.mkdir(parents=True)
    (crs_configs / "config-resource.yaml").write_text(
        yaml.dump({"crs": {"test-crs": {"image": "test-crs:latest"}}})
    )

    # 4. oss-fuzz directory (for symlink creation)
    oss_fuzz = tmp_path / "oss-fuzz"
    (oss_fuzz / "projects").mkdir(parents=True)

    # 5. Filestore directories
    (tmp_path / "filestore").mkdir()
    (tmp_path / "report").mkdir()

    return tmp_path


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------


def _build_config_dict(env: Path, **overrides: Any) -> dict[str, Any]:
    """Build serialized config dict from the fixture path."""
    from crsbench.validation.schemas import ExperimentConfig

    raw = make_bugfind_config_dict(env, **overrides)
    config = ExperimentConfig(**raw)
    return config.model_dump(mode="json")


def _run_trial(config_dict: dict[str, Any]) -> TrialResult:
    """Call run_crs_trial with standard arguments."""
    return run_crs_trial(
        crs="test-crs",
        benchmark="test-project",
        harness_name="fuzz_target",
        harness_path="/src/project/fuzz_target.c",
        trial_num=1,
        trial_id="trial-e2e-bugfind-001",
        config_dict=config_dict,
        mode="full",
        sanitizer="address",
    )


def _trial_output_dir(env: Path) -> Path:
    """Return the expected trial output directory path."""
    return (
        env
        / "filestore"
        / "e2e-bugfind-test"
        / "test-crs"
        / "test-project"
        / "fuzz_target"
        / "full"
        / "address"
        / "trial-1"
    )


def _setup_noop_resource_ctx(mock_resource_ctx: MagicMock) -> None:
    """Configure ResourceContext mock as a no-op context manager."""
    mock_resource_ctx.return_value.__enter__ = MagicMock(return_value=None)
    mock_resource_ctx.return_value.__exit__ = MagicMock(return_value=False)


# ===========================================================================
# TestBugFindE2ECompose
# ===========================================================================

# Common patch targets
_PATCH_SUBPROCESS = "crsbench.evaluation.adapter.compose_common.subprocess.run"
_PATCH_RWGT = "crsbench.evaluation.adapter.compose_common.run_with_graceful_timeout"
_PATCH_RESOURCE_CTX = "crsbench.evaluation.resource_context.ResourceContext"
_PATCH_ARTIFACTS = "crsbench.evaluation.adapter.oss_crs.run_oss_crs_artifacts"

_SUBPROCESS_OK = subprocess.CompletedProcess(
    args=[], returncode=0, stdout="ok", stderr=""
)


def _artifacts_side_effect_factory(crs_name: str, harness_name: str) -> Any:
    """Create a side_effect for run_oss_crs_artifacts that returns correct submit_dir.

    The submit_dir is created by _run_side_effect_factory during the run phase,
    so artifacts just needs to point to that same path.
    """

    def _side_effect(
        _compose_file: Any,
        work_dir: Any,
        _target_proj_path: Any,
        _target_harness: str,
        run_id: str,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        # Build the submit_dir path matching _create_submit_dir() layout
        submit_str = str(
            Path(str(work_dir))
            / "crs_compose"
            / "abc123hash"
            / "address"
            / "runs"
            / "run-0"
            / "crs"
            / crs_name
            / "target_img"
            / "SUBMIT_DIR"
            / harness_name
        )
        exchange_base = str(Path(str(work_dir)) / "exchange")
        return {
            "build_id": "test",
            "run_id": run_id,
            "sanitizer": "address",
            "exchange_dir": {
                "base": exchange_base,
                "pov": f"{exchange_base}/povs",
                "patch": f"{exchange_base}/patches",
            },
            "crs": {
                crs_name: {
                    "submit_dir": submit_str,
                }
            },
        }

    return _side_effect


class TestBugFindE2ECompose:
    """E2E tests exercising run_crs_trial -> OssCrsAdapter (bug-finding) -> TrialResult."""

    @patch(_PATCH_ARTIFACTS)
    @patch(_PATCH_RESOURCE_CTX)
    @patch(_PATCH_RWGT)
    @patch(_PATCH_SUBPROCESS)
    def test_full_trial_lifecycle_bugfind(
        self,
        mock_subprocess_run: MagicMock,
        mock_rwgt: MagicMock,
        mock_resource_ctx: MagicMock,
        mock_artifacts: MagicMock,
        e2e_bugfind_env: Path,
    ) -> None:
        """Complete happy path: prepare/build-target/run -> TrialResult + POVs."""
        env = e2e_bugfind_env
        config_dict = _build_config_dict(env)

        mock_subprocess_run.return_value = _SUBPROCESS_OK
        _setup_noop_resource_ctx(mock_resource_ctx)
        mock_rwgt.side_effect = _run_side_effect_factory("test-crs", "fuzz_target")
        mock_artifacts.side_effect = _artifacts_side_effect_factory(
            "test-crs", "fuzz_target"
        )

        result = _run_trial(config_dict)

        # TrialResult assertions
        assert isinstance(result, TrialResult)
        assert result.crs == "test-crs"
        assert result.benchmark == "test-project"
        assert result.harness == "fuzz_target"
        assert result.mode == "full"
        assert result.success is True
        assert result.crs_type == "bug-finding"

        # trial_output_dir assertions
        tod = _trial_output_dir(env)
        assert tod.exists()

        # metadata.json written
        metadata_file = tod / "metadata.json"
        assert metadata_file.exists()
        metadata = json.loads(metadata_file.read_text())
        assert metadata["crs"] == "test-crs"
        assert metadata["benchmark"] == "test-project"
        assert metadata["harness"] == "fuzz_target"

        # .success marker
        assert (tod / ".success").exists()

        # POVs copied to output/povs/ (plural, from collect_results wiring)
        povs_dir = tod / "output" / "povs"
        assert povs_dir.exists()
        pov_files = list(povs_dir.iterdir())
        assert len(pov_files) == 2
        pov_names = sorted(f.name for f in pov_files)
        assert pov_names == ["crash-001", "crash-002"]

    @patch(_PATCH_ARTIFACTS)
    @patch(_PATCH_RESOURCE_CTX)
    @patch(_PATCH_RWGT)
    @patch(_PATCH_SUBPROCESS)
    def test_prepare_failure_produces_error_result(
        self,
        mock_subprocess_run: MagicMock,
        mock_rwgt: MagicMock,
        mock_resource_ctx: MagicMock,
        mock_artifacts: MagicMock,
        e2e_bugfind_env: Path,
    ) -> None:
        """prepare rc=1 -> TrialResult with success=False."""
        env = e2e_bugfind_env
        config_dict = _build_config_dict(env)

        # prepare fails (first subprocess.run call returns rc=1)
        mock_subprocess_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="prepare error"
        )
        _setup_noop_resource_ctx(mock_resource_ctx)

        result = _run_trial(config_dict)

        assert isinstance(result, TrialResult)
        assert result.success is False
        assert result.error is not None
        assert "prepare" in result.error.lower()
        # run_with_graceful_timeout should NOT have been called (build failed first)
        mock_rwgt.assert_not_called()

    @patch(_PATCH_ARTIFACTS)
    @patch(_PATCH_RESOURCE_CTX)
    @patch(_PATCH_RWGT)
    @patch(_PATCH_SUBPROCESS)
    def test_build_target_failure_produces_error_result(
        self,
        mock_subprocess_run: MagicMock,
        mock_rwgt: MagicMock,
        mock_resource_ctx: MagicMock,
        mock_artifacts: MagicMock,
        e2e_bugfind_env: Path,
    ) -> None:
        """prepare succeeds, build-target fails -> TrialResult with success=False."""
        env = e2e_bugfind_env
        config_dict = _build_config_dict(env)

        # prepare succeeds, build-target fails
        mock_subprocess_run.side_effect = [
            subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr=""),
            subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr="build-target error"
            ),
        ]
        _setup_noop_resource_ctx(mock_resource_ctx)

        result = _run_trial(config_dict)

        assert isinstance(result, TrialResult)
        assert result.success is False
        # run_with_graceful_timeout should NOT have been called
        mock_rwgt.assert_not_called()

    @patch(_PATCH_ARTIFACTS)
    @patch(_PATCH_RESOURCE_CTX)
    @patch(_PATCH_RWGT)
    @patch(_PATCH_SUBPROCESS)
    def test_run_timeout_produces_result(
        self,
        mock_subprocess_run: MagicMock,
        mock_rwgt: MagicMock,
        mock_resource_ctx: MagicMock,
        mock_artifacts: MagicMock,
        e2e_bugfind_env: Path,
    ) -> None:
        """Run phase times out -> TrialResult returned (not an exception)."""
        env = e2e_bugfind_env
        config_dict = _build_config_dict(env)

        mock_subprocess_run.return_value = _SUBPROCESS_OK
        _setup_noop_resource_ctx(mock_resource_ctx)
        mock_artifacts.side_effect = _artifacts_side_effect_factory(
            "test-crs", "fuzz_target"
        )

        # run phase times out (rc=1, timed_out=True)
        mock_rwgt.return_value = ("", "", 1, True)

        result = _run_trial(config_dict)

        # Timeout produces a result, not an exception
        assert isinstance(result, TrialResult)
        assert result is not None

    @patch(_PATCH_ARTIFACTS)
    @patch(_PATCH_RESOURCE_CTX)
    @patch(_PATCH_RWGT)
    @patch(_PATCH_SUBPROCESS)
    def test_trial_output_directory_structure(
        self,
        mock_subprocess_run: MagicMock,
        mock_rwgt: MagicMock,
        mock_resource_ctx: MagicMock,
        mock_artifacts: MagicMock,
        e2e_bugfind_env: Path,
    ) -> None:
        """After a successful trial, verify trial_output_dir structure."""
        env = e2e_bugfind_env
        config_dict = _build_config_dict(env)

        mock_subprocess_run.return_value = _SUBPROCESS_OK
        _setup_noop_resource_ctx(mock_resource_ctx)
        mock_rwgt.side_effect = _run_side_effect_factory("test-crs", "fuzz_target")
        mock_artifacts.side_effect = _artifacts_side_effect_factory(
            "test-crs", "fuzz_target"
        )

        _run_trial(config_dict)

        tod = _trial_output_dir(env)

        # metadata.json is parseable JSON with expected fields
        metadata_file = tod / "metadata.json"
        assert metadata_file.exists()
        metadata = json.loads(metadata_file.read_text())
        assert "timestamp" in metadata
        assert "trial_num" in metadata
        assert "crs" in metadata

        # worker.log exists (per-trial logging)
        assert (tod / "worker.log").exists()

        # .success marker
        assert (tod / ".success").exists()

        # output/ directory from collect_results
        assert (tod / "output").exists()
        assert (tod / "output").is_dir()


# ===========================================================================
# TestResultFormatInterchangeability
# ===========================================================================


class TestResultFormatInterchangeability:
    """Verify compose adapter results match expected evaluation format."""

    @patch(_PATCH_ARTIFACTS)
    @patch(_PATCH_RESOURCE_CTX)
    @patch(_PATCH_RWGT)
    @patch(_PATCH_SUBPROCESS)
    def test_trial_result_has_required_fields(
        self,
        mock_subprocess_run: MagicMock,
        mock_rwgt: MagicMock,
        mock_resource_ctx: MagicMock,
        mock_artifacts: MagicMock,
        e2e_bugfind_env: Path,
    ) -> None:
        """TrialResult from compose adapter has all required fields."""
        env = e2e_bugfind_env
        config_dict = _build_config_dict(env)

        mock_subprocess_run.return_value = _SUBPROCESS_OK
        _setup_noop_resource_ctx(mock_resource_ctx)
        mock_rwgt.side_effect = _run_side_effect_factory("test-crs", "fuzz_target")
        mock_artifacts.side_effect = _artifacts_side_effect_factory(
            "test-crs", "fuzz_target"
        )

        result = _run_trial(config_dict)

        # All required TrialResult fields are present and typed correctly
        assert isinstance(result.crs, str)
        assert isinstance(result.benchmark, str)
        assert isinstance(result.harness, str)
        assert isinstance(result.trial_num, int)
        assert isinstance(result.crs_type, str)
        assert isinstance(result.mode, str)
        assert isinstance(result.sanitizer, str)
        assert isinstance(result.success, bool)
        assert isinstance(result.execution_time, float)
        assert isinstance(result.povs_found, int)
        assert isinstance(result.total_povs, int)
        assert isinstance(result.report, dict)
        assert result.metadata is not None
        assert result.metadata.timestamp_start > 0
        assert result.metadata.timestamp_end >= result.metadata.timestamp_start

    @patch(_PATCH_ARTIFACTS)
    @patch(_PATCH_RESOURCE_CTX)
    @patch(_PATCH_RWGT)
    @patch(_PATCH_SUBPROCESS)
    def test_metadata_json_schema_matches_legacy(
        self,
        mock_subprocess_run: MagicMock,
        mock_rwgt: MagicMock,
        mock_resource_ctx: MagicMock,
        mock_artifacts: MagicMock,
        e2e_bugfind_env: Path,
    ) -> None:
        """metadata.json in trial_output_dir has all legacy-required fields."""
        env = e2e_bugfind_env
        config_dict = _build_config_dict(env)

        mock_subprocess_run.return_value = _SUBPROCESS_OK
        _setup_noop_resource_ctx(mock_resource_ctx)
        mock_rwgt.side_effect = _run_side_effect_factory("test-crs", "fuzz_target")
        mock_artifacts.side_effect = _artifacts_side_effect_factory(
            "test-crs", "fuzz_target"
        )

        _run_trial(config_dict)

        tod = _trial_output_dir(env)
        metadata_file = tod / "metadata.json"
        assert metadata_file.exists()
        metadata = json.loads(metadata_file.read_text())

        # Verify all required fields exist
        required_fields = [
            "timestamp",
            "trial_num",
            "crs",
            "benchmark",
            "harness",
            "mode",
            "source",
            "config",
            "worker_machine",
            "worker_trial_dir",
            "experiment_name",
        ]
        for field in required_fields:
            assert field in metadata, f"Missing required field: {field}"

        # Verify field values
        assert metadata["crs"] == "test-crs"
        assert metadata["benchmark"] == "test-project"
        assert metadata["harness"] == "fuzz_target"
        assert metadata["trial_num"] == 1
        assert metadata["experiment_name"] == "e2e-bugfind-test"
        assert metadata["mode"] == "bug_finding"
        assert isinstance(metadata["source"], dict)
        assert isinstance(metadata["config"], dict)
