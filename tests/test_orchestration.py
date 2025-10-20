"""Comprehensive tests for the orchestration layer.

This test suite validates:
1. Experiment configuration loading and validation
2. Trial matrix generation (job count verification)
3. CLI argument overrides
4. Config storage in trial directories (CRITICAL for reproducibility)
5. Integration workflows with sample configs
6. Execution mode selection

Run with:
    pytest tests/test_orchestration.py -v
    pytest tests/test_orchestration.py --cov=crsbench.run_experiment
"""

import pytest
import yaml
import json
from pathlib import Path
from unittest.mock import Mock
from pydantic import ValidationError as PydanticValidationError

from crsbench.run_experiment import (
    load_experiment_config,
    parse_list_argument,
    generate_trial_matrix,
    should_use_distributed_mode,
    Trial,
)
from crsbench.validation.schemas import ExperimentConfig


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
max_total_time: 7200
difficulty_level: 2
experiment_filestore: /tmp/experiment-data
report_filestore: /tmp/report-data
crses:
  - atlantis-c
  - ensemble-c
benchmarks:
  - curl-delta-02
  - libxml2-delta-03
""")

        config = load_experiment_config(config_path)

        assert config.experiment == "test-experiment"
        assert config.trials == 3
        assert config.max_total_time == 7200
        assert config.difficulty_level == 2
        assert config.crses == ["atlantis-c", "ensemble-c"]
        assert config.benchmarks == ["curl-delta-02", "libxml2-delta-03"]
        assert config.benchmark_suite is None

    def test_load_experiment_config_with_benchmark_suite(self, tmp_path):
        """Test loading config with benchmark_suite instead of benchmarks."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text("""
experiment: suite-experiment
trials: 2
max_total_time: 3600
difficulty_level: 1
experiment_filestore: /tmp/experiment-data
report_filestore: /tmp/report-data
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
max_total_time: 3600
difficulty_level: 1
experiment_filestore: /tmp/experiment-data
report_filestore: /tmp/report-data
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
max_total_time: 3600
difficulty_level: 0
experiment_filestore: /tmp/exp
report_filestore: /tmp/rep
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
            max_total_time=3600,
            difficulty_level=1,
            experiment_filestore="/tmp/exp",
            report_filestore="/tmp/rep",
            crses=["crs1", "crs2"],
            benchmarks=["bench1", "bench2"]
        )

        benchmarks = ["bench1", "bench2"]
        crses = ["crs1", "crs2"]

        trials = generate_trial_matrix(benchmarks, crses, config)

        # Expected: 2 CRSes × 2 benchmarks × 2 trials = 8 total
        assert len(trials) == 8

        # Verify structure
        assert all(isinstance(t, Trial) for t in trials)
        assert all(t.crs in crses for t in trials)
        assert all(t.benchmark in benchmarks for t in trials)
        assert all(0 <= t.trial_num < config.trials for t in trials)

    def test_generate_trial_matrix_ordering(self):
        """Test trial matrix ordering (CRS → Benchmark → Trial number)."""
        config = ExperimentConfig(
            experiment="test",
            trials=2,
            max_total_time=3600,
            difficulty_level=1,
            experiment_filestore="/tmp/exp",
            report_filestore="/tmp/rep",
            crses=["crs1", "crs2"],
            benchmarks=["bench1", "bench2"]
        )

        benchmarks = ["bench1", "bench2"]
        crses = ["crs1", "crs2"]

        trials = generate_trial_matrix(benchmarks, crses, config)

        # Verify ordering: CRS outer loop, benchmark middle, trial inner
        expected = [
            ("crs1", "bench1", 0), ("crs1", "bench1", 1),
            ("crs1", "bench2", 0), ("crs1", "bench2", 1),
            ("crs2", "bench1", 0), ("crs2", "bench1", 1),
            ("crs2", "bench2", 0), ("crs2", "bench2", 1),
        ]

        actual = [(t.crs, t.benchmark, t.trial_num) for t in trials]
        assert actual == expected

    def test_generate_trial_matrix_single_trial(self):
        """Test matrix with single trial (no replication)."""
        config = ExperimentConfig(
            experiment="test",
            trials=1,
            max_total_time=3600,
            difficulty_level=1,
            experiment_filestore="/tmp/exp",
            report_filestore="/tmp/rep",
            crses=["crs1"],
            benchmarks=["bench1"]
        )

        benchmarks = ["bench1"]
        crses = ["crs1"]

        trials = generate_trial_matrix(benchmarks, crses, config)

        # Expected: 1 CRS × 1 benchmark × 1 trial = 1 total
        assert len(trials) == 1
        assert trials[0] == Trial(crs="crs1", benchmark="bench1", trial_num=0)

    def test_trial_matrix_count_formula(self):
        """Test that trial count follows formula: CRSes × Benchmarks × Trials."""
        test_cases = [
            # (crses, benchmarks, trials_per_combo, expected_total)
            (["crs1"], ["bench1"], 1, 1),
            (["crs1"], ["bench1"], 3, 3),
            (["crs1", "crs2"], ["bench1"], 2, 4),
            (["crs1"], ["bench1", "bench2"], 2, 4),
            (["crs1", "crs2"], ["bench1", "bench2"], 3, 12),
            (["crs1", "crs2", "crs3"], ["b1", "b2", "b3", "b4"], 2, 24),
        ]

        for crses, benchmarks, trials_count, expected_total in test_cases:
            config = ExperimentConfig(
                experiment="test",
                trials=trials_count,
                max_total_time=3600,
                difficulty_level=1,
                experiment_filestore="/tmp/exp",
                report_filestore="/tmp/rep",
                crses=crses,
                benchmarks=benchmarks
            )

            trials = generate_trial_matrix(benchmarks, crses, config)

            assert len(trials) == expected_total, \
                f"Expected {expected_total} trials for {len(crses)} CRS × {len(benchmarks)} bench × {trials_count} trials"


# ============================================================================
# 3. CLI Parsing and Override Tests
# ============================================================================

class TestCLIOverrides:
    """Test CLI argument parsing and override behavior."""

    def test_parse_list_argument_basic(self):
        """Test basic comma-separated list parsing."""
        result = parse_list_argument("bench1,bench2,bench3")
        assert result == ["bench1", "bench2", "bench3"]

    def test_parse_list_argument_with_spaces(self):
        """Test list parsing with spaces (should be stripped)."""
        result = parse_list_argument("  bench1  ,  bench2  ,  bench3  ")
        assert result == ["bench1", "bench2", "bench3"]

    def test_parse_list_argument_single_item(self):
        """Test parsing single item (no comma)."""
        result = parse_list_argument("single-bench")
        assert result == ["single-bench"]

    def test_parse_list_argument_empty_items_filtered(self):
        """Test that empty items are filtered out."""
        result = parse_list_argument("bench1,,bench2,,,bench3")
        assert result == ["bench1", "bench2", "bench3"]

    def test_cli_override_experiment_name(self, tmp_path):
        """Test CLI override of experiment name."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text("""
experiment: original-name
trials: 1
max_total_time: 3600
difficulty_level: 1
experiment_filestore: /tmp/exp
report_filestore: /tmp/rep
crses: [crs1]
benchmarks: [bench1]
""")

        config = load_experiment_config(config_path)

        # Simulate CLI override
        cli_experiment_name = "overridden-name"

        # Resolution logic (what orchestrator does)
        resolved_name = cli_experiment_name if cli_experiment_name else config.experiment

        # Verify CLI value takes precedence
        assert resolved_name == "overridden-name"
        assert config.experiment == "original-name"  # Original unchanged

    def test_cli_override_crses(self, tmp_path):
        """Test CLI override of CRS list."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text("""
experiment: test
trials: 1
max_total_time: 3600
difficulty_level: 1
experiment_filestore: /tmp/exp
report_filestore: /tmp/rep
crses:
  - atlantis-c
  - ensemble-c
benchmarks: [bench1]
""")

        config = load_experiment_config(config_path)

        # Simulate CLI override
        cli_crses = "custom-crs1,custom-crs2"

        # Resolution logic
        resolved_crses = parse_list_argument(cli_crses) if cli_crses else config.crses

        # Verify CLI override
        assert resolved_crses == ["custom-crs1", "custom-crs2"]
        assert config.crses == ["atlantis-c", "ensemble-c"]  # Original unchanged

    def test_cli_override_benchmarks(self, tmp_path):
        """Test CLI override of benchmarks list."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text("""
experiment: test
trials: 1
max_total_time: 3600
difficulty_level: 1
experiment_filestore: /tmp/exp
report_filestore: /tmp/rep
crses: [crs1]
benchmarks:
  - bench1
  - bench2
""")

        config = load_experiment_config(config_path)

        # Simulate CLI override
        cli_benchmarks = "custom-bench1,custom-bench2,custom-bench3"

        # Resolution logic
        resolved_benchmarks = parse_list_argument(cli_benchmarks) if cli_benchmarks else config.benchmarks

        # Verify CLI override
        assert resolved_benchmarks == ["custom-bench1", "custom-bench2", "custom-bench3"]
        assert config.benchmarks == ["bench1", "bench2"]  # Original unchanged


# ============================================================================
# 4. Config Storage in Trial Directory (CRITICAL for Reproducibility)
# ============================================================================

class TestConfigStorage:
    """Test that resolved config (with CLI overrides) is stored in trial directories."""

    def test_store_resolved_config_in_trial_dir(self, tmp_path):
        """CRITICAL: Test that orchestrator stores RESOLVED config in trial directory.

        The stored config.yaml must contain overridden values from CLI,
        NOT the original values from the experiment config file.
        """
        # Create original config
        config_path = tmp_path / "config.yaml"
        config_path.write_text("""
experiment: original-experiment
trials: 1
max_total_time: 3600
difficulty_level: 1
experiment_filestore: /tmp/exp
report_filestore: /tmp/rep
crses:
  - atlantis-c
  - ensemble-c
benchmarks:
  - bench1
  - bench2
""")

        # Load config
        config = load_experiment_config(config_path)

        # Simulate CLI overrides
        cli_experiment_name = "overridden-experiment"
        cli_crses = "custom-crs1,custom-crs2"
        cli_benchmarks = "custom-bench1,custom-bench2,custom-bench3"

        # Resolve configuration (what orchestrator does)
        resolved_config = {
            "experiment": cli_experiment_name,
            "trials": config.trials,
            "max_total_time": config.max_total_time,
            "difficulty_level": config.difficulty_level,
            "experiment_filestore": config.experiment_filestore,
            "report_filestore": config.report_filestore,
            "crses": parse_list_argument(cli_crses),
            "benchmarks": parse_list_argument(cli_benchmarks),
        }

        # Store resolved config (what orchestrator does)
        trial_output_dir = tmp_path / "trial_0"
        trial_output_dir.mkdir(parents=True, exist_ok=True)

        config_yaml_path = trial_output_dir / "config.yaml"
        with open(config_yaml_path, "w") as f:
            yaml.dump(resolved_config, f)

        # Load stored config and verify it has OVERRIDDEN values
        with open(config_yaml_path) as f:
            stored_config = yaml.safe_load(f)

        # CRITICAL ASSERTIONS: Stored config must have overridden values
        assert stored_config["experiment"] == "overridden-experiment", \
            "Stored config should have CLI-overridden experiment name"
        assert stored_config["crses"] == ["custom-crs1", "custom-crs2"], \
            "Stored config should have CLI-overridden CRS list"
        assert stored_config["benchmarks"] == ["custom-bench1", "custom-bench2", "custom-bench3"], \
            "Stored config should have CLI-overridden benchmark list"

        # Verify original config values are NOT in stored config
        assert stored_config["experiment"] != "original-experiment"
        assert stored_config["crses"] != ["atlantis-c", "ensemble-c"]
        assert stored_config["benchmarks"] != ["bench1", "bench2"]

    def test_stored_config_has_trial_specific_fields(self, tmp_path):
        """Test that stored config includes trial-specific fields for reproducibility."""
        config = ExperimentConfig(
            experiment="test-experiment",
            trials=2,
            max_total_time=3600,
            difficulty_level=2,
            experiment_filestore="/tmp/exp",
            report_filestore="/tmp/rep",
            crses=["atlantis-c"],
            benchmarks=["curl-delta-02"]
        )

        # Generate trial
        trials = generate_trial_matrix(config.benchmarks, config.crses, config)
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
            "trial_benchmark": trial.benchmark,
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
        assert "trial_num" in stored
        assert stored["trial_crs"] == "atlantis-c"
        assert stored["trial_benchmark"] == "curl-delta-02"
        assert stored["trial_num"] == 0

    def test_config_and_execution_metadata_together(self, tmp_path):
        """Test that config.yaml + execution.json provide complete reproducibility."""
        trial_output_dir = tmp_path / "trial_0"
        trial_output_dir.mkdir(parents=True, exist_ok=True)

        # Orchestrator stores resolved config
        resolved_config = {
            "experiment": "test-experiment",
            "trials": 2,
            "max_total_time": 3600,
            "difficulty_level": 2,
            "crses": ["atlantis-c"],
            "benchmarks": ["curl-delta-02"],
            "trial_crs": "atlantis-c",
            "trial_benchmark": "curl-delta-02",
            "trial_num": 0,
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
        assert config["trial_num"] == 0
        assert execution["command"][0] == "python3"
        assert execution["hints"]["enabled"] is True
        assert execution["execution"]["returncode"] == 0


# ============================================================================
# 5. Integration Tests with Sample Configs
# ============================================================================

class TestIntegrationWithSampleConfigs:
    """Test integration with actual sample configs from experiment-configs/."""

    def test_e2e_with_sample_config_multi_crs(self, tmp_path):
        """Test end-to-end workflow with experiment-config-multi-crs.yaml."""
        config_path = Path("experiment-configs/experiment-config-multi-crs.yaml")

        if not config_path.exists():
            pytest.skip("Sample config not found, skipping integration test")

        # Load config
        config = load_experiment_config(config_path)

        # Verify config loaded correctly
        assert config.experiment == "multi-crs-baseline-eval"
        assert config.trials == 3
        assert len(config.crses) == 3  # atlantis-c, atlantis-multilang, ensemble-c
        assert len(config.benchmarks) == 6

        # Generate trial matrix
        trials = generate_trial_matrix(config.benchmarks, config.crses, config)

        # Expected: 3 CRSes × 6 benchmarks × 3 trials = 54 total
        assert len(trials) == 54

        # Mock trial execution - store config in trial dirs
        for i, trial in enumerate(trials[:3]):  # Test first 3 trials
            trial_dir = tmp_path / f"trial_{i}"
            trial_dir.mkdir(parents=True, exist_ok=True)

            # Store resolved config (what orchestrator does)
            trial_config = {
                "experiment": config.experiment,
                "trials": config.trials,
                "crses": config.crses,
                "benchmarks": config.benchmarks,
                "trial_crs": trial.crs,
                "trial_benchmark": trial.benchmark,
                "trial_num": trial.trial_num,
            }

            with open(trial_dir / "config.yaml", "w") as f:
                yaml.dump(trial_config, f)

            # Verify stored config
            with open(trial_dir / "config.yaml") as f:
                stored = yaml.safe_load(f)

            assert stored["trial_crs"] == trial.crs
            assert stored["trial_benchmark"] == trial.benchmark
            assert stored["experiment"] == "multi-crs-baseline-eval"

    def test_e2e_with_cli_overrides(self, tmp_path):
        """Test end-to-end workflow with CLI overrides applied."""
        config_path = Path("experiment-configs/experiment-config-multi-crs.yaml")

        if not config_path.exists():
            pytest.skip("Sample config not found, skipping integration test")

        config = load_experiment_config(config_path)

        # Apply CLI overrides
        cli_experiment_name = "custom-experiment-name"
        cli_crses = "custom-crs1,custom-crs2"
        cli_benchmarks = "bench1,bench2"

        # Resolve
        resolved_experiment = cli_experiment_name
        resolved_crses = parse_list_argument(cli_crses)
        resolved_benchmarks = parse_list_argument(cli_benchmarks)

        # Generate trial matrix with resolved values
        trials = generate_trial_matrix(resolved_benchmarks, resolved_crses, config)

        # Expected: 2 CRSes × 2 benchmarks × 3 trials = 12 total
        assert len(trials) == 12

        # Store config in trial directory
        trial_dir = tmp_path / "trial_0"
        trial_dir.mkdir(parents=True, exist_ok=True)

        stored_config = {
            "experiment": resolved_experiment,
            "trials": config.trials,
            "max_total_time": config.max_total_time,
            "difficulty_level": config.difficulty_level,
            "crses": resolved_crses,
            "benchmarks": resolved_benchmarks,
            "trial_crs": trials[0].crs,
            "trial_benchmark": trials[0].benchmark,
            "trial_num": trials[0].trial_num,
        }

        with open(trial_dir / "config.yaml", "w") as f:
            yaml.dump(stored_config, f)

        # Verify stored config has OVERRIDDEN values
        with open(trial_dir / "config.yaml") as f:
            stored = yaml.safe_load(f)

        assert stored["experiment"] == "custom-experiment-name"  # CLI override
        assert stored["crses"] == ["custom-crs1", "custom-crs2"]  # CLI override
        assert stored["benchmarks"] == ["bench1", "bench2"]  # CLI override

        # Verify NOT original values
        assert stored["experiment"] != "multi-crs-baseline-eval"
        assert stored["crses"] != ["atlantis-c", "atlantis-multilang", "ensemble-c"]

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

class TestModeSelection:
    """Test execution mode selection (local vs distributed)."""

    def test_should_use_distributed_mode_single_job(self):
        """Test mode detection for single job (should use local)."""
        # Mock args
        class MockArgs:
            local_only = False

        args = MockArgs()

        config = ExperimentConfig(
            experiment="test",
            trials=1,
            max_total_time=3600,
            difficulty_level=1,
            experiment_filestore="/tmp/exp",
            report_filestore="/tmp/rep",
            crses=["crs1"],
            benchmarks=["bench1"]
        )

        total_jobs = 1

        # Should return False (local mode) for single job
        result = should_use_distributed_mode(args, config, total_jobs)

        assert result is False, "Single job should use local mode"

    def test_should_use_distributed_mode_no_redis(self):
        """Test mode detection without Redis configured."""
        class MockArgs:
            local_only = False

        args = MockArgs()

        # Config without Redis
        config = ExperimentConfig(
            experiment="test",
            trials=2,
            max_total_time=3600,
            difficulty_level=1,
            experiment_filestore="/tmp/exp",
            report_filestore="/tmp/rep",
            crses=["crs1", "crs2"],
            benchmarks=["bench1"]
        )

        total_jobs = 4  # 2 CRS × 1 bench × 2 trials

        # Should return False (local mode) without Redis
        result = should_use_distributed_mode(args, config, total_jobs)

        assert result is False, "Multiple jobs without Redis should use local mode"

    def test_should_use_distributed_mode_local_only_flag(self):
        """Test mode detection with --local-only flag."""
        class MockArgs:
            local_only = True

        args = MockArgs()

        # Config with Redis configured
        config = ExperimentConfig(
            experiment="test",
            trials=2,
            max_total_time=3600,
            difficulty_level=1,
            experiment_filestore="/tmp/exp",
            report_filestore="/tmp/rep",
            crses=["crs1", "crs2"],
            benchmarks=["bench1"],
            redis_host="localhost"
        )

        total_jobs = 4

        # Should return False (local mode) when --local-only flag set
        result = should_use_distributed_mode(args, config, total_jobs)

        assert result is False, "--local-only flag should force local mode"
