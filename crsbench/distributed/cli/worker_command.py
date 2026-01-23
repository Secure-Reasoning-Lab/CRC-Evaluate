"""Worker command for distributed execution.

This module provides the 'crsbench worker' subcommand for running
distributed workers that process jobs from the Redis queue.
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
        help="Run distributed worker to process jobs from Redis queue",
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
        "-j",
        "--jobs",
        type=int,
        default=1,
        metavar="N",
        help="Number of parallel worker processes (default: 1)",
    )

    worker_parser.add_argument(
        "--no-cpuset",
        action="store_true",
        help="Disable CPU affinity (not supported with -j > 1)",
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

    # Resolve settings: CLI > config > defaults
    # Helper to get value with priority
    def resolve(cli_val, config_val, cli_default):
        """Resolve value with priority: CLI > config > default."""
        if cli_val != cli_default:  # CLI was explicitly set
            return cli_val
        if config_val is not None:
            return config_val
        return cli_val  # Use CLI default

    redis_host = resolve(
        args.redis_host,
        worker_config.redis_host if worker_config else None,
        "localhost",
    )
    num_workers = resolve(args.jobs, worker_config.jobs if worker_config else None, 1)
    continuous = args.continuous or (
        worker_config.continuous if worker_config else False
    )
    experiment_name = resolve(
        args.experiment_name, experiment_name_from_config, "default"
    )

    # Set worker override environment variables
    if worker_config:
        override_fields = [
            "oss_fuzz_path",
            "registry_dir",
            "crs_configs_dir",
            "benchmarks_root",
            "benchmark_suites_root",
            "experiment_filestore",
            "report_filestore",
            "keep_only_results",
            "cleanup_after_trial",
            "copy_results_after_trial",
            "copy_results_to_filestore",
            "experiment_results_filestore",
            "reports_results_filestore",
        ]
        for field in override_fields:
            value = getattr(worker_config, field, None)
            if value is not None:
                env_var = f"CRSBENCH_WORKER_{field.upper()}"
                os.environ[env_var] = str(value)
                logger.info(f"Worker override: {field} = {value}")

    # Validate --no-cpuset with multiple workers
    if getattr(args, "no_cpuset", False) and num_workers > 1:
        raise ValueError("--no-cpuset is not supported with multiple workers (-j > 1)")

    # cpuset is enabled by default, disabled with --no-cpuset
    use_cpuset = not getattr(args, "no_cpuset", False)

    # TODO: fix timeout too short
    # Prepare worker arguments with resolved values
    worker_args = {
        "redis_host": redis_host,
        "experiment_name": experiment_name,
        "timeout": args.timeout,
        "worker_name": args.worker_name,
        "num_workers": num_workers,
    }

    try:
        if continuous:
            # Run in continuous mode (use resolved values)
            run_worker_continuous(
                redis_host=redis_host,
                experiment_name=experiment_name,
                _timeout=args.timeout,
                worker_name=args.worker_name,
                num_workers=num_workers,
                use_cpuset=use_cpuset,
            )
            return 0
        # Run in burst mode (default)
        return worker_main(**worker_args, use_cpuset=use_cpuset)

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
