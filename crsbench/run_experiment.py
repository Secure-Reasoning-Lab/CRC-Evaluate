#!/usr/bin/env python3
# PYTHON_ARGCOMPLETE_OK
"""Experiment runner for CRSBench evaluation framework.

This script provides a single entry point for running CRS evaluations with
standardized experiment configurations, CRS integration, and benchmark suite
management.

Usage:
    # Download benchmarks and run experiments
    crsbench download --all
    crsbench run --experiment-config config.yaml

    # Verify POVs / patches / coverage
    crsbench verify benchmarks/afc-curl-delta-01 --pov-dir ./povs/
    crsbench patch-verify benchmarks/afc-curl-delta-01 --patch-dir ./patches --pov-dir ./povs
    crsbench coverage --experiment-config ./experiment.yaml

    # Benchmark management (validate, bundle, CI, stats)
    crsbench benchmark validate ./benchmarks/afc-curl-delta-01
    crsbench benchmark ci all --all
"""

import argparse
import base64
import importlib.util
import json
import os
import secrets
import shutil
import string
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import List

import argcomplete
import yaml
from dotenv import load_dotenv
from pydantic import BaseModel

from crsbench.cloud.bootstrap import CloudVmBootstrapInputs
from crsbench.distributed.jobs import (
    _build_trial_output_path,
    _check_existing_trial,
    get_crs_type,
)
from crsbench.evaluation.cleanup import cleanup_trial_directory
from crsbench.evaluation.results import TrialMetadata, TrialResult
from crsbench.evaluation.trial_paths import (
    experiment_dir as resolve_experiment_dir,
)
from crsbench.evaluation.trial_paths import (
    resolve_benchmarks_root,
)
from crsbench.utils import log_progress, log_section, log_summary
from crsbench.utils.benchmark_utils import (
    filter_benchmarks_by_mode,
    get_available_modes_for_benchmark,
)
from crsbench.utils.logger import configure_logger, get_logger
from crsbench.validation.ground_truth_paths import GroundTruthPaths
from crsbench.validation.meta_adapter import MetaYamlAdapter
from crsbench.validation.schemas import (
    BenchmarkHarness,
    ExperimentConfig,
    HarnessFile,
)

# Load environment variables from .env file if present
load_dotenv()

# Get logger instance
logger = get_logger(__name__)


def sanitize_trial_id(raw_id: str) -> str:
    """Sanitize trial_id for Docker Compose project name compatibility.

    Docker Compose project names must be lowercase and can only contain
    letters, numbers, underscores, and hyphens.

    Equivalent to: tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9_-' '_' | sed 's/_*$//'

    Args:
        raw_id: Raw trial identifier string

    Returns:
        Sanitized trial_id safe for Docker Compose
    """
    import re

    # Convert to lowercase
    result = raw_id.lower()
    # Replace any character that's NOT a-z, 0-9, underscore, or hyphen with underscore
    result = re.sub(r"[^a-z0-9_-]", "_", result)
    # Remove trailing underscores
    return result.rstrip("_")


def format_duration(seconds: int) -> str:
    """Format duration in seconds to human-readable string.

    Args:
        seconds: Duration in seconds

    Returns:
        Human-readable duration string (e.g., "4h 0m", "30m", "1d 2h 30m")
    """
    if seconds < 60:
        return f"{seconds}s"

    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"

    hours = minutes // 60
    remaining_minutes = minutes % 60

    if hours < 24:
        if remaining_minutes > 0:
            return f"{hours}h {remaining_minutes}m"
        return f"{hours}h"

    days = hours // 24
    remaining_hours = hours % 24

    if remaining_hours > 0:
        return f"{days}d {remaining_hours}h"
    return f"{days}d"


def display_estimated_runtime(
    total_jobs: int,
    config: "ExperimentConfig",
    *,
    worker_count: int = 1,
) -> None:
    """Display estimated experiment runtime before execution.

    Calculates and logs the estimated runtime based on phase timeouts
    and total number of jobs. For distributed mode with multiple workers,
    shows parallel estimate.

    Args:
        total_jobs: Total number of jobs to execute
        config: Experiment configuration with timeout settings
        worker_count: Number of workers for parallel execution (default: 1 for sequential)
    """
    if total_jobs == 0:
        return

    # Calculate per-job estimate from phase timeouts
    per_job_seconds = config.build_timeout + config.run_timeout + config.verify_timeout
    total_seconds = per_job_seconds * total_jobs

    # Format individual timeouts
    build_fmt = format_duration(config.build_timeout)
    run_fmt = format_duration(config.run_timeout)
    verify_fmt = format_duration(config.verify_timeout)

    # Display estimates
    logger.info(
        f"Estimated max runtime per job: {format_duration(per_job_seconds)} "
        f"(build: {build_fmt} + run: {run_fmt} + verify: {verify_fmt})"
    )

    if worker_count > 1:
        # Parallel execution with multiple workers
        parallel_seconds = total_seconds // worker_count
        logger.info(
            f"Estimated total runtime: {format_duration(parallel_seconds)} "
            f"({total_jobs} jobs / {worker_count} workers)"
        )
    else:
        # Sequential execution
        logger.info(
            f"Estimated total runtime (sequential): {format_duration(total_seconds)}"
        )


# Trial configuration
class Trial(BaseModel):
    """Trial configuration for CRS evaluation.

    Attributes:
        crs: CRS identifier
        benchmark_harness: Resolved benchmark-harness pair
        trial_num: Trial number (1-indexed)
        mode: Evaluation mode ("delta" or "full")
        sanitizer: Sanitizer type ("address", "memory", or "undefined")
    """

    crs: str
    benchmark_harness: BenchmarkHarness
    trial_num: int
    mode: str
    sanitizer: str
    target_cpv_id: str | None = None


def build_trial_id(experiment_name: str, trial: Trial, trial_suffix: str) -> str:
    """Build a deterministic trial_id with CPV isolation and compose-safe charset."""
    bh = trial.benchmark_harness

    if trial.target_cpv_id is None:
        cpv_component = "untargeted"
    else:
        cpv_encoded = (
            base64.b32encode(trial.target_cpv_id.encode("utf-8"))
            .decode("ascii")
            .rstrip("=")
            .lower()
        )
        cpv_component = f"cpv-{cpv_encoded}"

    raw_trial_id = (
        f"{experiment_name}-{trial.crs}-{bh.name}-{bh.harness.name}-"
        f"{trial.mode}-{trial.sanitizer}-{cpv_component}-trial{trial.trial_num}"
        f"{trial_suffix}"
    )
    return sanitize_trial_id(raw_trial_id)


def _add_run_arguments(parser: argparse.ArgumentParser) -> None:
    """Add arguments for the 'run' subcommand.

    All benchmark, CRS, and path configuration belongs in the experiment
    config YAML.  CLI flags are limited to runtime control only.

    Args:
        parser: ArgumentParser to add arguments to
    """
    parser.add_argument(
        "--experiment-config",
        type=str,
        required=True,
        metavar="CONFIG_FILE",
        help="Path to experiment configuration YAML file (e.g., experiment-config.yaml)",
    )

    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable verbose/debug logging",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print trials that would be enqueued without executing them",
    )

    parser.add_argument(
        "--local-only",
        action="store_true",
        help="Force local execution mode without Redis (useful for single jobs or testing)",
    )

    parser.add_argument(
        "--distributed",
        action="store_true",
        help="Force distributed execution mode with Redis (raises error if Redis unavailable)",
    )
    parser.add_argument(
        "--queue-mode",
        choices=["fresh", "continue", "quit"],
        default=None,
        help="Existing-queue behavior for distributed mode (default: prompt in TTY, continue in non-TTY)",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="With --queue-mode continue, requeue failed trials after cleaning trial dirs",
    )


def parse_arguments() -> argparse.Namespace:
    """Parse command line arguments with subcommand support.

    Returns:
        Parsed arguments with experiment configuration.
    """
    parser = argparse.ArgumentParser(
        prog="crsbench",
        description="CRSBench - Cyber Reasoning System Evaluation Framework",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Prepare local environment (managed oss-fuzz + base images)
  %(prog)s prepare

  # Download benchmarks from HuggingFace
  %(prog)s download --all

  # Run CRS experiments
  %(prog)s run --experiment-config config.yaml

  # Start distributed worker / evaluator
  %(prog)s worker --experiment-config config.yaml
  %(prog)s evaluator --experiment-config config.yaml

  # Verify POVs against benchmark variants
  %(prog)s verify benchmarks/afc-curl-delta-01 --pov-dir ./povs/

  # Verify CRS-generated patches
  %(prog)s patch-verify benchmarks/afc-curl-delta-01 --patch-dir ./patches --pov-dir ./povs

  # Collect code coverage timelines
  %(prog)s coverage --experiment-config ./experiment.yaml

  # Generate reports and start dashboard
  %(prog)s report --experiment my-experiment
  %(prog)s dashboard --base-dir ./experiments

  # Benchmark management (validate, bundle, CI, stats, migrate)
  %(prog)s benchmark validate ./benchmarks/afc-curl-delta-01
  %(prog)s benchmark ci all --all
  %(prog)s benchmark stats --summary-only
        """,
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # --- Ordered by experiment lifecycle ---

    # 'download' subcommand - HuggingFace dataset download
    from crsbench.dataset.cli import add_dataset_subparser

    add_dataset_subparser(subparsers)

    # 'prepare' subcommand - local environment bootstrap
    from crsbench.prepare.cli import add_prepare_subparser

    add_prepare_subparser(subparsers)

    # 'gen-config' subcommand - interactive config scaffolding
    from crsbench.genconfig.cli import add_gen_config_subparser

    add_gen_config_subparser(subparsers)

    # 'run' subcommand - experiment execution
    run_parser = subparsers.add_parser(
        "run",
        help="Run CRS evaluation experiments",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --experiment-config config.yaml
  %(prog)s --experiment-config config.yaml --dry-run
  %(prog)s --experiment-config config.yaml --distributed
        """,
    )
    _add_run_arguments(run_parser)
    run_parser.set_defaults(command="run")

    # 'worker' subcommand - distributed worker
    from crsbench.distributed.cli.worker_command import add_worker_subparser

    add_worker_subparser(subparsers)

    # 'evaluator' subcommand - distributed POV verification
    from crsbench.distributed.cli.evaluator_command import add_evaluator_subparser

    add_evaluator_subparser(subparsers)

    # 'queue' subcommand - distributed queue cleanup/ops
    from crsbench.distributed.cli.queue_command import add_queue_subparser

    add_queue_subparser(subparsers)

    # 'cloud' subcommand - cloud experiment lifecycle management
    from crsbench.cloud.cli.cloud_command import add_cloud_subparser

    add_cloud_subparser(subparsers)

    # 'verify' subcommand - POV verification
    from crsbench.evaluation.verification.cli.pov_verify_command import (
        add_verify_subparser,
    )

    add_verify_subparser(subparsers)

    # 'patch-verify' subcommand - patch verification
    from crsbench.evaluation.verification.cli.patch_verify_command import (
        add_patch_verify_subparser,
    )

    add_patch_verify_subparser(subparsers)

    # 'coverage' subcommand - coverage collection
    from crsbench.evaluation.coverage.cli.coverage_command import (
        add_coverage_subparser,
    )

    add_coverage_subparser(subparsers)

    # 'report' subcommand - report generation
    from crsbench.reporting.cli import add_report_subparser

    add_report_subparser(subparsers)

    # 'dashboard' subcommand - web dashboard
    from crsbench.reporting.cli import add_dashboard_subparser

    add_dashboard_subparser(subparsers)

    # 're-eval' subcommand - re-run verification on existing trials
    from crsbench.evaluation.reeval.cli import add_reeval_subparser

    add_reeval_subparser(subparsers)

    # 'benchmark' subcommand - benchmark management including migrate, stats, ci
    from crsbench.benchmark.packaging.cli import add_benchmark_subparser

    add_benchmark_subparser(subparsers)

    argcomplete.autocomplete(parser)
    args = parser.parse_args()

    # Default to 'run' if no command specified (shouldn't happen due to legacy handling)
    if args.command is None:
        parser.print_help()
        sys.exit(0)

    return args


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
    if config_path.suffix not in [".yaml", ".yml"]:
        logger.warning(
            f"Configuration file does not have .yaml/.yml extension: {config_path}"
        )

    logger.info(f"Experiment configuration: {config_path}")


def validate_filestore_permissions(config: ExperimentConfig) -> None:
    """Validate write permissions for filestore directories.

    Attempts to create directories and verifies write access early
    to avoid permission errors during experiment execution.

    Args:
        config: Experiment configuration

    Raises:
        SystemExit: If directory creation fails or write permission is denied
    """
    directories_to_check = [
        ("experiment_filestore", config.experiment_filestore),
        ("report_filestore", config.report_filestore),
    ]

    # Add results filestores if copy is enabled
    if config.copy_results_after_trial:
        if config.results_filestore:
            directories_to_check.append(("results_filestore", config.results_filestore))

    for name, path in directories_to_check:
        try:
            # Try to create the directory
            path.mkdir(parents=True, exist_ok=True)

            # Verify write permission by creating a test file
            test_file = path / ".crsbench_permission_test"
            test_file.touch()
            test_file.unlink()

            logger.debug(f"Verified write access to {name}: {path}")
        except PermissionError as e:
            logger.error(f"Permission denied for {name}: {path}")
            logger.error(f"  Error: {e}")
            logger.error(
                "Please ensure you have write permissions or use a different directory."
            )
            sys.exit(1)
        except OSError as e:
            logger.error(f"Failed to create {name} directory: {path}")
            logger.error(f"  Error: {e}")
            sys.exit(1)

    logger.info("Filestore permissions verified")


def load_experiment_config(config_path: Path) -> ExperimentConfig:
    """Load and validate experiment configuration from YAML file.

    Args:
        config_path: Path to experiment configuration YAML file

    Returns:
        ExperimentConfig: Validated experiment configuration

    Raises:
        SystemExit: If configuration is invalid
    """
    from crsbench.validation import validate_experiment_config

    # Validate the configuration file
    result = validate_experiment_config(config_path)

    if not result.is_valid:
        logger.error("Experiment configuration validation failed:")
        for error in result.errors:
            logger.error(f"  - {error.message}")
        sys.exit(1)

    # Load and parse the YAML file
    with config_path.open("r") as f:
        config_data = yaml.safe_load(f)

    # Create validated config object
    config = ExperimentConfig(**config_data)

    logger.info("Experiment configuration loaded and validated successfully")
    return config


def resolve_benchmark_harnesses(
    benchmark_entries: List,
    benchmarks_root: Path,
) -> List[BenchmarkHarness]:
    """Resolve BenchmarkHarness objects from BenchmarkEntry list.

    For entries with harnesses specified: use those harnesses
    For entries without harnesses: load all harnesses from meta.yaml

    Args:
        benchmark_entries: List of BenchmarkEntry objects
        benchmarks_root: Root directory containing benchmarks

    Returns:
        List of BenchmarkHarness objects with resolved paths and harnesses

    Raises:
        FileNotFoundError: If benchmark directory or meta.yaml not found
        ValueError: If meta.yaml is invalid or has no harnesses
    """
    from crsbench.validation import validate_benchmark
    from crsbench.validation.schemas import BenchmarkHarness, HarnessFile

    harness_pairs = []

    for entry in benchmark_entries:
        if entry.harnesses:
            # Validate specified harnesses exist in meta.yaml
            benchmark_path = benchmarks_root / entry.name
            if not benchmark_path.exists():
                raise FileNotFoundError(
                    f"Benchmark directory not found: {benchmark_path}"
                )

            meta_yaml_path = GroundTruthPaths(benchmark_path).meta_yaml
            if not meta_yaml_path.exists():
                raise FileNotFoundError(f"meta.yaml not found: {meta_yaml_path}")

            with meta_yaml_path.open() as f:
                meta_data = yaml.safe_load(f)

            # Parse harness files into dict for lookup
            harness_files_dict = {
                h.get("name"): h for h in meta_data.get("harness_files", [])
            }

            # Validate each specified harness exists and create BenchmarkHarness
            harness_cpvs = entry.harness_cpvs or {}
            for harness_name in entry.harnesses:
                if harness_name not in harness_files_dict:
                    raise ValueError(
                        f"Harness '{harness_name}' not found in benchmark '{entry.name}'. "
                        f"Available harnesses: {sorted(harness_files_dict.keys())}"
                    )
                harness_data = harness_files_dict[harness_name]
                harness_obj = HarnessFile(
                    name=harness_name, path=harness_data.get("path", "")
                )
                harness_pairs.append(
                    BenchmarkHarness(
                        name=entry.name,
                        path=benchmark_path,
                        harness=harness_obj,
                        target_cpvs=harness_cpvs.get(harness_name),
                    )
                )
        else:
            # Load all harnesses from meta.yaml
            benchmark_path = benchmarks_root / entry.name
            if not benchmark_path.exists():
                raise FileNotFoundError(
                    f"Benchmark directory not found: {benchmark_path}"
                )

            # Validate and load benchmark config
            validation_result = validate_benchmark(benchmark_path)
            if not validation_result.is_valid:
                raise ValueError(
                    f"Invalid benchmark '{entry.name}': "
                    + ", ".join(e.message for e in validation_result.errors)
                )

            # Load meta.yaml to get harnesses
            meta_yaml_path = GroundTruthPaths(benchmark_path).meta_yaml
            if not meta_yaml_path.exists():
                raise FileNotFoundError(f"meta.yaml not found: {meta_yaml_path}")

            with meta_yaml_path.open() as f:
                meta_data = yaml.safe_load(f)

            harness_files = meta_data.get("harness_files", [])
            if not harness_files:
                raise ValueError(
                    f"No harness_files found in meta.yaml for benchmark '{entry.name}'"
                )

            # Create BenchmarkHarness for each harness
            for harness_data in harness_files:
                harness_name = harness_data.get("name")
                harness_path = harness_data.get("path", "")
                if harness_name:
                    harness_obj = HarnessFile(name=harness_name, path=harness_path)
                    harness_pairs.append(
                        BenchmarkHarness(
                            name=entry.name,
                            path=benchmark_path,
                            harness=harness_obj,
                        )
                    )

    logger.info(
        f"Resolved {len(harness_pairs)} BenchmarkHarness pairs from {len(benchmark_entries)} benchmarks"
    )
    return harness_pairs


def _harness_has_cpv_with_sanitizer(harness: HarnessFile, sanitizer: str) -> bool:
    """Check if harness has any CPV requiring the specified sanitizer.

    Args:
        harness: HarnessFile object to check
        sanitizer: Sanitizer type to match (e.g., 'address', 'undefined')

    Returns:
        True if any POV in the harness requires this sanitizer, False otherwise
    """
    if not harness.vulns:
        return False
    return any(
        pov.sanitizer == sanitizer for vuln in harness.vulns for pov in vuln.povs
    )


def _filter_matched_cpvs(
    harness: HarnessFile, sanitizer: str, allowed_cpvs: set[str] | None = None
) -> list[str]:
    """Collect unique CPV IDs matching sanitizer and optional CPV allow-list."""
    matched_cpvs: list[str] = []
    seen_cpvs: set[str] = set()

    for vuln in harness.vulns or []:
        if not any(pov.sanitizer == sanitizer for pov in vuln.povs):
            continue
        cpv_id = vuln.vuln_keyword
        if allowed_cpvs is not None and cpv_id not in allowed_cpvs:
            continue
        if cpv_id in seen_cpvs:
            continue
        seen_cpvs.add(cpv_id)
        matched_cpvs.append(cpv_id)

    return matched_cpvs


def generate_trial_matrix(
    benchmark_harnesses: List["BenchmarkHarness"],
    oss_crs_registry: List[str],
    config,
    registry_dir: Path,
) -> List[Trial]:
    """Generate all trial combinations from BenchmarkHarness objects, CRSes, and trials.

    Args:
        benchmark_harnesses: List of BenchmarkHarness objects
        oss_crs_registry: List of CRS registry identifiers
        config: Experiment configuration with trials count and mode
        registry_dir: Path to CRS registry directory

    Returns:
        List of Trial namedtuples with mode information
    """
    trials = []
    config_mode = config.mode.value  # Get string value from enum

    for crs in oss_crs_registry:
        # CRS entries are resolved directly from registry.
        crs_type = get_crs_type(crs, registry_dir)
        is_bug_fixing = crs_type == "bug-fixing"

        for benchmark_harness in benchmark_harnesses:
            # Skip harnesses without CPVs:
            # - Bug-fixing CRS: always skip (required for POV-based operation)
            # - Bug-finding CRS: skip only when only_cpv_harnesses is enabled
            should_check_cpvs = is_bug_fixing or config.only_cpv_harnesses
            harness = None
            if should_check_cpvs:
                meta_path = GroundTruthPaths(Path(benchmark_harness.path)).meta_yaml
                adapter = MetaYamlAdapter.from_meta_yaml(
                    meta_path,
                    benchmark_name=benchmark_harness.name,
                    lang="",
                    main_repo="",
                )
                harness = adapter.get_harness(benchmark_harness.harness.name)
                if not harness or not harness.vulns:
                    skip_reason = (
                        "bug-fixing CRS" if is_bug_fixing else "only_cpv_harnesses=True"
                    )
                    logger.debug(
                        f"Skipping {benchmark_harness.name}/{benchmark_harness.harness.name}: "
                        f"no CPVs ({skip_reason})"
                    )
                    continue
            target_cpvs = benchmark_harness.target_cpvs
            target_cpv_set = set(target_cpvs) if target_cpvs else None
            # Determine which modes to run for this benchmark
            if config_mode == "all":
                available_modes = get_available_modes_for_benchmark(
                    benchmark_harness.path
                )
                if not available_modes:
                    logger.warning(
                        f"No modes available for {benchmark_harness.name}, skipping"
                    )
                    continue
                modes_to_run = available_modes
            elif config_mode == "auto":
                # Auto mode: run single mode, prefer delta
                available_modes = get_available_modes_for_benchmark(
                    benchmark_harness.path
                )
                if not available_modes:
                    logger.warning(
                        f"No modes available for {benchmark_harness.name}, skipping"
                    )
                    continue
                # Prefer delta, fallback to full
                modes_to_run = ["delta"] if "delta" in available_modes else ["full"]
            else:
                # Single mode: delta or full
                modes_to_run = [config_mode]

            # Generate trials for each mode and sanitizer
            for mode in modes_to_run:
                for sanitizer in config.sanitizers:
                    # Skip sanitizers that don't match any CPV in this harness
                    if should_check_cpvs and harness:
                        if not _filter_matched_cpvs(
                            harness, sanitizer.value, target_cpv_set
                        ):
                            logger.debug(
                                f"Skipping {benchmark_harness.name}/{benchmark_harness.harness.name}: "
                                f"no CPVs with sanitizer {sanitizer.value}"
                            )
                            continue
                    if is_bug_fixing and harness:
                        matched_cpvs = _filter_matched_cpvs(
                            harness, sanitizer.value, target_cpv_set
                        )

                        if not matched_cpvs:
                            logger.debug(
                                f"Skipping {benchmark_harness.name}/{benchmark_harness.harness.name}: "
                                f"no CPVs with sanitizer {sanitizer.value}"
                            )
                            continue

                        for cpv_id in matched_cpvs:
                            for trial_num in range(1, config.trials + 1):
                                trials.append(
                                    Trial(
                                        crs=crs,
                                        benchmark_harness=benchmark_harness,
                                        trial_num=trial_num,
                                        mode=mode,
                                        sanitizer=sanitizer.value,
                                        target_cpv_id=cpv_id,
                                    )
                                )
                    else:
                        for trial_num in range(1, config.trials + 1):
                            trials.append(
                                Trial(
                                    crs=crs,
                                    benchmark_harness=benchmark_harness,
                                    trial_num=trial_num,
                                    mode=mode,
                                    sanitizer=sanitizer.value,
                                )
                            )

    logger.info(
        f"Generated {len(trials)} trials: {len(oss_crs_registry)} CRSes × "
        f"{len(benchmark_harnesses)} benchmark-harness pairs × "
        f"{len(config.sanitizers)} sanitizers × "
        f"{config.trials} trials × mode={config_mode}"
    )
    return trials


def dump_trial_matrix(
    trials: List[Trial],
    config,
) -> None:
    """Dump trial matrix to JSON file in experiment filestore.

    Args:
        trials: List of Trial objects
        config: Experiment configuration
    """
    output_dir = resolve_experiment_dir(
        config.experiment_filestore.resolve(), config.experiment
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "trial_matrix.json"

    # Convert trials to serializable format
    trial_data = []
    for trial in trials:
        bh = trial.benchmark_harness
        trial_data.append(
            {
                "crs": trial.crs,
                "benchmark": bh.name,
                "benchmark_path": str(bh.path),
                "harness": bh.harness.name,
                "harness_path": bh.harness.path,
                "trial_num": trial.trial_num,
                "mode": trial.mode,
                "sanitizer": trial.sanitizer,
                "target_cpv_id": trial.target_cpv_id,
            }
        )

    matrix = {
        "experiment": config.experiment,
        "total_trials": len(trials),
        "trials": trial_data,
    }

    with output_path.open("w") as f:
        json.dump(matrix, f, indent=2)

    logger.info(f"Trial matrix saved to {output_path}")


def _display_trial_matrix_rich(trials: List[Trial], start_index: int) -> None:
    """Display trial matrix using Rich Table."""
    from rich.console import Console
    from rich.table import Table

    console = Console()
    table = Table(title="Trials to be enqueued")

    # Add columns with styles
    table.add_column("Order", justify="right", style="cyan")
    table.add_column("CRS", style="green")
    table.add_column("Benchmark", style="yellow")
    table.add_column("Harness", style="yellow")
    table.add_column("CPV", style="magenta")
    table.add_column("Mode", style="blue")
    table.add_column("Sanitizer", style="magenta")
    table.add_column("Trial", justify="right")

    # Add rows
    for i, trial in enumerate(trials):
        order = start_index + i + 1
        bh = trial.benchmark_harness
        table.add_row(
            str(order),
            trial.crs,
            bh.name,
            bh.harness.name,
            trial.target_cpv_id or "-",
            trial.mode,
            trial.sanitizer,
            str(trial.trial_num),
        )

    console.print(table)
    console.print(f"\nTotal trials: {len(trials)}")


def _display_trial_matrix_basic(trials: List[Trial], start_index: int) -> None:
    """Display trial matrix using basic text output."""
    header = (
        f"{'Order':<6} {'CRS':<35} {'Benchmark':<25} {'Harness':<20} {'CPV':<10} "
        f"{'Mode':<8} {'Sanitizer':<10} {'Trial':<5}"
    )
    separator = "-" * len(header)

    sys.stdout.write("\nTrials to be enqueued:\n")
    sys.stdout.write(separator + "\n")
    sys.stdout.write(header + "\n")
    sys.stdout.write(separator + "\n")

    for i, trial in enumerate(trials):
        order = start_index + i + 1
        bh = trial.benchmark_harness
        sys.stdout.write(
            f"{order:<6} {trial.crs:<35} {bh.name:<25} {bh.harness.name:<20} "
            f"{(trial.target_cpv_id or '-'): <10}"
            f"{trial.mode:<8} {trial.sanitizer:<10} {trial.trial_num:<5}\n"
        )

    sys.stdout.write(separator + "\n")
    sys.stdout.write(f"Total trials: {len(trials)}\n\n")


def display_trial_matrix(trials: List[Trial], start_index: int = 0) -> None:
    """Display trial matrix in table format for dry-run mode.

    Uses Rich Table for better formatting when available, falls back to basic text.

    Args:
        trials: List of Trial objects to display
        start_index: Starting index for trial order numbering (0-indexed)
    """
    if not trials:
        sys.stdout.write("No trials to display.\n")
        return

    # Check if Rich is available
    rich_available = importlib.util.find_spec("rich") is not None

    if rich_available:
        _display_trial_matrix_rich(trials, start_index)
    else:
        _display_trial_matrix_basic(trials, start_index)


def _is_all_bug_fixing_crs(
    trials: List[Trial],
    registry_dir: Path,
) -> bool:
    """Check if all trials use bug-fixing CRS only.

    Bug-fixing CRS does not need upfront variant builds because:
    - CRS execution only uses POVs, not variants
    - Patch verification builds PATCHED variants on-demand

    Args:
        trials: List of trials to check
        registry_dir: Path to CRS registry directory

    Returns:
        True if all trials are bug-fixing CRS
    """
    from crsbench.distributed.jobs import get_crs_type

    if not trials:
        return False

    # Get unique CRS names from trials
    unique_crses = {trial.crs for trial in trials}

    for crs in unique_crses:
        try:
            crs_type = get_crs_type(crs, registry_dir)
            if crs_type != "bug-fixing":
                return False
        except (FileNotFoundError, ValueError) as e:
            logger.warning(f"Could not determine CRS type for '{crs}': {e}")
            return False  # Be conservative - build variants if unsure

    return True


def should_use_distributed_mode(
    args: argparse.Namespace, config, total_jobs: int
) -> bool:
    """Determine if distributed mode should be used.

    Criteria for local mode:
    - Only 1 total trial (1 CRS × 1 benchmark × 1 trial)
    - redis_host not specified in config
    - Redis not available (connection check)
    - User explicitly requests local mode via --local-only flag

    Criteria for forced distributed mode:
    - User explicitly requests via --distributed flag
    - Raises error if Redis is not configured or unavailable

    Args:
        args: Parsed command line arguments
        config: Experiment configuration
        total_jobs: Total number of jobs to execute

    Returns:
        bool: True if should use distributed mode, False for local mode

    Raises:
        RuntimeError: If --distributed is set but Redis is unavailable
    """
    from crsbench.distributed.common import normalize_redis_host
    from crsbench.distributed.queue import check_redis_available

    redis_host = normalize_redis_host(getattr(config, "redis_host", None))
    cloud_config = getattr(config, "cloud", None)
    cloud_gce = getattr(cloud_config, "gce", None)
    cloud_workers = getattr(cloud_config, "workers", None)

    # Check for conflicting flags
    distributed = getattr(args, "distributed", False)
    if args.local_only and distributed:
        raise RuntimeError("Cannot specify both --local-only and --distributed flags")

    # User explicitly forced distributed mode
    if distributed:
        logger.info("Distributed mode explicitly requested via --distributed flag")

        # Validate Redis host is configured
        if not redis_host:
            raise RuntimeError(
                "Cannot use distributed mode: No Redis host configured. "
                "Please set 'redis_host' in experiment config or remove --distributed flag."
            )

        # Validate Redis is available
        if not check_redis_available(redis_host):
            raise RuntimeError(
                f"Cannot use distributed mode: Redis not available at {redis_host}. "
                "Please ensure Redis server is running or remove --distributed flag."
            )

        logger.info(f"Forcing distributed mode with Redis at {redis_host}")
        return True

    # User explicitly disabled distributed mode
    if args.local_only:
        logger.info("Local mode explicitly requested via --local-only flag")
        return False

    if cloud_gce is not None or cloud_workers is not None:
        if not redis_host:
            raise RuntimeError(
                "cloud worker provisioning requires Redis for distributed execution. "
                "Set redis_host for local orchestration or ensure the remote "
                "orchestrator bootstrap patches the experiment config."
            )
        if not check_redis_available(redis_host):
            raise RuntimeError(
                "cloud worker provisioning requires distributed execution, but Redis is not "
                f"available at {redis_host}."
            )
        logger.info("Cloud worker config detected, using distributed mode")
        return True

    # Only 1 job - use local mode by default
    if total_jobs == 1:
        logger.info(f"Single job detected ({total_jobs} jobs total), using local mode")
        return False

    # No Redis host configured
    if not redis_host:
        logger.info("No Redis host configured, using local mode")
        return False

    # Check if Redis is available
    if not check_redis_available(redis_host):
        logger.warning(
            f"Redis not available at {redis_host}, falling back to local mode"
        )
        return False

    # Multiple jobs and Redis available - use distributed
    logger.info(
        f"Multiple jobs detected ({total_jobs} jobs total), using distributed mode"
    )
    return True


def run_experiment_local(
    experiment_name: str,
    config,
    trials: List[Trial],
) -> None:
    """Run experiment locally without Redis queue.

    Executes all trials sequentially in the current process.

    Args:
        experiment_name: Experiment identifier
        config: Experiment configuration
        trials: List of Trial objects to execute
    """
    log_section("Running CRSBench in Local Mode (No Redis)", width=60)

    # Dump trial matrix to JSON
    dump_trial_matrix(trials, config)

    logger.info(f"Total trials to execute: {len(trials)}")
    logger.info("=" * 60)

    log_section("Executing Trials", width=60)

    # Generate 6-char random alphanumeric suffix (shared by all trials)
    trial_suffix = "_" + "".join(
        secrets.choice(string.ascii_lowercase + string.digits) for _ in range(6)
    )

    # Execute trials sequentially
    results = []
    for idx, trial in enumerate(trials, 1):
        bh = trial.benchmark_harness
        logger.info(f"[{idx}/{len(trials)}] Starting trial:")
        logger.info(f"  CRS: {trial.crs}")
        logger.info(f"  Benchmark: {bh.name}")
        logger.info(f"  Harness: {bh.harness.name}")
        logger.info(f"  Trial: {trial.trial_num}")

        # Import and execute job directly
        from crsbench.distributed.jobs import run_crs_trial

        trial_id = build_trial_id(experiment_name, trial, trial_suffix)

        config_payload = config.model_dump()

        result = run_crs_trial(
            crs=trial.crs,
            benchmark=bh.name,
            harness_name=bh.harness.name,
            harness_path=bh.harness.path,
            trial_num=trial.trial_num,
            trial_id=trial_id,
            config_dict=config_payload,
            mode=trial.mode,
            sanitizer=trial.sanitizer,
            target_cpv_id=trial.target_cpv_id,
        )

        results.append(result)

        # Log result and raise exception on failure
        if result.success:
            if result.crs_type == "bug-fixing":
                logger.info(
                    f"  ✓ Success: {result.patches_generated} patches produced, "
                    f"{result.patches_verified} verified, {result.patches_valid} valid"
                )
            else:
                logger.info(
                    f"  ✓ Success: {result.povs_found}/{result.total_povs} POVs found"
                )
        else:
            error_msg = result.error or "Unknown error"
            logger.error(f"  ✗ Failed: {error_msg}")
            raise RuntimeError(f"Trial failed: {error_msg}")

    # Generate final report
    log_section("Experiment Complete - Generating Report", width=60)

    generate_final_report(results, experiment_name, config)

    # Post-experiment operations
    # Cleanup bulky artifacts
    if config.keep_only_results:
        _cleanup_experiment_artifacts(experiment_name, config)


def _write_orchestrator_marker(
    trial_result: TrialResult, config: ExperimentConfig
) -> None:
    """Write completion marker and metadata on the orchestrator's disk.

    Creates .success/.fail marker and metadata.json in the orchestrator's
    experiment_filestore so completed trials are skipped on re-launch.
    Skips writing metadata.json if it already exists (worker on same machine
    already wrote full metadata).

    Args:
        trial_result: Result from a completed trial
        config: Experiment configuration
    """
    if not trial_result.mode or not trial_result.sanitizer:
        logger.debug(
            f"Skipping orchestrator marker for trial {trial_result.trial_num}: "
            "missing mode or sanitizer"
        )
        return

    trial_dir = _build_trial_output_path(
        filestore=config.experiment_filestore.resolve(),
        experiment_name=config.experiment,
        crs=trial_result.crs,
        benchmark=trial_result.benchmark,
        harness=trial_result.harness,
        mode=trial_result.mode,
        sanitizer=trial_result.sanitizer,
        trial_num=trial_result.trial_num,
        target_cpv_id=trial_result.target_cpv_id,
    )
    trial_dir.mkdir(parents=True, exist_ok=True)

    # Write marker
    marker_name = ".success" if trial_result.success else ".fail"
    marker_path = trial_dir / marker_name
    if not marker_path.exists():
        marker_path.touch()

    # Write metadata.json only if it doesn't already exist
    # (local worker already wrote full metadata)
    metadata_path = trial_dir / "metadata.json"
    if not metadata_path.exists():
        metadata_dict = {
            "crs": trial_result.crs,
            "benchmark": trial_result.benchmark,
            "harness": trial_result.harness,
            "trial_num": trial_result.trial_num,
            "mode": trial_result.mode,
            "sanitizer": trial_result.sanitizer,
            "target_cpv_id": trial_result.target_cpv_id,
            "success": trial_result.success,
            "execution_time": trial_result.execution_time,
            "timestamp_start": trial_result.metadata.timestamp_start,
            "timestamp_end": trial_result.metadata.timestamp_end,
            "build_time": trial_result.metadata.build_time,
            "run_time": trial_result.metadata.run_time,
            "worker_machine": trial_result.metadata.worker_machine,
            "worker_trial_dir": trial_result.metadata.worker_trial_dir,
            "povs_found": trial_result.povs_found,
            "total_povs": trial_result.total_povs,
            "patches_generated": trial_result.patches_generated,
            "patches_verified": trial_result.patches_verified,
            "patches_valid": trial_result.patches_valid,
            "error": trial_result.error,
            "error_type": trial_result.error_type,
        }
        with metadata_path.open("w") as f:
            json.dump(metadata_dict, f, indent=2)

    logger.info(
        f"Orchestrator marker written: {trial_dir / marker_name} "
        f"(worker={trial_result.metadata.worker_machine})"
    )


def monitor_jobs(
    queue,
    job_list: List,
    experiment_name: str,
    config: ExperimentConfig,
    *,
    disk_skipped: int = 0,
    registry=None,
) -> List[TrialResult]:
    """Monitor job progress and display status.

    Args:
        queue: RQ queue instance
        job_list: List of enqueued RQ jobs
        experiment_name: Experiment identifier
        config: Experiment configuration (for writing orchestrator markers)
        disk_skipped: Number of trials skipped due to existing .success markers
        registry: Optional RegistryClient for lock renewal

    Returns:
        List of TrialResult objects
    """
    import importlib.util

    rich_available = importlib.util.find_spec("rich") is not None
    if not rich_available:
        logger.warning("Rich library not available, using basic progress display")

    if rich_available:
        return _monitor_jobs_rich(
            queue,
            job_list,
            experiment_name,
            config,
            disk_skipped=disk_skipped,
            registry=registry,
        )
    return _monitor_jobs_basic(
        queue,
        job_list,
        experiment_name,
        config,
        disk_skipped=disk_skipped,
        registry=registry,
    )


def _get_experiment_queue_stats(queue, experiment_name: str) -> dict:
    """Return experiment-scoped queue counts plus global worker visibility.

    In flat-queue mode, queue registries may contain jobs from unrelated
    experiments. The progress monitor should therefore count queued/started/
    finished/failed jobs only for the target experiment while still reporting
    the total number of connected workers for operator visibility.
    """
    from crsbench.distributed.queue import get_existing_trials, get_queue_stats

    global_stats = get_queue_stats(queue)
    existing = get_existing_trials(queue, experiment_name=experiment_name)
    return {
        "queued": len(existing["queued"]),
        "started": len(existing["started"]),
        "finished": len(existing["finished"]),
        "failed": len(existing["failed"]),
        "workers": global_stats.get("workers", 0),
    }


def _monitor_jobs_basic(
    queue,
    job_list: List,
    experiment_name: str,
    config: ExperimentConfig,
    *,
    disk_skipped: int = 0,
    registry=None,
) -> List[TrialResult]:
    """Basic job monitoring without Rich UI."""
    last_renew = time.monotonic()
    logger.info(f"\nMonitoring {len(job_list)} jobs for experiment: {experiment_name}")

    # Track jobs that have already had markers written
    marked_jobs: set[str] = set()

    while True:
        stats = _get_experiment_queue_stats(queue, experiment_name)

        # Display stats
        log_section(f"Experiment: {experiment_name}", width=60)
        logger.info(f"Workers connected: {stats.get('workers', 0)}")
        status_dict = {
            "queued": stats["queued"],
            "started": stats["started"],
            "finished": stats["finished"],
            "failed": stats["failed"],
        }
        if disk_skipped > 0:
            status_dict["skipped (disk)"] = disk_skipped
        status_dict["total"] = len(job_list) + disk_skipped
        log_summary(
            "Queue Status",
            status_dict,
            show_percentage=False,
            level="debug",
        )

        # Display currently running jobs with metadata
        running_jobs = []
        for job in job_list:
            job.refresh()
            if job.get_status() == "started":
                worker_name = job.meta.get("worker_name", "?")
                crs = job.meta.get("crs", "?")
                benchmark = job.meta.get("benchmark", "?")
                harness = job.meta.get("harness", "?")
                target_cpv_id = job.meta.get("target_cpv_id", "-")
                mode = job.meta.get("mode", "?")
                trial_num = job.meta.get("trial_num", "?")
                phase = job.meta.get("phase", "queued")
                phase_started_at = job.meta.get("phase_started_at")
                elapsed = ""
                if phase_started_at:
                    elapsed_sec = int(time.time() - phase_started_at)
                    mins, secs = divmod(elapsed_sec, 60)
                    elapsed = f"{mins}m{secs}s"
                running_jobs.append(
                    f"  [{worker_name}] [{crs}] {benchmark}/{harness} cpv={target_cpv_id} mode={mode} trial={trial_num} phase={phase} ({elapsed})"
                )

        if running_jobs:
            logger.info("Currently Running:")
            for line in running_jobs:
                logger.info(line)

        # Check if all jobs completed and write markers incrementally
        completed = 0
        failed = 0
        for job in job_list:
            job.refresh()
            if job.is_finished:
                completed += 1
                # Write orchestrator marker as soon as job finishes
                if job.id not in marked_jobs:
                    try:
                        result = job.result
                        if result is None:
                            logger.warning(
                                f"Job {job.id[:8]} finished but result is None"
                            )
                            _write_orchestrator_marker(
                                _build_missing_job_result(job), config
                            )
                        else:
                            _write_orchestrator_marker(result, config)
                        marked_jobs.add(job.id)
                    except Exception as e:
                        logger.warning(
                            f"Failed to write orchestrator marker for {job.id[:8]}: {e}"
                        )
                        marked_jobs.add(job.id)
            elif job.is_failed:
                failed += 1
                # Write .fail marker for RQ-level failures
                if job.id not in marked_jobs:
                    kwargs = job.kwargs or {}
                    meta = job.meta or {}
                    fail_result = TrialResult(
                        crs=kwargs.get("crs", "unknown"),
                        benchmark=kwargs.get("benchmark", "unknown"),
                        harness=kwargs.get("harness_name", "unknown"),
                        trial_num=kwargs.get("trial_num", 0),
                        crs_type="bug-finding",
                        mode=kwargs.get("mode"),
                        sanitizer=kwargs.get("sanitizer"),
                        success=False,
                        execution_time=0.0,
                        error=f"Job failed: {job.exc_info}",
                        error_type="RQJobFailure",
                        report={},
                        metadata=TrialMetadata(
                            timestamp_start=0.0,
                            timestamp_end=0.0,
                            worker_machine=meta.get("worker_name"),
                        ),
                    )
                    try:
                        _write_orchestrator_marker(fail_result, config)
                    except Exception as e:
                        logger.warning(f"Failed to write orchestrator fail marker: {e}")
                    marked_jobs.add(job.id)

        log_progress(
            completed + failed,
            len(job_list),
            f"Jobs complete ({completed} success, {failed} failed)",
        )

        if completed + failed >= len(job_list):
            break

        if registry and time.monotonic() - last_renew >= 60:
            if not registry.renew(experiment_name):
                logger.warning("Experiment lock lost — another run may have taken over")
            last_renew = time.monotonic()

        time.sleep(3)

    # Collect results
    results: List[TrialResult] = []
    for job in job_list:
        job.refresh()
        if job.result is not None:
            results.append(job.result)
        elif job.is_finished:
            results.append(_build_missing_job_result(job))
        elif job.is_failed:
            # Create TrialResult for failed jobs using job kwargs
            kwargs = job.kwargs or {}
            meta = job.meta or {}
            results.append(
                TrialResult(
                    crs=kwargs.get("crs", "unknown"),
                    benchmark=kwargs.get("benchmark", "unknown"),
                    harness=kwargs.get("harness_name", "unknown"),
                    trial_num=kwargs.get("trial_num", 0),
                    crs_type="bug-finding",
                    mode=kwargs.get("mode"),
                    sanitizer=kwargs.get("sanitizer"),
                    success=False,
                    execution_time=0.0,
                    error=f"Job failed: {job.exc_info}",
                    error_type="RQJobFailure",
                    report={},
                    metadata=TrialMetadata(
                        timestamp_start=0.0,
                        timestamp_end=0.0,
                        worker_machine=meta.get("worker_name"),
                    ),
                )
            )

    return results


def _monitor_jobs_rich(
    queue,
    job_list: List,
    experiment_name: str,
    config: ExperimentConfig,
    *,
    disk_skipped: int = 0,
    registry=None,
) -> List[TrialResult]:
    """Monitor jobs with Rich UI."""
    from rich.console import Console, Group
    from rich.live import Live
    from rich.table import Table

    console = Console()

    # Track jobs that have already had markers written
    marked_jobs: set[str] = set()

    def generate_status_table():
        stats = _get_experiment_queue_stats(queue, experiment_name)

        # Debug: log queue info
        logger.debug(f"Experiment queue stats for {experiment_name}: {stats}")

        # Queue status table
        table = Table(title=f"Experiment: {experiment_name}")
        table.add_column("Status", style="cyan")
        table.add_column("Count", justify="right", style="magenta")

        table.add_row("Queued", str(stats["queued"]))
        table.add_row("Started", str(stats["started"]))
        table.add_row("Finished", str(stats["finished"]), style="green")
        table.add_row("Failed", str(stats["failed"]), style="red")
        if disk_skipped > 0:
            table.add_row("Skipped (disk)", str(disk_skipped), style="dim")
        table.add_row("Total", str(len(job_list) + disk_skipped))

        # Running jobs table
        running_table = Table(title="Running Jobs")
        running_table.add_column("Worker", style="green")
        running_table.add_column("CRS", style="cyan")
        running_table.add_column("Benchmark", style="yellow")
        running_table.add_column("Harness", style="yellow")
        running_table.add_column("CPV", style="magenta")
        running_table.add_column("Mode", style="blue")
        running_table.add_column("Trial", justify="right")
        running_table.add_column("Phase", style="magenta")
        running_table.add_column("Elapsed", justify="right", style="magenta")

        for job in job_list:
            job.refresh()
            status = job.get_status()
            logger.debug(
                f"Job {job.id[:8]}: status={status}, is_queued={job.is_queued}, is_started={job.is_started}, is_finished={job.is_finished}"
            )
            if status == "started":
                worker_name = job.meta.get("worker_name", "?")
                crs = job.meta.get("crs", "?")
                benchmark = job.meta.get("benchmark", "?")
                harness = job.meta.get("harness", "?")
                target_cpv_id = job.meta.get("target_cpv_id", "-")
                mode = job.meta.get("mode", "?")
                trial_num = str(job.meta.get("trial_num", "?"))
                phase = job.meta.get("phase", "queued")
                phase_started_at = job.meta.get("phase_started_at")
                elapsed = "N/A"
                if phase_started_at:
                    elapsed_sec = int(time.time() - phase_started_at)
                    mins, secs = divmod(elapsed_sec, 60)
                    elapsed = f"{mins}m {secs}s"
                running_table.add_row(
                    worker_name,
                    crs,
                    benchmark,
                    harness,
                    target_cpv_id,
                    mode,
                    trial_num,
                    phase,
                    elapsed,
                )

        return Group(table, running_table)

    last_renew = time.monotonic()
    with Live(generate_status_table(), refresh_per_second=1, console=console) as live:
        while True:
            # Check if all jobs completed and write markers incrementally
            completed = 0
            failed = 0
            for job in job_list:
                job.refresh()
                if job.is_finished:
                    completed += 1
                    if job.id not in marked_jobs:
                        try:
                            result = job.result
                            if result is None:
                                logger.warning(
                                    f"Job {job.id[:8]} finished but result is None"
                                )
                                _write_orchestrator_marker(
                                    _build_missing_job_result(job), config
                                )
                            else:
                                _write_orchestrator_marker(result, config)
                            marked_jobs.add(job.id)
                        except Exception as e:
                            logger.warning(
                                f"Failed to write orchestrator marker for "
                                f"{job.id[:8]}: {e}"
                            )
                            marked_jobs.add(job.id)
                elif job.is_failed:
                    failed += 1
                    if job.id not in marked_jobs:
                        kwargs = job.kwargs or {}
                        meta = job.meta or {}
                        fail_result = TrialResult(
                            crs=kwargs.get("crs", "unknown"),
                            benchmark=kwargs.get("benchmark", "unknown"),
                            harness=kwargs.get("harness_name", "unknown"),
                            trial_num=kwargs.get("trial_num", 0),
                            crs_type="bug-finding",
                            mode=kwargs.get("mode"),
                            sanitizer=kwargs.get("sanitizer"),
                            target_cpv_id=kwargs.get("target_cpv_id"),
                            success=False,
                            execution_time=0.0,
                            error=f"Job failed: {job.exc_info}",
                            error_type="RQJobFailure",
                            report={},
                            metadata=TrialMetadata(
                                timestamp_start=0.0,
                                timestamp_end=0.0,
                                worker_machine=meta.get("worker_name"),
                            ),
                        )
                        try:
                            _write_orchestrator_marker(fail_result, config)
                        except Exception as e:
                            logger.warning(
                                f"Failed to write orchestrator fail marker: {e}"
                            )
                        marked_jobs.add(job.id)

            if completed + failed >= len(job_list):
                break

            if registry and time.monotonic() - last_renew >= 60:
                if not registry.renew(experiment_name):
                    logger.warning(
                        "Experiment lock lost — another run may have taken over"
                    )
                last_renew = time.monotonic()

            live.update(generate_status_table())
            time.sleep(1)

    # Collect results
    results: List[TrialResult] = []
    for job in job_list:
        job.refresh()
        if job.result is not None:
            results.append(job.result)
        elif job.is_finished:
            results.append(_build_missing_job_result(job))
        elif job.is_failed:
            # Create TrialResult for failed jobs using job kwargs
            kwargs = job.kwargs or {}
            meta = job.meta or {}
            results.append(
                TrialResult(
                    crs=kwargs.get("crs", "unknown"),
                    benchmark=kwargs.get("benchmark", "unknown"),
                    harness=kwargs.get("harness_name", "unknown"),
                    trial_num=kwargs.get("trial_num", 0),
                    crs_type="bug-finding",
                    mode=kwargs.get("mode"),
                    sanitizer=kwargs.get("sanitizer"),
                    target_cpv_id=kwargs.get("target_cpv_id"),
                    success=False,
                    execution_time=0.0,
                    error=f"Job failed: {job.exc_info}",
                    error_type="RQJobFailure",
                    report={},
                    metadata=TrialMetadata(
                        timestamp_start=0.0,
                        timestamp_end=0.0,
                        worker_machine=meta.get("worker_name"),
                    ),
                )
            )

    console.print("\n[green]✓[/green] All jobs completed!")
    return results


def _build_missing_job_result(job) -> TrialResult:
    """Create a synthetic failed TrialResult for finished jobs with no result."""
    kwargs = job.kwargs or {}
    meta = job.meta or {}
    return TrialResult(
        crs=kwargs.get("crs", meta.get("crs", "unknown")),
        benchmark=kwargs.get("benchmark", meta.get("benchmark", "unknown")),
        harness=kwargs.get("harness_name", meta.get("harness", "unknown")),
        trial_num=kwargs.get("trial_num", meta.get("trial_num", 0)),
        crs_type="bug-finding",
        mode=kwargs.get("mode", meta.get("mode")),
        sanitizer=kwargs.get("sanitizer"),
        target_cpv_id=kwargs.get("target_cpv_id", meta.get("target_cpv_id")),
        success=False,
        execution_time=0.0,
        error="Job finished without TrialResult payload",
        error_type="MissingJobResult",
        report={},
        metadata=TrialMetadata(
            timestamp_start=0.0,
            timestamp_end=0.0,
            worker_machine=meta.get("worker_name"),
        ),
    )


def prompt_queue_mode(existing: dict[str, dict]) -> str:
    """
    Prompt user interactively for queue mode when stale jobs are detected.

    Args:
        existing: Dict of existing jobs by status (from get_existing_trials)

    Returns:
        str: User choice - "fresh", "continue", or "quit"
    """
    orphaned_count = 0
    if existing["started"]:
        # We'll check if there are workers later, for now just show the count
        orphaned_count = len(existing["started"])

    # Display existing jobs summary (interactive CLI output)
    print("\n" + "=" * 60)  # noqa: T201
    print("Existing jobs detected in queue:")  # noqa: T201
    print(f"  Queued:   {len(existing['queued'])}")  # noqa: T201
    if orphaned_count > 0:
        print(f"  Started:  {len(existing['started'])} (may be orphaned)")  # noqa: T201
    else:
        print(f"  Started:  {len(existing['started'])}")  # noqa: T201
    print(f"  Finished: {len(existing['finished'])}")  # noqa: T201
    print(f"  Failed:   {len(existing['failed'])}")  # noqa: T201
    print("=" * 60)  # noqa: T201
    print("\nHow do you want to proceed?")  # noqa: T201
    print("  [f] Fresh - purge all existing jobs and start from scratch")  # noqa: T201
    print("  [c] Continue - skip existing, recover orphaned started jobs")  # noqa: T201
    print("  [q] Quit - abort without changes")  # noqa: T201
    print()  # noqa: T201

    # Prompt for choice
    while True:
        choice = input("Choice [f/c/q]: ").strip().lower()
        if choice == "f":
            return "fresh"
        if choice == "c":
            return "continue"
        if choice == "q":
            return "quit"
        print("Invalid choice. Please enter 'f', 'c', or 'q'.")  # noqa: T201


def _archive_trial_dir_for_retry(trial_dir: Path) -> bool:
    """Archive a trial directory into retries/ and reset it for a clean retry."""
    if not trial_dir.exists():
        return True

    retries_dir = trial_dir / "retries"
    retries_dir.mkdir(parents=True, exist_ok=True)
    attempt_name = datetime.now().strftime("attempt-%Y%m%d-%H%M%S")
    archive_dir = retries_dir / attempt_name
    archive_dir.mkdir(parents=True, exist_ok=True)

    try:
        for item in list(trial_dir.iterdir()):
            if item.name == "retries":
                continue
            shutil.move(str(item), str(archive_dir / item.name))
    except Exception as e:
        logger.warning(f"Failed to prepare retry directory for {trial_dir}: {e}")
        return False
    return True


def _prepare_trial_dir_for_retry(config: ExperimentConfig, job) -> bool:
    """Archive failed trial directories (experiment/results filestores) for retry."""
    kwargs = job.kwargs or {}
    filestores = [config.experiment_filestore.resolve()]
    if config.results_filestore:
        results_root = config.results_filestore.resolve()
        if results_root not in filestores:
            filestores.append(results_root)

    for filestore in filestores:
        trial_dir = _build_trial_output_path(
            filestore=filestore,
            experiment_name=config.experiment,
            crs=kwargs.get("crs", ""),
            benchmark=kwargs.get("benchmark", ""),
            harness=kwargs.get("harness_name", ""),
            mode=kwargs.get("mode", ""),
            sanitizer=kwargs.get("sanitizer", "address"),
            trial_num=kwargs.get("trial_num", 0),
            target_cpv_id=kwargs.get("target_cpv_id"),
        )
        if not _archive_trial_dir_for_retry(trial_dir):
            return False
    return True


def get_crs_cpu_count(crs_name: str, config: ExperimentConfig) -> int | None:
    """Get CPU count for a CRS from compose overrides or experiment defaults."""
    if config.crs_compose:
        service = config.crs_compose.services.get(crs_name)
        if service:
            return service.num_cores
    if config.resources and config.resources.cores_per_trial:
        return config.resources.cores_per_trial
    return None


def get_crs_memory(crs_name: str, config: ExperimentConfig) -> str | None:
    """Get memory limit for a CRS from compose overrides or experiment defaults."""
    if config.crs_compose:
        service = config.crs_compose.services.get(crs_name)
        if service and service.mem_limit:
            return service.mem_limit
    if config.resources and config.resources.memory_per_trial:
        return config.resources.memory_per_trial
    return None


def run_experiment_distributed(
    experiment_name: str,
    config: ExperimentConfig,
    trials: List[Trial],
    *,
    queue_mode: str | None = None,
    retry_failed: bool = False,
) -> None:
    """Run experiment using Redis queue-based distributed execution.

    Args:
        experiment_name: Experiment identifier
        config: Experiment configuration
        trials: List of Trial objects to execute
    """
    from crsbench.distributed.common import normalize_redis_host
    from crsbench.distributed.runtime_session import (
        DistributedRuntimeSession,
        LockContentionError,
    )

    log_section("Running CRSBench in Distributed Mode (Redis)", width=60)

    redis_host = normalize_redis_host(config.redis_host)
    if not redis_host:
        raise ValueError("redis_host is required for distributed mode")

    logger.info(f"Redis host: {redis_host}")

    # Mark this process as orchestrator for logging
    os.environ["CRSBENCH_ORCHESTRATOR"] = "1"

    session = DistributedRuntimeSession.for_run(
        redis_host=redis_host, experiment_name=experiment_name
    )
    if session is None or session.trial_queue is None:
        raise RuntimeError(f"Failed to initialize Redis queue at {redis_host}")
    queue = session.trial_queue

    # Check for existing jobs in queue (queue sanity check)
    from crsbench.distributed.queue import (
        clear_experiment_jobs,
        get_existing_trials,
        handle_orphaned_jobs,
    )

    existing = get_existing_trials(queue, experiment_name=experiment_name)
    has_existing = any(existing.values())
    from crsbench.distributed.registry import RuntimeRegistration

    registration = RuntimeRegistration.from_experiment_config(config)
    bootstrap_inputs = (
        CloudVmBootstrapInputs.from_experiment_config(config)
        if isinstance(config, BaseModel)
        else None
    )
    lock_acquired = False
    requeued_failed_jobs = 0

    normalized_queue_mode = queue_mode.lower() if queue_mode else None

    try:
        if has_existing and normalized_queue_mode is None:
            if sys.stdin.isatty():
                normalized_queue_mode = prompt_queue_mode(existing)
                if normalized_queue_mode == "quit":
                    logger.info("Aborted by user")
                    return
            else:
                normalized_queue_mode = "continue"
                logger.info(
                    "Non-interactive mode detected with existing queue jobs; "
                    "using scoped queue mode: continue"
                )

        if has_existing and normalized_queue_mode == "quit":
            logger.info("Aborted by queue-mode=quit")
            return

        # Default to fresh if no existing jobs and no mode specified
        if normalized_queue_mode is None:
            normalized_queue_mode = "fresh"

        # TODO: too later to purge queue; as the old jobs are already taken by workers
        # Handle queue based on mode
        if normalized_queue_mode == "fresh":
            if has_existing:
                try:
                    session.register_or_raise(registration)
                    lock_acquired = True
                except LockContentionError:
                    logger.error(
                        f"Experiment '{experiment_name}' is already running. "
                        "Use a different experiment name or wait for the current run to finish."
                    )
                    return
                total_existing = sum(len(v) for v in existing.values())
                logger.warning(
                    f"Purging {total_existing} existing jobs from queue for experiment={experiment_name}"
                )
                clear_experiment_jobs(queue, experiment_name)
        elif normalized_queue_mode == "continue":
            if has_existing and not lock_acquired:
                try:
                    session.register_or_raise(registration)
                    lock_acquired = True
                except LockContentionError:
                    logger.error(
                        f"Experiment '{experiment_name}' is already running. "
                        "Use a different experiment name or wait for the current run to finish."
                    )
                    return

            # Handle orphaned started jobs (move to failed + retry)
            if existing["started"]:
                orphaned_count = handle_orphaned_jobs(queue, existing["started"])
                if orphaned_count > 0:
                    logger.info(f"Handled {orphaned_count} orphaned jobs")

            # Optional failed retries require explicit opt-in and clean trial dirs.
            if retry_failed and existing["failed"]:
                retried = 0
                for failed_job in existing["failed"].values():
                    if not _prepare_trial_dir_for_retry(config, failed_job):
                        continue
                    failed_job.meta["force_retry"] = True
                    failed_job.save_meta()
                    try:
                        queue.enqueue_job(failed_job)
                        retried += 1
                    except Exception as e:
                        logger.warning(
                            f"Failed to requeue failed job {failed_job.id[:8]}: {e}"
                        )
                if retried > 0:
                    requeued_failed_jobs = retried
                    logger.info(f"Requeued {retried} failed jobs with clean retry dirs")

        # Filter trials if in continue mode
        if normalized_queue_mode == "continue" and has_existing:
            # Build set of existing trial keys
            existing_keys = set()
            for status_dict in existing.values():
                existing_keys.update(status_dict.keys())

            # Filter out existing trials
            original_count = len(trials)
            trials = [
                t
                for t in trials
                if (
                    f"{t.crs}:{t.benchmark_harness.name}:{t.benchmark_harness.harness.name}:"
                    f"{t.mode}:{t.sanitizer}:{t.trial_num}:{t.target_cpv_id or '-'}"
                )
                not in existing_keys
            ]
            skipped_count = original_count - len(trials)
            if skipped_count > 0:
                logger.info(
                    f"Skipping {skipped_count} existing trials, enqueueing {len(trials)} new trials"
                )

        # Disk-based filtering: skip trials with .success markers on orchestrator disk
        # This catches trials completed by remote workers in previous runs
        original_count = len(trials)
        trials = [
            t
            for t in trials
            if _check_existing_trial(
                config,
                t.crs,
                t.benchmark_harness.name,
                t.benchmark_harness.harness.name,
                t.mode,
                t.sanitizer,
                t.trial_num,
                t.target_cpv_id,
            )
            is None
        ]
        disk_skipped = original_count - len(trials)
        if disk_skipped > 0:
            logger.info(f"Skipping {disk_skipped} trials already complete on disk")

        # Dump trial matrix to JSON
        dump_trial_matrix(trials, config)

        logger.info(f"Total trials to enqueue: {len(trials)}")
        if not trials and not existing["queued"] and not existing["started"]:
            if requeued_failed_jobs == 0:
                logger.info(
                    "No trial work remains after queue and disk filtering; "
                    "skipping registration and cloud bring-up"
                )
                return
        logger.info("=" * 60)

        log_section("Enqueuing Trial Jobs", width=60)

        # Enqueue jobs
        logger.info("\nEnqueuing jobs...")

        # Extract unique CRS names from trials
        crses = sorted({t.crs for t in trials})

        # Get CPU counts for each CRS.
        # Priority: crs_compose service override > experiment config > unconstrained.
        crs_cpu_counts = {}
        for crs in crses:
            crs_cpu_counts[crs] = get_crs_cpu_count(crs, config)
            logger.debug(f"CRS {crs} cpu_count={crs_cpu_counts[crs]}")

        # Get memory limits for each CRS
        # Priority: crs_compose service override > experiment config > None
        crs_memory_limits = {}
        for crs in crses:
            crs_memory_limits[crs] = get_crs_memory(crs, config)
            if crs_memory_limits[crs]:
                logger.debug(f"CRS {crs} memory={crs_memory_limits[crs]}")
            else:
                logger.debug(f"CRS {crs} has no memory limit configured")

        cpu_tag = None
        if config.resources:
            cpu_tag = config.resources.cpu_tag

        # Generate 6-char random alphanumeric suffix (shared by all trials)
        trial_suffix = "_" + "".join(
            secrets.choice(string.ascii_lowercase + string.digits) for _ in range(6)
        )

        # Generate timestamp once for all jobs in this experiment batch
        results_timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")

        try:
            if not lock_acquired:
                session.register_or_raise(registration)
                lock_acquired = True
        except LockContentionError:
            logger.error(
                f"Experiment '{experiment_name}' is already running. "
                "Use a different experiment name or wait for the current run to finish."
            )
            return

        cloud_config = getattr(config, "cloud", None)
        uses_provider_neutral_cloud = (
            isinstance(cloud_config, BaseModel)
            and cloud_config.providers is not None
            and cloud_config.workers is not None
            and cloud_config.orchestrator is not None
        )
        if (
            uses_provider_neutral_cloud
            or isinstance(cloud_config, BaseModel)
            and cloud_config.gce is not None
        ):
            if session.cloud_readiness is None:
                raise RuntimeError(
                    "Distributed runtime session missing cloud readiness store"
                )

            from crsbench.cloud.status import CloudFleetStatusManager

            fleet_status_manager = CloudFleetStatusManager(
                readiness_store=session.cloud_readiness
            )
            if uses_provider_neutral_cloud:
                assert cloud_config is not None
                from crsbench.cloud.gce.launch_preflight import (
                    prepare_gce_launch_inputs,
                )
                from crsbench.cloud.gce.provider import GceProviderAdapter
                from crsbench.cloud.models import build_cloud_launch_plan
                from crsbench.cloud.quota import QuotaValidator

                launch_plan = build_cloud_launch_plan(config)
                adapter = GceProviderAdapter()
                if os.environ.get("CRSBENCH_CLOUD_PREPROVISIONED_WORKERS") == "1":
                    logger.info(
                        "Waiting for pre-provisioned cloud workers because "
                        "CRSBENCH_CLOUD_PREPROVISIONED_WORKERS=1"
                    )
                    fleet_status = fleet_status_manager.wait_for_existing_workers(
                        plan=launch_plan,
                        adapter=adapter,
                    )
                else:
                    preflight = prepare_gce_launch_inputs(
                        plan=launch_plan,
                        bootstrap=cloud_config.bootstrap,
                        cwd=Path.cwd(),
                    )
                    assert preflight.resolved_plan is not None
                    QuotaValidator(adapters={"gce": adapter}).validate(
                        launch_plan,
                        include_orchestrator=False,
                    )
                    fleet_status = fleet_status_manager.bring_up_workers(
                        plan=preflight.resolved_plan,
                        adapter=adapter,
                        redis_host=redis_host,
                        redis_password=os.environ.get("CRSBENCH_REDIS_PASSWORD"),
                        registration=registration,
                        bootstrap_inputs=bootstrap_inputs,
                        env_passthrough=preflight.worker_env,
                    )
                    logger.info(
                        "Cloud worker placements ready: "
                        f"{fleet_status.ready_count}/{fleet_status.requested_count}"
                    )
            else:
                assert cloud_config is not None
                legacy_cloud_gce = cloud_config.gce
                assert legacy_cloud_gce is not None
                if os.environ.get("CRSBENCH_CLOUD_PREPROVISIONED_WORKERS") == "1":
                    logger.info(
                        "Waiting for pre-provisioned cloud workers because "
                        "CRSBENCH_CLOUD_PREPROVISIONED_WORKERS=1"
                    )
                    fleet_status = fleet_status_manager.wait_for_existing_gce_workers(
                        experiment_name=experiment_name,
                        fleet=legacy_cloud_gce,
                    )
                else:
                    from crsbench.cloud.gce.launch_preflight import (
                        prepare_gce_launch_inputs,
                    )

                    preflight = prepare_gce_launch_inputs(
                        worker_fleets=[legacy_cloud_gce],
                        bootstrap=cloud_config.bootstrap,
                        cwd=Path.cwd(),
                    )
                    assert preflight.resolved_worker_fleets is not None
                    fleet_status = fleet_status_manager.bring_up_gce_workers(
                        experiment_name=experiment_name,
                        fleet=preflight.resolved_worker_fleets[0],
                        redis_host=redis_host,
                        redis_password=os.environ.get("CRSBENCH_REDIS_PASSWORD"),
                        registration=registration,
                        bootstrap_inputs=bootstrap_inputs,
                        env_passthrough=preflight.worker_env,
                    )
                    logger.info(
                        "Cloud worker fleet ready: "
                        f"{fleet_status.ready_count}/{fleet_status.requested_count}"
                    )

        jobs = []
        for trial in trials:
            bh = trial.benchmark_harness
            cpu_count = crs_cpu_counts.get(trial.crs)
            memory_limit = crs_memory_limits.get(trial.crs)

            trial_id = build_trial_id(experiment_name, trial, trial_suffix)

            config_payload = config.model_dump()

            job = queue.enqueue(
                "crsbench.distributed.jobs.run_crs_trial",
                crs=trial.crs,
                benchmark=bh.name,
                harness_name=bh.harness.name,
                harness_path=bh.harness.path,
                trial_num=trial.trial_num,
                trial_id=trial_id,
                config_dict=config_payload,
                mode=trial.mode,
                sanitizer=trial.sanitizer,
                target_cpv_id=trial.target_cpv_id,
                results_timestamp=results_timestamp,
                job_timeout=config.max_total_time,
                result_ttl=-1,  # Persist results forever
                meta={
                    "crs": trial.crs,
                    "benchmark": bh.name,
                    "harness": bh.harness.name,
                    "mode": trial.mode,
                    "sanitizer": trial.sanitizer,
                    "trial_num": trial.trial_num,
                    "target_cpv_id": trial.target_cpv_id,
                    "cpu_count": cpu_count,  # Explicit CRS/runtime CPU limit, or None
                    "memory_limit": memory_limit,  # Explicit CRS/runtime memory limit, or None
                    "cpu_tag": cpu_tag,
                    "experiment_name": experiment_name,
                },
            )
            jobs.append(job)
            logger.debug(
                f"Enqueued job {job.id} for {trial.crs} on {bh.name}/{bh.harness.name} (trial {trial.trial_num})"
            )

        logger.info(f"✓ Enqueued {len(jobs)} jobs successfully")

        # Monitor progress
        logger.info("\nMonitoring job progress...")
        results = monitor_jobs(
            queue,
            jobs,
            experiment_name,
            config,
            disk_skipped=disk_skipped,
            registry=session.registry,
        )

        # Generate final report
        log_section("Experiment Complete - Generating Report", width=60)

        generate_final_report(results, experiment_name, config)
    finally:
        session.cleanup()

    # Post-experiment operations
    # Cleanup bulky artifacts
    if config.keep_only_results:
        _cleanup_experiment_artifacts(experiment_name, config)


def _cleanup_experiment_artifacts(experiment_name: str, config) -> None:
    """Clean up bulky artifacts from experiment directory.

    Args:
        experiment_name: Experiment identifier
        config: Experiment configuration
    """
    from crsbench.reporting.snapshot_loader import discover_trials

    experiment_dir = resolve_experiment_dir(
        config.experiment_filestore, experiment_name
    )

    if not experiment_dir.exists():
        logger.warning(f"Experiment directory not found for cleanup: {experiment_dir}")
        return

    # Discover all trial directories
    trial_infos = discover_trials(experiment_dir)
    trial_dirs = [trial_info.trial_dir for trial_info in trial_infos]

    if not trial_dirs:
        logger.warning(f"No trial directories found in {experiment_dir}")
        return

    log_section("Cleaning up bulky artifacts", width=60)
    logger.info(f"Found {len(trial_dirs)} trial directories to clean")

    for trial_dir in trial_dirs:
        cleanup_trial_directory(trial_dir)

    logger.info("Cleanup complete")


def generate_final_report(
    results: List[TrialResult], experiment_name: str, config
) -> None:
    """Generate and display final experiment report.

    Generates both console summary and HTML/JSON reports.

    Args:
        results: List of trial results
        experiment_name: Experiment identifier
        config: Experiment configuration
    """
    logger.info(f"\nFinal Report for Experiment: {experiment_name}")
    logger.info("=" * 60)

    # Count successes and failures
    total_trials = len(results)
    successful_trials = sum(1 for r in results if r.success)
    failed_trials = total_trials - successful_trials

    logger.info(f"Total trials: {total_trials}")
    if total_trials > 0:
        logger.info(
            f"Successful: {successful_trials} ({successful_trials / total_trials * 100:.1f}%)"
        )
        logger.info(
            f"Failed: {failed_trials} ({failed_trials / total_trials * 100:.1f}%)"
        )

    # Aggregate POV statistics
    if successful_trials > 0:
        total_povs_found = sum(r.povs_found for r in results if r.success)
        total_povs_available = sum(r.total_povs for r in results if r.success)

        if total_povs_available > 0:
            overall_success_rate = total_povs_found / total_povs_available
            logger.info("\nPOV Discovery:")
            logger.info(
                f"  Total POVs found: {total_povs_found}/{total_povs_available}"
            )
            logger.info(f"  Overall success rate: {overall_success_rate:.1%}")

    # Report failures
    if failed_trials > 0:
        logger.warning(f"\nFailed trials ({failed_trials}):")
        for idx, result in enumerate(results):
            if not result.success:
                error = result.error or "Unknown error"
                logger.warning(
                    f"  [{idx + 1}] {result.crs} on {result.benchmark} "
                    f"(trial {result.trial_num}): {error}"
                )

    # Generate HTML/JSON reports from snapshots
    _generate_html_json_reports(experiment_name, config)

    log_section("Report generation complete", width=60)
    logger.info(f"Experiment filestore: {config.experiment_filestore}")
    logger.info(f"Report filestore: {config.report_filestore}")


def _generate_html_json_reports(experiment_name: str, config) -> None:
    """Generate HTML and JSON reports from trial snapshots.

    Args:
        experiment_name: Experiment identifier
        config: Experiment configuration
    """
    from crsbench.reporting import ReportGenerator

    experiment_dir = resolve_experiment_dir(
        config.experiment_filestore, experiment_name
    )
    report_dir = Path(config.report_filestore) / experiment_name
    benchmarks_root = resolve_benchmarks_root(config.benchmarks_root)

    # Check if experiment directory exists
    if not experiment_dir.exists():
        logger.warning(
            f"Experiment directory not found: {experiment_dir}. "
            "Skipping HTML/JSON report generation."
        )
        return

    try:
        logger.info("\nGenerating HTML/JSON reports from snapshots...")

        generator = ReportGenerator(
            output_dir=report_dir, benchmarks_root=benchmarks_root
        )
        report_paths = generator.generate_experiment_report(
            experiment_dir=experiment_dir,
            format="both",
            skip_incomplete=True,
        )

        if "html" in report_paths:
            logger.info(f"HTML report: {report_paths['html']}")
        if "json" in report_paths:
            logger.info(f"JSON report: {report_paths['json']}")

    except Exception as e:
        # Don't fail the experiment if report generation fails
        logger.warning(f"Failed to generate HTML/JSON reports: {e}")
        logger.warning(
            "This may happen if snapshots are disabled (snapshot_period=0) "
            "or no complete snapshots were captured."
        )


def main() -> None:
    """Main entry point for the experiment runner."""
    # Load .env file (CRSBENCH_REDIS_PASSWORD, etc.) if present.
    # override=True ensures .env is the source of truth over stale shell exports.
    from dotenv import load_dotenv

    load_dotenv(override=True)

    # Parse arguments
    args = parse_arguments()

    # Dispatch to appropriate command handler
    if args.command == "gen-config":
        from crsbench.genconfig.cli import run_gen_config

        sys.exit(run_gen_config(args))

    if args.command == "verify":
        # Handle verify command
        from crsbench.evaluation.verification.cli.pov_verify_command import run_verify

        sys.exit(run_verify(args))

    if args.command == "coverage":
        # Handle coverage command
        from crsbench.evaluation.coverage.cli.coverage_command import run_coverage

        sys.exit(run_coverage(args))

    if args.command == "patch-verify":
        # Handle patch-verify command
        from crsbench.evaluation.verification.cli.patch_verify_command import (
            run_patch_verify,
        )

        sys.exit(run_patch_verify(args))

    if args.command == "worker":
        # Handle worker command
        from crsbench.distributed.cli.worker_command import run_worker

        sys.exit(run_worker(args))

    if args.command == "evaluator":
        # Handle evaluator command
        from crsbench.distributed.cli.evaluator_command import run_evaluator

        sys.exit(run_evaluator(args))

    if args.command == "queue":
        from crsbench.distributed.cli.queue_command import run_queue

        sys.exit(run_queue(args))

    if args.command == "cloud":
        from crsbench.cloud.cli.cloud_command import run_cloud

        sys.exit(run_cloud(args))

    if args.command == "report":
        # Handle report command
        from crsbench.reporting.cli import run_report

        sys.exit(run_report(args))

    if args.command == "dashboard":
        # Handle dashboard command
        from crsbench.reporting.cli import run_dashboard

        sys.exit(run_dashboard(args))

    if args.command == "benchmark":
        # Handle benchmark command (bundle, validate, prepare-delta)
        from crsbench.benchmark.packaging.cli.benchmark_command import (
            run_benchmark_command,
        )

        sys.exit(run_benchmark_command(args))

    if args.command == "re-eval":
        from crsbench.evaluation.reeval.cli import run_reeval

        sys.exit(run_reeval(args))

    if args.command == "download":
        from crsbench.dataset.cli import run_download

        sys.exit(run_download(args))

    if args.command == "prepare":
        from crsbench.prepare.cli import run_prepare

        sys.exit(run_prepare(args))

    # Below is for 'run' command (experiment execution)

    # Set verbose logging if requested
    if hasattr(args, "verbose") and args.verbose:
        configure_logger(level="DEBUG")
        logger.debug("Verbose logging enabled")

    # Validate arguments
    validate_arguments(args)

    # Load and validate experiment configuration
    config_path = Path(args.experiment_config)
    config = load_experiment_config(config_path)

    # Validate filestore directory permissions early
    validate_filestore_permissions(config)

    # Resolve experiment name from config (single source of truth)
    experiment_name = config.experiment
    logger.info("=" * 60)
    logger.info("CRSBench Experiment Runner")
    logger.info("=" * 60)
    logger.info(f"Experiment name: {experiment_name}")
    logger.info(f"Configuration file: {args.experiment_config}")

    # Resolve CRSes from crs_compose service keys
    oss_crs_registry = config.get_crs_registry_ids()
    if not oss_crs_registry:
        logger.error("No CRS configured in crs_compose")
        sys.exit(1)
    logger.info(
        f"OSS CRS registry IDs ({len(oss_crs_registry)}): {', '.join(oss_crs_registry)}"
    )

    # Resolve benchmarks from config
    try:
        benchmark_entries = config.get_benchmark_entries()
        benchmark_names = config.get_benchmark_list()
        if config.benchmark_suite:
            logger.info(f"Benchmark suite: {config.benchmark_suite}")
        logger.info(
            f"Benchmarks ({len(benchmark_names)}): {', '.join(benchmark_names)}"
        )

        # Filter benchmarks by mode early (before resolving harnesses)
        benchmarks_root = resolve_benchmarks_root(config.benchmarks_root)
        mode_str = config.mode.value  # Get string value from enum
        if mode_str not in ("all", "auto"):
            original_count = len(benchmark_names)
            benchmark_names = filter_benchmarks_by_mode(
                benchmark_names, mode_str, benchmarks_root
            )
            # Also filter benchmark_entries to match
            benchmark_entries = [
                entry for entry in benchmark_entries if entry.name in benchmark_names
            ]
            if original_count != len(benchmark_names):
                logger.info(
                    f"Filtered by mode={mode_str}: {len(benchmark_names)} of {original_count} benchmarks"
                )

    except ValueError as e:
        logger.error(f"Failed to resolve benchmarks: {e}")
        sys.exit(1)

    logger.info("=" * 60)

    # Resolve BenchmarkHarness objects
    try:
        benchmark_harnesses = resolve_benchmark_harnesses(
            benchmark_entries, benchmarks_root
        )
        logger.info(f"Resolved {len(benchmark_harnesses)} benchmark-harness pairs")
    except Exception as e:
        logger.error(f"Failed to resolve harnesses: {e}")
        sys.exit(1)

    # Calculate total jobs using trial matrix (accounts for mode and CPV filtering)
    registry_dir = config.registry_dir
    trial_matrix = generate_trial_matrix(
        benchmark_harnesses, oss_crs_registry, config, registry_dir
    )

    total_jobs = len(trial_matrix)
    logger.info(f"Total jobs to execute: {total_jobs}")

    # Determine execution mode
    use_distributed = should_use_distributed_mode(args, config, total_jobs)

    # Display estimated runtime (with worker count for distributed mode)
    if use_distributed and config.worker:
        display_estimated_runtime(
            total_jobs, config, worker_count=config.worker.jobs or 1
        )
    else:
        display_estimated_runtime(total_jobs, config)

    # Handle dry-run mode: display trials and exit without executing
    if hasattr(args, "dry_run") and args.dry_run:
        display_trial_matrix(trial_matrix)
        return

    # Run experiment in appropriate mode
    if use_distributed:
        run_experiment_distributed(
            experiment_name,
            config,
            trial_matrix,
            queue_mode=getattr(args, "queue_mode", None),
            retry_failed=bool(getattr(args, "retry_failed", False)),
        )
    else:
        run_experiment_local(experiment_name, config, trial_matrix)


if __name__ == "__main__":
    main()
