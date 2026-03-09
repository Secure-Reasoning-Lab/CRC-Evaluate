"""Worker command for distributed execution.

This module provides the 'crsbench worker' subcommand for running
distributed workers that process trial jobs from the Redis queue.

Two modes:
  - Default (no config): discover experiments from Redis registry
  - --experiment-config: focus on a specific experiment

For CI build/verify jobs, use 'crsbench evaluator --ci' instead.
"""

import argparse
import os
import sys


def _positive_int(value: str) -> int:
    """Parse positive integer CLI values."""
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError(f"Expected integer >= 1, got {value}")
    return parsed


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
  # Discover experiments from Redis registry (default)
  %(prog)s --continuous

  # Focus on a specific experiment
  %(prog)s --experiment-config my-experiment.yaml --continuous

  # With CPU affinity
  %(prog)s --continuous --cpuset 16-47
        """,
    )

    worker_parser.add_argument(
        "--experiment-config",
        type=str,
        default=None,
        metavar="CONFIG_FILE",
        help="Focus on a specific experiment (default: discover all from registry)",
    )

    worker_parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose logging (DEBUG level)",
    )

    worker_parser.add_argument(
        "--jobs",
        type=_positive_int,
        default=None,
        metavar="N",
        help="Max concurrent worker jobs (CLI override)",
    )

    worker_parser.add_argument(
        "--cores-per-job",
        type=_positive_int,
        default=None,
        metavar="M",
        help="CPUs per worker job when cpuset supervisor is used (CLI override)",
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
        "--cpuset",
        type=str,
        default=None,
        metavar="CPUSET",
        help="CPU cores for worker pool. Integer count (e.g., '32') or cpuset range (e.g., '16-47')",
    )

    worker_parser.add_argument(
        "--skip-cpuset",
        type=str,
        default=None,
        metavar="CPUSET",
        help="CPUs to exclude from allocation (cpuset format, e.g., '0-3,8-11')",
    )

    worker_parser.add_argument(
        "--cpu-tag",
        type=str,
        default=None,
        metavar="TAG",
        help="Optional CPU capability tag. Worker only executes jobs with matching cpu_tag (or untagged jobs).",
    )

    worker_parser.set_defaults(command="worker")


def run_worker(args: argparse.Namespace) -> int:
    """Execute the worker command.

    Without ``--experiment-config``, discovers experiments from the Redis
    registry and listens on all discovered trial queues.

    With ``--experiment-config``, focuses on the specified experiment.

    Args:
        args: Parsed command line arguments

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    from crsbench.distributed.common import normalize_cpu_tag, normalize_redis_host
    from crsbench.utils.logger import configure_logger, get_logger

    log_level = "DEBUG" if args.verbose else "INFO"
    configure_logger(level=log_level, sink=sys.stdout)
    os.environ["CRSBENCH_LOG_LEVEL"] = log_level
    logger = get_logger(__name__)

    cores = getattr(args, "cpuset", None)
    skip_cpus = getattr(args, "skip_cpuset", None)
    use_cpuset = cores is not None or skip_cpus is not None
    cpu_tag = getattr(args, "cpu_tag", None)
    jobs_override = getattr(args, "jobs", None)
    cores_per_job_override = getattr(args, "cores_per_job", None)

    # --- Configless mode: discover experiments from registry ---
    if not args.experiment_config:
        from crsbench.distributed.worker import run_worker_configless

        redis_host = normalize_redis_host(
            os.environ.get("CRSBENCH_REDIS_HOST", "localhost")
        )
        if redis_host is None:
            logger.error(
                "Distributed worker requires a Redis host; "
                "set CRSBENCH_REDIS_HOST to a non-empty hostname"
            )
            return 1

        try:
            return run_worker_configless(
                redis_host=redis_host,
                worker_name=args.worker_name,
                use_cpuset=use_cpuset,
                cores=cores,
                skip_cpus=skip_cpus,
                cpu_tag=cpu_tag,
                continuous=args.continuous,
                jobs_override=jobs_override,
                cores_per_job_override=cores_per_job_override,
                log_level=log_level,
            )
        except KeyboardInterrupt:
            logger.info("Worker interrupted by user")
            return 0
        except Exception as e:
            logger.error(f"Worker failed: {e}")
            return 1

    # --- Config mode: focus on a specific experiment ---
    from pathlib import Path

    from crsbench.distributed.worker import main as worker_main
    from crsbench.distributed.worker import run_worker_continuous
    from crsbench.run_experiment import load_experiment_config

    config_path = Path(args.experiment_config)
    logger.info(f"Loading experiment config from: {config_path}")
    config = load_experiment_config(config_path)
    worker_config = config.worker

    # Preflight: check that benchmarks exist on this machine
    if config.benchmarks:
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
    redis_host = (
        (worker_config.redis_host if worker_config else None)
        or (config.redis_host if config else None)
        or os.environ.get("CRSBENCH_REDIS_HOST", "localhost")
    )
    redis_host = normalize_redis_host(redis_host)
    if redis_host is None:
        logger.error(
            "Distributed worker requires a Redis host; "
            "set worker.redis_host, redis_host, or CRSBENCH_REDIS_HOST"
        )
        return 1
    num_workers = (
        jobs_override
        if jobs_override is not None
        else (worker_config.jobs if worker_config else 1)
    )
    default_cores_per_job = config.resources.cores_per_trial if config.resources else 4
    cores_per_job = (
        cores_per_job_override
        if cores_per_job_override is not None
        else (
            worker_config.cores_per_job
            if worker_config and worker_config.cores_per_job is not None
            else default_cores_per_job
        )
    )
    continuous = args.continuous or (
        worker_config.continuous if worker_config else False
    )
    experiment_name = config.experiment
    from crsbench.distributed.queue import resolve_queue_names

    queue_name, _build_queue_name, _verify_queue_name = resolve_queue_names(
        experiment_name
    )

    worker_name = args.worker_name
    if worker_name is None and worker_config and worker_config.worker_name:
        worker_name = worker_config.worker_name

    minimum_disk_size = worker_config.minimum_disk_size if worker_config else "10GB"
    disk_check_interval = worker_config.disk_check_interval if worker_config else 60

    if cpu_tag is None:
        worker_cpu_tag = normalize_cpu_tag(
            worker_config.cpu_tag if worker_config else None
        )
        resources_cpu_tag = normalize_cpu_tag(
            config.resources.cpu_tag if config.resources else None
        )
        cpu_tag = worker_cpu_tag or resources_cpu_tag
    else:
        cpu_tag = normalize_cpu_tag(cpu_tag)

    try:
        if continuous:
            run_worker_continuous(
                redis_host=redis_host,
                experiment_name=experiment_name,
                worker_name=worker_name,
                num_workers=num_workers,
                queue_name=queue_name,
                use_cpuset=use_cpuset,
                minimum_disk_size=minimum_disk_size,
                disk_check_interval=disk_check_interval,
                cores=cores,
                skip_cpus=skip_cpus,
                cpu_tag=cpu_tag,
                cores_per_job=cores_per_job,
                log_level=log_level,
            )
            return 0
        return worker_main(
            redis_host=redis_host,
            experiment_name=experiment_name,
            worker_name=worker_name,
            num_workers=num_workers,
            queue_name=queue_name,
            use_cpuset=use_cpuset,
            cores=cores,
            skip_cpus=skip_cpus,
            cpu_tag=cpu_tag,
            cores_per_job=cores_per_job,
            log_level=log_level,
        )
    except KeyboardInterrupt:
        logger.info("Worker interrupted by user")
        return 0
    except Exception as e:
        logger.error(f"Worker failed: {e}")
        return 1
