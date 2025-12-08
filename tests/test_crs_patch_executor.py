"""Basic tests for CRSPatchExecutor."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

from crsbench.evaluation.crs_patch_executor import CRSPatchExecutor
from crsbench.evaluation.crs_executor import CRSResult
from crsbench.evaluation.results import POVStatus
from crsbench.validation.schemas import HarnessFile, POV, Vulnerability


class TestCRSPatchExecutor(unittest.TestCase):
    """Test CRSPatchExecutor functionality."""

    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.crs_patch_path = Path(self.temp_dir) / "oss-patch"
        self.oss_fuzz_path = Path(self.temp_dir) / "oss-fuzz"
        self.benchmarks_root = Path(self.temp_dir) / "benchmarks"

        # Create directory structure
        self.crs_patch_path.mkdir(parents=True)
        self.oss_fuzz_path.mkdir(parents=True)
        self.benchmarks_root.mkdir(parents=True)

        # Create executor
        self.crs_configs_dir = Path(self.temp_dir) / "crses" / "configs"
        self.crs_configs_dir.mkdir(parents=True)

        self.executor = CRSPatchExecutor(
            crs_config_name="test-crs",
            crs_patch_path=self.crs_patch_path,
            oss_fuzz_path=self.oss_fuzz_path,
            litellm_base="https://api.test.com",
            litellm_key="test-key",
            benchmarks_root=self.benchmarks_root,
            crs_configs_dir=self.crs_configs_dir
        )

    def test_init(self):
        """Test executor initialization."""
        self.assertEqual(self.executor.crs_config_name, "test-crs")
        self.assertEqual(self.executor.crs_patch_path, self.crs_patch_path)
        self.assertEqual(self.executor.oss_fuzz_path, self.oss_fuzz_path)
        self.assertEqual(self.executor.litellm_base, "https://api.test.com")
        self.assertEqual(self.executor.litellm_key, "test-key")
        self.assertEqual(self.executor.benchmarks_root, self.benchmarks_root)

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

    @patch('crsbench.utils.repo_manager.ensure_project_repository')
    @patch('subprocess.run')
    def test_build_crs_if_needed(self, mock_run, mock_ensure_repo):
        """Test CRS build with repository manager integration."""
        # Mock repository manager response
        mock_ensure_repo.return_value = "/path/to/source"

        # Mock successful build
        mock_run.return_value = Mock(returncode=0, stdout="Build successful", stderr="")

        benchmark_path = self.benchmarks_root / "test-project"
        benchmark_path.mkdir()

        # Build CRS
        self.executor._build_crs_if_needed(benchmark_path, "test-project")

        # Verify repository manager was called
        mock_ensure_repo.assert_called_once_with(
            benchmark_dir=str(benchmark_path),
            verbose=False
        )

        # Verify build command was called
        mock_run.assert_called_once()
        call_args = mock_run.call_args
        cmd = call_args[0][0]

        # Check command structure
        self.assertEqual(cmd[0], "oss-bugfix-crs")
        self.assertEqual(cmd[1], "build")
        self.assertEqual(cmd[2], "test-crs")
        self.assertEqual(cmd[3], "test-project")
        self.assertIn("--oss-fuzz", cmd)
        self.assertIn("--project-path", cmd)
        self.assertIn("--source-path", cmd)

        # Verify project is cached
        self.assertIn("test-crs:test-project", self.executor.built_projects)

    @patch('crsbench.utils.repo_manager.ensure_project_repository')
    @patch('subprocess.run')
    def test_build_crs_already_built(self, mock_run, mock_ensure_repo):
        """Test that CRS build is skipped if already built."""
        # Pre-populate cache
        self.executor.built_projects.add("test-crs:test-project")

        benchmark_path = self.benchmarks_root / "test-project"
        benchmark_path.mkdir()

        # Attempt to build
        self.executor._build_crs_if_needed(benchmark_path, "test-project")

        # Verify no calls were made
        mock_ensure_repo.assert_not_called()
        mock_run.assert_not_called()

    def test_prepare_povs(self):
        """Test POVs directory preparation."""
        # Create mock benchmark structure
        benchmark_path = self.benchmarks_root / "test-project"
        harness_dir = benchmark_path / ".aixcc" / "test_harness"
        cpv_dir = harness_dir / "cpv_0" / "blobs"
        cpv_dir.mkdir(parents=True)

        # Create mock POV blobs
        (cpv_dir / "pov_0.blob").write_text("mock pov 0")
        (cpv_dir / "pov_1.blob").write_text("mock pov 1")

        trial_dir = Path(self.temp_dir) / "trial-1"

        # Prepare POVs
        povs_path = self.executor._prepare_povs(benchmark_path, "test_harness", trial_dir)

        # Verify directory created
        self.assertIsNotNone(povs_path)
        self.assertTrue(povs_path.exists())

        # Verify POVs copied
        self.assertTrue((povs_path / "pov_0").exists())
        self.assertTrue((povs_path / "pov_1").exists())

    def test_prepare_hints_disabled(self):
        """Test that hints preparation is skipped when disabled."""
        benchmark_path = self.benchmarks_root / "test-project"
        trial_dir = Path(self.temp_dir) / "trial-1"

        # Configure with hints disabled
        self.executor.configure_crs({"hints_enabled": False})

        # Prepare hints
        hints_path = self.executor._prepare_hints(benchmark_path, "test_harness", trial_dir)

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
        hints_path = self.executor._prepare_hints(benchmark_path, "test_harness", trial_dir)

        # Verify directory created
        self.assertIsNotNone(hints_path)
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
            returncode=0
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

    def test_process_pov_results_with_patches(self):
        """Test POV result processing when patches are generated."""
        trial_dir = Path(self.temp_dir) / "trial-1"
        patches_dir = trial_dir / "output" / "patches"
        patches_dir.mkdir(parents=True)

        # Create mock patch for pov_0
        pov_0_dir = patches_dir / "pov_0"
        pov_0_dir.mkdir()
        (pov_0_dir / "patch.diff").write_text("mock patch")

        # Create CRS result
        crs_result = CRSResult(
            harness_name="test_harness",
            execution_time=100.0,
            success=True,
            output="CRS executed successfully"
        )

        # Create harness with POVs
        harness = HarnessFile(
            name="test_harness",
            path="/src/test_harness.c",
            vulns=[
                Vulnerability(
                    vuln_keyword="heap-overflow",
                    povs=[
                        POV(id="pov_0", sanitizer="address", error_token="heap-buffer-overflow"),
                        POV(id="pov_1", sanitizer="address", error_token="use-after-free")
                    ]
                )
            ]
        )

        # Process POV results
        pov_results = self.executor.process_pov_results(crs_result, harness, trial_dir)

        # Verify results
        self.assertEqual(len(pov_results), 2)
        self.assertEqual(pov_results[0].status, POVStatus.FOUND)  # patch exists for pov_0
        self.assertEqual(pov_results[1].status, POVStatus.MISSED)  # no patch for pov_1

    def test_process_pov_results_execution_failed(self):
        """Test POV result processing when CRS execution failed."""
        trial_dir = Path(self.temp_dir) / "trial-1"

        # Create failed CRS result
        crs_result = CRSResult(
            harness_name="test_harness",
            execution_time=10.0,
            success=False,
            output="",
            error="Build failed"
        )

        # Create harness with POVs
        harness = HarnessFile(
            name="test_harness",
            path="/src/test_harness.c",
            vulns=[
                Vulnerability(
                    vuln_keyword="heap-overflow",
                    povs=[
                        POV(id="pov_0", sanitizer="address", error_token="heap-buffer-overflow")
                    ]
                )
            ]
        )

        # Process POV results
        pov_results = self.executor.process_pov_results(crs_result, harness, trial_dir)

        # Verify all POVs marked as ERROR
        self.assertEqual(len(pov_results), 1)
        self.assertEqual(pov_results[0].status, POVStatus.ERROR)
        self.assertEqual(pov_results[0].error_message, "Build failed")


if __name__ == "__main__":
    unittest.main()
