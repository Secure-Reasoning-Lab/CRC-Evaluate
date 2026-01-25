"""Tests for trial directory preparation module."""

import json
from unittest.mock import patch

import pytest
from crsbench.evaluation.trial_preparation import (
    SourceCloneError,
    TrialDirectoryPreparer,
    TrialPreparationResult,
)

# Fixtures


@pytest.fixture
def temp_experiment_dir(tmp_path):
    """Create temporary experiment directory."""
    exp_dir = tmp_path / "experiment-1"
    exp_dir.mkdir()
    return exp_dir


@pytest.fixture
def temp_benchmarks_dir(tmp_path):
    """Create temporary benchmarks directory."""
    benchmarks_dir = tmp_path / "benchmarks"
    benchmarks_dir.mkdir()
    return benchmarks_dir


@pytest.fixture
def temp_oss_fuzz_dir(tmp_path):
    """Create temporary oss-fuzz directory."""
    oss_fuzz_dir = tmp_path / "oss-fuzz"
    oss_fuzz_dir.mkdir()
    return oss_fuzz_dir


@pytest.fixture
def mock_benchmark(temp_benchmarks_dir):
    """Create mock benchmark directory structure."""
    benchmark_dir = temp_benchmarks_dir / "test-bench"
    benchmark_dir.mkdir()

    # Create .aixcc directory
    aixcc_dir = benchmark_dir / ".aixcc"
    aixcc_dir.mkdir()

    # Create meta.yaml
    meta_yaml = aixcc_dir / "meta.yaml"
    meta_yaml.write_text("""
delta_mode:
  base_commit: "abc123def456"
  ref_commit: "def456abc789"
""")

    # Create project.yaml
    project_yaml = benchmark_dir / "project.yaml"
    project_yaml.write_text("""
language: c
main_repo: https://github.com/test/test-repo.git
""")

    return benchmark_dir


@pytest.fixture
def mock_benchmark_with_hints(mock_benchmark):
    """Create mock benchmark with hints in new aggregated structure."""
    # Update meta.yaml to include harness_files structure
    meta_yaml = mock_benchmark / ".aixcc" / "meta.yaml"
    meta_yaml.write_text("""
delta_mode:
  base_commit: "abc123def456"
  ref_commit: "def456abc789"

harness_files:
  - name: test_harness
    vulns:
      - vuln_keyword: cpv_0
      - vuln_keyword: cpv_1
""")

    # Create cpv_0 hints with SARIF
    cpv_0_hints = mock_benchmark / ".aixcc" / "test_harness" / "cpv_0" / "hints"
    cpv_0_hints.mkdir(parents=True)
    (cpv_0_hints / "level_1.sarif").write_text('{"version": "2.1.0", "runs": []}')
    (cpv_0_hints / "level_2.sarif").write_text('{"version": "2.1.0", "runs": []}')

    # Create cpv_1 hints with SARIF
    cpv_1_hints = mock_benchmark / ".aixcc" / "test_harness" / "cpv_1" / "hints"
    cpv_1_hints.mkdir(parents=True)
    (cpv_1_hints / "level_1.sarif").write_text('{"version": "2.1.0", "runs": []}')

    return mock_benchmark


@pytest.fixture
def mock_benchmark_with_povs(mock_benchmark):
    """Create mock benchmark with POVs."""
    harness_dir = mock_benchmark / ".aixcc" / "test_harness"
    harness_dir.mkdir()

    # Create cpv directories with POV blobs
    cpv_0 = harness_dir / "cpv_0" / "blobs"
    cpv_0.mkdir(parents=True)
    (cpv_0 / "pov_0.blob").write_bytes(b"POV data 0")

    cpv_1 = harness_dir / "cpv_1" / "blobs"
    cpv_1.mkdir(parents=True)
    (cpv_1 / "pov_1.blob").write_bytes(b"POV data 1")
    (cpv_1 / "pov_2.blob").write_bytes(b"POV data 2")

    return mock_benchmark


@pytest.fixture
def preparer(temp_experiment_dir, temp_benchmarks_dir, temp_oss_fuzz_dir):
    """Create TrialDirectoryPreparer instance."""
    config = {"hints_enabled": False, "verbose": False}
    return TrialDirectoryPreparer(
        experiment_dir=temp_experiment_dir,
        benchmarks_root=temp_benchmarks_dir,
        oss_fuzz_dir=temp_oss_fuzz_dir,
        config=config,
    )


# Tests


class TestTrialDirectoryCreation:
    """Tests for trial directory creation."""

    def test_create_trial_directory(self, preparer):
        """Test basic trial directory creation."""
        trial_dir = preparer._create_trial_directory(0)

        assert trial_dir.exists()
        assert trial_dir.name == "trial-0"
        assert trial_dir.parent == preparer.experiment_dir

    def test_create_multiple_trial_directories(self, preparer):
        """Test creating multiple trial directories."""
        trial_dir_0 = preparer._create_trial_directory(0)
        trial_dir_1 = preparer._create_trial_directory(1)
        trial_dir_2 = preparer._create_trial_directory(2)

        assert trial_dir_0.name == "trial-0"
        assert trial_dir_1.name == "trial-1"
        assert trial_dir_2.name == "trial-2"
        assert all(d.exists() for d in [trial_dir_0, trial_dir_1, trial_dir_2])


class TestSourceCodePreparation:
    """Tests for source code preparation."""

    def test_prepare_source_code_success(self, preparer, mock_benchmark):
        """Test successful source code preparation."""
        build_dir = preparer.experiment_dir / "trial-0" / "crs-build"
        build_dir.mkdir(parents=True)

        with patch(
            "crsbench.utils.repo_manager.ensure_project_repository"
        ) as mock_ensure:
            mock_source_path = build_dir / "src" / "test-bench"
            mock_source_path.mkdir(parents=True)
            mock_ensure.return_value = str(mock_source_path)

            source_path = preparer._prepare_source_code("test-bench", build_dir)

            assert source_path == mock_source_path
            mock_ensure.assert_called_once()

    def test_prepare_source_code_benchmark_not_found(self, preparer):
        """Test source code preparation when benchmark doesn't exist."""
        build_dir = preparer.experiment_dir / "trial-0" / "crs-build"
        build_dir.mkdir(parents=True)

        with pytest.raises(SourceCloneError, match="Benchmark directory not found"):
            preparer._prepare_source_code("nonexistent-bench", build_dir)

    def test_prepare_source_code_clone_failure(self, preparer, mock_benchmark):
        """Test source code preparation when cloning fails."""
        build_dir = preparer.experiment_dir / "trial-0" / "crs-build"
        build_dir.mkdir(parents=True)

        with patch(
            "crsbench.utils.repo_manager.ensure_project_repository"
        ) as mock_ensure:
            mock_ensure.return_value = None

            with pytest.raises(SourceCloneError, match="Failed to clone source"):
                preparer._prepare_source_code("test-bench", build_dir)


class TestHintsPreparation:
    """Tests for hints preparation."""

    def test_prepare_hints_disabled_full_mode(
        self, preparer, mock_benchmark_with_hints
    ):
        """Test hints preparation when disabled and mode is full."""
        trial_dir = preparer.experiment_dir / "trial-0"
        trial_dir.mkdir()

        # Config has hints_enabled=False by default and no mode set (defaults to full)
        hints_dir = preparer._prepare_hints("test-bench", trial_dir)

        assert hints_dir is None

    def test_prepare_hints_disabled_delta_mode_with_ref_diff(
        self,
        mock_benchmark_with_hints,
        temp_experiment_dir,
        temp_benchmarks_dir,
        temp_oss_fuzz_dir,
    ):
        """Test that ref.diff is prepared for delta mode even when hints_enabled=False."""
        # Create ref.diff in the benchmark
        ref_diff_path = mock_benchmark_with_hints / ".aixcc" / "ref.diff"
        ref_diff_path.write_text(
            "--- a/file.c\n+++ b/file.c\n@@ -1 +1 @@\n-old\n+new\n"
        )

        # hints_enabled=False, but mode=delta
        config = {"hints_enabled": False, "mode": "delta"}
        preparer = TrialDirectoryPreparer(
            experiment_dir=temp_experiment_dir,
            benchmarks_root=temp_benchmarks_dir,
            oss_fuzz_dir=temp_oss_fuzz_dir,
            config=config,
        )

        trial_dir = temp_experiment_dir / "trial-0"
        trial_dir.mkdir()

        hints_dir = preparer._prepare_hints("test-bench", trial_dir)

        # Should return hints_dir with ref.diff even though hints_enabled=False
        assert hints_dir is not None
        assert hints_dir.exists()
        assert (hints_dir / "ref.diff").exists()
        # No SARIF files should be copied since hints_enabled=False
        sarif_files = list(hints_dir.glob("*.sarif"))
        assert len(sarif_files) == 0

    def test_prepare_hints_enabled(
        self,
        mock_benchmark_with_hints,
        temp_experiment_dir,
        temp_benchmarks_dir,
        temp_oss_fuzz_dir,
    ):
        """Test hints preparation when enabled."""
        config = {"hints_enabled": True, "hint_sarif_level": 1}
        preparer = TrialDirectoryPreparer(
            experiment_dir=temp_experiment_dir,
            benchmarks_root=temp_benchmarks_dir,
            oss_fuzz_dir=temp_oss_fuzz_dir,
            config=config,
        )

        trial_dir = temp_experiment_dir / "trial-0"
        trial_dir.mkdir()

        hints_dir = preparer._prepare_hints("test-bench", trial_dir)

        assert hints_dir is not None
        assert hints_dir.exists()
        # New structure: SARIF files are aggregated directly in hints_dir
        sarif_files = list(hints_dir.glob("*.sarif"))
        assert len(sarif_files) == 2  # cpv_0 and cpv_1 both have level_1.sarif

    def test_prepare_hints_sarif_level_2(
        self,
        mock_benchmark_with_hints,
        temp_experiment_dir,
        temp_benchmarks_dir,
        temp_oss_fuzz_dir,
    ):
        """Test hints preparation with SARIF level 2."""
        config = {"hints_enabled": True, "hint_sarif_level": 2}
        preparer = TrialDirectoryPreparer(
            experiment_dir=temp_experiment_dir,
            benchmarks_root=temp_benchmarks_dir,
            oss_fuzz_dir=temp_oss_fuzz_dir,
            config=config,
        )

        trial_dir = temp_experiment_dir / "trial-0"
        trial_dir.mkdir()

        hints_dir = preparer._prepare_hints("test-bench", trial_dir)

        assert hints_dir is not None
        sarif_files = list(hints_dir.glob("*.sarif"))
        assert len(sarif_files) == 1  # Only cpv_0 has level_2.sarif


class TestPOVsPreparation:
    """Tests for POVs preparation."""

    def test_prepare_povs_success(self, preparer, mock_benchmark_with_povs):
        """Test successful POVs preparation."""
        trial_dir = preparer.experiment_dir / "trial-0"
        trial_dir.mkdir()

        povs_dir = preparer._prepare_povs("test-bench", "test_harness", trial_dir)

        assert povs_dir is not None
        assert povs_dir.exists()
        # One POV per CPV: cpv_0 and cpv_1
        assert len(list(povs_dir.iterdir())) == 2

    def test_prepare_povs_flattened_structure(self, preparer, mock_benchmark_with_povs):
        """Test that POVs use CPV ID as filename (no subdirectories)."""
        trial_dir = preparer.experiment_dir / "trial-0"
        trial_dir.mkdir()

        povs_dir = preparer._prepare_povs("test-bench", "test_harness", trial_dir)

        # Check files are named with CPV IDs
        pov_files = [f.name for f in povs_dir.iterdir()]
        assert "cpv_0" in pov_files
        assert "cpv_1" in pov_files

        # Ensure no .blob extension
        assert not any(f.endswith(".blob") for f in pov_files)

    def test_prepare_povs_with_filter(
        self,
        mock_benchmark_with_povs,
        temp_experiment_dir,
        temp_benchmarks_dir,
        temp_oss_fuzz_dir,
    ):
        """Test POVs preparation with target_cpvs filter."""
        config = {
            "target_cpvs": ["cpv_0"]  # Only include cpv_0
        }
        preparer = TrialDirectoryPreparer(
            experiment_dir=temp_experiment_dir,
            benchmarks_root=temp_benchmarks_dir,
            oss_fuzz_dir=temp_oss_fuzz_dir,
            config=config,
        )

        trial_dir = temp_experiment_dir / "trial-0"
        trial_dir.mkdir()

        povs_dir = preparer._prepare_povs("test-bench", "test_harness", trial_dir)

        pov_files = [f.name for f in povs_dir.iterdir()]
        assert len(pov_files) == 1
        assert "cpv_0" in pov_files
        assert "cpv_1" not in pov_files

    def test_prepare_povs_no_harness_dir(self, preparer, mock_benchmark):
        """Test POVs preparation when harness directory doesn't exist."""
        trial_dir = preparer.experiment_dir / "trial-0"
        trial_dir.mkdir()

        povs_dir = preparer._prepare_povs(
            "test-bench", "nonexistent_harness", trial_dir
        )

        assert povs_dir is None

    def test_should_include_cpv_no_filter(self, preparer):
        """Test CPV inclusion without filter."""
        assert preparer._should_include_cpv("cpv_0") is True
        assert preparer._should_include_cpv("cpv_1") is True
        assert preparer._should_include_cpv("any_cpv") is True

    def test_should_include_cpv_with_filter(
        self, temp_experiment_dir, temp_benchmarks_dir, temp_oss_fuzz_dir
    ):
        """Test CPV inclusion with filter."""
        config = {"target_cpvs": ["cpv_0", "cpv_2"]}
        preparer = TrialDirectoryPreparer(
            experiment_dir=temp_experiment_dir,
            benchmarks_root=temp_benchmarks_dir,
            oss_fuzz_dir=temp_oss_fuzz_dir,
            config=config,
        )

        assert preparer._should_include_cpv("cpv_0") is True
        assert preparer._should_include_cpv("cpv_1") is False
        assert preparer._should_include_cpv("cpv_2") is True


class TestMetadataGeneration:
    """Tests for metadata generation."""

    def test_create_metadata(self, preparer, temp_experiment_dir):
        """Test metadata creation."""
        source_path = temp_experiment_dir / "src" / "test-bench"
        source_path.mkdir(parents=True)

        hints_dir = temp_experiment_dir / "hints"
        hints_dir.mkdir()
        (hints_dir / "sarif").mkdir()
        (hints_dir / "corpus").mkdir()

        povs_dir = temp_experiment_dir / "povs"
        povs_dir.mkdir()
        (povs_dir / "pov_0").write_bytes(b"test")

        with patch.object(preparer, "_get_git_commit", return_value="abc123"):
            metadata = preparer._create_metadata(
                crs="test-crs",
                benchmark="test-bench",
                harness="test_harness",
                trial_num=0,
                mode="patch_generation",
                source_path=source_path,
                hints_dir=hints_dir,
                povs_dir=povs_dir,
            )

        assert metadata["trial_num"] == 0
        assert metadata["crs"] == "test-crs"
        assert metadata["benchmark"] == "test-bench"
        assert metadata["harness"] == "test_harness"
        assert metadata["mode"] == "patch_generation"
        assert metadata["source"]["commit"] == "abc123"
        assert metadata["hints"] is not None
        assert metadata["povs"] is not None

    def test_write_metadata(self, preparer, temp_experiment_dir):
        """Test metadata writing."""
        trial_dir = temp_experiment_dir / "trial-0"
        trial_dir.mkdir()

        metadata = {
            "timestamp": "2025-01-01T00:00:00",
            "trial_num": 0,
            "crs": "test-crs",
        }

        preparer._write_metadata(trial_dir, metadata)

        metadata_file = trial_dir / "metadata.json"
        assert metadata_file.exists()

        with open(metadata_file) as f:
            loaded = json.load(f)
        assert loaded == metadata


class TestCompleteTrialPreparation:
    """Tests for complete trial preparation."""

    def test_prepare_trial_bug_finding(
        self,
        mock_benchmark,
        temp_experiment_dir,
        temp_benchmarks_dir,
        temp_oss_fuzz_dir,
    ):
        """Test complete trial preparation for bug finding."""
        config = {"hints_enabled": False}
        preparer = TrialDirectoryPreparer(
            experiment_dir=temp_experiment_dir,
            benchmarks_root=temp_benchmarks_dir,
            oss_fuzz_dir=temp_oss_fuzz_dir,
            config=config,
        )

        with patch(
            "crsbench.utils.repo_manager.ensure_project_repository"
        ) as mock_ensure:
            mock_source_path = (
                temp_experiment_dir / "trial-0" / "crs-build" / "src" / "test-bench"
            )
            mock_source_path.mkdir(parents=True)
            mock_ensure.return_value = str(mock_source_path)

            result = preparer.prepare_trial(
                crs="test-crs",
                benchmark="test-bench",
                harness="test_harness",
                trial_num=0,
                mode="bug_finding",
            )

        assert result.success is True
        assert result.trial_dir.exists()
        assert result.build_dir.exists()
        assert result.source_path.exists()
        assert result.output_dir.exists()
        assert result.hints_dir is None  # Hints disabled
        assert result.povs_dir is None  # Bug finding mode
        assert (result.trial_dir / "metadata.json").exists()

    def test_prepare_trial_patch_generation(
        self,
        mock_benchmark_with_povs,
        temp_experiment_dir,
        temp_benchmarks_dir,
        temp_oss_fuzz_dir,
    ):
        """Test complete trial preparation for patch generation."""
        config = {"hints_enabled": False}
        preparer = TrialDirectoryPreparer(
            experiment_dir=temp_experiment_dir,
            benchmarks_root=temp_benchmarks_dir,
            oss_fuzz_dir=temp_oss_fuzz_dir,
            config=config,
        )

        with patch(
            "crsbench.utils.repo_manager.ensure_project_repository"
        ) as mock_ensure:
            mock_source_path = (
                temp_experiment_dir / "trial-0" / "crs-build" / "src" / "test-bench"
            )
            mock_source_path.mkdir(parents=True)
            mock_ensure.return_value = str(mock_source_path)

            result = preparer.prepare_trial(
                crs="test-crs",
                benchmark="test-bench",
                harness="test_harness",
                trial_num=0,
                mode="patch_generation",
            )

        assert result.success is True
        assert result.povs_dir is not None  # Patch generation mode
        assert result.povs_dir.exists()

    def test_prepare_trial_with_hints(
        self,
        mock_benchmark_with_hints,
        temp_experiment_dir,
        temp_benchmarks_dir,
        temp_oss_fuzz_dir,
    ):
        """Test complete trial preparation with hints enabled."""
        config = {"hints_enabled": True, "hint_sarif_level": 1}
        preparer = TrialDirectoryPreparer(
            experiment_dir=temp_experiment_dir,
            benchmarks_root=temp_benchmarks_dir,
            oss_fuzz_dir=temp_oss_fuzz_dir,
            config=config,
        )

        with patch(
            "crsbench.utils.repo_manager.ensure_project_repository"
        ) as mock_ensure:
            mock_source_path = (
                temp_experiment_dir / "trial-0" / "crs-build" / "src" / "test-bench"
            )
            mock_source_path.mkdir(parents=True)
            mock_ensure.return_value = str(mock_source_path)

            result = preparer.prepare_trial(
                crs="test-crs",
                benchmark="test-bench",
                harness="test_harness",
                trial_num=0,
                mode="bug_finding",
            )

        assert result.success is True
        assert result.hints_dir is not None
        assert result.hints_dir.exists()
        # Verify SARIF files were aggregated
        sarif_files = list(result.hints_dir.glob("*.sarif"))
        assert len(sarif_files) == 2  # cpv_0 and cpv_1 both have level_1.sarif

    def test_prepare_trial_failure_handling(
        self, temp_experiment_dir, temp_benchmarks_dir, temp_oss_fuzz_dir
    ):
        """Test trial preparation failure handling."""
        config = {}
        preparer = TrialDirectoryPreparer(
            experiment_dir=temp_experiment_dir,
            benchmarks_root=temp_benchmarks_dir,
            oss_fuzz_dir=temp_oss_fuzz_dir,
            config=config,
        )

        # Try to prepare with nonexistent benchmark
        result = preparer.prepare_trial(
            crs="test-crs",
            benchmark="nonexistent-bench",
            harness="test_harness",
            trial_num=0,
            mode="bug_finding",
        )

        assert result.success is False
        assert result.error is not None
        assert "Benchmark directory not found" in result.error

    def test_trial_preparation_result_to_dict(self, temp_experiment_dir):
        """Test TrialPreparationResult to_dict conversion."""
        result = TrialPreparationResult(
            trial_dir=temp_experiment_dir / "trial-0",
            build_dir=temp_experiment_dir / "trial-0" / "crs-build",
            source_path=temp_experiment_dir / "src",
            output_dir=temp_experiment_dir / "output",
            hints_dir=temp_experiment_dir / "hints",
            povs_dir=temp_experiment_dir / "povs",
            metadata={"test": "data"},
            success=True,
            error=None,
        )

        result_dict = result.to_dict()

        assert result_dict["success"] is True
        assert result_dict["error"] is None
        assert result_dict["metadata"] == {"test": "data"}
        assert all(
            isinstance(result_dict[k], (str, type(None)))
            for k in ["trial_dir", "build_dir", "source_path"]
        )
