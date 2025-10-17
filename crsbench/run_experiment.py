#!/usr/bin/env python3
"""Experiment runner for CRSBench evaluation framework.

This script provides a single entry point for running CRS evaluations with
standardized experiment configurations, CRS integration, and benchmark suite
management.

Usage:
    crsbench \
        --experiment-config experiment-config.yaml \
        --benchmarks benchmark1,benchmark2 \
        --experiment-name my-experiment \
        --crses atlantis-c,atlantis-multilang
"""

import argparse
import logging
import sys
import yaml
import time
from pathlib import Path
from typing import List, NamedTuple, Dict, Any, Optional
from collections import namedtuple
from dotenv import load_dotenv

# Load environment variables from .env file if present
load_dotenv()

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Trial configuration
Trial = namedtuple('Trial', ['crs', 'benchmark', 'trial_num'])


def parse_arguments() -> argparse.Namespace:
    """Parse command line arguments.

    Returns:
        Parsed arguments with experiment configuration.
    """
    parser = argparse.ArgumentParser(
        prog='crsbench',
        description='Run CRS evaluation experiments',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run single benchmark with single CRS
  %(prog)s --experiment-config config.yaml --benchmarks bench1 \\
           --experiment-name exp1 --crses atlantis-c

  # Run multiple benchmarks with multiple CRSes
  %(prog)s --experiment-config config.yaml \\
           --benchmarks bench1,bench2,bench3 \\
           --experiment-name multi-exp --crses crs1,crs2

  # Run benchmark suite
  %(prog)s --experiment-config config.yaml --benchmarks crsbench-c \\
           --experiment-name suite-exp --crses atlantis-multilang
        """
    )

    parser.add_argument(
        '--experiment-config',
        type=str,
        required=True,
        metavar='CONFIG_FILE',
        help='Path to experiment configuration YAML file (e.g., experiment-config.yaml)'
    )

    parser.add_argument(
        '--benchmarks',
        type=str,
        required=True,
        metavar='BENCHMARK_LIST',
        help='Comma-separated list of benchmarks or benchmark suite name (e.g., bench1,bench2 or crsbench-c)'
    )

    parser.add_argument(
        '--experiment-name',
        type=str,
        required=True,
        metavar='EXPERIMENT_NAME',
        help='Name for this experiment (used for tracking and reporting)'
    )

    parser.add_argument(
        '--crses',
        type=str,
        required=True,
        metavar='CRS_LIST',
        help='Comma-separated list of CRS implementations to evaluate (e.g., atlantis-c,atlantis-multilang)'
    )

    parser.add_argument(
        '--local-only',
        action='store_true',
        help='Force local execution mode without Redis (useful for single jobs or testing)'
    )

    return parser.parse_args()


def validate_arguments(args: argparse.Namespace) -> None:
    """Validate parsed arguments.

    Args:
        args: Parsed command line arguments

    Raises:
        SystemExit: If validation fails
    """
    # Validate experiment config file exists
    config_path = Path(args.experiment_config)
    if not config_path.exists():
        logger.error(f"Experiment configuration file not found: {config_path}")
        sys.exit(1)

    if not config_path.is_file():
        logger.error(f"Experiment configuration path is not a file: {config_path}")
        sys.exit(1)

    # Validate file extension
    if config_path.suffix not in ['.yaml', '.yml']:
        logger.warning(f"Configuration file does not have .yaml/.yml extension: {config_path}")

    logger.info(f"Experiment configuration: {config_path}")


def parse_list_argument(arg_value: str) -> List[str]:
    """Parse comma-separated list argument.

    Args:
        arg_value: Comma-separated string value

    Returns:
        List of stripped strings
    """
    return [item.strip() for item in arg_value.split(',') if item.strip()]


def load_experiment_config(config_path: Path):
    """Load and validate experiment configuration from YAML file.

    Args:
        config_path: Path to experiment configuration YAML file

    Returns:
        ExperimentConfig: Validated experiment configuration

    Raises:
        SystemExit: If configuration is invalid
    """
    from crsbench.validation import validate_experiment_config
    from crsbench.validation.schemas import ExperimentConfig

    # Validate the configuration file
    result = validate_experiment_config(config_path)

    if not result.is_valid:
        logger.error("Experiment configuration validation failed:")
        for error in result.errors:
            logger.error(f"  - {error.message}")
        sys.exit(1)

    # Load and parse the YAML file
    with open(config_path, 'r') as f:
        config_data = yaml.safe_load(f)

    # Create validated config object
    config = ExperimentConfig(**config_data)

    logger.info("Experiment configuration loaded and validated successfully")
    return config


def generate_trial_matrix(benchmarks: List[str], crses: List[str], config) -> List[Trial]:
    """Generate all trial combinations from benchmarks, CRSes, and trials.

    Args:
        benchmarks: List of benchmark identifiers
        crses: List of CRS identifiers
        config: Experiment configuration with trials count

    Returns:
        List of Trial namedtuples
    """
    trials = []
    for crs in crses:
        for benchmark in benchmarks:
            for trial_num in range(config.trials):
                trials.append(Trial(crs, benchmark, trial_num))

    logger.info(f"Generated {len(trials)} trials: {len(crses)} CRSes × {len(benchmarks)} benchmarks × {config.trials} trials")
    return trials


def should_use_distributed_mode(args: argparse.Namespace, config, total_jobs: int) -> bool:
    """Determine if distributed mode should be used.

    Criteria for local mode:
    - Only 1 total trial (1 CRS × 1 benchmark × 1 trial)
    - redis_host not specified in config
    - Redis not available (connection check)
    - User explicitly requests local mode via --local-only flag

    Args:
        args: Parsed command line arguments
        config: Experiment configuration
        total_jobs: Total number of jobs to execute

    Returns:
        bool: True if should use distributed mode, False for local mode
    """
    from crsbench.distributed.queue import check_redis_available

    # User explicitly disabled distributed mode
    if args.local_only:
        logger.info("Local mode explicitly requested via --local-only flag")
        return False

    # Only 1 job - use local mode by default
    if total_jobs == 1:
        logger.info(f"Single job detected ({total_jobs} jobs total), using local mode")
        return False

    # No Redis host configured
    if not config.redis_host or config.redis_host == "none":
        logger.info("No Redis host configured, using local mode")
        return False

    # Check if Redis is available
    if not check_redis_available(config.redis_host):
        logger.warning(f"Redis not available at {config.redis_host}, falling back to local mode")
        return False

    # Multiple jobs and Redis available - use distributed
    logger.info(f"Multiple jobs detected ({total_jobs} jobs total), using distributed mode")
    return True


def run_experiment_local(args: argparse.Namespace, config, benchmarks: List[str], crses: List[str]) -> None:
    """Run experiment locally without Redis queue.

    Executes all trials sequentially in the current process.

    Args:
        args: Parsed command line arguments
        config: Experiment configuration
        benchmarks: List of benchmark identifiers
        crses: List of CRS identifiers
    """
    logger.info("="*60)
    logger.info("Running CRSBench in Local Mode (No Redis)")
    logger.info("="*60)

    # Generate trial matrix
    trials = generate_trial_matrix(benchmarks, crses, config)

    logger.info(f"Total trials to execute: {len(trials)}")
    logger.info(f"CRSes: {', '.join(crses)}")
    logger.info(f"Benchmarks: {', '.join(benchmarks)}")
    logger.info(f"Trials per combination: {config.trials}")
    logger.info("="*60)

    # Execute trials sequentially
    results = []
    for idx, trial in enumerate(trials, 1):
        logger.info(f"\n[{idx}/{len(trials)}] Starting trial:")
        logger.info(f"  CRS: {trial.crs}")
        logger.info(f"  Benchmark: {trial.benchmark}")
        logger.info(f"  Trial: {trial.trial_num}")

        # Import and execute job directly
        from crsbench.distributed.jobs import run_crs_trial

        result = run_crs_trial(
            crs=trial.crs,
            benchmark=trial.benchmark,
            trial_num=trial.trial_num,
            config=config.to_dict()
        )

        results.append(result)

        # Log result
        if result.get('success'):
            logger.info(f"  ✓ Success: {result.get('povs_found', 0)}/{result.get('total_povs', 0)} POVs found")
        else:
            logger.error(f"  ✗ Failed: {result.get('error', 'Unknown error')}")

    # Generate final report
    logger.info("\n" + "="*60)
    logger.info("Experiment Complete - Generating Report")
    logger.info("="*60)

    generate_final_report(results, args.experiment_name, config)


def monitor_jobs(queue, job_list: List, experiment_name: str) -> List[Dict[str, Any]]:
    """Monitor job progress and display status.

    Args:
        queue: RQ queue instance
        job_list: List of enqueued RQ jobs
        experiment_name: Experiment identifier

    Returns:
        List of job results
    """
    try:
        from rich.console import Console
        from rich.table import Table
        from rich.live import Live
        from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
        RICH_AVAILABLE = True
    except ImportError:
        RICH_AVAILABLE = False
        logger.warning("Rich library not available, using basic progress display")

    if RICH_AVAILABLE:
        return _monitor_jobs_rich(queue, job_list, experiment_name)
    else:
        return _monitor_jobs_basic(queue, job_list, experiment_name)


def _monitor_jobs_basic(queue, job_list: List, experiment_name: str) -> List[Dict[str, Any]]:
    """Basic job monitoring without Rich UI."""
    from crsbench.distributed.queue import get_queue_stats

    logger.info(f"\nMonitoring {len(job_list)} jobs for experiment: {experiment_name}")

    while True:
        stats = get_queue_stats(queue)

        # Display stats
        print(f"\n{'='*60}")
        print(f"Experiment: {experiment_name}")
        print(f"{'='*60}")
        print(f"Queued:    {stats['queued']}")
        print(f"Started:   {stats['started']}")
        print(f"Finished:  {stats['finished']}")
        print(f"Failed:    {stats['failed']}")
        print(f"{'='*60}\n")

        # Check if all jobs completed
        completed = 0
        failed = 0
        for job in job_list:
            job.refresh()
            if job.is_finished:
                completed += 1
            elif job.is_failed:
                failed += 1

        print(f"Progress: {completed + failed}/{len(job_list)} jobs complete ({completed} success, {failed} failed)")

        if completed + failed >= len(job_list):
            break

        time.sleep(3)

    # Collect results
    results = []
    for job in job_list:
        job.refresh()
        if job.result:
            results.append(job.result)
        elif job.is_failed:
            results.append({
                'success': False,
                'error': f"Job failed: {job.exc_info}",
                'job_id': job.id
            })

    return results


def _monitor_jobs_rich(queue, job_list: List, experiment_name: str) -> List[Dict[str, Any]]:
    """Monitor jobs with Rich UI."""
    from rich.console import Console
    from rich.table import Table
    from rich.live import Live
    from crsbench.distributed.queue import get_queue_stats

    console = Console()

    def generate_status_table():
        stats = get_queue_stats(queue)

        table = Table(title=f"Experiment: {experiment_name}")
        table.add_column("Status", style="cyan")
        table.add_column("Count", justify="right", style="magenta")

        table.add_row("Queued", str(stats['queued']))
        table.add_row("Started", str(stats['started']))
        table.add_row("Finished", str(stats['finished']), style="green")
        table.add_row("Failed", str(stats['failed']), style="red")
        table.add_row("Total", str(len(job_list)))

        return table

    with Live(generate_status_table(), refresh_per_second=1, console=console) as live:
        while True:
            # Check if all jobs completed
            completed = 0
            failed = 0
            for job in job_list:
                job.refresh()
                if job.is_finished:
                    completed += 1
                elif job.is_failed:
                    failed += 1

            if completed + failed >= len(job_list):
                break

            live.update(generate_status_table())
            time.sleep(1)

    # Collect results
    results = []
    for job in job_list:
        job.refresh()
        if job.result:
            results.append(job.result)
        elif job.is_failed:
            results.append({
                'success': False,
                'error': f"Job failed: {job.exc_info}",
                'job_id': job.id
            })

    console.print(f"\n[green]✓[/green] All jobs completed!")
    return results


def run_experiment_distributed(args: argparse.Namespace, config, benchmarks: List[str], crses: List[str]) -> None:
    """Run experiment using Redis queue-based distributed execution.

    Args:
        args: Parsed command line arguments
        config: Experiment configuration
        benchmarks: List of benchmark identifiers
        crses: List of CRS identifiers
    """
    from crsbench.distributed.queue import initialize_queue

    logger.info("="*60)
    logger.info("Running CRSBench in Distributed Mode (Redis)")
    logger.info("="*60)
    logger.info(f"Redis host: {config.redis_host}")

    # Initialize queue
    try:
        queue = initialize_queue(config.redis_host, args.experiment_name)
    except Exception as e:
        logger.error(f"Failed to initialize queue: {e}")
        logger.error("Falling back to local execution mode")
        run_experiment_local(args, config, benchmarks, crses)
        return

    # Generate trial matrix
    trials = generate_trial_matrix(benchmarks, crses, config)

    logger.info(f"Total trials to enqueue: {len(trials)}")
    logger.info(f"CRSes: {', '.join(crses)}")
    logger.info(f"Benchmarks: {', '.join(benchmarks)}")
    logger.info(f"Trials per combination: {config.trials}")
    logger.info("="*60)

    # Enqueue jobs
    logger.info("\nEnqueuing jobs...")
    jobs = []
    for trial in trials:
        job = queue.enqueue(
            'crsbench.distributed.jobs.run_crs_trial',
            crs=trial.crs,
            benchmark=trial.benchmark,
            trial_num=trial.trial_num,
            config=config.to_dict(),
            job_timeout=config.max_total_time,
            result_ttl=-1  # Persist results forever
        )
        jobs.append(job)
        logger.debug(f"Enqueued job {job.id} for {trial.crs} on {trial.benchmark} (trial {trial.trial_num})")

    logger.info(f"✓ Enqueued {len(jobs)} jobs successfully")

    # Monitor progress
    logger.info("\nMonitoring job progress...")
    results = monitor_jobs(queue, jobs, args.experiment_name)

    # Generate final report
    logger.info("\n" + "="*60)
    logger.info("Experiment Complete - Generating Report")
    logger.info("="*60)

    generate_final_report(results, args.experiment_name, config)


def generate_final_report(results: List[Dict[str, Any]], experiment_name: str, config) -> None:
    """Generate and display final experiment report.

    Args:
        results: List of trial results
        experiment_name: Experiment identifier
        config: Experiment configuration
    """
    logger.info(f"\nFinal Report for Experiment: {experiment_name}")
    logger.info("="*60)

    # Count successes and failures
    total_trials = len(results)
    successful_trials = sum(1 for r in results if r.get('success', False))
    failed_trials = total_trials - successful_trials

    logger.info(f"Total trials: {total_trials}")
    logger.info(f"Successful: {successful_trials} ({successful_trials/total_trials*100:.1f}%)")
    logger.info(f"Failed: {failed_trials} ({failed_trials/total_trials*100:.1f}%)")

    # Aggregate POV statistics
    if successful_trials > 0:
        total_povs_found = sum(r.get('povs_found', 0) for r in results if r.get('success', False))
        total_povs_available = sum(r.get('total_povs', 0) for r in results if r.get('success', False))

        if total_povs_available > 0:
            overall_success_rate = total_povs_found / total_povs_available
            logger.info(f"\nPOV Discovery:")
            logger.info(f"  Total POVs found: {total_povs_found}/{total_povs_available}")
            logger.info(f"  Overall success rate: {overall_success_rate:.1%}")

    # Report failures
    if failed_trials > 0:
        logger.warning(f"\nFailed trials ({failed_trials}):")
        for idx, result in enumerate(results):
            if not result.get('success', False):
                error = result.get('error', 'Unknown error')
                crs = result.get('crs', 'unknown')
                benchmark = result.get('benchmark', 'unknown')
                trial_num = result.get('trial_num', '?')
                logger.warning(f"  [{idx+1}] {crs} on {benchmark} (trial {trial_num}): {error}")

    logger.info("\n" + "="*60)
    logger.info("Report generation complete")
    logger.info(f"Experiment filestore: {config.experiment_filestore}")
    logger.info(f"Report filestore: {config.report_filestore}")
    logger.info("="*60)


def main() -> None:
    """Main entry point for the experiment runner."""
    # Parse arguments
    args = parse_arguments()

    # Validate arguments
    validate_arguments(args)

    # Parse list arguments
    benchmarks = parse_list_argument(args.benchmarks)
    crses = parse_list_argument(args.crses)

    # Log experiment configuration
    logger.info("="*60)
    logger.info("CRSBench Experiment Runner")
    logger.info("="*60)
    logger.info(f"Experiment name: {args.experiment_name}")
    logger.info(f"Configuration file: {args.experiment_config}")
    logger.info(f"Benchmarks ({len(benchmarks)}): {', '.join(benchmarks)}")
    logger.info(f"CRSes ({len(crses)}): {', '.join(crses)}")
    logger.info("="*60)

    # Load and validate experiment configuration
    config_path = Path(args.experiment_config)
    config = load_experiment_config(config_path)

    # Calculate total jobs
    total_jobs = len(benchmarks) * len(crses) * config.trials
    logger.info(f"Total jobs to execute: {total_jobs}")

    # Determine execution mode
    use_distributed = should_use_distributed_mode(args, config, total_jobs)

    # Run experiment in appropriate mode
    if use_distributed:
        run_experiment_distributed(args, config, benchmarks, crses)
    else:
        run_experiment_local(args, config, benchmarks, crses)


if __name__ == "__main__":
    main()
