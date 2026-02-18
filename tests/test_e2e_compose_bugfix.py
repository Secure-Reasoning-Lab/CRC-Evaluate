"""E2E integration tests for bug-fixing evaluation through OssCrsAdapter.

Tests the complete pipeline: run_crs_trial() -> OssCrsAdapter (mode=bug-fixing)
-> BenchmarkRunner -> TrialResult, including patch collection from SUBMIT_DIR to
trial_output_dir/output/patches/.

Requirement: EVAL-02
"""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest
import yaml
from crsbench.distributed.jobs import run_crs_trial
from crsbench.evaluation.results import TrialResult
from crsbench.validation.schemas import ExperimentConfig

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_bugfix_config_dict(tmp_path: Path, **overrides: Any) -> dict[str, Any]:
    """Create ExperimentConfig dict for bug-fixing compose E2E tests."""
    base: dict[str, Any] = {
        "experiment": "e2e-bugfix-test",
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
        "build_timeout": 60,
        "run_timeout": 10,
        "crs_compose": {
            "docker_registry": "ghcr.io/test",
        },
    }
    base.update(overrides)
    return base


def _create_patch_submit_dir(work_dir: Path, crs_name: str, harness: str) -> Path:
    """Create a SUBMIT_DIR with mock patch files.

    Returns the path to the created SUBMIT_DIR/<harness> directory.
    """
    submit = (
        work_dir
        / "crs_compose"
        / "abc123hash"
        / "crs"
        / crs_name
        / "target_img"
        / "SUBMIT_DIR"
        / harness
    )
    patch_dir = submit / "patch"
    patch_dir.mkdir(parents=True)
    (patch_dir / "fix.patch").write_text("--- a/bug.c\n+++ b/bug.c\n@@ -1 +1 @@\n")
    return submit


def _make_run_side_effect_with_patches(tmp_path: Path, crs_name: str, harness: str):
    """Return a side_effect callable that creates SUBMIT_DIR patches during run.

    The side_effect finds the crs-compose-workdir under the filestore and creates
    the SUBMIT_DIR/patch/ directory with a mock patch file.
    """

    def side_effect(*_args: Any, **_kwargs: Any) -> tuple[str, str, int, bool]:
        # Look for the crs-compose-workdir directory under filestore
        filestore = tmp_path / "filestore"
        work_dirs = list(filestore.rglob("crs-compose-workdir"))
        if work_dirs:
            work_dir = work_dirs[0]
            _create_patch_submit_dir(work_dir, crs_name, harness)
        return ("output", "", 0, False)

    return side_effect


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def e2e_bugfix_env(tmp_path: Path) -> Path:
    """Create complete filesystem environment for bug-fixing E2E tests."""
    # 1. Mock benchmark directory
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

    # 2. CRS registry with pkg.yaml (bug-fixing type)
    registry = tmp_path / "registry"
    crs_dir = registry / "test-crs"
    crs_dir.mkdir(parents=True)
    (crs_dir / "pkg.yaml").write_text(
        yaml.dump(
            {
                "type": "bug-fixing",
                "source": {
                    "url": "https://github.com/test/crs.git",
                    "ref": "main",
                },
            }
        )
    )

    # 3. oss-fuzz directory
    oss_fuzz = tmp_path / "oss-fuzz"
    (oss_fuzz / "projects").mkdir(parents=True)

    # 4. Filestore and report directories
    (tmp_path / "filestore").mkdir()
    (tmp_path / "report").mkdir()

    # 5. CRS configs directory with config-resource.yaml
    crs_configs = tmp_path / "crs-configs"
    crs_config_dir = crs_configs / "test-crs"
    crs_config_dir.mkdir(parents=True)
    (crs_config_dir / "config-resource.yaml").write_text(
        yaml.dump({"crs": {"test-crs": {"image": "test-image"}}})
    )

    return tmp_path


# ---------------------------------------------------------------------------
# Shared mock targets
# ---------------------------------------------------------------------------

_SUBPROCESS_RUN = "crsbench.evaluation.adapter.compose_common.subprocess.run"
_RUN_GRACEFUL = "crsbench.evaluation.adapter.compose_common.run_with_graceful_timeout"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBugFixE2ECompose:
    """E2E tests for bug-fixing compose adapter through run_crs_trial."""

    @patch(_RUN_GRACEFUL)
    @patch(_SUBPROCESS_RUN)
    def test_full_trial_lifecycle_bugfix(
        self,
        mock_subprocess: MagicMock,
        mock_run_graceful: MagicMock,
        e2e_bugfix_env: Path,
    ) -> None:
        """Complete bug-fixing flow through run_crs_trial."""
        tmp_path = e2e_bugfix_env

        # subprocess.run succeeds for prepare + build-target + docker cleanup
        mock_subprocess.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="ok", stderr=""
        )

        # run_with_graceful_timeout succeeds and creates SUBMIT_DIR
        mock_run_graceful.side_effect = _make_run_side_effect_with_patches(
            tmp_path, "test-crs", "fuzz_target"
        )

        config_dict = make_bugfix_config_dict(tmp_path)
        config = ExperimentConfig(**config_dict)

        result = run_crs_trial(
            crs="test-crs",
            benchmark="test-project",
            harness_name="fuzz_target",
            harness_path="/src/project/fuzz_target.c",
            trial_num=1,
            trial_id="trial-bugfix-001",
            config_dict=config.model_dump(mode="json"),
            mode="full",
        )

        assert isinstance(result, TrialResult)
        assert result.success is True
        assert result.crs_type == "bug-fixing"
        assert result.crs == "test-crs"
        assert result.benchmark == "test-project"

        # Verify trial output directory has expected artifacts
        filestore = tmp_path / "filestore"
        trial_dirs = list(filestore.rglob("trial-1"))
        assert len(trial_dirs) == 1
        trial_dir = trial_dirs[0]

        assert (trial_dir / "metadata.json").exists()
        assert (trial_dir / ".success").exists()

        # Verify patches were collected to output/patches/ (plural)
        patches_dir = trial_dir / "output" / "patches"
        assert patches_dir.exists()
        assert (patches_dir / "fix.patch").exists()

    @patch(_RUN_GRACEFUL)
    @patch(_SUBPROCESS_RUN)
    def test_bugfix_prepare_failure(
        self,
        mock_subprocess: MagicMock,
        mock_run_graceful: MagicMock,
        e2e_bugfix_env: Path,
    ) -> None:
        """Prepare failure results in failed trial."""
        tmp_path = e2e_bugfix_env

        # prepare fails (rc=1)
        mock_subprocess.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="prepare error"
        )

        config_dict = make_bugfix_config_dict(tmp_path)
        config = ExperimentConfig(**config_dict)

        result = run_crs_trial(
            crs="test-crs",
            benchmark="test-project",
            harness_name="fuzz_target",
            harness_path="/src/project/fuzz_target.c",
            trial_num=1,
            trial_id="trial-bugfix-fail-001",
            config_dict=config.model_dump(mode="json"),
            mode="full",
        )

        assert isinstance(result, TrialResult)
        assert result.success is False

    @patch(_RUN_GRACEFUL)
    @patch(_SUBPROCESS_RUN)
    def test_bugfix_run_timeout(
        self,
        mock_subprocess: MagicMock,
        mock_run_graceful: MagicMock,
        e2e_bugfix_env: Path,
    ) -> None:
        """Run timeout produces failure result, not exception."""
        tmp_path = e2e_bugfix_env

        # subprocess.run succeeds for prepare + build-target
        mock_subprocess.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="ok", stderr=""
        )
        # run_with_graceful_timeout reports timeout
        mock_run_graceful.return_value = ("", "", 1, True)

        config_dict = make_bugfix_config_dict(tmp_path)
        config = ExperimentConfig(**config_dict)

        result = run_crs_trial(
            crs="test-crs",
            benchmark="test-project",
            harness_name="fuzz_target",
            harness_path="/src/project/fuzz_target.c",
            trial_num=1,
            trial_id="trial-bugfix-timeout-001",
            config_dict=config.model_dump(mode="json"),
            mode="full",
        )

        # Timeout produces a result (not an exception)
        assert isinstance(result, TrialResult)
        # Run failed because CRS returned non-zero
        assert result.success is False

    @patch(_RUN_GRACEFUL)
    @patch(_SUBPROCESS_RUN)
    def test_bugfix_metadata_mode_is_patch_generation(
        self,
        mock_subprocess: MagicMock,
        mock_run_graceful: MagicMock,
        e2e_bugfix_env: Path,
    ) -> None:
        """Bug-fixing trial metadata mode is patch_generation."""
        tmp_path = e2e_bugfix_env

        mock_subprocess.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="ok", stderr=""
        )
        mock_run_graceful.side_effect = _make_run_side_effect_with_patches(
            tmp_path, "test-crs", "fuzz_target"
        )

        config_dict = make_bugfix_config_dict(tmp_path)
        config = ExperimentConfig(**config_dict)

        run_crs_trial(
            crs="test-crs",
            benchmark="test-project",
            harness_name="fuzz_target",
            harness_path="/src/project/fuzz_target.c",
            trial_num=1,
            trial_id="trial-bugfix-meta-001",
            config_dict=config.model_dump(mode="json"),
            mode="full",
        )

        # Find and read metadata.json
        filestore = tmp_path / "filestore"
        metadata_files = list(filestore.rglob("metadata.json"))
        assert len(metadata_files) >= 1

        with metadata_files[0].open() as f:
            metadata = json.load(f)

        assert metadata["mode"] == "patch_generation"

    @patch(_RUN_GRACEFUL)
    @patch(_SUBPROCESS_RUN)
    def test_bugfix_patches_collected_to_output(
        self,
        mock_subprocess: MagicMock,
        mock_run_graceful: MagicMock,
        e2e_bugfix_env: Path,
    ) -> None:
        """Patches from SUBMIT_DIR are collected to output/patches/ directory."""
        tmp_path = e2e_bugfix_env

        mock_subprocess.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="ok", stderr=""
        )
        mock_run_graceful.side_effect = _make_run_side_effect_with_patches(
            tmp_path, "test-crs", "fuzz_target"
        )

        config_dict = make_bugfix_config_dict(tmp_path)
        config = ExperimentConfig(**config_dict)

        result = run_crs_trial(
            crs="test-crs",
            benchmark="test-project",
            harness_name="fuzz_target",
            harness_path="/src/project/fuzz_target.c",
            trial_num=1,
            trial_id="trial-bugfix-patches-001",
            config_dict=config.model_dump(mode="json"),
            mode="full",
        )

        assert result.success is True

        # Verify patches were collected to output/patches/ (plural)
        filestore = tmp_path / "filestore"
        trial_dirs = list(filestore.rglob("trial-1"))
        assert len(trial_dirs) == 1
        trial_dir = trial_dirs[0]

        patches_dir = trial_dir / "output" / "patches"
        assert patches_dir.exists()
        patch_files = list(patches_dir.iterdir())
        assert len(patch_files) > 0
        assert any(p.name == "fix.patch" for p in patch_files)

        # Report dict should be non-empty
        assert result.report

    @patch(_RUN_GRACEFUL)
    @patch(_SUBPROCESS_RUN)
    def test_bugfix_no_patches_still_succeeds(
        self,
        mock_subprocess: MagicMock,
        mock_run_graceful: MagicMock,
        e2e_bugfix_env: Path,
    ) -> None:
        """Run succeeds even when no patches are generated (no SUBMIT_DIR)."""
        tmp_path = e2e_bugfix_env

        mock_subprocess.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="ok", stderr=""
        )
        # Run succeeds but does NOT create SUBMIT_DIR (no patches)
        mock_run_graceful.return_value = ("output", "", 0, False)

        config_dict = make_bugfix_config_dict(tmp_path)
        config = ExperimentConfig(**config_dict)

        result = run_crs_trial(
            crs="test-crs",
            benchmark="test-project",
            harness_name="fuzz_target",
            harness_path="/src/project/fuzz_target.c",
            trial_num=1,
            trial_id="trial-bugfix-nopatch-001",
            config_dict=config.model_dump(mode="json"),
            mode="full",
        )

        assert isinstance(result, TrialResult)
        assert result.success is True
        assert result.crs_type == "bug-fixing"
