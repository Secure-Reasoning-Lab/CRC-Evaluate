"""Worker command for distributed execution.

This module provides the 'crsbench worker' subcommand for running
distributed workers that process trial jobs from the Redis queue.

For CI build/verify jobs, use 'crsbench evaluator --ci' instead.
"""

import argparse
import os
import sys


def add_worker_subparser(subparsers) -> None:
    """Add 'worker' subcommand to the CLI.

    Args:
        subparsers: Subparsers object from argparse
    """
    worker_parser = subparsers.add_parser(
        "worker",
        help="Run distributed worker to process trial jobs from Redis queue",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run worker with default settings (localhost Redis, 'default' experiment)
  %(prog)s

  # Run worker for specific experiment
  %(prog)s --experiment-name my-experiment --redis-host redis.example.com

  # Run worker in continuous mode with DEBUG logging
  %(prog)s --continuous --log-level DEBUG

  # Run worker with custom timeout
  %(prog)s --timeout 7200 --experiment-name long-running-exp
        """,
    )

    worker_parser.add_argument(
        "--experiment-config",
        type=str,
        default=None,
        metavar="CONFIG_FILE",
        help="Path to experiment configuration YAML file (optional, provides defaults from 'worker' section)",
    )

    worker_parser.add_argument(
        "--redis-host",
        type=str,
        default="localhost",
        metavar="HOST",
        help="Redis server hostname or IP address (default: localhost)",
    )

    worker_parser.add_argument(
        "--experiment-name",
        type=str,
        default="default",
        metavar="NAME",
        help="Experiment identifier for queue naming (default: default)",
    )

    worker_parser.add_argument(
        "--timeout",
        type=int,
        default=3600,
        metavar="SECONDS",
        help="Job execution timeout in seconds (default: 3600)",
    )

    worker_parser.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        metavar="LEVEL",
        help="Logging level (default: INFO)",
    )

    worker_parser.add_argument(
        "--continuous",
        action="store_true",
        help="Run in continuous mode (never exits, keeps processing jobs)",
    )

    worker_parser.add_argument(
        "--worker-name",
        type=str,
        default=None,
        metavar="NAME",
        help="Worker name for identification (default: hostname)",
    )

    worker_parser.add_argument(
        "--no-cpuset",
        action="store_true",
        help="Disable CPU affinity (not supported with multiple workers)",
    )

    worker_parser.add_argument(
        "--cores",
        type=str,
        default=None,
        metavar="CORES",
        help="CPU cores for worker pool. Integer count (e.g., '32') or cpuset range (e.g., '16-47')",
    )

    worker_parser.add_argument(
        "--skip-cpus",
        type=str,
        default=None,
        metavar="CPUSET",
        help="CPUs to exclude from allocation (cpuset format, e.g., '0-3,8-11')",
    )

    worker_parser.set_defaults(command="worker")


def run_worker(args: argparse.Namespace) -> int:
    """Execute the worker command.

    Args:
        args: Parsed command line arguments

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    from pathlib import Path

    from crsbench.distributed.worker import main as worker_main
    from crsbench.distributed.worker import run_worker_continuous
    from crsbench.utils.logger import configure_logger, get_logger

    # Configure logging
    configure_logger(level=args.log_level, sink=sys.stdout)
    logger = get_logger(__name__)

    # Load experiment config if provided
    config = None
    worker_config = None
    experiment_name_from_config = None
    if args.experiment_config:
        from crsbench.run_experiment import load_experiment_config

        config_path = Path(args.experiment_config)
        logger.info(f"Loading experiment config from: {config_path}")
        config = load_experiment_config(config_path)
        worker_config = config.worker
        experiment_name_from_config = config.experiment
        if worker_config:
            logger.info("Using worker configuration from experiment config")

    # Preflight: check that benchmarks exist on this machine
    if config and config.benchmarks:
        from crsbench.distributed.jobs import check_benchmarks_available

        effective_config = config.model_copy()
        if config.worker and config.worker.benchmarks_root:
            effective_config.benchmarks_root = config.worker.benchmarks_root

        benchmark_names = config.get_benchmark_list()
        missing = check_benchmarks_available(benchmark_names, effective_config)
        if missing:
            logger.error(
                f"Missing {len(missing)} of {len(benchmark_names)} benchmarks "
                f"at {effective_config.benchmarks_root}:\n"
                + "\n".join(f"  - {name}" for name in missing)
            )
            logger.error(
                "Download them with:\n"
                f"  crsbench download --dataset crsbench "
                f"--benchmarks {' '.join(missing)} "
                f"--output-dir {effective_config.benchmarks_root}"
            )
            return 1

    # Resolve settings: CLI > config > defaults
    def resolve(cli_val, config_val, cli_default):
        """Resolve value with priority: CLI > config > default."""
        if cli_val != cli_default:  # CLI was explicitly set
            return cli_val
        if config_val is not None:
            return config_val
        return cli_val  # Use CLI default

    redis_host = resolve(
        args.redis_host,
        (worker_config.redis_host if worker_config else None)
        or (config.redis_host if config else None),
        "localhost",
    )
    num_workers = worker_config.jobs if worker_config else 1
    continuous = args.continuous or (
        worker_config.continuous if worker_config else False
    )
    experiment_name = resolve(
        args.experiment_name, experiment_name_from_config, "default"
    )

    # Trial queue name
    queue_name = f"crsbench_{experiment_name}"

    # Export experiment name for worker logging (internal operational env var)
    os.environ["CRSBENCH_EXPERIMENT_NAME"] = experiment_name

    # Validate --no-cpuset with multiple workers
    if getattr(args, "no_cpuset", False) and num_workers > 1:
        raise ValueError("--no-cpuset is not supported with multiple workers")

    # cpuset is enabled by default, disabled with --no-cpuset
    use_cpuset = not getattr(args, "no_cpuset", False)

    # Get disk space config from worker_config
    minimum_disk_size = worker_config.minimum_disk_size if worker_config else "10GB"
    disk_check_interval = worker_config.disk_check_interval if worker_config else 60

    # Resolve cores and skip_cpus: CLI > worker config > None
    cores = getattr(args, "cores", None)
    if cores is None and worker_config:
        cores = worker_config.cores

    skip_cpus = getattr(args, "skip_cpus", None)
    if skip_cpus is None and worker_config:
        skip_cpus = worker_config.skip_cpus

    try:
        if continuous:
            run_worker_continuous(
                redis_host=redis_host,
                experiment_name=experiment_name,
                worker_name=args.worker_name,
                num_workers=num_workers,
                queue_name=queue_name,
                use_cpuset=use_cpuset,
                minimum_disk_size=minimum_disk_size,
                disk_check_interval=disk_check_interval,
                cores=cores,
                skip_cpus=skip_cpus,
            )
            return 0
        return worker_main(
            redis_host=redis_host,
            experiment_name=experiment_name,
            worker_name=args.worker_name,
            num_workers=num_workers,
            queue_name=queue_name,
            use_cpuset=use_cpuset,
            cores=cores,
            skip_cpus=skip_cpus,
        )

    except KeyboardInterrupt:
        from crsbench.utils.logger import get_logger

        logger = get_logger(__name__)
        logger.info("Worker interrupted by user")
        return 0
    except Exception as e:
        from crsbench.utils.logger import get_logger

        logger = get_logger(__name__)
        logger.error(f"Worker failed: {e}")
        return 1
