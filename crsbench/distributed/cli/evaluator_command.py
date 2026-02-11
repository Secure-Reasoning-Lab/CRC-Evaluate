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
  # Run evaluator (experiment name read from config)
  %(prog)s --experiment-config experiment-config.yaml

  # Run evaluator with 4 parallel verify jobs
  %(prog)s --experiment-config config.yaml -j 4

  # Override experiment name from config
  %(prog)s --experiment-config config.yaml --experiment-name custom-name

  # Run evaluator with custom paths
  %(prog)s --experiment-config config.yaml \\
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
        default=None,
        metavar="NAME",
        help="Override experiment name for queue naming (default: from config)",
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

    evaluator_parser.add_argument(
        "--cores",
        type=str,
        default=None,
        metavar="CORES",
        help="CPU cores for evaluator pool. Integer count (e.g., '32') or cpuset range (e.g., '16-47')",
    )

    evaluator_parser.add_argument(
        "--skip-cpus",
        type=str,
        default=None,
        metavar="CPUSET",
        help="CPUs to exclude from allocation (cpuset format, e.g., '0-3,8-11')",
    )

    evaluator_parser.add_argument(
        "--build-jobs",
        type=int,
        default=None,
        metavar="N",
        help="Max concurrent build jobs (default: value of -j)",
    )

    evaluator_parser.add_argument(
        "--build-cores-per-job",
        type=int,
        default=1,
        metavar="M",
        help="CPUs per build job (default: 1)",
    )

    evaluator_parser.add_argument(
        "--verify-jobs",
        type=int,
        default=None,
        metavar="K",
        help="Max concurrent verify jobs, 1 CPU each (default: build-jobs * build-cores-per-job)",
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

    # Resolve experiment name: CLI > config
    experiment_name = args.experiment_name or config.experiment
    logger.info(f"Experiment name: {experiment_name}")

    # Resolve redis_host: CLI > config > default
    redis_host = args.redis_host
    if redis_host == "localhost" and config.redis_host:
        redis_host = config.redis_host
    logger.info(f"Redis host: {redis_host}")

    # Set evaluator override environment variables
    if args.oss_fuzz_path:
        os.environ["CRSBENCH_EVALUATOR_OSS_FUZZ_PATH"] = args.oss_fuzz_path
        logger.info(f"Evaluator override: oss_fuzz_path = {args.oss_fuzz_path}")

    if args.benchmarks_root:
        os.environ["CRSBENCH_EVALUATOR_BENCHMARKS_ROOT"] = args.benchmarks_root
        logger.info(f"Evaluator override: benchmarks_root = {args.benchmarks_root}")

    # cpuset is enabled by default, disabled with --no-cpuset
    use_cpuset = not getattr(args, "no_cpuset", False)

    # Resolve cores and skip_cpus
    cores = getattr(args, "cores", None)
    skip_cpus = getattr(args, "skip_cpus", None)

    # Resolve dual-queue CI parameters
    build_jobs = getattr(args, "build_jobs", None) or args.jobs
    build_cores_per_job = getattr(args, "build_cores_per_job", 1)
    verify_jobs = getattr(args, "verify_jobs", None) or build_jobs * build_cores_per_job

    try:
        return run_evaluator_main(
            config=config,
            experiment_name=experiment_name,
            redis_host=redis_host,
            max_jobs=args.jobs,
            use_cpuset=use_cpuset,
            cores=cores,
            skip_cpus=skip_cpus,
            build_jobs=build_jobs,
            build_cores_per_job=build_cores_per_job,
            verify_jobs=verify_jobs,
        )
    except KeyboardInterrupt:
        logger.info("Evaluator interrupted by user")
        return 0
    except Exception as e:
        logger.error(f"Evaluator failed: {e}")
        return 1
