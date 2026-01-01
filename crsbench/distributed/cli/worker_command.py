"""Worker command for distributed execution.

This module provides the 'crsbench worker' subcommand for running
distributed workers that process jobs from the Redis queue.
"""

import argparse
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

    worker_parser.set_defaults(command="worker")


def run_worker(args: argparse.Namespace) -> int:
    """Execute the worker command.

    Args:
        args: Parsed command line arguments

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    from crsbench.distributed.worker import main as worker_main
    from crsbench.distributed.worker import run_worker_continuous
    from crsbench.utils.logger import configure_logger

    # Configure logging
    configure_logger(level=args.log_level, sink=sys.stdout)

    # Prepare worker arguments
    worker_args = {
        "redis_host": args.redis_host,
        "experiment_name": args.experiment_name,
        "timeout": args.timeout,
        "worker_name": args.worker_name,
    }

    try:
        if args.continuous:
            # Run in continuous mode
            run_worker_continuous(
                redis_host=args.redis_host,
                experiment_name=args.experiment_name,
                _timeout=args.timeout,
                worker_name=args.worker_name,
            )
            return 0
        # Run in burst mode (default)
        return worker_main(**worker_args)

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
