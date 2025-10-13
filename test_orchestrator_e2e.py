#!/usr/bin/env python3
"""End-to-end test for the orchestrator implementation.

This script tests the run_experiment.py orchestrator with both local and distributed modes.
"""

import sys
import tempfile
import shutil
from pathlib import Path

# Add crsbench to path
sys.path.insert(0, str(Path(__file__).parent))

from crsbench.run_experiment import (
    load_experiment_config,
    generate_trial_matrix,
    should_use_distributed_mode,
    Trial,
    parse_list_argument
)
from crsbench.validation.schemas import ExperimentConfig


def test_parse_list_argument():
    """Test comma-separated list parsing."""
    print("Testing parse_list_argument()...")

    result = parse_list_argument("bench1,bench2,bench3")
    assert result == ["bench1", "bench2", "bench3"], f"Expected 3 items, got {result}"

    result = parse_list_argument("single")
    assert result == ["single"], f"Expected 1 item, got {result}"

    result = parse_list_argument("  spaced  ,  items  ")
    assert result == ["spaced", "items"], f"Expected stripped items, got {result}"

    print("  ✓ parse_list_argument() works correctly")


def test_load_experiment_config():
    """Test loading experiment configuration."""
    print("\nTesting load_experiment_config()...")

    # Create temporary config file
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        f.write("""trials: 2
max_total_time: 3600
difficulty_level: 1
experiment_filestore: /tmp/experiment-data
report_filestore: /tmp/report-data
""")
        config_path = Path(f.name)

    try:
        config = load_experiment_config(config_path)

        assert config.trials == 2, f"Expected trials=2, got {config.trials}"
        assert config.max_total_time == 3600, f"Expected max_total_time=3600, got {config.max_total_time}"
        assert config.difficulty_level == 1, f"Expected difficulty_level=1, got {config.difficulty_level}"
        assert config.redis_host is None, f"Expected redis_host=None, got {config.redis_host}"

        print("  ✓ load_experiment_config() works correctly")
    finally:
        config_path.unlink()


def test_load_experiment_config_with_redis():
    """Test loading experiment configuration with Redis."""
    print("\nTesting load_experiment_config() with Redis...")

    # Create temporary config file with Redis
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        f.write("""trials: 1
max_total_time: 3600
difficulty_level: 1
experiment_filestore: /tmp/experiment-data
report_filestore: /tmp/report-data
redis_host: localhost
""")
        config_path = Path(f.name)

    try:
        config = load_experiment_config(config_path)

        assert config.redis_host == "localhost", f"Expected redis_host=localhost, got {config.redis_host}"

        print("  ✓ load_experiment_config() with Redis works correctly")
    finally:
        config_path.unlink()


def test_load_experiment_config_with_benchmarks_root():
    """Test loading experiment configuration with benchmarks_root."""
    print("\nTesting load_experiment_config() with benchmarks_root...")

    # Create temporary directory for benchmarks
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create temporary config file
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(f"""trials: 1
max_total_time: 3600
difficulty_level: 1
experiment_filestore: /tmp/experiment-data
report_filestore: /tmp/report-data
benchmarks_root: {tmpdir}
""")
            config_path = Path(f.name)

        try:
            config = load_experiment_config(config_path)

            assert config.benchmarks_root == str(Path(tmpdir).absolute()), \
                f"Expected benchmarks_root={tmpdir}, got {config.benchmarks_root}"

            print("  ✓ load_experiment_config() with benchmarks_root works correctly")
        finally:
            config_path.unlink()


def test_generate_trial_matrix():
    """Test trial matrix generation."""
    print("\nTesting generate_trial_matrix()...")

    # Create mock config
    config = ExperimentConfig(
        trials=2,
        max_total_time=3600,
        difficulty_level=1,
        experiment_filestore="/tmp/exp",
        report_filestore="/tmp/rep"
    )

    benchmarks = ["bench1", "bench2"]
    crses = ["crs1", "crs2"]

    trials = generate_trial_matrix(benchmarks, crses, config)

    # Should generate 2 CRSes × 2 benchmarks × 2 trials = 8 trials
    assert len(trials) == 8, f"Expected 8 trials, got {len(trials)}"

    # Check structure
    assert isinstance(trials[0], Trial), f"Expected Trial namedtuple, got {type(trials[0])}"
    assert trials[0].crs in crses, f"Invalid CRS: {trials[0].crs}"
    assert trials[0].benchmark in benchmarks, f"Invalid benchmark: {trials[0].benchmark}"
    assert 0 <= trials[0].trial_num < config.trials, f"Invalid trial_num: {trials[0].trial_num}"

    # Check all combinations exist
    expected_combinations = [
        ("crs1", "bench1", 0), ("crs1", "bench1", 1),
        ("crs1", "bench2", 0), ("crs1", "bench2", 1),
        ("crs2", "bench1", 0), ("crs2", "bench1", 1),
        ("crs2", "bench2", 0), ("crs2", "bench2", 1),
    ]

    actual_combinations = [(t.crs, t.benchmark, t.trial_num) for t in trials]
    assert actual_combinations == expected_combinations, \
        f"Trial combinations don't match. Expected:\n{expected_combinations}\nGot:\n{actual_combinations}"

    print("  ✓ generate_trial_matrix() works correctly")


def test_should_use_distributed_mode_single_job():
    """Test mode detection for single job (should use local)."""
    print("\nTesting should_use_distributed_mode() with single job...")

    # Mock args
    class MockArgs:
        local_only = False

    args = MockArgs()

    # Config without Redis
    config = ExperimentConfig(
        trials=1,
        max_total_time=3600,
        difficulty_level=1,
        experiment_filestore="/tmp/exp",
        report_filestore="/tmp/rep"
    )

    total_jobs = 1

    result = should_use_distributed_mode(args, config, total_jobs)

    assert result is False, "Single job should use local mode"
    print("  ✓ Single job correctly uses local mode")


def test_should_use_distributed_mode_no_redis():
    """Test mode detection without Redis configured."""
    print("\nTesting should_use_distributed_mode() without Redis...")

    class MockArgs:
        local_only = False

    args = MockArgs()

    # Config without Redis
    config = ExperimentConfig(
        trials=2,
        max_total_time=3600,
        difficulty_level=1,
        experiment_filestore="/tmp/exp",
        report_filestore="/tmp/rep"
    )

    total_jobs = 4

    result = should_use_distributed_mode(args, config, total_jobs)

    assert result is False, "Multiple jobs without Redis should use local mode"
    print("  ✓ Multiple jobs without Redis correctly uses local mode")


def test_should_use_distributed_mode_local_only_flag():
    """Test mode detection with --local-only flag."""
    print("\nTesting should_use_distributed_mode() with --local-only flag...")

    class MockArgs:
        local_only = True

    args = MockArgs()

    # Config with Redis
    config = ExperimentConfig(
        trials=2,
        max_total_time=3600,
        difficulty_level=1,
        experiment_filestore="/tmp/exp",
        report_filestore="/tmp/rep",
        redis_host="localhost"
    )

    total_jobs = 4

    result = should_use_distributed_mode(args, config, total_jobs)

    assert result is False, "--local-only flag should force local mode"
    print("  ✓ --local-only flag correctly forces local mode")


def test_config_to_dict():
    """Test ExperimentConfig.to_dict() method."""
    print("\nTesting ExperimentConfig.to_dict()...")

    config = ExperimentConfig(
        trials=3,
        max_total_time=7200,
        difficulty_level=2,
        experiment_filestore="/tmp/exp",
        report_filestore="/tmp/rep",
        redis_host="redis-server"
    )

    config_dict = config.to_dict()

    assert isinstance(config_dict, dict), f"Expected dict, got {type(config_dict)}"
    assert config_dict['trials'] == 3, f"Expected trials=3, got {config_dict['trials']}"
    assert config_dict['max_total_time'] == 7200, f"Expected max_total_time=7200, got {config_dict['max_total_time']}"
    assert config_dict['difficulty_level'] == 2, f"Expected difficulty_level=2, got {config_dict['difficulty_level']}"
    assert config_dict['redis_host'] == "redis-server", f"Expected redis_host=redis-server, got {config_dict['redis_host']}"
    assert 'experiment_filestore' in config_dict
    assert 'report_filestore' in config_dict
    assert 'benchmarks_root' in config_dict

    print("  ✓ ExperimentConfig.to_dict() works correctly")


def run_all_tests():
    """Run all tests."""
    print("="*60)
    print("Running Orchestrator End-to-End Tests")
    print("="*60)

    try:
        test_parse_list_argument()
        test_load_experiment_config()
        test_load_experiment_config_with_redis()
        test_load_experiment_config_with_benchmarks_root()
        test_generate_trial_matrix()
        test_should_use_distributed_mode_single_job()
        test_should_use_distributed_mode_no_redis()
        test_should_use_distributed_mode_local_only_flag()
        test_config_to_dict()

        print("\n" + "="*60)
        print("✓ All tests passed!")
        print("="*60)
        return 0
    except AssertionError as e:
        print(f"\n✗ Test failed: {e}")
        return 1
    except Exception as e:
        print(f"\n✗ Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(run_all_tests())
