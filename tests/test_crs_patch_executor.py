"""Basic tests for CRSPatchExecutor."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from crsbench.evaluation.crs_patch_executor import CRSPatchExecutor


class TestCRSPatchExecutor(unittest.TestCase):
    """Test CRSPatchExecutor functionality."""

    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.oss_fuzz_path = Path(self.temp_dir) / "oss-fuzz"
        self.benchmarks_root = Path(self.temp_dir) / "benchmarks"
        self.registry_dir = Path(self.temp_dir) / "crses" / "registry"
        self.crs_configs_dir = Path(self.temp_dir) / "crses" / "configs"

        # Create directory structure
        self.oss_fuzz_path.mkdir(parents=True)
        self.benchmarks_root.mkdir(parents=True)
        self.registry_dir.mkdir(parents=True)
        self.crs_configs_dir.mkdir(parents=True)

        # Create executor
        self.executor = CRSPatchExecutor(
            crs_config_name="test-crs",
            oss_fuzz_path=self.oss_fuzz_path,
            registry_dir=self.registry_dir,
            benchmarks_root=self.benchmarks_root,
            crs_configs_dir=self.crs_configs_dir,
            litellm_mode="passthrough",
        )

    def test_init(self):
        """Test executor initialization."""
        self.assertEqual(self.executor.crs_config_name, "test-crs")
        self.assertEqual(self.executor.oss_fuzz_path, self.oss_fuzz_path)
        self.assertEqual(self.executor.registry_dir, self.registry_dir)
        self.assertEqual(self.executor.benchmarks_root, self.benchmarks_root)
        self.assertEqual(self.executor.crs_configs_dir, self.crs_configs_dir)
        self.assertEqual(self.executor.litellm_mode, "passthrough")

    def test_configure_crs(self):
        """Test CRS configuration."""
        config = {"hints_enabled": True, "hints_corpus_level": "1h"}
        self.executor.configure_crs(config)
        self.assertEqual(self.executor.config, config)

    def test_extract_project_name(self):
        """Test project name extraction from path."""
        benchmark_path = Path("/path/to/benchmarks/json-c")
        project_name = self.executor._extract_project_name(benchmark_path)
        self.assertEqual(project_name, "json-c")

    def test_prepare_output_directory(self):
        """Test output directory preparation."""
        trial_dir = Path(self.temp_dir) / "trial-1"
        self.executor._prepare_output_directory(trial_dir)

        output_dir = trial_dir / "output"
        self.assertTrue(output_dir.exists())
        self.assertTrue(output_dir.is_dir())

    @patch("crsbench.utils.crs_helper.get_crs_registry_name")
    @patch("crsbench.benchmark.runtime.loader.prepare_source_from_bundle")
    @patch("subprocess.run")
    def test_build_crs_if_needed(
        self, mock_run, mock_prepare_source, mock_get_registry_name
    ):
        """Test CRS build with bundled source (pkgs/)."""
        # Mock CRS registry name lookup
        mock_get_registry_name.return_value = "test-crs-registry"

        # Mock successful build
        mock_run.return_value = Mock(returncode=0, stdout="Build successful", stderr="")

        benchmark_path = self.benchmarks_root / "test-project"
        benchmark_path.mkdir()

        # Create fake pkgs/ directory with a dummy tarball (for has_bundled_source)
        pkgs_dir = benchmark_path / "pkgs"
        pkgs_dir.mkdir()
        (pkgs_dir / "source.tar.gz").write_bytes(b"fake tarball")

        trial_build_dir = Path(self.temp_dir) / "trial-build"
        trial_build_dir.mkdir()

        trial_registry_dir = Path(self.temp_dir) / "trial-registry"
        trial_registry_dir.mkdir()

        # Configure executor to use pkgs mode (bundled source)
        self.executor.configure_crs({"source_mode": "pkgs"})

        # Mock prepare_source_from_bundle to return a path (bundled source has path now)
        mock_prepare_source.return_value = trial_build_dir / "src" / "test-project"

        # Build CRS
        self.executor._build_crs_if_needed(
            benchmark_path,
            "test-project",
            trial_build_dir,
            trial_registry_dir,
        )

        # Verify build command was called
        mock_run.assert_called_once()
        call_args = mock_run.call_args
        cmd = call_args[0][0]

        # Check command structure
        self.assertEqual(cmd[0], "oss-bugfix-crs")
        self.assertEqual(cmd[1], "build")
        self.assertEqual(cmd[2], "test-crs-registry")  # Registry name from mock
        self.assertEqual(cmd[3], "test-project")
        self.assertIn("--oss-fuzz", cmd)
        self.assertIn("--project-path", cmd)
        # Both bundled and cloned sources now have paths, so --source-path is passed
        self.assertIn("--source-path", cmd)
        self.assertIn("--registry", cmd)
        self.assertIn("--work-dir", cmd)

        # Verify project is cached
        self.assertIn("test-crs:test-project", self.executor.built_projects)

    @patch("crsbench.utils.repo_manager.ensure_project_repository")
    @patch("subprocess.run")
    def test_build_crs_already_built(self, mock_run, mock_ensure_repo):
        """Test that CRS build is skipped if already built."""
        # Pre-populate cache
        self.executor.built_projects.add("test-crs:test-project")

        benchmark_path = self.benchmarks_root / "test-project"
        benchmark_path.mkdir()

        trial_build_dir = Path(self.temp_dir) / "trial-build"
        trial_build_dir.mkdir()

        trial_registry_dir = Path(self.temp_dir) / "trial-registry"
        trial_registry_dir.mkdir()

        # Attempt to build
        self.executor._build_crs_if_needed(
            benchmark_path,
            "test-project",
            trial_build_dir,
            trial_registry_dir,
        )

        # Verify no calls were made
        mock_ensure_repo.assert_not_called()
        mock_run.assert_not_called()

    def test_prepare_povs(self):
        """Test POVs directory preparation."""
        # Create mock benchmark structure with multiple CPVs
        benchmark_path = self.benchmarks_root / "test-project"
        harness_dir = benchmark_path / ".aixcc" / "test_harness"

        # Create cpv_0 with a POV blob
        cpv_0_blobs = harness_dir / "cpv_0" / "blobs"
        cpv_0_blobs.mkdir(parents=True)
        (cpv_0_blobs / "pov_0.blob").write_text("mock pov 0")

        # Create cpv_1 with a POV blob
        cpv_1_blobs = harness_dir / "cpv_1" / "blobs"
        cpv_1_blobs.mkdir(parents=True)
        (cpv_1_blobs / "pov_1.blob").write_text("mock pov 1")

        trial_dir = Path(self.temp_dir) / "trial-1"

        # Prepare POVs
        povs_path = self.executor._prepare_povs(
            benchmark_path, "test_harness", trial_dir
        )

        # Verify directory created
        self.assertIsNotNone(povs_path)
        assert povs_path is not None  # for type checker
        self.assertTrue(povs_path.exists())

        # Verify POVs copied with CPV ID as filename (one per CPV)
        self.assertTrue((povs_path / "cpv_0").exists())
        self.assertTrue((povs_path / "cpv_1").exists())

    def test_prepare_hints_disabled(self):
        """Test that hints preparation is skipped when disabled."""
        benchmark_path = self.benchmarks_root / "test-project"
        trial_dir = Path(self.temp_dir) / "trial-1"

        # Configure with hints disabled
        self.executor.configure_crs({"hints_enabled": False})

        # Prepare hints
        hints_path = self.executor._prepare_hints(
            benchmark_path, "test_harness", trial_dir
        )

        # Verify None returned
        self.assertIsNone(hints_path)

    def test_prepare_hints_enabled(self):
        """Test hints directory preparation when enabled."""
        # Create mock benchmark structure
        benchmark_path = self.benchmarks_root / "test-project"
        hints_dir = benchmark_path / ".aixcc" / "test_harness" / "hints"
        sarif_dir = hints_dir / "sarif"
        corpus_dir = hints_dir / "corpus" / "1h"
        sarif_dir.mkdir(parents=True)
        corpus_dir.mkdir(parents=True)

        # Create mock hints
        (sarif_dir / "codeql.sarif").write_text("{}")
        (corpus_dir / "input-001").write_text("test input")

        trial_dir = Path(self.temp_dir) / "trial-1"

        # Configure with hints enabled
        self.executor.configure_crs({"hints_enabled": True, "hints_corpus_level": "1h"})

        # Prepare hints
        hints_path = self.executor._prepare_hints(
            benchmark_path, "test_harness", trial_dir
        )

        # Verify directory created
        self.assertIsNotNone(hints_path)
        assert hints_path is not None  # for type checker
        self.assertTrue(hints_path.exists())

        # Verify SARIF copied
        self.assertTrue((hints_path / "sarif" / "codeql.sarif").exists())

        # Verify corpus copied
        self.assertTrue((hints_path / "corpus" / "input-001").exists())

    def test_store_execution_metadata(self):
        """Test execution metadata storage."""
        trial_dir = Path(self.temp_dir) / "trial-1"
        trial_dir.mkdir()

        cmd = ["oss-bugfix-crs", "run", "test-crs", "test-project"]
        hints_path = Path("/path/to/hints")
        povs_path = Path("/path/to/povs")

        self.executor._store_execution_metadata(
            trial_output_dir=trial_dir,
            cmd=cmd,
            hints_path=hints_path,
            povs_path=povs_path,
            execution_time=100.5,
            returncode=0,
        )

        # Verify file created
        execution_file = trial_dir / "execution.json"
        self.assertTrue(execution_file.exists())

        # Verify content
        with open(execution_file) as f:
            metadata = json.load(f)

        self.assertEqual(metadata["command"], cmd)
        self.assertEqual(metadata["execution"]["duration_seconds"], 100.5)
        self.assertEqual(metadata["execution"]["returncode"], 0)
        self.assertTrue(metadata["execution"]["success"])
        self.assertTrue(metadata["hints"]["enabled"])
        self.assertTrue(metadata["povs"]["provided"])

    def test_collect_patches(self):
        """Test patch collection from output directory."""
        trial_dir = Path(self.temp_dir) / "trial-1"
        patches_dir = trial_dir / "output" / "patches"
        patches_dir.mkdir(parents=True)

        # Create mock patches
        pov_0_dir = patches_dir / "pov_0"
        pov_0_dir.mkdir()
        (pov_0_dir / "patch.diff").write_text("mock patch 0")

        pov_1_dir = patches_dir / "pov_1"
        pov_1_dir.mkdir()
        (pov_1_dir / "patch.diff").write_text("mock patch 1")

        # Collect patches
        patches = self.executor._collect_patches(trial_dir)

        # Verify patches collected
        self.assertEqual(len(patches), 2)
        self.assertIn("pov_0", patches)
        self.assertIn("pov_1", patches)
        self.assertEqual(patches["pov_0"], "mock patch 0")
        self.assertEqual(patches["pov_1"], "mock patch 1")


if __name__ == "__main__":
    unittest.main()
