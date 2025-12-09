"""Job definitions for CRSBench distributed execution.

This module defines all job types that can be executed by workers in the distributed
job queue system. Jobs are enqueued by the orchestrator and executed by workers.
"""

import time
from pathlib import Path
from typing import Dict, Any, Optional

from crsbench.utils.logger import get_logger
from crsbench.evaluation.runner import BenchmarkRunner
from crsbench.evaluation.crs_executor import StubCRSExecutor
from crsbench.evaluation.crs_bug_finding_executor import CRSBugFindingExecutor

logger = get_logger(__name__)


def build_crs_environment(
    crs: str,
    benchmark: str,
    config: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Prepare CRS execution environment.

    This job handles environment setup tasks before CRS trial execution:
    - Validate CRS installation
    - Prepare Docker images if needed
    - Set up CRS-specific configuration
    - Verify benchmark accessibility

    Args:
        crs: CRS implementation name (e.g., 'atlantis-c', 'test-crs')
        benchmark: Benchmark identifier or path
        config: Experiment configuration dictionary

    Returns:
        dict: Environment setup result with status and metadata

    Example:
        >>> result = build_crs_environment('test-crs', 'test-benchmark', {})
        >>> assert result['success'] is True
    """
    logger.info(f"Building environment for CRS '{crs}' on benchmark '{benchmark}'")

    try:
        # TODO: Implement actual CRS environment setup
        # For now, this is a placeholder that always succeeds

        # Validate CRS exists
        # Prepare Docker images
        # Set up CRS configuration
        # Verify benchmark path

        result = {
            'success': True,
            'crs': crs,
            'benchmark': benchmark,
            'environment_ready': True,
            'metadata': {
                'setup_time': 0.0,
                'timestamp': time.time()
            }
        }

        logger.info(f"Environment setup completed for {crs}")
        return result

    except Exception as e:
        logger.error(f"Environment setup failed for {crs}: {e}")
        return {
            'success': False,
            'crs': crs,
            'benchmark': benchmark,
            'environment_ready': False,
            'error': str(e),
            'metadata': {
                'timestamp': time.time()
            }
        }


def run_crs_trial(
    crs: str,
    benchmark: str,
    trial_num: int,
    config: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Execute a single CRS trial.

    This is the main job type for CRS evaluation. It runs a complete trial
    for one CRS on one benchmark, including:
    - Loading benchmark configuration
    - Executing CRS
    - Collecting results
    - Storing trial data

    Args:
        crs: CRS implementation name
        benchmark: Benchmark identifier or path
        trial_num: Trial number (0-indexed) for this execution
        config: Experiment configuration dictionary

    Returns:
        dict: Trial results including POVs found, success rate, and metadata

    Example:
        >>> config = {
        ...     'experiment_filestore': '/tmp/exp',
        ...     'max_total_time': 3600
        ... }
        >>> result = run_crs_trial('test-crs', 'test-benchmark', 0, config)
        >>> assert 'povs_found' in result
    """
    logger.info(f"[Trial {trial_num}] Starting CRS '{crs}' on benchmark '{benchmark}'")
    start_time = time.time()

    try:
        # Get snapshot configuration
        snapshot_period = config.get('snapshot_period')

        # Initialize CRS executor
        # Get required paths from config or use defaults
        oss_fuzz_path = Path(config.get('oss_fuzz_path') or (Path.cwd() / 'oss-fuzz'))
        registry_dir = Path(config.get('registry_dir') or (Path.cwd() / 'crses' / 'registry'))
        benchmarks_root = Path(config.get('benchmarks_root') or (Path.cwd() / 'benchmarks'))
        crs_configs_dir = Path(config.get('crs_configs_dir') or (Path.cwd() / 'crses' / 'configs'))

        # Create CRS bug finding executor
        crs_executor = CRSBugFindingExecutor(
            crs_config_name=crs,
            oss_fuzz_path=oss_fuzz_path,
            registry_dir=registry_dir,
            benchmarks_root=benchmarks_root,
            crs_configs_dir=crs_configs_dir
        )

        # Configure executor
        crs_executor.configure_crs({
            'build_timeout': config.get('build_timeout', 3600),
            'run_timeout': config.get('max_total_time', 7200),
            'hints_enabled': config.get('hints_enabled', False),
            'hint_sarif_level': config.get('hint_sarif_level'),
            'hint_corpus_level': config.get('hint_corpus_level'),
        })

        # Initialize benchmark runner with CRS executor and snapshot configuration
        runner = BenchmarkRunner(crs_executor, snapshot_period=snapshot_period)

        # Resolve benchmark path
        benchmark_path = _resolve_benchmark_path(benchmark, config)
        logger.debug(f"Resolved benchmark path: {benchmark_path}")

        # Create trial output directory for snapshots
        trial_output_dir = Path('trial-output').resolve()
        if snapshot_period and snapshot_period > 0:
            # Create trial-specific output directory
            experiment_filestore = Path(config.get('experiment_filestore', '/tmp/experiments')).resolve()
            experiment_name = config.get('experiment', 'unknown')
            trial_output_dir = experiment_filestore / experiment_name / f"{crs}_{benchmark}_trial{trial_num}"
            trial_output_dir.mkdir(parents=True, exist_ok=True)
            logger.info(f"Trial output directory: {trial_output_dir}")

        # Run benchmark evaluation
        # Note: CRS is already configured via executor.configure_crs() above
        result = runner.run_benchmark(
            benchmark_path=benchmark_path,
            mode='auto',  # Auto-detect delta/full mode
            crs_config={},  # Empty config - executor already configured
            trial_output_dir=trial_output_dir
        )

        execution_time = time.time() - start_time

        # Prepare trial result
        trial_result = {
            'crs': crs,
            'benchmark': benchmark,
            'trial_num': trial_num,
            'success': result.is_valid,
            'povs_found': result.povs_found,
            'total_povs': result.total_povs,
            'success_rate': result.success_rate,
            'execution_time': execution_time,
            'report': result.report.to_dict(),
            'metadata': {
                'experiment_filestore': config.get('experiment_filestore'),
                'max_total_time': config.get('max_total_time'),
                'difficulty_level': config.get('difficulty_level'),
                'timestamp_start': start_time,
                'timestamp_end': time.time(),
            }
        }

        logger.info(
            f"[Trial {trial_num}] Completed: {result.povs_found}/{result.total_povs} "
            f"POVs found ({result.success_rate:.1%}) in {execution_time:.1f}s"
        )

        return trial_result

    except FileNotFoundError as e:
        execution_time = time.time() - start_time
        logger.error(f"[Trial {trial_num}] Benchmark not found: {e}")
        return {
            'crs': crs,
            'benchmark': benchmark,
            'trial_num': trial_num,
            'success': False,
            'error': f"Benchmark not found: {str(e)}",
            'error_type': 'FileNotFoundError',
            'execution_time': execution_time,
            'metadata': {
                'timestamp_start': start_time,
                'timestamp_end': time.time(),
            }
        }

    except Exception as e:
        execution_time = time.time() - start_time
        logger.error(f"[Trial {trial_num}] Failed with error: {e}", exc_info=True)
        return {
            'crs': crs,
            'benchmark': benchmark,
            'trial_num': trial_num,
            'success': False,
            'error': str(e),
            'error_type': type(e).__name__,
            'execution_time': execution_time,
            'metadata': {
                'timestamp_start': start_time,
                'timestamp_end': time.time(),
            }
        }


def evaluate_crs_trial(
    trial_id: str,
    trial_data: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Evaluate and aggregate CRS trial results.

    This job handles post-processing of trial results:
    - POV deduplication
    - Patch validation
    - Result aggregation
    - Report generation

    Args:
        trial_id: Unique trial identifier
        trial_data: Trial execution data from run_crs_trial

    Returns:
        dict: Evaluation results with aggregated data

    Example:
        >>> trial_data = {'crs': 'test', 'benchmark': 'test', 'trial_num': 0}
        >>> result = evaluate_crs_trial('trial-001', trial_data)
        >>> assert result['evaluation_complete'] is True
    """
    logger.info(f"Evaluating trial {trial_id}")

    try:
        # TODO: Implement evaluation logic
        # - POV deduplication using crsbench.deduplication
        # - Patch validation using crsbench.patch_tester
        # - Result aggregation
        # - Statistical analysis

        evaluation_result = {
            'trial_id': trial_id,
            'evaluation_complete': True,
            'crs': trial_data.get('crs'),
            'benchmark': trial_data.get('benchmark'),
            'trial_num': trial_data.get('trial_num'),
            'metadata': {
                'evaluation_time': 0.0,
                'timestamp': time.time()
            }
        }

        logger.info(f"Evaluation completed for trial {trial_id}")
        return evaluation_result

    except Exception as e:
        logger.error(f"Evaluation failed for trial {trial_id}: {e}")
        return {
            'trial_id': trial_id,
            'evaluation_complete': False,
            'error': str(e),
            'metadata': {
                'timestamp': time.time()
            }
        }


def _resolve_benchmark_path(benchmark: str, config: Dict[str, Any]) -> Path:
    """
    Resolve benchmark identifier to filesystem path.

    Handles both absolute paths and benchmark identifiers. Searches in standard
    locations if identifier provided.

    Args:
        benchmark: Benchmark identifier or absolute path
        config: Experiment configuration (may contain benchmarks root)

    Returns:
        Path: Resolved benchmark directory path

    Raises:
        FileNotFoundError: If benchmark cannot be found

    Example:
        >>> path = _resolve_benchmark_path('/abs/path/to/bench', {})
        >>> assert path.exists()

        >>> path = _resolve_benchmark_path('test-benchmark', {})
        >>> assert path.name == 'test-benchmark'
    """
    # If it's already an absolute path and exists, use it
    benchmark_path = Path(benchmark)
    if benchmark_path.is_absolute() and benchmark_path.exists():
        logger.debug(f"Using absolute benchmark path: {benchmark_path}")
        return benchmark_path

    # Try to get benchmarks root from config
    benchmarks_root = config.get('benchmarks_root')
    if benchmarks_root:
        benchmark_path = Path(benchmarks_root) / benchmark
        if benchmark_path.exists():
            logger.debug(f"Found benchmark at: {benchmark_path}")
            return benchmark_path

    # Fall back to standard location
    # TODO: Make this configurable or discover dynamically
    default_benchmarks_root = Path(__file__).parent.parent.parent / 'benchmarks'
    benchmark_path = default_benchmarks_root / benchmark

    if benchmark_path.exists():
        logger.debug(f"Found benchmark at default location: {benchmark_path}")
        return benchmark_path

    # Benchmark not found
    error_msg = (
        f"Benchmark not found: {benchmark}\n"
        f"Searched in:\n"
        f"  - Absolute path: {Path(benchmark).absolute()}\n"
    )
    if benchmarks_root:
        error_msg += f"  - Config root: {Path(benchmarks_root) / benchmark}\n"
    error_msg += f"  - Default root: {default_benchmarks_root / benchmark}"

    raise FileNotFoundError(error_msg)
