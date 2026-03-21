"""Evaluator command for distributed POV verification.

This module provides the 'crsbench evaluator' subcommand for running
distributed evaluators that build variant images and process POV verification
jobs from the Redis verify queue.

Three modes:
  - Default (no config, no --ci): discover experiments from Redis registry
  - --experiment-config: focus on a specific experiment
  - --ci: compatibility alias that runs the unified evaluator path on legacy CI queues
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


def _non_negative_int(value: str) -> int:
    """Parse non-negative integer CLI values."""
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError(f"Expected integer >= 0, got {value}")
    return parsed


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
  # Discover experiments from Redis registry (default)
  %(prog)s --jobs 4 --cores-per-job 4

  # Focus on a specific experiment
  %(prog)s --experiment-config experiment-config.yaml --jobs 4 --cores-per-job 4

  # CI compatibility alias (legacy queues)
  %(prog)s --ci --jobs 4 --cores-per-job 4
        """,
    )

    evaluator_parser.add_argument(
        "--experiment-config",
        type=str,
        required=False,
        default=None,
        metavar="CONFIG_FILE",
        help="Focus on a specific experiment (default: discover all from registry)",
    )

    evaluator_parser.add_argument(
        "--ci",
        action="store_true",
        help="Compatibility alias for legacy CI queues via unified evaluator mode",
    )

    evaluator_parser.add_argument(
        "--benchmarks-root",
        type=str,
        default=None,
        metavar="PATH",
        help="Override benchmarks root directory",
    )

    evaluator_parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose logging (DEBUG level)",
    )

    evaluator_parser.add_argument(
        "--cpuset",
        type=str,
        default=None,
        metavar="CPUSET",
        help="CPU cores for evaluator pool. Integer count (e.g., '32') or cpuset range (e.g., '16-47')",
    )

    evaluator_parser.add_argument(
        "--skip-cpuset",
        type=str,
        default=None,
        metavar="CPUSET",
        help="CPUs to exclude from allocation (cpuset format, e.g., '0-3,8-11')",
    )
    evaluator_parser.add_argument(
        "--cpu-tag",
        type=str,
        default=None,
        metavar="TAG",
        help="Optional CPU capability tag. Evaluator only executes jobs with matching cpu_tag (or untagged jobs).",
    )

    evaluator_parser.add_argument(
        "--jobs",
        type=_positive_int,
        default=None,
        metavar="N",
        help="Default concurrent evaluator jobs used for both build and verify when split overrides are not set",
    )

    evaluator_parser.add_argument(
        "--cores-per-job",
        type=_positive_int,
        default=None,
        metavar="M",
        help="Default CPUs per evaluator job used for both build and verify when split overrides are not set",
    )

    evaluator_parser.add_argument(
        "--build-jobs",
        type=_positive_int,
        default=None,
        metavar="N",
        help="Advanced override for build job concurrency (defaults to --jobs when set)",
    )

    evaluator_parser.add_argument(
        "--build-cores-per-job",
        type=_positive_int,
        default=None,
        metavar="M",
        help="Advanced override for build CPUs per job (defaults to --cores-per-job when set)",
    )

    evaluator_parser.add_argument(
        "--verify-jobs",
        type=_positive_int,
        default=None,
        metavar="K",
        help="Advanced override for verify job concurrency (defaults to --jobs when set)",
    )

    evaluator_parser.add_argument(
        "--verify-cores-per-job",
        type=_positive_int,
        default=None,
        metavar="M",
        help="Advanced override for verify CPUs per job (defaults to --cores-per-job when set)",
    )

    evaluator_parser.add_argument(
        "--idle-timeout",
        type=_non_negative_int,
        default=None,
        metavar="SECONDS",
        help="Exit after N seconds with no build or verify activity after backlog drains (0=disabled/infinite, default: from config/metadata, else 0)",
    )

    evaluator_parser.add_argument(
        "--worker-name",
        type=str,
        default=None,
        metavar="NAME",
        help="Worker name for identification (default: auto-generated)",
    )

    evaluator_parser.set_defaults(command="evaluator")


def run_evaluator(args: argparse.Namespace) -> int:
    """Execute the evaluator command.

    Without ``--experiment-config`` or ``--ci``, discovers experiments from
    the Redis registry.  With ``--experiment-config``, focuses on that
    experiment.  With ``--ci``, runs the unified evaluator path against
    legacy CI queues for backward compatibility.

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

    ci_mode = args.ci
    cores = getattr(args, "cpuset", None)
    skip_cpus = getattr(args, "skip_cpuset", None)
    use_cpuset = cores is not None or skip_cpus is not None
    cpu_tag = getattr(args, "cpu_tag", None)
    jobs = getattr(args, "jobs", None)
    cores_per_job = getattr(args, "cores_per_job", None)
    build_jobs = args.build_jobs
    build_cores_per_job = args.build_cores_per_job
    verify_cores_per_job = args.verify_cores_per_job
    verify_jobs = args.verify_jobs
    idle_timeout = args.idle_timeout

    if ci_mode and args.experiment_config:
        logger.error("--ci and --experiment-config are mutually exclusive")
        return 1

    try:
        # --- Configless mode: no --ci and no --experiment-config ---
        if not ci_mode and not args.experiment_config:
            from crsbench.distributed.evaluator import run_evaluator_configless

            worker_name = args.worker_name or "configless-evaluator"
            benchmarks_root = args.benchmarks_root
            redis_host = normalize_redis_host(
                os.environ.get("CRSBENCH_REDIS_HOST", "localhost")
            )
            if redis_host is None:
                logger.error(
                    "Distributed evaluator requires a Redis host; "
                    "set CRSBENCH_REDIS_HOST to a non-empty hostname"
                )
                return 1

            return run_evaluator_configless(
                redis_host=redis_host,
                worker_name=worker_name,
                build_jobs=build_jobs if build_jobs is not None else jobs,
                build_cores_per_job=(
                    build_cores_per_job
                    if build_cores_per_job is not None
                    else cores_per_job
                ),
                verify_jobs=verify_jobs if verify_jobs is not None else jobs,
                verify_cores_per_job=(
                    verify_cores_per_job
                    if verify_cores_per_job is not None
                    else cores_per_job
                ),
                use_cpuset=use_cpuset,
                cores=cores,
                skip_cpus=skip_cpus,
                cpu_tag=cpu_tag,
                idle_timeout=idle_timeout,
                benchmarks_root=benchmarks_root,
            )

        # --- CI mode ---
        if ci_mode:
            from crsbench.distributed.evaluator import run_evaluator_ci_mode

            worker_name = args.worker_name or "ci-evaluator"
            redis_host = normalize_redis_host(
                os.environ.get("CRSBENCH_REDIS_HOST", "localhost")
            )
            if redis_host is None:
                logger.error(
                    "CI evaluator requires a Redis host; "
                    "set CRSBENCH_REDIS_HOST to a non-empty hostname"
                )
                return 1

            return run_evaluator_ci_mode(
                redis_host=redis_host,
                worker_name=worker_name,
                build_jobs=build_jobs if build_jobs is not None else jobs,
                build_cores_per_job=(
                    build_cores_per_job
                    if build_cores_per_job is not None
                    else cores_per_job
                ),
                verify_jobs=verify_jobs if verify_jobs is not None else jobs,
                verify_cores_per_job=(
                    verify_cores_per_job
                    if verify_cores_per_job is not None
                    else cores_per_job
                ),
                use_cpuset=use_cpuset,
                cores=cores,
                skip_cpus=skip_cpus,
                cpu_tag=cpu_tag,
                idle_timeout=idle_timeout,
            )

        # --- Config mode: focus on a specific experiment ---
        from pathlib import Path

        from crsbench.distributed.evaluator import run_evaluator_main
        from crsbench.run_experiment import load_experiment_config

        config_path = Path(args.experiment_config)
        logger.info(f"Loading experiment config from: {config_path}")
        config = load_experiment_config(config_path)

        experiment_name = config.experiment
        logger.info(f"Experiment name: {experiment_name}")

        normalized_config_redis_host = normalize_redis_host(config.redis_host)
        if normalized_config_redis_host is not None:
            redis_host = normalized_config_redis_host
        else:
            env_redis_host = os.environ.get("CRSBENCH_REDIS_HOST")
            if env_redis_host is not None:
                redis_host = normalize_redis_host(env_redis_host)
                if redis_host is None:
                    logger.error(
                        "Distributed evaluator requires a Redis host; "
                        "set redis_host or CRSBENCH_REDIS_HOST"
                    )
                    return 1
            else:
                redis_host = "localhost"
        logger.info(f"Redis host: {redis_host}")

        if args.benchmarks_root:
            config.benchmarks_root = Path(args.benchmarks_root)
            logger.info(f"Evaluator override: benchmarks_root = {args.benchmarks_root}")

        evaluator_cfg = config.evaluator

        def _int_attr(obj, attr_name: str):
            value = getattr(obj, attr_name, None) if obj else None
            return value if isinstance(value, int) else None

        default_jobs = (
            _int_attr(evaluator_cfg, "jobs")
            if _int_attr(evaluator_cfg, "jobs") is not None
            else 1
        )
        default_cores_per_job = _int_attr(evaluator_cfg, "cores_per_job")
        resolved_build_jobs = (
            build_jobs
            if build_jobs is not None
            else (
                jobs
                if jobs is not None
                else (
                    _int_attr(evaluator_cfg, "build_jobs")
                    if _int_attr(evaluator_cfg, "build_jobs") is not None
                    else default_jobs
                )
            )
        )
        resolved_build_cores_per_job = (
            build_cores_per_job
            if build_cores_per_job is not None
            else (
                cores_per_job
                if cores_per_job is not None
                else (
                    _int_attr(evaluator_cfg, "build_cores_per_job")
                    if _int_attr(evaluator_cfg, "build_cores_per_job") is not None
                    else default_cores_per_job
                )
            )
        )
        resolved_verify_cores_per_job = (
            verify_cores_per_job
            if verify_cores_per_job is not None
            else (
                cores_per_job
                if cores_per_job is not None
                else (
                    _int_attr(evaluator_cfg, "verify_cores_per_job")
                    if _int_attr(evaluator_cfg, "verify_cores_per_job") is not None
                    else default_cores_per_job
                )
            )
        )
        resolved_verify_jobs = (
            verify_jobs
            if verify_jobs is not None
            else (
                jobs
                if jobs is not None
                else (
                    _int_attr(evaluator_cfg, "verify_jobs")
                    if _int_attr(evaluator_cfg, "verify_jobs") is not None
                    else (
                        default_jobs
                        if _int_attr(evaluator_cfg, "jobs") is not None
                        else (
                            max(
                                1,
                                (resolved_build_jobs * resolved_build_cores_per_job)
                                // resolved_verify_cores_per_job,
                            )
                            if (
                                resolved_build_cores_per_job is not None
                                and resolved_verify_cores_per_job is not None
                            )
                            else default_jobs
                        )
                    )
                )
            )
        )
        resolved_idle_timeout = (
            idle_timeout
            if idle_timeout is not None
            else (
                _int_attr(evaluator_cfg, "idle_timeout")
                if _int_attr(evaluator_cfg, "idle_timeout") is not None
                else 0
            )
        )
        resolved_cores = cores
        resolved_skip_cpus = skip_cpus
        cli_cpu_tag = normalize_cpu_tag(cpu_tag)
        evaluator_cpu_tag = normalize_cpu_tag(
            evaluator_cfg.cpu_tag if evaluator_cfg else None
        )
        resources_cpu_tag = normalize_cpu_tag(
            config.resources.cpu_tag if config.resources else None
        )
        resolved_cpu_tag = cli_cpu_tag or evaluator_cpu_tag or resources_cpu_tag

        return run_evaluator_main(
            config=config,
            experiment_name=experiment_name,
            redis_host=redis_host,
            use_cpuset=use_cpuset,
            cores=resolved_cores,
            skip_cpus=resolved_skip_cpus,
            cpu_tag=resolved_cpu_tag,
            build_jobs=resolved_build_jobs,
            build_cores_per_job=resolved_build_cores_per_job,
            verify_cores_per_job=resolved_verify_cores_per_job,
            verify_jobs=resolved_verify_jobs,
            idle_timeout=resolved_idle_timeout,
        )
    except KeyboardInterrupt:
        logger.info("Evaluator interrupted by user")
        return 0
    except Exception as e:
        logger.error(f"Evaluator failed: {e}")
        return 1
