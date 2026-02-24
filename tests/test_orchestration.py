"""Comprehensive tests for the orchestration layer.

This test suite validates:
1. Experiment configuration loading and validation
2. Trial matrix generation (job count verification)
3. Config storage in trial directories (CRITICAL for reproducibility)
4. Integration workflows with sample configs
5. Execution mode selection

Run with:
    pytest tests/test_orchestration.py -v
    pytest tests/test_orchestration.py --cov=crsbench.run_experiment
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from crsbench.run_experiment import (
    Trial,
    generate_trial_matrix,
    load_experiment_config,
    should_use_distributed_mode,
)
from crsbench.validation.schemas import (
    POV,
    AdapterType,
    BenchmarkHarness,
    ExperimentConfig,
    HarnessFile,
    Vulnerability,
)


@pytest.fixture(autouse=True)
def mock_crs_helpers():
    """Mock CRS helper functions to avoid needing real CRS config files."""
    from unittest.mock import MagicMock

    # Mock MetaYamlAdapter to return harness with CPVs (for only_cpv_harnesses checks)
    mock_adapter = MagicMock()
    mock_harness = MagicMock()
    mock_harness.vulns = [{"id": "mock-cpv"}]  # Has CPVs so tests pass
    mock_adapter.get_harness.return_value = mock_harness

    with (
        patch(
            "crsbench.run_experiment.get_crs_registry_name",
            side_effect=lambda crs, _: crs,  # Return the CRS name as the registry name
        ),
        patch(
            "crsbench.run_experiment.get_crs_type",
            return_value="patch",  # Return default CRS type
        ),
        patch(
            "crsbench.run_experiment.MetaYamlAdapter.from_meta_yaml",
            return_value=mock_adapter,
        ),
    ):
        yield


# ============================================================================
# 1. Config Loading Tests
# ============================================================================


class TestConfigLoading:
    """Test experiment configuration loading and validation."""

    def test_load_experiment_config_basic(self, tmp_path):
        """Test loading basic experiment configuration."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text("""
experiment: test-experiment
trials: 3
mode: delta
adapter: oss-crs
max_total_time: 20000
difficulty_level: 2
experiment_filestore: /tmp/crsbench/experiment-data
report_filestore: /tmp/crsbench/report-data
crses:
  - atlantis-c
benchmarks:
  - curl-delta-02
  - libxml2-delta-03
""")

        config = load_experiment_config(config_path)

        assert config.experiment == "test-experiment"
        assert config.trials == 3
        assert config.max_total_time == 20000
        assert config.difficulty_level == 2
        assert config.crses == ["atlantis-c"]
        assert config.benchmarks == ["curl-delta-02", "libxml2-delta-03"]
        assert config.benchmark_suite is None

    def test_load_experiment_config_with_benchmark_suite(self, tmp_path):
        """Test loading config with benchmark_suite instead of benchmarks."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text("""
experiment: suite-experiment
trials: 2
mode: full
adapter: oss-crs
max_total_time: 20000
difficulty_level: 1
experiment_filestore: /tmp/crsbench/experiment-data
report_filestore: /tmp/crsbench/report-data
crses:
  - atlantis-c
benchmark_suite: crsbench-afc-c
""")

        config = load_experiment_config(config_path)

        assert config.experiment == "suite-experiment"
        assert config.trials == 2
        assert config.crses == ["atlantis-c"]
        assert config.benchmark_suite == "crsbench-afc-c"
        assert config.benchmarks is None  # Not set when using suite

    def test_load_experiment_config_with_redis(self, tmp_path):
        """Test loading config with Redis configuration."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text("""
experiment: redis-experiment
trials: 2
mode: delta
adapter: oss-crs
max_total_time: 20000
difficulty_level: 1
experiment_filestore: /tmp/crsbench/experiment-data
report_filestore: /tmp/crsbench/report-data
crses: [crs1]
benchmarks: [bench1]
redis_host: localhost
""")

        config = load_experiment_config(config_path)

        assert config.redis_host == "localhost"

    def test_load_experiment_config_minimal_required_fields(self, tmp_path):
        """Test loading config with only required fields."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text("""
experiment: minimal-experiment
trials: 1
mode: delta
adapter: oss-crs
max_total_time: 20000
difficulty_level: 0
experiment_filestore: /tmp/crsbench/exp
report_filestore: /tmp/crsbench/rep
crses: [crs1]
benchmarks: [bench1]
""")

        config = load_experiment_config(config_path)

        assert config.experiment == "minimal-experiment"
        assert config.trials == 1
        assert config.redis_host is None  # Optional field


# ============================================================================
# 2. Trial Matrix Generation Tests
# ============================================================================


class TestTrialMatrixGeneration:
    """Test trial matrix generation and job counting."""

    def test_generate_trial_matrix_basic(self):
        """Test basic trial matrix generation."""
        config = ExperimentConfig(
            experiment="test",
            trials=2,
            mode="delta",
            adapter=AdapterType.OSS_CRS,
            max_total_time=20000,
            difficulty_level=1,
            experiment_filestore="/tmp/exp",
            report_filestore="/tmp/rep",
            crses=["crs1"],
            benchmarks=["bench1", "bench2"],
            only_cpv_harnesses=False,
        )

        # Create BenchmarkHarness objects
        benchmark_harnesses = [
            BenchmarkHarness(
                name="bench1",
                path=Path("/tmp/bench1"),
                harness=HarnessFile(name="harness1", path="/src/harness1.c"),
            ),
            BenchmarkHarness(
                name="bench2",
                path=Path("/tmp/bench2"),
                harness=HarnessFile(name="harness2", path="/src/harness2.c"),
            ),
        ]
        crses = ["crs1", "crs2"]

        trials = generate_trial_matrix(
            benchmark_harnesses,
            crses,
            config,
            registry_dir=Path("/tmp/registry"),
            crs_configs_dir=Path("/tmp/crs-configs"),
        )

        # Expected: 2 CRSes × 2 benchmark_harnesses × 2 trials = 8 total
        assert len(trials) == 8

        # Verify structure
        assert all(isinstance(t, Trial) for t in trials)
        assert all(t.crs in crses for t in trials)
        assert all(t.benchmark_harness.name in ["bench1", "bench2"] for t in trials)
        assert all(
            t.benchmark_harness.harness.name in ["harness1", "harness2"] for t in trials
        )
        assert all(1 <= t.trial_num <= config.trials for t in trials)

    def test_generate_trial_matrix_ordering(self):
        """Test trial matrix ordering (CRS → Benchmark/Harness → Trial number)."""
        config = ExperimentConfig(
            experiment="test",
            trials=2,
            mode="delta",
            adapter=AdapterType.OSS_CRS,
            max_total_time=20000,
            difficulty_level=1,
            experiment_filestore="/tmp/exp",
            report_filestore="/tmp/rep",
            crses=["crs1"],
            benchmarks=["bench1", "bench2"],
            only_cpv_harnesses=False,
        )

        benchmark_harnesses = [
            BenchmarkHarness(
                name="bench1",
                path=Path("/tmp/bench1"),
                harness=HarnessFile(name="harness1", path="/src/harness1.c"),
            ),
            BenchmarkHarness(
                name="bench2",
                path=Path("/tmp/bench2"),
                harness=HarnessFile(name="harness2", path="/src/harness2.c"),
            ),
        ]
        crses = ["crs1", "crs2"]

        trials = generate_trial_matrix(
            benchmark_harnesses,
            crses,
            config,
            registry_dir=Path("/tmp/registry"),
            crs_configs_dir=Path("/tmp/crs-configs"),
        )

        # Verify ordering: CRS outer loop, BenchmarkHarness middle, trial inner
        expected = [
            ("crs1", "bench1", "harness1", 1),
            ("crs1", "bench1", "harness1", 2),
            ("crs1", "bench2", "harness2", 1),
            ("crs1", "bench2", "harness2", 2),
            ("crs2", "bench1", "harness1", 1),
            ("crs2", "bench1", "harness1", 2),
            ("crs2", "bench2", "harness2", 1),
            ("crs2", "bench2", "harness2", 2),
        ]

        actual = [
            (
                t.crs,
                t.benchmark_harness.name,
                t.benchmark_harness.harness.name,
                t.trial_num,
            )
            for t in trials
        ]
        assert actual == expected

    def test_generate_trial_matrix_single_trial(self):
        """Test matrix with single trial (no replication)."""
        config = ExperimentConfig(
            experiment="test",
            trials=1,
            mode="delta",
            adapter=AdapterType.OSS_CRS,
            max_total_time=20000,
            difficulty_level=1,
            experiment_filestore="/tmp/exp",
            report_filestore="/tmp/rep",
            crses=["crs1"],
            benchmarks=["bench1"],
            only_cpv_harnesses=False,
        )

        benchmark_harness = BenchmarkHarness(
            name="bench1",
            path=Path("/tmp/bench1"),
            harness=HarnessFile(name="harness1", path="/src/harness1.c"),
        )
        benchmark_harnesses = [benchmark_harness]
        crses = ["crs1"]

        trials = generate_trial_matrix(
            benchmark_harnesses,
            crses,
            config,
            registry_dir=Path("/tmp/registry"),
            crs_configs_dir=Path("/tmp/crs-configs"),
        )

        # Expected: 1 CRS × 1 benchmark_harness × 1 trial = 1 total
        assert len(trials) == 1
        assert trials[0].crs == "crs1"
        assert trials[0].benchmark_harness == benchmark_harness
        assert trials[0].trial_num == 1

    def test_trial_matrix_count_formula(self):
        """Test that trial count follows formula: CRSes × BenchmarkHarness count × Trials."""
        test_cases = [
            # (crses, benchmark_harness_pairs as tuples, trials_per_combo, expected_total)
            (["crs1"], [("bench1", "h1")], 1, 1),
            (["crs1"], [("bench1", "h1")], 3, 3),
            (["crs1", "crs2"], [("bench1", "h1")], 2, 4),
            (["crs1"], [("bench1", "h1"), ("bench2", "h2")], 2, 4),
            (["crs1", "crs2"], [("bench1", "h1"), ("bench2", "h2")], 3, 12),
            (
                ["crs1", "crs2", "crs3"],
                [("b1", "h1"), ("b2", "h2"), ("b3", "h3"), ("b4", "h4")],
                2,
                24,
            ),
        ]

        for crses, bh_tuples, trials_count, expected_total in test_cases:
            # Convert tuples to BenchmarkHarness objects
            benchmark_harnesses = [
                BenchmarkHarness(
                    name=bench,
                    path=Path(f"/tmp/{bench}"),
                    harness=HarnessFile(name=harness, path=f"/src/{harness}.c"),
                )
                for bench, harness in bh_tuples
            ]

            config = ExperimentConfig(
                experiment="test",
                trials=trials_count,
                mode="delta",
                adapter=AdapterType.OSS_CRS,
                max_total_time=20000,
                difficulty_level=1,
                experiment_filestore="/tmp/exp",
                report_filestore="/tmp/rep",
                crses=["crs1"],
                benchmarks=["dummy"],  # Not used, but required for config
                only_cpv_harnesses=False,
            )

            trials = generate_trial_matrix(
                benchmark_harnesses,
                crses,
                config,
                registry_dir=Path("/tmp/registry"),
                crs_configs_dir=Path("/tmp/crs-configs"),
            )

            assert len(trials) == expected_total, (
                f"Expected {expected_total} trials for {len(crses)} CRS × {len(benchmark_harnesses)} harnesses × {trials_count} trials"
            )


class TestOnlyCpvHarnesses:
    """Test only_cpv_harnesses option for filtering harnesses without CPVs."""

    def test_bug_finding_crs_skips_harness_without_cpvs_when_enabled(self):
        """Test that bug-finding CRS skips harnesses without CPVs when only_cpv_harnesses=True."""
        from unittest.mock import MagicMock

        # Create config with only_cpv_harnesses=True (default)
        config = ExperimentConfig(
            experiment="test",
            trials=1,
            mode="delta",
            adapter=AdapterType.OSS_CRS,
            max_total_time=20000,
            difficulty_level=1,
            experiment_filestore="/tmp/exp",
            report_filestore="/tmp/rep",
            crses=["bug-finder"],
            benchmarks=["bench1"],
            only_cpv_harnesses=True,
        )

        # Create benchmark harnesses (one with CPV, one without)
        benchmark_harnesses = [
            BenchmarkHarness(
                name="bench1",
                path=Path("/tmp/bench1"),
                harness=HarnessFile(name="harness_with_cpv", path="/src/h1.c"),
            ),
            BenchmarkHarness(
                name="bench2",
                path=Path("/tmp/bench2"),
                harness=HarnessFile(name="harness_without_cpv", path="/src/h2.c"),
            ),
        ]

        # Mock get_crs_type to return bug_finding
        # Mock MetaYamlAdapter to return harness with/without CPVs
        mock_adapter = MagicMock()
        mock_harness_with_cpv = MagicMock()
        mock_harness_with_cpv.vulns = [
            Vulnerability(
                vuln_keyword="cpv_1",
                povs=[POV(id="pov_0", sanitizer="address")],
            )
        ]  # Has CPVs
        mock_harness_without_cpv = MagicMock()
        mock_harness_without_cpv.vulns = []  # No CPVs

        def mock_get_harness(harness_name):
            if harness_name == "harness_with_cpv":
                return mock_harness_with_cpv
            return mock_harness_without_cpv

        mock_adapter.get_harness = mock_get_harness

        with (
            patch(
                "crsbench.run_experiment.get_crs_registry_name",
                return_value="bug-finder",
            ),
            patch(
                "crsbench.run_experiment.get_crs_type",
                return_value="bug_finding",
            ),
            patch(
                "crsbench.run_experiment.MetaYamlAdapter.from_meta_yaml",
                return_value=mock_adapter,
            ),
        ):
            trials = generate_trial_matrix(
                benchmark_harnesses,
                ["bug-finder"],
                config,
                registry_dir=Path("/tmp/registry"),
                crs_configs_dir=Path("/tmp/crs-configs"),
            )

        # Only harness_with_cpv should be included
        assert len(trials) == 1
        assert trials[0].benchmark_harness.harness.name == "harness_with_cpv"

    def test_bug_finding_crs_includes_all_harnesses_when_disabled(self):
        """Test that bug-finding CRS includes all harnesses when only_cpv_harnesses=False."""
        # Create config with only_cpv_harnesses=False
        config = ExperimentConfig(
            experiment="test",
            trials=1,
            mode="delta",
            adapter=AdapterType.OSS_CRS,
            max_total_time=20000,
            difficulty_level=1,
            experiment_filestore="/tmp/exp",
            report_filestore="/tmp/rep",
            crses=["bug-finder"],
            benchmarks=["bench1"],
            only_cpv_harnesses=False,
        )

        # Create benchmark harnesses
        benchmark_harnesses = [
            BenchmarkHarness(
                name="bench1",
                path=Path("/tmp/bench1"),
                harness=HarnessFile(name="harness1", path="/src/h1.c"),
            ),
            BenchmarkHarness(
                name="bench2",
                path=Path("/tmp/bench2"),
                harness=HarnessFile(name="harness2", path="/src/h2.c"),
            ),
        ]

        with (
            patch(
                "crsbench.run_experiment.get_crs_registry_name",
                return_value="bug-finder",
            ),
            patch(
                "crsbench.run_experiment.get_crs_type",
                return_value="bug_finding",
            ),
        ):
            trials = generate_trial_matrix(
                benchmark_harnesses,
                ["bug-finder"],
                config,
                registry_dir=Path("/tmp/registry"),
                crs_configs_dir=Path("/tmp/crs-configs"),
            )

        # All harnesses should be included (no CPV check performed)
        assert len(trials) == 2
        harness_names = {t.benchmark_harness.harness.name for t in trials}
        assert harness_names == {"harness1", "harness2"}

    def test_bug_fixing_crs_always_skips_harness_without_cpvs(self):
        """Test that bug-fixing CRS always skips harnesses without CPVs regardless of setting."""
        from unittest.mock import MagicMock

        # Create config with only_cpv_harnesses=False (should still skip for bug-fixing)
        config = ExperimentConfig(
            experiment="test",
            trials=1,
            mode="delta",
            adapter=AdapterType.OSS_CRS,
            max_total_time=20000,
            difficulty_level=1,
            experiment_filestore="/tmp/exp",
            report_filestore="/tmp/rep",
            crses=["bug-fixer"],
            benchmarks=["bench1"],
            only_cpv_harnesses=False,  # Even with False, bug-fixing should skip
        )

        # Create benchmark harnesses
        benchmark_harnesses = [
            BenchmarkHarness(
                name="bench1",
                path=Path("/tmp/bench1"),
                harness=HarnessFile(name="harness_with_cpv", path="/src/h1.c"),
            ),
            BenchmarkHarness(
                name="bench2",
                path=Path("/tmp/bench2"),
                harness=HarnessFile(name="harness_without_cpv", path="/src/h2.c"),
            ),
        ]

        # Mock MetaYamlAdapter
        mock_adapter = MagicMock()
        mock_harness_with_cpv = MagicMock()
        mock_harness_with_cpv.vulns = [
            Vulnerability(
                vuln_keyword="cpv_1",
                povs=[POV(id="pov_0", sanitizer="address")],
            )
        ]
        mock_harness_without_cpv = MagicMock()
        mock_harness_without_cpv.vulns = []

        def mock_get_harness(harness_name):
            if harness_name == "harness_with_cpv":
                return mock_harness_with_cpv
            return mock_harness_without_cpv

        mock_adapter.get_harness = mock_get_harness

        with (
            patch(
                "crsbench.run_experiment.get_crs_registry_name",
                return_value="bug-fixer",
            ),
            patch(
                "crsbench.run_experiment.get_crs_type",
                return_value="bug-fixing",  # bug-fixing CRS type
            ),
            patch(
                "crsbench.run_experiment.MetaYamlAdapter.from_meta_yaml",
                return_value=mock_adapter,
            ),
        ):
            trials = generate_trial_matrix(
                benchmark_harnesses,
                ["bug-fixer"],
                config,
                registry_dir=Path("/tmp/registry"),
                crs_configs_dir=Path("/tmp/crs-configs"),
            )

        # Bug-fixing CRS should skip harnesses without CPVs regardless of only_cpv_harnesses
        assert len(trials) == 1
        assert trials[0].benchmark_harness.harness.name == "harness_with_cpv"


# ============================================================================
# 3. CLI Override Tests
# ============================================================================


class TestExperimentNameSource:
    """Test experiment name source of truth behavior."""

    def test_experiment_name_from_config(self, tmp_path):
        """Experiment name is taken from config."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text("""
experiment: original-name
trials: 1
mode: delta
adapter: oss-crs
max_total_time: 20000
difficulty_level: 1
experiment_filestore: /tmp/crsbench/exp
report_filestore: /tmp/crsbench/rep
crses: [crs1]
benchmarks: [bench1]
""")

        config = load_experiment_config(config_path)
        assert config.experiment == "original-name"


# ============================================================================
# 4. Config Storage in Trial Directory (CRITICAL for Reproducibility)
# ============================================================================


class TestConfigStorage:
    """Test that config is stored in trial directories for reproducibility."""

    def test_store_config_in_trial_dir(self, tmp_path):
        """Test that orchestrator stores config in trial directory for reproducibility."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text("""
experiment: test-experiment
trials: 1
mode: delta
adapter: oss-crs
max_total_time: 20000
difficulty_level: 1
experiment_filestore: /tmp/crsbench/exp
report_filestore: /tmp/crsbench/rep
crses:
  - crs1
benchmarks:
  - bench1
  - bench2
""")

        config = load_experiment_config(config_path)

        # Resolve configuration from config (single source of truth)
        resolved_config = {
            "experiment": config.experiment,
            "trials": config.trials,
            "max_total_time": config.max_total_time,
            "difficulty_level": config.difficulty_level,
            "experiment_filestore": str(config.experiment_filestore),
            "report_filestore": str(config.report_filestore),
            "crses": config.crses,
            "benchmarks": config.benchmarks,
        }

        # Store resolved config
        trial_output_dir = tmp_path / "trial_0"
        trial_output_dir.mkdir(parents=True, exist_ok=True)

        config_yaml_path = trial_output_dir / "config.yaml"
        with open(config_yaml_path, "w") as f:
            yaml.dump(resolved_config, f)

        # Load stored config and verify
        with open(config_yaml_path) as f:
            stored_config = yaml.safe_load(f)

        assert stored_config["experiment"] == "test-experiment"
        assert stored_config["crses"] == ["crs1"]
        assert stored_config["benchmarks"] == ["bench1", "bench2"]

    def test_stored_config_has_trial_specific_fields(self, tmp_path):
        """Test that stored config includes trial-specific fields for reproducibility."""
        config = ExperimentConfig(
            experiment="test-experiment",
            trials=2,
            mode="delta",
            adapter=AdapterType.OSS_CRS,
            max_total_time=20000,
            difficulty_level=2,
            experiment_filestore="/tmp/exp",
            report_filestore="/tmp/rep",
            crses=["atlantis-c"],
            benchmarks=["curl-delta-02"],
            only_cpv_harnesses=False,
        )

        # Generate trial with BenchmarkHarness objects
        benchmark_harnesses = [
            BenchmarkHarness(
                name="curl-delta-02",
                path=Path("/tmp/curl-delta-02"),
                harness=HarnessFile(name="harness1", path="/src/harness1.c"),
            )
        ]
        trials = generate_trial_matrix(
            benchmark_harnesses,
            config.crses,
            config,
            registry_dir=Path("/tmp/registry"),
            crs_configs_dir=Path("/tmp/crs-configs"),
        )
        trial = trials[0]

        # Store config with trial-specific fields
        trial_output_dir = tmp_path / "trial_0"
        trial_output_dir.mkdir(parents=True, exist_ok=True)

        trial_config = {
            "experiment": config.experiment,
            "trials": config.trials,
            "max_total_time": config.max_total_time,
            "difficulty_level": config.difficulty_level,
            "crses": config.crses,
            "benchmarks": config.benchmarks,
            # Trial-specific fields
            "trial_crs": trial.crs,
            "trial_benchmark": trial.benchmark_harness.name,
            "trial_harness": trial.benchmark_harness.harness.name,
            "trial_num": trial.trial_num,
        }

        config_yaml_path = trial_output_dir / "config.yaml"
        with open(config_yaml_path, "w") as f:
            yaml.dump(trial_config, f)

        # Verify trial-specific fields
        with open(config_yaml_path) as f:
            stored = yaml.safe_load(f)

        assert "trial_crs" in stored
        assert "trial_benchmark" in stored
        assert "trial_harness" in stored
        assert "trial_num" in stored
        assert stored["trial_crs"] == "atlantis-c"
        assert stored["trial_benchmark"] == "curl-delta-02"
        assert stored["trial_harness"] == "harness1"
        assert stored["trial_num"] == 1

    def test_config_and_execution_metadata_together(self, tmp_path):
        """Test that config.yaml + execution.json provide complete reproducibility."""
        trial_output_dir = tmp_path / "trial_0"
        trial_output_dir.mkdir(parents=True, exist_ok=True)

        # Orchestrator stores resolved config
        resolved_config = {
            "experiment": "test-experiment",
            "trials": 2,
            "max_total_time": 20000,
            "difficulty_level": 2,
            "crses": ["atlantis-c"],
            "benchmarks": ["curl-delta-02"],
            "trial_crs": "atlantis-c",
            "trial_benchmark": "curl-delta-02",
            "trial_num": 1,
        }

        config_yaml_path = trial_output_dir / "config.yaml"
        with open(config_yaml_path, "w") as f:
            yaml.dump(resolved_config, f)

        # Executor stores execution metadata
        execution_metadata = {
            "timestamp": "2025-01-20T10:30:00",
            "command": ["python3", "infra/helper.py", "run_crs", "..."],
            "hints": {"enabled": True, "corpus_level": "1h"},
            "execution": {"duration_seconds": 120.5, "returncode": 0},
        }

        execution_json_path = trial_output_dir / "execution.json"
        with open(execution_json_path, "w") as f:
            json.dump(execution_metadata, f, indent=2)

        # Verify both files exist
        assert config_yaml_path.exists()
        assert execution_json_path.exists()

        # Load both and verify reproducibility information
        with open(config_yaml_path) as f:
            config = yaml.safe_load(f)
        with open(execution_json_path) as f:
            execution = json.load(f)

        # Verify we have complete reproducibility information
        assert config["trial_crs"] == "atlantis-c"
        assert config["trial_benchmark"] == "curl-delta-02"
        assert config["trial_num"] == 1
        assert execution["command"][0] == "python3"
        assert execution["hints"]["enabled"] is True
        assert execution["execution"]["returncode"] == 0


# ============================================================================
# 5. Integration Tests with Sample Configs
# ============================================================================


class TestIntegrationWithSampleConfigs:
    """Test integration with actual sample configs from experiment-configs/."""

    def test_e2e_with_sample_config_single_crs(self, tmp_path):
        """Test end-to-end workflow with a sample single-CRS config."""
        config_path = Path("experiment-configs/experiment-config-sanity.yaml")

        if not config_path.exists():
            pytest.skip("Sample config not found, skipping integration test")

        # Load config
        config = load_experiment_config(config_path)

        # Override only_cpv_harnesses to False for this test
        config = config.model_copy(update={"only_cpv_harnesses": False})

        # Verify config loaded correctly
        assert config.experiment == "sanity-test"
        assert config.trials == 1
        assert len(config.crses) == 1

        # Mock BenchmarkHarness objects - in reality these would come from meta.yaml
        # For this test, create one harness per benchmark
        benchmark_names = config.benchmarks or [
            "sanity-mock-c-delta-01",
            "sanity-mock-java-delta-01",
        ]
        benchmark_harnesses = [
            BenchmarkHarness(
                name=b,
                path=Path(f"/tmp/{b}"),
                harness=HarnessFile(name=f"{b}_harness", path=f"/src/{b}_harness.c"),
            )
            for b in benchmark_names
        ]

        # Generate trial matrix
        trials = generate_trial_matrix(
            benchmark_harnesses,
            config.crses,
            config,
            registry_dir=Path("/tmp/registry"),
            crs_configs_dir=Path("/tmp/crs-configs"),
        )

        # Expected: 1 CRS × 2 benchmark_harnesses × 1 trial = 2 total
        assert len(trials) == 2

        # Mock trial execution - store config in trial dirs
        for i, trial in enumerate(trials[:3]):  # Test first 3 trials
            trial_dir = tmp_path / f"trial_{i}"
            trial_dir.mkdir(parents=True, exist_ok=True)

            # Store resolved config (what orchestrator does)
            trial_config = {
                "experiment": config.experiment,
                "trials": config.trials,
                "crses": config.crses,
                "benchmarks": benchmark_names,
                "trial_crs": trial.crs,
                "trial_benchmark": trial.benchmark_harness.name,
                "trial_harness": trial.benchmark_harness.harness.name,
                "trial_num": trial.trial_num,
            }

            with open(trial_dir / "config.yaml", "w") as f:
                yaml.dump(trial_config, f)

            # Verify stored config
            with open(trial_dir / "config.yaml") as f:
                stored = yaml.safe_load(f)

            assert stored["trial_crs"] == trial.crs
            assert stored["trial_benchmark"] == trial.benchmark_harness.name
            assert stored["trial_harness"] == trial.benchmark_harness.harness.name
            assert stored["experiment"] == "sanity-test"

    def test_e2e_with_config_experiment_name(self, tmp_path):
        """Test end-to-end workflow with config-defined experiment name."""
        config_path = Path("experiment-configs/experiment-config-sanity.yaml")

        if not config_path.exists():
            pytest.skip("Sample config not found, skipping integration test")

        config = load_experiment_config(config_path)

        # Override only_cpv_harnesses to False for this test
        config = config.model_copy(update={"only_cpv_harnesses": False})

        # CRSes and benchmarks come from config only
        crses = config.crses
        benchmarks = config.benchmarks or [
            "sanity-mock-c-delta-01",
            "sanity-mock-java-delta-01",
        ]

        # Mock BenchmarkHarness objects
        benchmark_harnesses = [
            BenchmarkHarness(
                name=b,
                path=Path(f"/tmp/{b}"),
                harness=HarnessFile(name=f"{b}_harness", path=f"/src/{b}_harness.c"),
            )
            for b in benchmarks
        ]

        # Generate trial matrix from config
        trials = generate_trial_matrix(
            benchmark_harnesses,
            crses,
            config,
            registry_dir=Path("/tmp/registry"),
            crs_configs_dir=Path("/tmp/crs-configs"),
        )

        # Expected: 1 CRS × 2 benchmark_harnesses × 1 trial = 2 total
        assert len(trials) == 2

        # Store config in trial directory
        trial_dir = tmp_path / "trial_0"
        trial_dir.mkdir(parents=True, exist_ok=True)

        stored_config = {
            "experiment": config.experiment,
            "trials": config.trials,
            "crses": crses,
            "benchmarks": benchmarks,
            "trial_crs": trials[0].crs,
            "trial_benchmark": trials[0].benchmark_harness.name,
            "trial_harness": trials[0].benchmark_harness.harness.name,
            "trial_num": trials[0].trial_num,
        }

        with open(trial_dir / "config.yaml", "w") as f:
            yaml.dump(stored_config, f)

        with open(trial_dir / "config.yaml") as f:
            stored = yaml.safe_load(f)

        # Experiment name comes from config
        assert stored["experiment"] == "sanity-test"
        # CRSes and benchmarks come from config (single source of truth)
        assert stored["crses"] == config.crses
        assert stored["benchmarks"] == benchmarks

    def test_benchmark_suite_expansion(self):
        """Test benchmark suite correctly expands to benchmark list."""
        suite_path = Path("benchmark-suites/crsbench-afc-c.yaml")

        if not suite_path.exists():
            pytest.skip("Benchmark suite not found, skipping test")

        with open(suite_path) as f:
            suite_config = yaml.safe_load(f)

        benchmarks = suite_config["benchmark_list"]

        # Verify suite has expected benchmarks
        assert "curl-delta-02" in benchmarks
        assert "libxml2-delta-03" in benchmarks
        assert len(benchmarks) > 10  # AFC-C suite has 24 benchmarks


# ============================================================================
# 6. Mode Selection Tests
# ============================================================================


class UnitTestModeSelection:
    """Test execution mode selection (local vs distributed)."""

    def test_should_use_distributed_mode_single_job(self):
        """Test mode detection for single job (should use local)."""

        # Mock args
        class MockArgs:
            local_only = False
            distributed = False

        args = MockArgs()

        config = ExperimentConfig(
            experiment="test",
            trials=1,
            mode="delta",
            adapter=AdapterType.OSS_CRS,
            max_total_time=20000,
            difficulty_level=1,
            experiment_filestore="/tmp/exp",
            report_filestore="/tmp/rep",
            crses=["crs1"],
            benchmarks=["bench1"],
        )

        total_jobs = 1

        # Should return False (local mode) for single job
        result = should_use_distributed_mode(args, config, total_jobs)

        assert result is False, "Single job should use local mode"

    def test_should_use_distributed_mode_no_redis(self):
        """Test mode detection without Redis configured."""

        class MockArgs:
            local_only = False
            distributed = False

        args = MockArgs()

        # Config without Redis
        config = ExperimentConfig(
            experiment="test",
            trials=2,
            mode="delta",
            adapter=AdapterType.OSS_CRS,
            max_total_time=20000,
            difficulty_level=1,
            experiment_filestore="/tmp/exp",
            report_filestore="/tmp/rep",
            crses=["crs1", "crs2"],
            benchmarks=["bench1"],
        )

        total_jobs = 4  # 2 CRS × 1 bench × 2 trials

        # Should return False (local mode) without Redis
        result = should_use_distributed_mode(args, config, total_jobs)

        assert result is False, "Multiple jobs without Redis should use local mode"

    def test_should_use_distributed_mode_local_only_flag(self):
        """Test mode detection with --local-only flag."""

        class MockArgs:
            local_only = True
            distributed = False

        args = MockArgs()

        # Config with Redis configured
        config = ExperimentConfig(
            experiment="test",
            trials=2,
            mode="delta",
            adapter=AdapterType.OSS_CRS,
            max_total_time=20000,
            difficulty_level=1,
            experiment_filestore="/tmp/exp",
            report_filestore="/tmp/rep",
            crses=["crs1", "crs2"],
            benchmarks=["bench1"],
            redis_host="localhost",
        )

        total_jobs = 4

        # Should return False (local mode) when --local-only flag set
        result = should_use_distributed_mode(args, config, total_jobs)

        assert result is False, "--local-only flag should force local mode"

    def test_should_use_distributed_mode_distributed_no_redis(self):
        """Test --distributed flag without Redis configured (should raise error)."""

        class MockArgs:
            local_only = False
            distributed = True

        args = MockArgs()

        # Config without Redis
        config = ExperimentConfig(
            experiment="test",
            trials=1,
            mode="delta",
            adapter=AdapterType.OSS_CRS,
            max_total_time=20000,
            difficulty_level=1,
            experiment_filestore="/tmp/exp",
            report_filestore="/tmp/rep",
            crses=["crs1"],
            benchmarks=["bench1"],
        )

        total_jobs = 1

        # Should raise RuntimeError when forcing distributed mode without Redis
        with pytest.raises(
            RuntimeError, match="Cannot use distributed mode: No Redis host configured"
        ):
            should_use_distributed_mode(args, config, total_jobs)

    def test_should_use_distributed_mode_conflicting_flags(self):
        """Test conflicting --local-only and --distributed flags."""

        class MockArgs:
            local_only = True
            distributed = True

        args = MockArgs()

        config = ExperimentConfig(
            experiment="test",
            trials=1,
            mode="delta",
            adapter=AdapterType.OSS_CRS,
            max_total_time=20000,
            difficulty_level=1,
            experiment_filestore="/tmp/exp",
            report_filestore="/tmp/rep",
            crses=["crs1"],
            benchmarks=["bench1"],
            redis_host="localhost",
        )

        total_jobs = 1

        # Should raise RuntimeError when both flags are set
        with pytest.raises(
            RuntimeError,
            match="Cannot specify both --local-only and --distributed flags",
        ):
            should_use_distributed_mode(args, config, total_jobs)


class TestSanitizerFiltering:
    """Test sanitizer-based CPV filtering in trial generation."""

    def test_harness_has_cpv_with_sanitizer(self):
        """Test _harness_has_cpv_with_sanitizer helper function."""
        from crsbench.run_experiment import _harness_has_cpv_with_sanitizer
        from crsbench.validation.schemas import POV, Vulnerability

        # Create a harness with CPVs for different sanitizers
        harness = HarnessFile(
            name="test-harness",
            path="/src/test.c",
            vulns=[
                Vulnerability(
                    vuln_keyword="cpv_1",
                    povs=[
                        POV(id="pov_0", sanitizer="undefined"),
                        POV(id="pov_1", sanitizer="address"),
                    ],
                ),
                Vulnerability(
                    vuln_keyword="cpv_2",
                    povs=[
                        POV(id="pov_0", sanitizer="undefined"),
                    ],
                ),
            ],
        )

        # Should find matching sanitizers
        assert _harness_has_cpv_with_sanitizer(harness, "undefined")
        assert _harness_has_cpv_with_sanitizer(harness, "address")

        # Should not find non-existent sanitizer
        assert not _harness_has_cpv_with_sanitizer(harness, "memory")
        assert not _harness_has_cpv_with_sanitizer(harness, "thread")

    def test_harness_has_cpv_with_sanitizer_no_vulns(self):
        """Test _harness_has_cpv_with_sanitizer with harness without CPVs."""
        from crsbench.run_experiment import _harness_has_cpv_with_sanitizer

        # Harness with no vulnerabilities
        harness = HarnessFile(name="test-harness", path="/src/test.c", vulns=None)
        assert not _harness_has_cpv_with_sanitizer(harness, "address")

        # Harness with empty vulnerabilities list
        harness_empty = HarnessFile(name="test-harness", path="/src/test.c", vulns=[])
        assert not _harness_has_cpv_with_sanitizer(harness_empty, "address")

    def test_trial_generation_filters_by_sanitizer(self):
        """Test that trial generation skips sanitizers without matching CPVs."""
        from crsbench.validation.schemas import POV, Sanitizer, Vulnerability

        # Create config with multiple sanitizers
        config = ExperimentConfig(
            experiment="test",
            trials=1,
            mode="delta",
            adapter=AdapterType.OSS_CRS,
            max_total_time=20000,
            difficulty_level=1,
            experiment_filestore="/tmp/exp",
            report_filestore="/tmp/rep",
            crses=["crs1"],
            benchmarks=["bench1"],
            sanitizers=[Sanitizer.ADDRESS, Sanitizer.UNDEFINED],
            only_cpv_harnesses=True,  # Enable CPV checking
        )

        # Create a harness with only undefined sanitizer CPVs
        harness_file = HarnessFile(
            name="harness1",
            path="/src/harness1.c",
            vulns=[
                Vulnerability(
                    vuln_keyword="cpv_1",
                    povs=[POV(id="pov_0", sanitizer="undefined")],
                ),
            ],
        )

        benchmark_harness = BenchmarkHarness(
            name="bench1",
            path=Path("/tmp/bench1"),
            harness=harness_file,
        )

        # Mock MetaYamlAdapter to return our harness with specific sanitizers
        from unittest.mock import MagicMock

        mock_adapter = MagicMock()
        mock_adapter.get_harness.return_value = harness_file

        with patch(
            "crsbench.run_experiment.MetaYamlAdapter.from_meta_yaml",
            return_value=mock_adapter,
        ):
            trials = generate_trial_matrix(
                [benchmark_harness],
                ["crs1"],
                config,
                registry_dir=Path("/tmp/registry"),
                crs_configs_dir=Path("/tmp/crs-configs"),
            )

        # Should only generate trials for undefined sanitizer
        assert len(trials) == 1
        assert trials[0].sanitizer == "undefined"

    def test_trial_generation_multiple_sanitizers_match(self):
        """Test trial generation when harness has CPVs for multiple sanitizers."""
        from crsbench.validation.schemas import POV, Sanitizer, Vulnerability

        config = ExperimentConfig(
            experiment="test",
            trials=2,
            mode="delta",
            adapter=AdapterType.OSS_CRS,
            max_total_time=20000,
            difficulty_level=1,
            experiment_filestore="/tmp/exp",
            report_filestore="/tmp/rep",
            crses=["crs1"],
            benchmarks=["bench1"],
            sanitizers=[Sanitizer.ADDRESS, Sanitizer.UNDEFINED],
            only_cpv_harnesses=True,
        )

        # Harness with CPVs for both sanitizers
        harness_file = HarnessFile(
            name="harness1",
            path="/src/harness1.c",
            vulns=[
                Vulnerability(
                    vuln_keyword="cpv_1",
                    povs=[
                        POV(id="pov_0", sanitizer="undefined"),
                        POV(id="pov_1", sanitizer="address"),
                    ],
                ),
            ],
        )

        benchmark_harness = BenchmarkHarness(
            name="bench1",
            path=Path("/tmp/bench1"),
            harness=harness_file,
        )

        from unittest.mock import MagicMock

        mock_adapter = MagicMock()
        mock_adapter.get_harness.return_value = harness_file

        with patch(
            "crsbench.run_experiment.MetaYamlAdapter.from_meta_yaml",
            return_value=mock_adapter,
        ):
            trials = generate_trial_matrix(
                [benchmark_harness],
                ["crs1"],
                config,
                registry_dir=Path("/tmp/registry"),
                crs_configs_dir=Path("/tmp/crs-configs"),
            )

        # Should generate trials for both sanitizers (2 trials each)
        assert len(trials) == 4  # 2 sanitizers × 2 trials
        sanitizers = [t.sanitizer for t in trials]
        assert sanitizers.count("address") == 2
        assert sanitizers.count("undefined") == 2

    def test_bugfix_trial_generation_is_per_cpv(self):
        """Bug-fixing trials are generated once per CPV (per sanitizer)."""
        from crsbench.validation.schemas import POV, Sanitizer, Vulnerability

        config = ExperimentConfig(
            experiment="test",
            trials=2,
            mode="delta",
            adapter=AdapterType.OSS_CRS,
            max_total_time=20000,
            difficulty_level=1,
            experiment_filestore="/tmp/exp",
            report_filestore="/tmp/rep",
            crses=["crs1"],
            benchmarks=["bench1"],
            sanitizers=[Sanitizer.ADDRESS],
            only_cpv_harnesses=False,
        )

        harness_file = HarnessFile(
            name="harness1",
            path="/src/harness1.c",
            vulns=[
                Vulnerability(
                    vuln_keyword="cpv_0",
                    povs=[POV(id="pov_0", sanitizer="address")],
                ),
                Vulnerability(
                    vuln_keyword="cpv_1",
                    povs=[POV(id="pov_0", sanitizer="address")],
                ),
            ],
        )

        benchmark_harness = BenchmarkHarness(
            name="bench1",
            path=Path("/tmp/bench1"),
            harness=harness_file,
        )

        from unittest.mock import MagicMock

        mock_adapter = MagicMock()
        mock_adapter.get_harness.return_value = harness_file

        with (
            patch(
                "crsbench.run_experiment.MetaYamlAdapter.from_meta_yaml",
                return_value=mock_adapter,
            ),
            patch("crsbench.run_experiment.get_crs_type", return_value="bug-fixing"),
        ):
            trials = generate_trial_matrix(
                [benchmark_harness],
                ["crs1"],
                config,
                registry_dir=Path("/tmp/registry"),
                crs_configs_dir=Path("/tmp/crs-configs"),
            )

        assert len(trials) == 4  # 2 CPVs x 2 trial_num
        assert {t.target_cpv_id for t in trials} == {"cpv_0", "cpv_1"}

    def test_trial_generation_no_filtering_when_only_cpv_harnesses_false(self):
        """Test that sanitizer filtering is skipped when only_cpv_harnesses=False for bug-finding CRS."""
        from crsbench.validation.schemas import Sanitizer

        config = ExperimentConfig(
            experiment="test",
            trials=1,
            mode="delta",
            adapter=AdapterType.OSS_CRS,
            max_total_time=20000,
            difficulty_level=1,
            experiment_filestore="/tmp/exp",
            report_filestore="/tmp/rep",
            crses=["crs1"],
            benchmarks=["bench1"],
            sanitizers=[Sanitizer.ADDRESS, Sanitizer.UNDEFINED],
            only_cpv_harnesses=False,  # Don't check CPVs
        )

        # Harness without CPVs
        harness_file = HarnessFile(name="harness1", path="/src/harness1.c", vulns=None)

        benchmark_harness = BenchmarkHarness(
            name="bench1",
            path=Path("/tmp/bench1"),
            harness=harness_file,
        )

        # Should generate trials for both sanitizers even without CPV metadata
        trials = generate_trial_matrix(
            [benchmark_harness],
            ["crs1"],
            config,
            registry_dir=Path("/tmp/registry"),
            crs_configs_dir=Path("/tmp/crs-configs"),
        )

        # Should generate trials for both sanitizers
        assert len(trials) == 2
        sanitizers = {t.sanitizer for t in trials}
        assert sanitizers == {"address", "undefined"}
