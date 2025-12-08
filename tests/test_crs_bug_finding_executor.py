"""Basic tests for CRSBugFindingExecutor."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

from crsbench.evaluation.crs_bug_finding_executor import CRSBugFindingExecutor, ExecutorError
from crsbench.evaluation.crs_executor import CRSResult
from crsbench.validation.schemas import HarnessFile, POV, Vulnerability


class TestCRSBugFindingExecutor(unittest.TestCase):
    """Test CRSBugFindingExecutor functionality."""

    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.oss_fuzz_path = Path(self.temp_dir) / "oss-fuzz"
        self.registry_dir = Path(self.temp_dir) / "crses"
        self.benchmarks_root = Path(self.temp_dir) / "benchmarks"

        # Create directory structure
        self.oss_fuzz_path.mkdir(parents=True)
        self.registry_dir.mkdir(parents=True)
        self.benchmarks_root.mkdir(parents=True)

        # Create executor
        self.crs_configs_dir = Path(self.temp_dir) / "crses" / "configs"
        self.crs_configs_dir.mkdir(parents=True)

        self.executor = CRSBugFindingExecutor(
            crs_config_name="test-crs",
            oss_fuzz_path=self.oss_fuzz_path,
            registry_dir=self.registry_dir,
            benchmarks_root=self.benchmarks_root,
            crs_configs_dir=self.crs_configs_dir
        )

    def test_init(self):
        """Test executor initialization."""
        self.assertEqual(self.executor.crs_config_name, "test-crs")
        self.assertEqual(self.executor.oss_fuzz_path, self.oss_fuzz_path)
        self.assertEqual(self.executor.registry_dir, self.registry_dir)
        self.assertEqual(self.executor.benchmarks_root, self.benchmarks_root)
        self.assertEqual(self.executor.config, {})
        self.assertEqual(self.executor.built_projects, set())

    def test_configure_crs(self):
        """Test CRS configuration."""
        config = {
            "hints_enabled": True,
            "hints_corpus_level": "1h",
            "build_timeout": 600,
            "run_timeout": 1200
        }
        self.executor.configure_crs(config)

        self.assertEqual(self.executor.config["hints_enabled"], True)
        self.assertEqual(self.executor.config["hints_corpus_level"], "1h")
        self.assertEqual(self.executor.config["build_timeout"], 600)
        self.assertEqual(self.executor.config["run_timeout"], 1200)

    def test_configure_crs_defaults(self):
        """Test CRS configuration with defaults."""
        self.executor.configure_crs({})

        self.assertEqual(self.executor.config["build_timeout"], 3600)
        self.assertEqual(self.executor.config["run_timeout"], 7200)
        self.assertEqual(self.executor.config["hints_enabled"], False)
        self.assertEqual(self.executor.config["hints_corpus_level"], "1h")

    def test_extract_project_name(self):
        """Test project name extraction from path."""
        benchmark_path = Path("/path/to/benchmarks/json-c-delta-01")
        project_name = self.executor._extract_project_name(benchmark_path)
        self.assertEqual(project_name, "json-c-delta-01")

    def test_find_source_path(self):
        """Test source path finding."""
        build_dir = Path(self.temp_dir) / "build"
        source_path = build_dir / "src" / "test-project"
        source_path.mkdir(parents=True)

        found_path = self.executor._find_source_path(build_dir, "test-project")
        self.assertEqual(found_path, source_path)

    def test_get_crs_output_dir(self):
        """Test CRS output directory derivation."""
        build_dir = Path(self.temp_dir) / "build"
        output_dir = self.executor._get_crs_output_dir(build_dir, "test-project")

        expected = build_dir / "out" / "test-crs" / "test-project"
        self.assertEqual(output_dir, expected)

    def test_resolve_crs_config_dir_absolute(self):
        """Test CRS config directory resolution with absolute path."""
        # Create a test config directory
        test_config = Path(self.temp_dir) / "test-config"
        test_config.mkdir()

        # Create new executor with absolute path
        executor = CRSBugFindingExecutor(
            crs_config_name=str(test_config),
            oss_fuzz_path=self.oss_fuzz_path,
            registry_dir=self.registry_dir,
            benchmarks_root=self.benchmarks_root,
            crs_configs_dir=self.crs_configs_dir
        )

        resolved = executor._resolve_crs_config_dir()
        self.assertEqual(resolved, test_config)

    def test_resolve_crs_config_dir_missing(self):
        """Test CRS config directory resolution with missing config."""
        self.executor.crs_config_name = "nonexistent-crs"

        with self.assertRaises(ExecutorError) as context:
            self.executor._resolve_crs_config_dir()

        self.assertIn("CRS config directory not found", str(context.exception))

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

        # Create temp test-crs config directory and use absolute path
        test_crs_dir = Path(self.temp_dir) / "test-crs-config"
        test_crs_dir.mkdir(parents=True)

        # Create executor with absolute path for config
        executor = CRSBugFindingExecutor(
            crs_config_name=str(test_crs_dir),  # Use absolute path
            oss_fuzz_path=self.oss_fuzz_path,
            registry_dir=self.registry_dir,
            benchmarks_root=self.benchmarks_root,
            crs_configs_dir=self.crs_configs_dir
        )

        trial_build_dir = Path(self.temp_dir) / "trial-0" / "build"
        trial_build_dir.mkdir(parents=True)

        # Build CRS
        executor._build_crs_if_needed(benchmark_path, "test-project", trial_build_dir)

        # Verify repository manager was called with project_dir
        expected_source_dest = trial_build_dir / "src" / "test-project"
        mock_ensure_repo.assert_called_once_with(
            benchmark_dir=str(benchmark_path),
            project_dir=str(expected_source_dest),
            verbose=False
        )

        # Verify build command was called
        mock_run.assert_called_once()
        call_args = mock_run.call_args
        cmd = call_args[0][0]

        # Check command structure
        self.assertEqual(cmd[0], "oss-bugfind-crs")
        self.assertEqual(cmd[1], "build")
        self.assertIn("--build-dir", cmd)
        self.assertIn("--oss-fuzz-dir", cmd)
        self.assertIn("--registry-dir", cmd)
        self.assertIn("--project-path", cmd)

        # Verify project is cached (use the executor instance we created)
        cache_key = f"{test_crs_dir}:test-project"
        self.assertIn(cache_key, executor.built_projects)

    @patch('crsbench.utils.repo_manager.ensure_project_repository')
    @patch('subprocess.run')
    def test_build_crs_already_built(self, mock_run, mock_ensure_repo):
        """Test that CRS build is skipped if already built."""
        # Pre-populate cache
        self.executor.built_projects.add("test-crs:test-project")

        benchmark_path = self.benchmarks_root / "test-project"
        benchmark_path.mkdir()

        trial_build_dir = Path(self.temp_dir) / "trial-0" / "build"

        # Attempt to build
        self.executor._build_crs_if_needed(benchmark_path, "test-project", trial_build_dir)

        # Verify no calls were made
        mock_ensure_repo.assert_not_called()
        mock_run.assert_not_called()

    def test_construct_run_command_no_output(self):
        """Test run command construction without --output parameter."""
        # Create test-crs config directory
        test_crs_dir = Path(self.temp_dir) / "test-crs-config"
        test_crs_dir.mkdir(parents=True)

        # Create executor with absolute path
        executor = CRSBugFindingExecutor(
            crs_config_name=str(test_crs_dir),
            oss_fuzz_path=self.oss_fuzz_path,
            registry_dir=self.registry_dir,
            benchmarks_root=self.benchmarks_root,
            crs_configs_dir=self.crs_configs_dir
        )

        trial_build_dir = Path(self.temp_dir) / "trial-0" / "build"

        cmd = executor._construct_run_command(
            project_name="test-project",
            harness_name="test_harness",
            trial_build_dir=trial_build_dir,
            hints_path=None
        )

        # Verify command structure
        self.assertEqual(cmd[0], "oss-bugfind-crs")
        self.assertEqual(cmd[1], "run")
        self.assertIn("--build-dir", cmd)
        self.assertIn("--oss-fuzz-dir", cmd)
        self.assertIn("--registry-dir", cmd)

        # CRITICAL: Verify --output is NOT present
        self.assertNotIn("--output", cmd)

    def test_construct_run_command_with_hints(self):
        """Test run command construction with hints."""
        # Create test-crs config directory
        test_crs_dir = Path(self.temp_dir) / "test-crs-config"
        test_crs_dir.mkdir(parents=True)

        # Create executor with absolute path
        executor = CRSBugFindingExecutor(
            crs_config_name=str(test_crs_dir),
            oss_fuzz_path=self.oss_fuzz_path,
            registry_dir=self.registry_dir,
            benchmarks_root=self.benchmarks_root,
            crs_configs_dir=self.crs_configs_dir
        )

        trial_build_dir = Path(self.temp_dir) / "trial-0" / "build"
        hints_path = Path(self.temp_dir) / "hints"
        hints_path.mkdir()

        cmd = executor._construct_run_command(
            project_name="test-project",
            harness_name="test_harness",
            trial_build_dir=trial_build_dir,
            hints_path=hints_path
        )

        # Verify hints parameter present
        self.assertIn("--hints", cmd)
        hints_idx = cmd.index("--hints")
        self.assertEqual(cmd[hints_idx + 1], str(hints_path))

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
        build_dir = trial_dir / "build"
        build_dir.mkdir()

        cmd = ["oss-bugfind-crs", "run", "test-crs", "test-project", "test_harness"]
        hints_path = Path("/path/to/hints")

        harness = HarnessFile(
            name="test_harness.c",
            path="/src/test_harness.c",
            vulns=[]
        )

        self.executor._store_execution_metadata(
            trial_output_dir=trial_dir,
            harness=harness,
            cmd=cmd,
            hints_path=hints_path,
            execution_time=100.5,
            returncode=0,
            stdout="success",
            stderr=""
        )

        # Verify file created
        execution_file = trial_dir / "execution.json"
        self.assertTrue(execution_file.exists())

        # Verify content
        with open(execution_file) as f:
            metadata = json.load(f)

        self.assertEqual(metadata["executor"], "CRSBugFindingExecutor")
        self.assertEqual(metadata["execution_time"], 100.5)
        self.assertEqual(metadata["execution"]["returncode"], 0)
        self.assertTrue(metadata["execution"]["success"])
        self.assertTrue(metadata["hints"]["enabled"])

    def test_process_pov_results_stub(self):
        """Test POV result processing (stub implementation)."""
        trial_dir = Path(self.temp_dir) / "trial-1"
        trial_dir.mkdir()

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
                        POV(id="pov_0", sanitizer="address", error_token="heap-buffer-overflow")
                    ]
                )
            ]
        )

        # Process POV results
        pov_results = self.executor.process_pov_results(crs_result, harness, trial_dir)

        # Verify empty list returned (stub implementation)
        self.assertEqual(len(pov_results), 0)
        self.assertIsInstance(pov_results, list)

    @patch('crsbench.utils.repo_manager.ensure_project_repository')
    @patch('subprocess.run')
    def test_run_crs_success(self, mock_run, mock_ensure_repo):
        """Test successful CRS execution."""
        # Setup
        benchmark_path = self.benchmarks_root / "test-project"
        benchmark_path.mkdir()

        trial_dir = Path(self.temp_dir) / "trial-0"
        trial_dir.mkdir()
        build_dir = trial_dir / "build"
        build_dir.mkdir()
        source_dir = build_dir / "src" / "test-project"
        source_dir.mkdir(parents=True)

        # Create test-crs config directory
        test_crs_dir = Path(self.temp_dir) / "test-crs-config"
        test_crs_dir.mkdir(parents=True)

        # Create executor with absolute path
        executor = CRSBugFindingExecutor(
            crs_config_name=str(test_crs_dir),
            oss_fuzz_path=self.oss_fuzz_path,
            registry_dir=self.registry_dir,
            benchmarks_root=self.benchmarks_root,
            crs_configs_dir=self.crs_configs_dir
        )

        harness = HarnessFile(
            name="test_harness.c",
            path="/src/test_harness.c",
            vulns=[]
        )

        # Mock repository manager
        mock_ensure_repo.return_value = str(source_dir)

        # Mock subprocess calls (build + run)
        mock_run.side_effect = [
            Mock(returncode=0, stdout="Build successful", stderr=""),  # Build
            Mock(returncode=0, stdout="Run successful", stderr="")     # Run
        ]

        # Configure and run
        executor.configure_crs({"hints_enabled": False})
        result = executor.run_crs(benchmark_path, harness, trial_dir)

        # Verify result
        self.assertTrue(result.success)
        self.assertEqual(result.harness_name, harness.name)
        self.assertGreater(result.execution_time, 0)
        self.assertEqual(result.output, "Run successful")

        # Verify execution metadata created
        self.assertTrue((trial_dir / "execution.json").exists())

    @patch('crsbench.utils.repo_manager.ensure_project_repository')
    @patch('subprocess.run')
    def test_run_crs_timeout(self, mock_run, mock_ensure_repo):
        """Test CRS execution timeout handling."""
        # Setup
        benchmark_path = self.benchmarks_root / "test-project"
        benchmark_path.mkdir()

        trial_dir = Path(self.temp_dir) / "trial-0"
        trial_dir.mkdir()
        build_dir = trial_dir / "build"
        build_dir.mkdir()
        source_dir = build_dir / "src" / "test-project"
        source_dir.mkdir(parents=True)

        # Create test-crs config directory
        test_crs_dir = Path(self.temp_dir) / "test-crs-config"
        test_crs_dir.mkdir(parents=True)

        # Create executor with absolute path
        executor = CRSBugFindingExecutor(
            crs_config_name=str(test_crs_dir),
            oss_fuzz_path=self.oss_fuzz_path,
            registry_dir=self.registry_dir,
            benchmarks_root=self.benchmarks_root,
            crs_configs_dir=self.crs_configs_dir
        )

        harness = HarnessFile(
            name="test_harness.c",
            path="/src/test_harness.c",
            vulns=[]
        )

        # Mock repository manager
        mock_ensure_repo.return_value = str(source_dir)

        # Mock build success, run timeout
        import subprocess
        timeout_error = subprocess.TimeoutExpired(cmd=["oss-bugfind-crs", "run"], timeout=10)
        timeout_error.stdout = b"partial output"
        timeout_error.stderr = b""

        mock_run.side_effect = [
            Mock(returncode=0, stdout="Build successful", stderr=""),  # Build
            timeout_error  # Run timeout
        ]

        # Configure and run
        executor.configure_crs({"hints_enabled": False, "run_timeout": 10})
        result = executor.run_crs(benchmark_path, harness, trial_dir)

        # Verify result
        self.assertFalse(result.success)
        self.assertIn("Timeout", result.error)


if __name__ == "__main__":
    unittest.main()
