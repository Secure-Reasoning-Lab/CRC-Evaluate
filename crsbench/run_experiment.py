#!/usr/bin/env python3
"""Experiment runner for CRSBench evaluation framework.

This script provides a single entry point for running CRS evaluations with
standardized experiment configurations, CRS integration, and benchmark suite
management.

Usage:
    # Run experiments
    crsbench run --experiment-config experiment-config.yaml --benchmarks benchmark1

    # Verify POVs
    crsbench verify benchmarks/sanity-mock-c-delta-01 --pov-dir ./povs/

    # Collect coverage
    crsbench coverage benchmarks/sanity-mock-c-delta-01 --corpus-dir ./corpus/

    # Legacy (backward compatible - runs 'run' subcommand)
    crsbench --experiment-config experiment-config.yaml --benchmarks benchmark1
"""

import argparse
import os
import sys
import time
from pathlib import Path
from typing import List

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel

from crsbench.builder import BuildResult, OSSFuzzBuilder
from crsbench.builder.types import BenchmarkMode
from crsbench.distributed.jobs import get_crs_type
from crsbench.evaluation.results import CRSType, TrialResult
from crsbench.utils import log_progress, log_section, log_summary, set_gitcache
from crsbench.utils.crs_helper import get_crs_registry_name
from crsbench.utils.logger import configure_logger, get_logger
from crsbench.utils.workers import resolve_build_workers
from crsbench.validation.meta_adapter import MetaYamlAdapter
from crsbench.validation.schemas import BenchmarkHarness, ExperimentConfig

# Load environment variables from .env file if present
load_dotenv()

# Get logger instance
logger = get_logger(__name__)


# Trial configuration
class Trial(BaseModel):
    """Trial configuration for CRS evaluation.

    Attributes:
        crs: CRS identifier
        benchmark_harness: Resolved benchmark-harness pair
        trial_num: Trial number (1-indexed)
        mode: Evaluation mode ("delta" or "full")
    """

    crs: str
    benchmark_harness: BenchmarkHarness
    trial_num: int
    mode: str


def _add_run_arguments(parser: argparse.ArgumentParser) -> None:
    """Add arguments for the 'run' subcommand.

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
        "--experiment-name",
        type=str,
        required=False,
        metavar="EXPERIMENT_NAME",
        help="Name for this experiment (overrides config file if specified)",
    )

    parser.add_argument(
        "--crses",
        type=str,
        required=False,
        metavar="CRS_LIST",
        help="Comma-separated list of CRS implementations (overrides config file if specified)",
    )

    parser.add_argument(
        "--benchmarks",
        type=str,
        required=False,
        metavar="BENCHMARK_LIST",
        help="Comma-separated list of benchmarks (overrides config file if specified)",
    )

    parser.add_argument(
        "--benchmark-suite",
        type=str,
        required=False,
        metavar="SUITE_NAME",
        help="Benchmark suite name (overrides config file if specified, mutually exclusive with --benchmarks)",
    )

    parser.add_argument(
        "--mode",
        type=str,
        choices=["delta", "full", "all"],
        required=False,
        metavar="MODE",
        help="Evaluation mode: 'delta' (diff-based), 'full' (complete codebase), or 'all' (run all available modes). "
        "Overrides config file if specified.",
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
        type=str,
        choices=["fresh", "continue"],
        required=False,
        metavar="MODE",
        help="Queue mode for distributed execution: 'fresh' purges existing jobs and starts from scratch, "
        "'continue' resumes from existing state (skips existing trials, retries failed). "
        "If not specified and stale jobs exist, you will be prompted interactively.",
    )

    parser.add_argument(
        "--oss-fuzz-path",
        type=str,
        required=False,
        metavar="OSS_FUZZ_PATH",
        help="Path to oss-fuzz directory (highest precedence, overrides config file)",
    )

    parser.add_argument(
        "--registry-dir",
        type=str,
        required=False,
        metavar="REGISTRY_DIR",
        help="Path to CRS registry directory (highest precedence, overrides config file)",
    )

    parser.add_argument(
        "--crs-configs-dir",
        type=str,
        required=False,
        metavar="CRS_CONFIGS_DIR",
        help="Path to CRS configs directory (highest precedence, overrides config file)",
    )

    parser.add_argument(
        "--benchmarks-root",
        type=str,
        required=False,
        metavar="BENCHMARKS_ROOT",
        help="Path to benchmarks root directory (highest precedence, overrides config file)",
    )

    parser.add_argument(
        "--benchmark-suites-root",
        type=str,
        required=False,
        metavar="BENCHMARK_SUITES_ROOT",
        help="Path to benchmark suites root directory (highest precedence, overrides config file)",
    )

    parser.add_argument(
        "--hints-enabled",
        action="store_true",
        help="Enable hints for CRS evaluation (overrides config file)",
    )

    parser.add_argument(
        "--hint-sarif-level",
        type=int,
        choices=[1, 2, 3, 4, 5],
        required=False,
        metavar="LEVEL",
        help="SARIF hint level 1-5 (1=vague, 5=detailed, overrides config file)",
    )

    parser.add_argument(
        "--hint-corpus-level",
        type=int,
        choices=[1, 2, 3, 4, 5],
        required=False,
        metavar="LEVEL",
        help="Corpus hint level 1-5 (1=minimal, 5=comprehensive, overrides config file) [PLACEHOLDER]",
    )

    parser.add_argument(
        "--litellm-mode",
        type=str,
        choices=["passthrough", "proxy"],
        required=False,
        metavar="MODE",
        help="LiteLLM mode: 'passthrough' uses external LiteLLM (UPSTREAM_LITELLM_BASE_URL, LITELLM_API_KEY), "
        "'proxy' uses self-hosted proxy (LITELLM_BASE_URL, LITELLM_MASTER_KEY). "
        "If not set, CRS deploys its own LiteLLM instance. (overrides config file)",
    )

    parser.add_argument(
        "--project-image-prefix",
        type=str,
        required=False,
        metavar="PREFIX",
        help="Docker image prefix for custom project images (default: aixcc-afc). "
        "Used when building project images locally. (overrides config file)",
    )

    parser.add_argument(
        "--gitcache",
        action="store_true",
        help="Use gitcache for git clone operations (caches git clones for faster CI/CD)",
    )

    parser.add_argument(
        "--build-workers",
        type=int,
        required=False,
        metavar="N",
        help="Number of parallel workers for building variants (default: 4). "
        "Priority: CLI > CRSBENCH_BUILD_WORKERS env > config file.",
    )

    parser.add_argument(
        "--verify-workers",
        type=int,
        required=False,
        metavar="N",
        help="Number of parallel workers for POV/patch verification (default: 4). "
        "Priority: CLI > CRSBENCH_VERIFY_WORKERS env > config file.",
    )

    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging (logs all commands executed with their working directories)",
    )

    parser.add_argument(
        "--only-cpv-harnesses",
        action="store_true",
        help="Skip harnesses without CPVs for bug-finding CRS (default: True, this flag is for explicit override). "
        "Bug-fixing CRS always skips harnesses without CPVs regardless of this flag.",
    )


def parse_arguments() -> argparse.Namespace:
    """Parse command line arguments with subcommand support.

    Supports both subcommands and legacy flat arguments for backward compatibility:
    - crsbench run --experiment-config ...  (new style)
    - crsbench verify <benchmark> ...         (POV verification)
    - crsbench --experiment-config ...       (legacy, equivalent to 'run')

    Returns:
        Parsed arguments with experiment configuration.
    """
    # Check for legacy invocation (no subcommand, starts with --)
    # or verify/coverage/worker/stats subcommand
    if len(sys.argv) > 1 and sys.argv[1] not in [
        "run",
        "verify",
        "coverage",
        "worker",
        "stats",
        "benchmark",
        "-h",
        "--help",
    ]:
        # Legacy mode: insert 'run' as default subcommand
        if sys.argv[1].startswith("-"):
            sys.argv.insert(1, "run")

    parser = argparse.ArgumentParser(
        prog="crsbench",
        description="CRSBench - Cyber Reasoning System Evaluation Framework",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run CRS experiments
  %(prog)s run --experiment-config config.yaml --benchmarks bench1 --crses crs1

  # Verify POVs against benchmark variants
  %(prog)s verify benchmarks/sanity-mock-c-delta-01 --pov-dir ./povs/

  # Legacy style (equivalent to 'run')
  %(prog)s --experiment-config config.yaml --benchmarks bench1 --crses crs1
        """,
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # 'run' subcommand - experiment execution
    run_parser = subparsers.add_parser(
        "run",
        help="Run CRS evaluation experiments",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --experiment-config config.yaml --benchmarks bench1 --crses crs1
  %(prog)s --experiment-config config.yaml --benchmark-suite crsbench-c
        """,
    )
    _add_run_arguments(run_parser)
    run_parser.set_defaults(command="run")

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

    # 'worker' subcommand - distributed worker
    from crsbench.distributed.cli.worker_command import add_worker_subparser

    add_worker_subparser(subparsers)

    # 'migrate' subcommand - migration and generation tools
    from crsbench.migration.cli.converter_command import add_migrate_subparser

    add_migrate_subparser(subparsers)

    # 'stats' subcommand - benchmark statistics
    from crsbench.statistics.cli import add_stats_subparser

    add_stats_subparser(subparsers)

    # 'report' subcommand - report generation
    from crsbench.reporting.cli import add_report_subparser

    add_report_subparser(subparsers)

    # 'dashboard' subcommand - web dashboard
    from crsbench.reporting.cli import add_dashboard_subparser

    add_dashboard_subparser(subparsers)

    # 'ci' subcommand - benchmark CI testing
    from crsbench.benchmark_ci.cli import add_ci_subparser

    add_ci_subparser(subparsers)

    # 'benchmark' subcommand - benchmark management (bundle, validate, prepare-delta)
    from crsbench.benchmark.packaging.cli import add_benchmark_subparser

    add_benchmark_subparser(subparsers)

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


def parse_list_argument(arg_value: str) -> List[str]:
    """Parse comma-separated list argument.

    Args:
        arg_value: Comma-separated string value

    Returns:
        List of stripped strings
    """
    return [item.strip() for item in arg_value.split(",") if item.strip()]


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

            meta_yaml_path = benchmark_path / ".aixcc" / "meta.yaml"
            if not meta_yaml_path.exists():
                raise FileNotFoundError(f"meta.yaml not found: {meta_yaml_path}")

            import yaml

            with meta_yaml_path.open() as f:
                meta_data = yaml.safe_load(f)

            # Parse harness files into dict for lookup
            harness_files_dict = {
                h.get("name"): h for h in meta_data.get("harness_files", [])
            }

            # Validate each specified harness exists and create BenchmarkHarness
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
                        name=entry.name, path=benchmark_path, harness=harness_obj
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
            import yaml

            meta_yaml_path = benchmark_path / ".aixcc" / "meta.yaml"
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
                            name=entry.name, path=benchmark_path, harness=harness_obj
                        )
                    )

    logger.info(
        f"Resolved {len(harness_pairs)} BenchmarkHarness pairs from {len(benchmark_entries)} benchmarks"
    )
    return harness_pairs


def get_available_modes_for_benchmark(benchmark_path: Path) -> List[str]:
    """Get list of available evaluation modes (delta/full) for a benchmark.

    Args:
        benchmark_path: Path to benchmark directory

    Returns:
        List of available mode strings ('delta' and/or 'full')
    """
    import yaml

    meta_yaml_path = benchmark_path / ".aixcc" / "meta.yaml"
    if not meta_yaml_path.exists():
        logger.warning(f"meta.yaml not found at {meta_yaml_path}")
        return []

    try:
        with meta_yaml_path.open() as f:
            meta_data = yaml.safe_load(f)

        modes = []
        if meta_data.get("delta_mode"):
            modes.append("delta")
        if meta_data.get("full_mode"):
            modes.append("full")

        return modes
    except Exception as e:
        logger.warning(f"Failed to read modes from {meta_yaml_path}: {e}")
        return []


def generate_trial_matrix(
    benchmark_harnesses: List["BenchmarkHarness"],
    crses: List[str],
    config,
    registry_dir: Path,
    crs_configs_dir: Path,
) -> List[Trial]:
    """Generate all trial combinations from BenchmarkHarness objects, CRSes, and trials.

    Args:
        benchmark_harnesses: List of BenchmarkHarness objects
        crses: List of CRS identifiers
        config: Experiment configuration with trials count and mode
        registry_dir: Path to CRS registry directory
        crs_configs_dir: Path to CRS configs directory

    Returns:
        List of Trial namedtuples with mode information
    """
    trials = []
    config_mode = config.mode.value  # Get string value from enum

    for crs in crses:
        # Detect CRS type
        registry_name = get_crs_registry_name(crs, crs_configs_dir)
        crs_type = get_crs_type(registry_name, registry_dir)
        is_bug_fixing = crs_type == CRSType.BUG_FIXING.value

        for benchmark_harness in benchmark_harnesses:
            # Skip harnesses without CPVs:
            # - Bug-fixing CRS: always skip (required for POV-based operation)
            # - Bug-finding CRS: skip only when only_cpv_harnesses is enabled
            should_check_cpvs = is_bug_fixing or config.only_cpv_harnesses
            if should_check_cpvs:
                meta_path = Path(benchmark_harness.path) / ".aixcc" / "meta.yaml"
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
                    logger.info(
                        f"Skipping {benchmark_harness.name}/{benchmark_harness.harness.name}: "
                        f"no CPVs ({skip_reason})"
                    )
                    continue
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
            else:
                # Single mode: delta or full
                modes_to_run = [config_mode]

            # Generate trials for each mode
            for mode in modes_to_run:
                for trial_num in range(1, config.trials + 1):
                    trials.append(
                        Trial(
                            crs=crs,
                            benchmark_harness=benchmark_harness,
                            trial_num=trial_num,
                            mode=mode,
                        )
                    )

    logger.info(
        f"Generated {len(trials)} trials: {len(crses)} CRSes × "
        f"{len(benchmark_harnesses)} benchmark-harness pairs × "
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
    import json

    output_dir = config.experiment_filestore.resolve() / config.experiment
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


def _is_all_bug_fixing_crs(
    trials: List[Trial],
    registry_dir: Path,
    crs_configs_dir: Path,
) -> bool:
    """Check if all trials use bug-fixing CRS only.

    Bug-fixing CRS does not need upfront variant builds because:
    - CRS execution only uses POVs, not variants
    - Patch verification builds PATCHED variants on-demand

    Args:
        trials: List of trials to check
        registry_dir: Path to CRS registry directory
        crs_configs_dir: Path to CRS configs directory

    Returns:
        True if all trials are bug-fixing CRS
    """
    from crsbench.distributed.jobs import get_crs_type
    from crsbench.utils.crs_helper import get_crs_registry_name

    if not trials:
        return False

    # Get unique CRS names from trials
    unique_crses = {trial.crs for trial in trials}

    for crs in unique_crses:
        try:
            registry_name = get_crs_registry_name(crs, crs_configs_dir)
            crs_type = get_crs_type(registry_name, registry_dir)
            if crs_type != "bug-fixing":
                return False
        except (FileNotFoundError, ValueError) as e:
            logger.warning(f"Could not determine CRS type for '{crs}': {e}")
            return False  # Be conservative - build variants if unsure

    return True


def build_variants_upfront(
    trials: List[Trial],
    config,
    args: argparse.Namespace,
) -> dict[str, BuildResult]:
    """Build all required variants upfront before trial execution.

    This function:
    1. Collects all unique (benchmark, mode) combinations from trials
    2. Creates build plans for each combination using MetaYamlAdapter
    3. Deduplicates across benchmarks (same variant only built once)
    4. Builds all variants in parallel using OSSFuzzBuilder

    Args:
        trials: List of Trial namedtuples
        config: Experiment configuration
        args: CLI arguments

    Returns:
        Dict mapping variant names to their build results
    """
    from crsbench.validation.meta_adapter import MetaYamlAdapter

    log_section("Building Variants Upfront", width=60)

    # Get oss-fuzz path
    oss_fuzz_path = Path(
        args.oss_fuzz_path or config.oss_fuzz_path or (Path.cwd() / "oss-fuzz")
    ).resolve()

    if not oss_fuzz_path.exists():
        logger.error(f"oss-fuzz directory not found: {oss_fuzz_path}")
        raise RuntimeError(f"oss-fuzz directory not found: {oss_fuzz_path}")

    # Get worker count
    build_workers = resolve_build_workers(
        getattr(args, "build_workers", None),
        config.build_workers,
    )
    logger.info(f"Using {build_workers} build workers")

    # Check if coverage is enabled
    coverage_enabled = config.coverage_enabled

    # Collect unique (benchmark_path, mode) combinations
    benchmark_modes: dict[tuple[Path, str], None] = {}
    for trial in trials:
        key = (trial.benchmark_harness.path, trial.mode)
        benchmark_modes[key] = None

    logger.info(f"Found {len(benchmark_modes)} unique (benchmark, mode) combinations")

    # Create builder
    builder = OSSFuzzBuilder(oss_fuzz_path, max_workers=build_workers)

    # Collect all build configs
    all_configs: list = []
    seen_variants: set[str] = set()

    for benchmark_path, mode in benchmark_modes:
        # Load project.yaml for main_repo
        project_yaml_path = benchmark_path / "project.yaml"
        if not project_yaml_path.exists():
            logger.warning(f"project.yaml not found: {project_yaml_path}, skipping")
            continue

        with project_yaml_path.open() as f:
            project_data = yaml.safe_load(f)

        main_repo = project_data.get("main_repo")
        repo_name = project_data.get("repo_name")
        language = project_data.get("language", "c")

        if not main_repo:
            logger.warning(f"main_repo not found in {project_yaml_path}, skipping")
            continue

        # Create adapter from meta.yaml
        meta_yaml_path = benchmark_path / ".aixcc" / "meta.yaml"
        if not meta_yaml_path.exists():
            logger.warning(f"meta.yaml not found: {meta_yaml_path}, skipping")
            continue

        try:
            adapter = MetaYamlAdapter.from_meta_yaml(
                meta_yaml_path=meta_yaml_path,
                benchmark_name=benchmark_path.name,
                lang=language,
                main_repo=main_repo,
                benchmark_path=benchmark_path,
                repo_name=repo_name,
            )
        except (FileNotFoundError, ValueError) as e:
            logger.warning(f"Failed to load meta.yaml: {e}, skipping")
            continue

        # Determine mode and get commit info based on trial's mode (not adapter's default)
        benchmark_mode = BenchmarkMode.DELTA if mode == "delta" else BenchmarkMode.FULL

        if mode == "delta":
            if not adapter.config.delta_mode:
                logger.warning(
                    f"Delta mode not configured for {benchmark_path}, skipping"
                )
                continue
            base_commit = adapter.config.delta_mode.base_commit
            ref_commit = adapter.config.delta_mode.ref_commit
        else:
            if not adapter.config.full_mode:
                logger.warning(
                    f"Full mode not configured for {benchmark_path}, skipping"
                )
                continue
            base_commit = adapter.config.full_mode.base_commit
            ref_commit = None

        # Get CPV numbers from adapter
        cpv_numbers = adapter.get_cpv_numbers()

        # Create build plan
        plan = builder.create_build_plan(
            benchmark_name=benchmark_path.name,
            benchmark_path=benchmark_path,
            main_repo=main_repo,
            mode=benchmark_mode,
            base_commit=base_commit,
            ref_commit=ref_commit,
            cpv_numbers=cpv_numbers,
            language=language,
            repo_name=repo_name,
            include_coverage=coverage_enabled,
        )

        # Add configs (deduplicate across benchmarks)
        for cfg in plan.configs:
            if cfg.variant_name not in seen_variants:
                all_configs.append(cfg)
                seen_variants.add(cfg.variant_name)

    logger.info(f"Total unique variants to build: {len(all_configs)}")

    # Build all variants
    if not all_configs:
        logger.warning("No variants to build")
        return {}

    results = builder.build_variants(all_configs)

    # Report build results
    successful = sum(1 for r in results.values() if r.success)
    cached = sum(1 for r in results.values() if r.cached)
    failed = sum(1 for r in results.values() if not r.success)

    logger.info(
        f"Build complete: {successful} succeeded ({cached} cached), {failed} failed"
    )

    if failed > 0:
        for name, result in results.items():
            if not result.success:
                logger.error(f"  Failed: {name} - {result.error}")

    return results


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
    from crsbench.distributed.queue import check_redis_available

    # Check for conflicting flags
    distributed = getattr(args, "distributed", False)
    if args.local_only and distributed:
        raise RuntimeError("Cannot specify both --local-only and --distributed flags")

    # User explicitly forced distributed mode
    if distributed:
        logger.info("Distributed mode explicitly requested via --distributed flag")

        # Validate Redis host is configured
        if not config.redis_host or config.redis_host == "none":
            raise RuntimeError(
                "Cannot use distributed mode: No Redis host configured. "
                "Please set 'redis_host' in experiment config or remove --distributed flag."
            )

        # Validate Redis is available
        if not check_redis_available(config.redis_host):
            raise RuntimeError(
                f"Cannot use distributed mode: Redis not available at {config.redis_host}. "
                "Please ensure Redis server is running or remove --distributed flag."
            )

        logger.info(f"Forcing distributed mode with Redis at {config.redis_host}")
        return True

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
        logger.warning(
            f"Redis not available at {config.redis_host}, falling back to local mode"
        )
        return False

    # Multiple jobs and Redis available - use distributed
    logger.info(
        f"Multiple jobs detected ({total_jobs} jobs total), using distributed mode"
    )
    return True


def enhance_config_with_cli_args(
    config: ExperimentConfig, args: argparse.Namespace
) -> ExperimentConfig:
    """Enhance config with CLI arguments (highest precedence).

    CLI arguments override config file values.

    Args:
        config: Experiment configuration
        args: Parsed CLI arguments

    Returns:
        Enhanced experiment configuration
    """
    # Collect overrides
    overrides = {}

    # Override with CLI arguments (highest precedence)
    if args.oss_fuzz_path:
        overrides["oss_fuzz_path"] = args.oss_fuzz_path
        logger.info(f"Using oss-fuzz path from CLI: {args.oss_fuzz_path}")

    if args.registry_dir:
        overrides["registry_dir"] = args.registry_dir
        logger.info(f"Using registry directory from CLI: {args.registry_dir}")

    if args.crs_configs_dir:
        overrides["crs_configs_dir"] = args.crs_configs_dir
        logger.info(f"Using CRS configs directory from CLI: {args.crs_configs_dir}")

    if args.benchmarks_root:
        overrides["benchmarks_root"] = args.benchmarks_root
        logger.info(f"Using benchmarks root from CLI: {args.benchmarks_root}")

    if args.benchmark_suites_root:
        overrides["benchmark_suites_root"] = args.benchmark_suites_root
        logger.info(
            f"Using benchmark suites root from CLI: {args.benchmark_suites_root}"
        )

    # Mode override
    if hasattr(args, "mode") and args.mode is not None:
        overrides["mode"] = args.mode
        logger.info(f"Using evaluation mode from CLI: {args.mode}")

    # Hint configuration overrides
    if args.hints_enabled:
        overrides["hints_enabled"] = True
        logger.info("Hints enabled via CLI")

    if args.hint_sarif_level is not None:
        overrides["hint_sarif_level"] = args.hint_sarif_level
        logger.info(f"Using SARIF hint level from CLI: {args.hint_sarif_level}")

    if args.hint_corpus_level is not None:
        overrides["hint_corpus_level"] = args.hint_corpus_level
        logger.info(f"Using corpus hint level from CLI: {args.hint_corpus_level}")

    # LiteLLM mode override
    if args.litellm_mode is not None:
        overrides["litellm_mode"] = args.litellm_mode
        logger.info(f"Using LiteLLM mode from CLI: {args.litellm_mode}")

    # Project image prefix override
    if args.project_image_prefix is not None:
        overrides["project_image_prefix"] = args.project_image_prefix
        logger.info(f"Using project image prefix from CLI: {args.project_image_prefix}")

    # Build workers override
    if hasattr(args, "build_workers") and args.build_workers is not None:
        overrides["build_workers"] = args.build_workers
        logger.info(f"Using build_workers from CLI: {args.build_workers}")

    # Verify workers override
    if hasattr(args, "verify_workers") and args.verify_workers is not None:
        overrides["verify_workers"] = args.verify_workers
        logger.info(f"Using verify_workers from CLI: {args.verify_workers}")

    # only_cpv_harnesses override
    if hasattr(args, "only_cpv_harnesses") and args.only_cpv_harnesses:
        overrides["only_cpv_harnesses"] = True
        logger.info("Using only_cpv_harnesses=True from CLI")

    return config.model_copy(update=overrides)


def run_experiment_local(
    experiment_name: str,
    config,
    benchmark_harnesses: List[BenchmarkHarness],
    crses: List[str],
    args: argparse.Namespace,
) -> None:
    """Run experiment locally without Redis queue.

    Executes all trials sequentially in the current process.

    Args:
        experiment_name: Experiment identifier
        config: Experiment configuration
        benchmark_harnesses: List of BenchmarkHarness objects
        crses: List of CRS identifiers
        args: CLI arguments for config overrides
    """
    log_section("Running CRSBench in Local Mode (No Redis)", width=60)

    # Resolve CRS paths with defaults
    registry_dir = Path(config.registry_dir or "crses/registry").resolve()
    crs_configs_dir = Path(config.crs_configs_dir or "crses/configs").resolve()

    # Generate trial matrix
    trials = generate_trial_matrix(
        benchmark_harnesses, crses, config, registry_dir, crs_configs_dir
    )

    # Dump trial matrix to JSON
    dump_trial_matrix(trials, config)

    logger.info(f"Total trials to execute: {len(trials)}")
    logger.info(f"CRSes: {', '.join(crses)}")
    logger.info(f"Benchmark-harness pairs: {len(benchmark_harnesses)}")
    logger.info(f"Trials per combination: {config.trials}")
    logger.info("=" * 60)

    # Skip variant builds for bug-fixing CRS (built on-demand during patch verification)
    if _is_all_bug_fixing_crs(trials, registry_dir, crs_configs_dir):
        logger.info(
            "Skipping upfront variant builds for bug-fixing CRS "
            "(PATCHED variants built on-demand during patch verification)"
        )
        build_results = {}
    else:
        build_results = build_variants_upfront(trials, config, args)

    # Check for critical build failures
    failed_builds = [
        name for name, result in build_results.items() if not result.success
    ]
    if failed_builds:
        logger.warning(
            f"{len(failed_builds)} variant builds failed. "
            "Trials requiring these variants may fail."
        )

    log_section("Executing Trials", width=60)

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

        # Enhance config with CLI arguments (highest precedence)
        enhanced_config = enhance_config_with_cli_args(config, args)

        result = run_crs_trial(
            crs=trial.crs,
            benchmark=bh.name,
            harness_name=bh.harness.name,
            harness_path=bh.harness.path,
            trial_num=trial.trial_num,
            config_dict=enhanced_config.model_dump(),
            mode=trial.mode,
        )

        results.append(result)

        # Log result and raise exception on failure
        if result.success:
            if result.crs_type == CRSType.BUG_FIXING:
                logger.info(
                    f"  ✓ Success: {result.patches_generated} patches generated, "
                    f"{result.patches_valid} valid"
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


def monitor_jobs(queue, job_list: List, experiment_name: str) -> List[TrialResult]:
    """Monitor job progress and display status.

    Args:
        queue: RQ queue instance
        job_list: List of enqueued RQ jobs
        experiment_name: Experiment identifier

    Returns:
        List of TrialResult objects
    """
    import importlib.util

    rich_available = importlib.util.find_spec("rich") is not None
    if not rich_available:
        logger.warning("Rich library not available, using basic progress display")

    if rich_available:
        return _monitor_jobs_rich(queue, job_list, experiment_name)
    return _monitor_jobs_basic(queue, job_list, experiment_name)


def _monitor_jobs_basic(
    queue, job_list: List, experiment_name: str
) -> List[TrialResult]:
    """Basic job monitoring without Rich UI."""
    from crsbench.distributed.queue import get_queue_stats
    from crsbench.evaluation.results import TrialMetadata

    logger.info(f"\nMonitoring {len(job_list)} jobs for experiment: {experiment_name}")

    while True:
        stats = get_queue_stats(queue)

        # Display stats
        log_section(f"Experiment: {experiment_name}", width=60)
        logger.info(f"Workers connected: {stats.get('workers', 0)}")
        log_summary(
            "Queue Status",
            {
                "queued": stats["queued"],
                "started": stats["started"],
                "finished": stats["finished"],
                "failed": stats["failed"],
            },
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
                    f"  [{worker_name}] [{crs}] {benchmark}/{harness} mode={mode} trial={trial_num} phase={phase} ({elapsed})"
                )

        if running_jobs:
            logger.info("Currently Running:")
            for line in running_jobs:
                logger.info(line)

        # Check if all jobs completed
        completed = 0
        failed = 0
        for job in job_list:
            job.refresh()
            if job.is_finished:
                completed += 1
            elif job.is_failed:
                failed += 1

        log_progress(
            completed + failed,
            len(job_list),
            f"Jobs complete ({completed} success, {failed} failed)",
        )

        if completed + failed >= len(job_list):
            break

        time.sleep(3)

    # Collect results
    results: List[TrialResult] = []
    for job in job_list:
        job.refresh()
        if job.result:
            results.append(job.result)
        elif job.is_failed:
            # Create TrialResult for failed jobs using job kwargs
            kwargs = job.kwargs or {}
            results.append(
                TrialResult(
                    crs=kwargs.get("crs", "unknown"),
                    benchmark=kwargs.get("benchmark", "unknown"),
                    harness=kwargs.get("harness_name", "unknown"),
                    trial_num=kwargs.get("trial_num", 0),
                    crs_type=CRSType.BUG_FINDING,
                    success=False,
                    execution_time=0.0,
                    error=f"Job failed: {job.exc_info}",
                    error_type="RQJobFailure",
                    report={},
                    metadata=TrialMetadata(timestamp_start=0.0, timestamp_end=0.0),
                )
            )

    return results


def _monitor_jobs_rich(
    queue, job_list: List, experiment_name: str
) -> List[TrialResult]:
    """Monitor jobs with Rich UI."""
    from rich.console import Console, Group
    from rich.live import Live
    from rich.table import Table

    from crsbench.distributed.queue import get_queue_stats

    console = Console()

    def generate_status_table():
        stats = get_queue_stats(queue)

        # Debug: log queue info
        logger.debug(f"Queue name: crsbench_{experiment_name}, stats: {stats}")

        # Queue status table
        table = Table(title=f"Experiment: {experiment_name}")
        table.add_column("Status", style="cyan")
        table.add_column("Count", justify="right", style="magenta")

        table.add_row("Queued", str(stats["queued"]))
        table.add_row("Started", str(stats["started"]))
        table.add_row("Finished", str(stats["finished"]), style="green")
        table.add_row("Failed", str(stats["failed"]), style="red")
        table.add_row("Total", str(len(job_list)))

        # Running jobs table
        running_table = Table(title="Running Jobs")
        running_table.add_column("Worker", style="green")
        running_table.add_column("CRS", style="cyan")
        running_table.add_column("Benchmark", style="yellow")
        running_table.add_column("Harness", style="yellow")
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
                    mode,
                    trial_num,
                    phase,
                    elapsed,
                )

        return Group(table, running_table)

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
    from crsbench.evaluation.results import TrialMetadata

    results: List[TrialResult] = []
    for job in job_list:
        job.refresh()
        if job.result:
            results.append(job.result)
        elif job.is_failed:
            # Create TrialResult for failed jobs using job kwargs
            kwargs = job.kwargs or {}
            results.append(
                TrialResult(
                    crs=kwargs.get("crs", "unknown"),
                    benchmark=kwargs.get("benchmark", "unknown"),
                    harness=kwargs.get("harness_name", "unknown"),
                    trial_num=kwargs.get("trial_num", 0),
                    crs_type=CRSType.BUG_FINDING,
                    success=False,
                    execution_time=0.0,
                    error=f"Job failed: {job.exc_info}",
                    error_type="RQJobFailure",
                    report={},
                    metadata=TrialMetadata(timestamp_start=0.0, timestamp_end=0.0),
                )
            )

    console.print("\n[green]✓[/green] All jobs completed!")
    return results


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
    print("  [c] Continue - skip existing, retry failed")  # noqa: T201
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


def get_crs_cpu_count(crs_name: str, crs_configs_dir: Path) -> int:
    """Get CPU count for a CRS from its resource config.

    Reads the CRS resource configuration file and parses the cpuset
    string to determine the number of CPUs required.

    Args:
        crs_name: CRS name (e.g., "crs-libfuzzer")
        crs_configs_dir: Path to CRS configs directory

    Returns:
        Number of CPUs (default: 4 if config not found)

    Examples:
        >>> get_crs_cpu_count("crs-libfuzzer", Path("/crses/configs"))
        16  # If cpuset is "0-15"
    """
    from crsbench.utils.cpu_pool import cpuset_count

    resource_config_path = crs_configs_dir / crs_name / "config-resource.yaml"
    if not resource_config_path.exists():
        logger.debug(
            f"No resource config found for {crs_name}, using default cpu_count=4"
        )
        return 4  # Default

    try:
        with resource_config_path.open() as f:
            config_data = yaml.safe_load(f)

        # Get cpuset from first worker (or specific worker)
        workers = config_data.get("workers", {})
        if workers:
            first_worker = list(workers.values())[0]
            cpuset_str = first_worker.get("cpuset", "0-3")
            cpu_count = cpuset_count(cpuset_str)
            logger.debug(
                f"CRS {crs_name}: parsed cpuset '{cpuset_str}' → {cpu_count} CPUs"
            )
            return cpu_count

        logger.debug(
            f"No workers section in resource config for {crs_name}, using default cpu_count=4"
        )
        return 4  # Default

    except Exception as e:
        logger.warning(
            f"Error reading resource config for {crs_name}: {e}, using default cpu_count=4"
        )
        return 4  # Default on error


def get_crs_memory(crs_name: str, crs_configs_dir: Path) -> str | None:
    """Get memory limit for a CRS from its resource config.

    Reads the CRS resource configuration file and returns the memory setting.

    Args:
        crs_name: CRS name (e.g., "crs-libfuzzer")
        crs_configs_dir: Path to CRS configs directory

    Returns:
        Memory string (e.g., "16G") or None if not found

    Examples:
        >>> get_crs_memory("crs-libfuzzer", Path("/crses/configs"))
        "16G"
    """
    from crsbench.utils.crs_helper import get_crs_worker_resources

    try:
        resources = get_crs_worker_resources(crs_name, crs_configs_dir)
        return resources.get("memory")
    except FileNotFoundError:
        logger.debug(f"No resource config found for {crs_name}, memory not specified")
        return None
    except Exception as e:
        logger.warning(f"Error reading resource config for {crs_name}: {e}")
        return None


def run_experiment_distributed(
    experiment_name: str,
    config: ExperimentConfig,
    benchmark_harnesses: List[BenchmarkHarness],
    crses: List[str],
    args: argparse.Namespace,
) -> None:
    """Run experiment using Redis queue-based distributed execution.

    Args:
        experiment_name: Experiment identifier
        config: Experiment configuration
        benchmark_harnesses: List of BenchmarkHarness objects
        crses: List of CRS identifiers
        args: CLI arguments for config overrides
    """
    from crsbench.distributed.queue import initialize_queue

    log_section("Running CRSBench in Distributed Mode (Redis)", width=60)

    # Validate redis_host is provided
    if not config.redis_host:
        raise ValueError("redis_host is required for distributed mode")

    logger.info(f"Redis host: {config.redis_host}")

    # Mark this process as orchestrator for logging
    os.environ["CRSBENCH_ORCHESTRATOR"] = "1"

    # Initialize queue
    queue = initialize_queue(config.redis_host, experiment_name)
    if queue is None:
        raise RuntimeError(f"Failed to initialize Redis queue at {config.redis_host}")

    # Resolve CRS paths with defaults
    registry_dir = Path(config.registry_dir or "crses/registry").resolve()
    crs_configs_dir = Path(config.crs_configs_dir or "crses/configs").resolve()

    # Check for existing jobs in queue (queue sanity check)
    from crsbench.distributed.queue import (
        clear_queue,
        get_existing_trials,
        handle_orphaned_jobs,
        requeue_failed_jobs,
    )

    existing = get_existing_trials(queue)
    has_existing = any(existing.values())

    # Get queue mode from CLI args or prompt if stale jobs exist
    queue_mode = args.queue_mode if hasattr(args, "queue_mode") else None

    if has_existing and queue_mode is None:
        # Stale jobs exist and no mode specified - prompt user
        queue_mode = prompt_queue_mode(existing)
        if queue_mode == "quit":
            logger.info("Aborted by user")
            return

    # Default to fresh if no existing jobs and no mode specified
    if queue_mode is None:
        queue_mode = "fresh"

    # TODO: too later to purge queue; as the old jobs are already taken by workers
    # Handle queue based on mode
    if queue_mode == "fresh":
        if has_existing:
            total_existing = sum(len(v) for v in existing.values())
            logger.warning(f"Purging {total_existing} existing jobs from queue")
            clear_queue(queue)
    elif queue_mode == "continue":
        # Handle orphaned started jobs (move to failed + retry)
        if existing["started"]:
            orphaned_count = handle_orphaned_jobs(queue, existing["started"])
            if orphaned_count > 0:
                logger.info(f"Handled {orphaned_count} orphaned jobs")

        # Requeue failed jobs (at end of queue - FIFO)
        if existing["failed"]:
            failed_count = requeue_failed_jobs(queue, list(existing["failed"].values()))
            if failed_count > 0:
                logger.info(f"Requeued {failed_count} failed jobs")

    # Generate trial matrix
    trials = generate_trial_matrix(
        benchmark_harnesses, crses, config, registry_dir, crs_configs_dir
    )

    # Filter trials if in continue mode
    if queue_mode == "continue" and has_existing:
        # Build set of existing trial keys
        existing_keys = set()
        for status_dict in existing.values():
            existing_keys.update(status_dict.keys())

        # Filter out existing trials
        original_count = len(trials)
        trials = [
            t
            for t in trials
            if f"{t.crs}:{t.benchmark_harness.name}:{t.benchmark_harness.harness.name}:{t.mode}:{t.trial_num}"
            not in existing_keys
        ]
        skipped_count = original_count - len(trials)
        if skipped_count > 0:
            logger.info(
                f"Skipping {skipped_count} existing trials, enqueueing {len(trials)} new trials"
            )

    # Dump trial matrix to JSON
    dump_trial_matrix(trials, config)

    logger.info(f"Total trials to enqueue: {len(trials)}")
    logger.info(f"CRSes: {', '.join(crses)}")
    logger.info(f"Benchmark-harness pairs: {len(benchmark_harnesses)}")
    logger.info(f"Trials per combination: {config.trials}")
    logger.info("=" * 60)

    # Skip variant builds for bug-fixing CRS (built on-demand during patch verification)
    if _is_all_bug_fixing_crs(trials, registry_dir, crs_configs_dir):
        logger.info(
            "Skipping upfront variant builds for bug-fixing CRS "
            "(PATCHED variants built on-demand during patch verification)"
        )
        build_results = {}
    else:
        # Build variants upfront (on coordinator machine)
        # Workers will use the pre-built variants from oss-fuzz/build/out/
        # TODO: workers on different machines
        build_results = build_variants_upfront(trials, config, args)

    # Check for critical build failures
    failed_builds = [
        name for name, result in build_results.items() if not result.success
    ]
    if failed_builds:
        logger.warning(
            f"{len(failed_builds)} variant builds failed. "
            "Trials requiring these variants may fail."
        )

    log_section("Enqueuing Trial Jobs", width=60)

    # Enqueue jobs
    logger.info("\nEnqueuing jobs...")

    # Enhance config with CLI arguments (highest precedence)
    enhanced_config = enhance_config_with_cli_args(config, args)

    # Get CPU counts for each CRS
    # Priority: experiment config > CRS resource config > default (4)
    # Note: crs_configs_dir already resolved at line 1490
    crs_cpu_counts = {}
    if config.resources and config.resources.cores_per_trial:
        # Use experiment-level resource config (highest priority)
        for crs in crses:
            crs_cpu_counts[crs] = config.resources.cores_per_trial
            logger.debug(
                f"CRS {crs} using experiment config: {crs_cpu_counts[crs]} CPUs"
            )
    else:
        # Fall back to CRS-specific resource configs
        for crs in crses:
            crs_cpu_counts[crs] = get_crs_cpu_count(crs, crs_configs_dir)
            logger.debug(f"CRS {crs} using CRS config: {crs_cpu_counts[crs]} CPUs")

    # Get memory limits for each CRS
    # Priority: experiment config > CRS resource config > None
    crs_memory_limits = {}
    if config.resources and config.resources.memory_per_trial:
        # Use experiment-level resource config (highest priority)
        for crs in crses:
            crs_memory_limits[crs] = config.resources.memory_per_trial
            logger.debug(
                f"CRS {crs} using experiment config: {crs_memory_limits[crs]} memory"
            )
    else:
        # Fall back to CRS-specific resource configs
        for crs in crses:
            crs_memory_limits[crs] = get_crs_memory(crs, crs_configs_dir)
            if crs_memory_limits[crs]:
                logger.debug(
                    f"CRS {crs} using CRS config: {crs_memory_limits[crs]} memory"
                )
            else:
                logger.debug(f"CRS {crs} has no memory limit configured")

    jobs = []
    for trial in trials:
        bh = trial.benchmark_harness
        cpu_count = crs_cpu_counts.get(trial.crs, 4)
        memory_limit = crs_memory_limits.get(trial.crs)

        job = queue.enqueue(
            "crsbench.distributed.jobs.run_crs_trial",
            crs=trial.crs,
            benchmark=bh.name,
            harness_name=bh.harness.name,
            harness_path=bh.harness.path,
            trial_num=trial.trial_num,
            config_dict=enhanced_config.model_dump(),
            mode=trial.mode,
            job_timeout=config.max_total_time,
            result_ttl=-1,  # Persist results forever
            meta={
                "crs": trial.crs,
                "benchmark": bh.name,
                "harness": bh.harness.name,
                "mode": trial.mode,
                "trial_num": trial.trial_num,
                "cpu_count": cpu_count,  # CPU count from resource config
                "memory_limit": memory_limit,  # Memory limit from resource config
            },
        )
        jobs.append(job)
        logger.debug(
            f"Enqueued job {job.id} for {trial.crs} on {bh.name}/{bh.harness.name} (trial {trial.trial_num})"
        )

    logger.info(f"✓ Enqueued {len(jobs)} jobs successfully")

    # Monitor progress
    logger.info("\nMonitoring job progress...")
    results = monitor_jobs(queue, jobs, experiment_name)

    # Generate final report
    log_section("Experiment Complete - Generating Report", width=60)

    generate_final_report(results, experiment_name, config)


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

    experiment_dir = Path(config.experiment_filestore) / experiment_name
    report_dir = Path(config.report_filestore) / experiment_name

    # Check if experiment directory exists
    if not experiment_dir.exists():
        logger.warning(
            f"Experiment directory not found: {experiment_dir}. "
            "Skipping HTML/JSON report generation."
        )
        return

    try:
        logger.info("\nGenerating HTML/JSON reports from snapshots...")

        generator = ReportGenerator(output_dir=report_dir)
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
    # Parse arguments
    args = parse_arguments()

    # Dispatch to appropriate command handler
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

    if args.command == "migrate":
        # Handle migrate command (conversion and generation tools)
        from crsbench.migration.cli.converter_command import run_migrate

        sys.exit(run_migrate(args))

    if args.command == "stats":
        # Handle stats command
        from crsbench.statistics.cli import run_stats

        sys.exit(run_stats(args))

    if args.command == "report":
        # Handle report command
        from crsbench.reporting.cli import run_report

        sys.exit(run_report(args))

    if args.command == "dashboard":
        # Handle dashboard command
        from crsbench.reporting.cli import run_dashboard

        sys.exit(run_dashboard(args))

    if args.command == "ci":
        # Handle ci command
        from crsbench.benchmark_ci.cli import run_ci

        sys.exit(run_ci(args))

    if args.command == "benchmark":
        # Handle benchmark command (bundle, validate, prepare-delta)
        from crsbench.benchmark.packaging.cli.benchmark_command import (
            run_benchmark_command,
        )

        sys.exit(run_benchmark_command(args))

    # Below is for 'run' command (experiment execution)

    # Set debug logging if requested
    if hasattr(args, "debug") and args.debug:
        configure_logger(level="DEBUG")
        logger.debug("Debug logging enabled")

    # Set gitcache mode
    if hasattr(args, "gitcache"):
        set_gitcache(args.gitcache)

    # Validate arguments
    validate_arguments(args)

    # Load and validate experiment configuration
    config_path = Path(args.experiment_config)
    config = load_experiment_config(config_path)

    # Resolve experiment name (CLI overrides config)
    experiment_name = (
        args.experiment_name if args.experiment_name else config.experiment
    )
    logger.info("=" * 60)
    logger.info("CRSBench Experiment Runner")
    logger.info("=" * 60)
    logger.info(f"Experiment name: {experiment_name}")
    if args.experiment_name:
        logger.info(f"  (overridden from CLI, config has: {config.experiment})")
    logger.info(f"Configuration file: {args.experiment_config}")

    # Resolve CRSes (CLI overrides config)
    if args.crses:
        crses = parse_list_argument(args.crses)
        logger.info(f"CRSes ({len(crses)}): {', '.join(crses)}")
        logger.info(f"  (overridden from CLI, config has: {', '.join(config.crses)})")
    else:
        crses = config.crses
        logger.info(f"CRSes ({len(crses)}): {', '.join(crses)}")

    # Resolve benchmarks (CLI has highest priority):
    # --benchmarks > --benchmark-suite > config.benchmarks > config.benchmark_suite
    try:
        if args.benchmarks and args.benchmark_suite:
            logger.error("Cannot specify both --benchmarks and --benchmark-suite")
            sys.exit(1)

        if args.benchmarks:
            # CLI --benchmarks has highest priority
            from crsbench.validation.schemas import BenchmarkEntry

            benchmark_names = parse_list_argument(args.benchmarks)
            # Convert to BenchmarkEntry objects (no harness specified = all harnesses)
            benchmark_entries = [
                BenchmarkEntry(name=name, harnesses=None) for name in benchmark_names
            ]
            logger.info(
                f"Benchmarks ({len(benchmark_names)}): {', '.join(benchmark_names)}"
            )
            logger.info("  (overridden from CLI --benchmarks)")
        elif args.benchmark_suite:
            # CLI --benchmark-suite has second priority
            import yaml

            from crsbench.validation.schemas import BenchmarkSuiteConfig

            # Use configured benchmark suites root or default
            benchmark_suites_root = Path(
                config.benchmark_suites_root or "benchmark-suites"
            )
            suite_path = benchmark_suites_root / f"{args.benchmark_suite}.yaml"
            if not suite_path.exists():
                logger.error(f"Benchmark suite file not found: {suite_path}")
                sys.exit(1)

            with suite_path.open() as f:
                suite_data = yaml.safe_load(f)
            suite_config = BenchmarkSuiteConfig(**suite_data)
            benchmark_entries = suite_config.get_benchmark_entries()
            benchmark_names = suite_config.get_benchmark_names()
            logger.info(f"Benchmark suite: {args.benchmark_suite}")
            logger.info(
                f"Benchmarks ({len(benchmark_names)}): {', '.join(benchmark_names)}"
            )
            logger.info("  (overridden from CLI --benchmark-suite)")
        else:
            # Use config (benchmarks or benchmark_suite)
            benchmark_entries = config.get_benchmark_entries()
            benchmark_names = config.get_benchmark_list()
            if config.benchmark_suite:
                logger.info(f"Benchmark suite: {config.benchmark_suite}")
                logger.info(
                    f"Benchmarks ({len(benchmark_names)}): {', '.join(benchmark_names)}"
                )
            else:
                logger.info(
                    f"Benchmarks ({len(benchmark_names)}): {', '.join(benchmark_names)}"
                )

    except ValueError as e:
        logger.error(f"Failed to resolve benchmarks: {e}")
        sys.exit(1)

    logger.info("=" * 60)

    # Resolve BenchmarkHarness objects
    try:
        benchmarks_root = Path(config.benchmarks_root or "benchmarks")
        benchmark_harnesses = resolve_benchmark_harnesses(
            benchmark_entries, benchmarks_root
        )
        logger.info(f"Resolved {len(benchmark_harnesses)} benchmark-harness pairs")
    except Exception as e:
        logger.error(f"Failed to resolve harnesses: {e}")
        sys.exit(1)

    # Calculate total jobs
    total_jobs = len(benchmark_harnesses) * len(crses) * config.trials
    logger.info(f"Total jobs to execute: {total_jobs}")

    # Determine execution mode
    use_distributed = should_use_distributed_mode(args, config, total_jobs)

    # Run experiment in appropriate mode
    if use_distributed:
        run_experiment_distributed(
            experiment_name, config, benchmark_harnesses, crses, args
        )
    else:
        run_experiment_local(experiment_name, config, benchmark_harnesses, crses, args)


if __name__ == "__main__":
    main()
