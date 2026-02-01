"""Evaluator command for distributed POV verification.

This module provides the 'crsbench evaluator' subcommand for running
distributed evaluators that build variant images and process POV verification
jobs from the Redis verify queue.
"""

import argparse
import os
import sys


def add_evaluator_subparser(subparsers) -> None:
    """Add 'evaluator' subcommand to the CLI.

    Args:
        subparsers: Subparsers object from argparse
    """
    evaluator_parser = subparsers.add_parser(
        "evaluator",
        help="Run distributed evaluator to build variants and verify POVs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run evaluator with experiment config
  %(prog)s --experiment-config experiment-config.yaml --experiment-name my-exp

  # Run evaluator with 4 parallel verify jobs
  %(prog)s --experiment-config config.yaml --experiment-name exp1 -j 4

  # Run evaluator with custom paths
  %(prog)s --experiment-config config.yaml --experiment-name exp1 \\
    --oss-fuzz-path /opt/oss-fuzz --benchmarks-root /data/benchmarks
        """,
    )

    evaluator_parser.add_argument(
        "--experiment-config",
        type=str,
        required=True,
        metavar="CONFIG_FILE",
        help="Path to experiment configuration YAML file",
    )

    evaluator_parser.add_argument(
        "--experiment-name",
        type=str,
        required=True,
        metavar="NAME",
        help="Experiment identifier for queue naming",
    )

    evaluator_parser.add_argument(
        "--redis-host",
        type=str,
        default="localhost",
        metavar="HOST",
        help="Redis server hostname or IP address (default: localhost)",
    )

    evaluator_parser.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=1,
        metavar="N",
        help="Number of parallel verify jobs (default: 1)",
    )

    evaluator_parser.add_argument(
        "--benchmarks-root",
        type=str,
        default=None,
        metavar="PATH",
        help="Override benchmarks root directory",
    )

    evaluator_parser.add_argument(
        "--oss-fuzz-path",
        type=str,
        default=None,
        metavar="PATH",
        help="Override oss-fuzz directory path",
    )

    evaluator_parser.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        metavar="LEVEL",
        help="Logging level (default: INFO)",
    )

    evaluator_parser.add_argument(
        "--no-cpuset",
        action="store_true",
        help="Disable CPU affinity for verify jobs",
    )

    evaluator_parser.set_defaults(command="evaluator")


def run_evaluator(args: argparse.Namespace) -> int:
    """Execute the evaluator command.

    Loads experiment config, builds all variant images, then starts
    listening on the Redis verification queue.

    Args:
        args: Parsed command line arguments

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    from pathlib import Path

    from crsbench.distributed.evaluator import run_evaluator_main
    from crsbench.run_experiment import load_experiment_config
    from crsbench.utils.logger import configure_logger, get_logger

    # Configure logging
    configure_logger(level=args.log_level, sink=sys.stdout)
    logger = get_logger(__name__)

    # Load experiment config
    config_path = Path(args.experiment_config)
    logger.info(f"Loading experiment config from: {config_path}")
    config = load_experiment_config(config_path)

    # Set evaluator override environment variables
    if args.oss_fuzz_path:
        os.environ["CRSBENCH_EVALUATOR_OSS_FUZZ_PATH"] = args.oss_fuzz_path
        logger.info(f"Evaluator override: oss_fuzz_path = {args.oss_fuzz_path}")

    if args.benchmarks_root:
        os.environ["CRSBENCH_EVALUATOR_BENCHMARKS_ROOT"] = args.benchmarks_root
        logger.info(f"Evaluator override: benchmarks_root = {args.benchmarks_root}")

    # cpuset is enabled by default, disabled with --no-cpuset
    use_cpuset = not getattr(args, "no_cpuset", False)

    try:
        return run_evaluator_main(
            config=config,
            experiment_name=args.experiment_name,
            redis_host=args.redis_host,
            max_jobs=args.jobs,
            use_cpuset=use_cpuset,
        )
    except KeyboardInterrupt:
        logger.info("Evaluator interrupted by user")
        return 0
    except Exception as e:
        logger.error(f"Evaluator failed: {e}")
        return 1
