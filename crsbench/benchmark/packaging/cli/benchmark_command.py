"""CLI commands for benchmark management operations.

Provides:
- crsbench benchmark validate <path> - Validate benchmark structure
- crsbench benchmark bundle <path> - Create pkgs/ tarball
- crsbench benchmark bundle-all <dir> - Bundle all benchmarks in directory
- crsbench benchmark prepare-delta <path> - Generate ref.diff
- crsbench benchmark inject-canary <dir> --filter <pattern> - Add canary for contamination detection
- crsbench benchmark list-canaries - List registered canary UUIDs
- crsbench benchmark image build|push|prepare|check - Manage inc-build images
- crsbench benchmark dedup-povs <path> - Deduplicate POVs by crash signature
- crsbench benchmark import-povs <path> --source-dir <dir> - Import discovered POVs as variants
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


def _positive_int(value: str) -> int:
    """argparse type: strictly positive integer."""
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError(f"must be > 0 (got {value})")
    return parsed


def add_benchmark_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Add 'benchmark' subcommand with its subcommands.

    Args:
        subparsers: Parent subparsers to add to
    """
    benchmark_parser = subparsers.add_parser(
        "benchmark",
        help="Benchmark management commands",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Validate benchmark structure
  %(prog)s validate ./benchmarks/afc-curl-delta-01

  # Create pkgs/ tarball for benchmark
  %(prog)s bundle ./benchmarks/afc-curl-delta-01

  # Generate ref.diff for delta-mode benchmark
  %(prog)s prepare-delta ./benchmarks/afc-curl-delta-01
        """,
    )

    benchmark_subparsers = benchmark_parser.add_subparsers(
        dest="benchmark_command",
        help="Benchmark operations",
    )

    # crsbench benchmark validate
    validate_parser = benchmark_subparsers.add_parser(
        "validate",
        help="Validate benchmark structure and format",
    )
    validate_parser.add_argument(
        "benchmark_path",
        type=str,
        help="Path to benchmark directory",
    )
    validate_parser.set_defaults(func=handle_validate)

    # crsbench benchmark bundle
    bundle_parser = benchmark_subparsers.add_parser(
        "bundle",
        help="Create pkgs/ tarball for benchmark",
    )
    bundle_parser.add_argument(
        "benchmark_path",
        type=str,
        help="Path to benchmark directory",
    )
    bundle_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing pkgs/ directory",
    )
    bundle_parser.set_defaults(func=handle_bundle)

    # crsbench benchmark bundle-all
    bundle_all_parser = benchmark_subparsers.add_parser(
        "bundle-all",
        help="Bundle all benchmarks in a directory",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Bundle all benchmarks (skip existing)
  %(prog)s benchmarks/

  # Force re-bundle all benchmarks
  %(prog)s benchmarks/ --force

  # Bundle with parallel workers
  %(prog)s benchmarks/ --workers 8

  # Filter by glob pattern
  %(prog)s benchmarks/ --filter "afc-*"
        """,
    )
    bundle_all_parser.add_argument(
        "benchmarks_dir",
        type=str,
        help="Directory containing benchmarks",
    )
    bundle_all_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing pkgs/ directories",
    )
    bundle_all_parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Number of parallel workers (default: 4)",
    )
    bundle_all_parser.add_argument(
        "--filter",
        type=str,
        default=None,
        help="Filter benchmarks by glob pattern (e.g., 'afc-*')",
    )
    bundle_all_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be bundled without actually bundling",
    )
    bundle_all_parser.set_defaults(func=handle_bundle_all)

    # crsbench benchmark prepare-delta
    delta_parser = benchmark_subparsers.add_parser(
        "prepare-delta",
        help="Generate ref.diff for delta-mode benchmark",
    )
    delta_parser.add_argument(
        "benchmark_path",
        type=str,
        help="Path to benchmark directory",
    )
    delta_parser.set_defaults(func=handle_prepare_delta)

    # crsbench benchmark inject-canary
    canary_parser = benchmark_subparsers.add_parser(
        "inject-canary",
        help="Add canary string for contamination detection (BIG-bench style)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Injects canary strings into .aixcc/ files for contamination detection.
All benchmarks matching --filter get the SAME UUID (per-prefix grouping).

Files injected:
  - .aixcc/meta.yaml
  - .aixcc/**/vuln.yaml
  - .aixcc/ref.diff (ground truth patch)
  - .aixcc/**/*.patch, .aixcc/**/*.diff

Examples:
  # Inject canary into all atlanta-* benchmarks (same UUID for all)
  %(prog)s benchmarks/ --filter "atlanta-*"

  # Inject into afc-* benchmarks (different UUID from atlanta-*)
  %(prog)s benchmarks/ --filter "afc-*"

  # Use a specific UUID
  %(prog)s benchmarks/ --filter "sanity-*" --uuid 12345678-1234-5678-1234-567812345678

  # Force re-inject (overwrites existing)
  %(prog)s benchmarks/ --filter "sanity-*" --force

Registry stored at: ./canary-registry.json (repo root)
        """,
    )
    canary_parser.add_argument(
        "benchmarks_dir",
        type=str,
        help="Directory containing benchmarks",
    )
    canary_parser.add_argument(
        "--filter",
        type=str,
        required=True,
        help="Filter benchmarks by glob pattern (e.g., 'atlanta-*')",
    )
    canary_parser.add_argument(
        "--uuid",
        type=str,
        default=None,
        help="Use a specific UUID instead of generating one",
    )
    canary_parser.add_argument(
        "--registry",
        type=str,
        default=None,
        help="Path to canary registry file (default: ./canary-registry.json)",
    )
    canary_parser.add_argument(
        "--force",
        action="store_true",
        help="Force re-inject canary even if one exists",
    )
    canary_parser.set_defaults(func=handle_inject_canary)

    # crsbench benchmark list-canaries
    list_canary_parser = benchmark_subparsers.add_parser(
        "list-canaries",
        help="List registered canary UUIDs by prefix",
    )
    list_canary_parser.add_argument(
        "--registry",
        type=str,
        default=None,
        help="Path to canary registry file (default: ./canary-registry.json)",
    )
    list_canary_parser.set_defaults(func=handle_list_canaries)

    # crsbench benchmark seed-import
    seed_import_parser = benchmark_subparsers.add_parser(
        "seed-import",
        help="Import seed corpus from experiment output into benchmark directories",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Imports seed corpus files from one or more trial directories and stores them
deduplicated (by content hash) in each benchmark's
.aixcc/{harness}/corpus/ directory alongside a manifest.json.

Discovery walks every trial-*/output/seeds/ (or legacy trial-*/output/corpus/)
under the experiment directory. The benchmark/harness for each trial is taken
from metadata.json, config.yaml, or inferred from the path layout
<project>/<harness>/<mode>/<sanitizer>/trial-N.

Default behavior merges new files into any existing corpus directory.
Pass --force to replace the directory instead.

Output structure:
  benchmarks/<project>/.aixcc/<harness>/corpus/
  ├── manifest.json     # {total_files, source_trials, files}
  ├── {hash1}           # Seed file (named by SHA256 content hash)
  ├── {hash2}
  └── ...

Examples:
  # Single experiment (one benchmark/harness pair)
  %(prog)s ./experiment-output/

  # Import every (benchmark, harness) pair under a combined tree
  %(prog)s /data/final-combined/atlantis-multilang-given_fuzzer --all

  # Restrict to one benchmark + harness
  %(prog)s ./experiment-output/ --all \\
    --benchmark afc-curl-delta-02 --harness curl_fuzzer_ws

  # Replace existing corpus instead of merging
  %(prog)s ./experiment-output/ --force

  # Preview what would be imported without touching the filesystem
  %(prog)s /data/final-combined/... --all --dry-run
        """,
    )
    seed_import_parser.add_argument(
        "experiment_dir",
        type=str,
        help="Path to experiment output directory",
    )
    seed_import_parser.add_argument(
        "--benchmarks",
        type=str,
        default="./benchmarks",
        help="Directory containing benchmarks (default: ./benchmarks)",
    )
    seed_import_parser.add_argument(
        "--all",
        action="store_true",
        dest="all_mode",
        help="Import every (benchmark, harness) group found under experiment_dir",
    )
    seed_import_parser.add_argument(
        "--benchmark",
        type=str,
        default=None,
        help="Only import trials whose benchmark matches this name",
    )
    seed_import_parser.add_argument(
        "--harness",
        type=str,
        default=None,
        help="Only import trials whose harness matches this name",
    )
    seed_import_parser.add_argument(
        "--force",
        action="store_true",
        help="Replace existing corpus directory instead of merging",
    )
    seed_import_parser.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Report what would be imported without writing anything to disk",
    )
    seed_import_parser.set_defaults(func=handle_seed_import)

    # crsbench benchmark image ...
    image_parser = benchmark_subparsers.add_parser(
        "image",
        help="Manage benchmark inc-build images",
    )
    image_subparsers = image_parser.add_subparsers(
        dest="image_command",
        help="Image lifecycle operations",
    )

    image_parent = argparse.ArgumentParser(add_help=False)
    image_parent.add_argument(
        "benchmarks_dir",
        type=str,
        nargs="?",
        default="./benchmarks",
        help="Directory containing benchmarks (default: ./benchmarks)",
    )
    image_parent.add_argument(
        "--filter",
        type=str,
        default=None,
        help="Filter benchmarks by glob pattern (e.g., 'afc-*')",
    )
    image_parent.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Number of parallel workers (default: 4)",
    )
    image_parent.add_argument(
        "--registry",
        type=str,
        default="ghcr.io/team-atlanta/crsbench",
        help="Registry namespace for remote inc images",
    )
    image_parent.add_argument(
        "--local-prefix",
        type=str,
        default="crsbench",
        help="Local namespace prefix for inc images",
    )

    image_build = image_subparsers.add_parser(
        "build",
        parents=[image_parent],
        help="Build inc-build images locally",
    )
    image_build.set_defaults(func=handle_image_build)

    image_push = image_subparsers.add_parser(
        "push",
        parents=[image_parent],
        help="Push local inc-build images to registry",
    )
    image_push.add_argument(
        "--push-timeout",
        type=int,
        default=600,
        help="Push timeout in seconds (default: 600)",
    )
    image_push.set_defaults(func=handle_image_push)

    image_prepare = image_subparsers.add_parser(
        "prepare",
        parents=[image_parent],
        help="Prepare images on demand (local -> pull -> local build fallback)",
    )
    image_prepare.add_argument(
        "--policy",
        choices=["auto", "pull_only", "build_only"],
        default="auto",
        help="Prepare policy (default: auto)",
    )
    image_prepare.add_argument(
        "--max-pull-bytes",
        type=int,
        default=10 * 1024 * 1024 * 1024,
        help="Skip pull if remote image exceeds this size in bytes",
    )
    image_prepare.add_argument(
        "--pull-timeout",
        type=int,
        default=300,
        help="Pull timeout in seconds (default: 300)",
    )
    image_prepare.set_defaults(func=handle_image_prepare)

    image_check = image_subparsers.add_parser(
        "check",
        parents=[image_parent],
        help="Check inc-build images (local vs remote digests by default)",
    )
    image_check.add_argument(
        "--local-only",
        action="store_true",
        help="Check only local image presence and skip remote registry digest lookups",
    )
    image_check.set_defaults(func=handle_image_check)

    # crsbench benchmark upload
    from crsbench.dataset.registry import get_dataset_names

    dataset_names = ", ".join(get_dataset_names())
    upload_parser = benchmark_subparsers.add_parser(
        "upload",
        help="Upload benchmark datasets to HuggingFace",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Available datasets: {dataset_names}

Examples:
  %(prog)s --dataset crsbench
  %(prog)s --dataset crsbench --benchmarks afc-curl-delta-01 afc-curl-delta-02
  %(prog)s --dataset crsbench --dry-run
  %(prog)s --dataset crsbench --benchmarks-dir ./benchmarks
  %(prog)s --dataset crsbench --prune  # also delete remote benchmarks absent locally
        """,
    )
    upload_parser.add_argument(
        "--dataset",
        type=str,
        required=True,
        choices=get_dataset_names(),
        help="Dataset to upload",
    )
    upload_parser.add_argument(
        "--benchmarks-dir",
        type=Path,
        default=Path("benchmarks"),
        help="Path to benchmarks directory (default: benchmarks/)",
    )
    upload_parser.add_argument(
        "--benchmarks",
        nargs="+",
        type=str,
        default=None,
        help="Specific benchmark names to upload (e.g., afc-curl-delta-01)",
    )
    upload_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only list what would be uploaded/pruned, don't actually upload",
    )
    upload_parser.add_argument(
        "--prune",
        action="store_true",
        help=(
            "Delete remote benchmark folders and manifest entries that are "
            "absent from the local benchmarks directory. Incompatible with "
            "--benchmarks because a subset upload does not represent the "
            "intended full state."
        ),
    )
    upload_parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable verbose/debug logging",
    )
    upload_parser.set_defaults(func=handle_upload)

    # crsbench benchmark dedup-povs
    dedup_povs_parser = benchmark_subparsers.add_parser(
        "dedup-povs",
        help="Deduplicate POVs by crash signature",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Scans .aixcc/{harness}/{cpv}/blobs/ and logs/ directories, parses crash
signatures from log files, and identifies duplicate POV variants.

pov_0 is always kept as ground truth. POVs with unparseable logs are
conservatively kept to avoid false positives.

Default mode is dry-run (report only). Use --no-dry-run to delete files.

Examples:
  # Dry-run: see what would be removed
  %(prog)s benchmarks/afc-libexif-delta-03

  # Actually delete duplicates
  %(prog)s benchmarks/afc-libexif-delta-03 --no-dry-run

  # Filter to specific harness and CPV
  %(prog)s benchmarks/afc-libexif-delta-03 --harness exif_from_data_fuzzer --cpv cpv_0

  # Use fewer stack frames for coarser grouping
  %(prog)s benchmarks/afc-libexif-delta-03 --top-n 3

  # Write JSON report
  %(prog)s benchmarks/afc-libexif-delta-03 --output report.json

  # Run across every benchmark under the benchmarks root (dry-run)
  %(prog)s --all

  # Run across a subset via glob filter and actually delete
  %(prog)s --all --filter 'afc-*' --no-dry-run
        """,
    )
    dedup_povs_parser.add_argument(
        "benchmark_path",
        type=str,
        nargs="?",
        default=None,
        help="Path to a single benchmark directory (omit when using --all)",
    )
    dedup_povs_parser.add_argument(
        "--all",
        action="store_true",
        help="Process every benchmark with .aixcc/ under the benchmarks root",
    )
    dedup_povs_parser.add_argument(
        "--filter",
        type=str,
        default=None,
        help="Glob pattern to filter benchmark names (with --all)",
    )
    dedup_povs_parser.add_argument(
        "--harness",
        type=str,
        default=None,
        help="Filter to a specific harness name",
    )
    dedup_povs_parser.add_argument(
        "--cpv",
        type=str,
        default=None,
        help="Filter to a specific CPV (e.g., cpv_0)",
    )
    dedup_povs_parser.add_argument(
        "--top-n",
        type=_positive_int,
        default=5,
        help="Stack frame depth for crash signature (default: 5)",
    )
    dedup_povs_parser.add_argument(
        "--no-dry-run",
        action="store_true",
        help="Actually delete duplicate files (default: dry-run / report only)",
    )
    dedup_povs_parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Write JSON report to file",
    )
    dedup_povs_parser.set_defaults(func=handle_dedup_povs)

    # crsbench benchmark import-povs
    import_povs_parser = benchmark_subparsers.add_parser(
        "import-povs",
        help="Import POVs found by a bug-finding CRS as new pov_N.blob variants",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Reads bug-finding output laid out as:

  <source-dir>/<benchmark>/<harness>/<build_mode>/<sanitizer>/trial-*/povs/cpvs/<cpv_id>/blobs/*.blob

For each (harness, cpv) of each selected benchmark, the discovered blobs
are deduplicated by SHA-256 against existing pov_*.blob files in
.aixcc/<harness>/<cpv_id>/blobs/ and added as new pov_N.blob (next free
index). Existing POVs (including pov_0) are never modified.

Examples:
  # Dry-run: see what would be imported into one benchmark
  %(prog)s benchmarks/atlanta-libcue-delta-01 \\
      --source-dir /data/.../atlantis-multilang-given_fuzzer

  # Apply changes; cap to 5 new variants per CPV
  %(prog)s benchmarks/atlanta-libcue-delta-01 \\
      --source-dir /data/.../atlantis-multilang-given_fuzzer \\
      --max-povs-per-cpv 5 --apply

  # Import for every benchmark in the source root
  %(prog)s --all \\
      --source-dir /data/.../atlantis-multilang-given_fuzzer \\
      --max-povs-per-cpv 10 --apply
        """,
    )
    import_povs_parser.add_argument(
        "benchmark_path",
        type=str,
        nargs="?",
        default=None,
        help="Path to a single benchmark directory (omit when using --all)",
    )
    import_povs_parser.add_argument(
        "--source-dir",
        type=str,
        required=True,
        dest="source_dir",
        help="Bug-finding result root containing <benchmark>/<harness>/... subdirs",
    )
    import_povs_parser.add_argument(
        "--all",
        action="store_true",
        help="Process every benchmark present under --source-dir",
    )
    import_povs_parser.add_argument(
        "--filter",
        type=str,
        default=None,
        help="Glob pattern to filter benchmarks under --source-dir (with --all)",
    )
    import_povs_parser.add_argument(
        "--harness",
        type=str,
        default=None,
        help="Only import for this harness name",
    )
    import_povs_parser.add_argument(
        "--cpv",
        type=str,
        default=None,
        help="Only import for this CPV id (e.g., cpv_0)",
    )
    import_povs_parser.add_argument(
        "--max-povs-per-cpv",
        type=_positive_int,
        default=None,
        dest="max_povs_per_cpv",
        help="Maximum NEW POV variants to add per CPV (default: unlimited)",
    )
    import_povs_parser.add_argument(
        "--no-logs",
        action="store_true",
        dest="no_logs",
        help="Do not copy crash logs alongside as logs/pov_N.log",
    )
    import_povs_parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually copy files (default: dry-run / report only)",
    )
    import_povs_parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Write JSON report to this path",
    )
    import_povs_parser.set_defaults(func=handle_import_povs)

    # crsbench benchmark init
    init_parser = benchmark_subparsers.add_parser(
        "init",
        help="Build OSS-Fuzz discovery targets and generate .aixcc/meta.yaml",
    )
    init_parser.add_argument(
        "--experiment-config",
        type=str,
        required=True,
        help="Path to experiment config YAML (reads benchmarks + benchmarks_root)",
    )
    init_parser.add_argument(
        "--cpuset-cpus",
        type=str,
        default=None,
        help="Pin build to specific CPU cores (e.g., '0-7'). Default: use all cores.",
    )
    init_parser.set_defaults(func=handle_init)

    # crsbench benchmark migrate (nested subparser group)
    from crsbench.migration.cli.converter_command import register_migrate_subcommands

    register_migrate_subcommands(benchmark_subparsers)

    # crsbench benchmark stats (leaf subparser)
    from crsbench.statistics.cli import register_stats_subcommand

    register_stats_subcommand(benchmark_subparsers)

    # crsbench benchmark ci (nested subparser group)
    from crsbench.benchmark_ci.cli import register_ci_subcommands

    register_ci_subcommands(benchmark_subparsers)

    benchmark_parser.set_defaults(command="benchmark", func=handle_benchmark_help)


def handle_init(args: argparse.Namespace) -> int:
    """Handle 'crsbench benchmark init' command.

    Builds OSS-Fuzz discovery targets from an experiment config and
    generates .aixcc/meta.yaml for each. Prints a summary of discovered
    harnesses so the user can plan resource allocation before running.
    """
    from crsbench.benchmark.discovery import (
        auto_generate_meta_yaml,
        is_oss_fuzz_project,
    )
    from crsbench.evaluation.trial_paths import resolve_benchmarks_root
    from crsbench.run_experiment import load_experiment_config
    from crsbench.validation.ground_truth_paths import GroundTruthPaths

    config_path = Path(args.experiment_config)
    if not config_path.exists():
        logger.error(f"Config file not found: {config_path}")
        return 1

    config = load_experiment_config(config_path)
    benchmarks_root = resolve_benchmarks_root(config.benchmarks_root)
    oss_fuzz_path = config.oss_fuzz_path
    cpuset_cpus = args.cpuset_cpus

    benchmark_names = config.get_benchmark_list()
    if not benchmark_names:
        logger.error("No benchmarks specified in config")
        return 1

    total_harnesses = 0
    initialized = 0

    for name in benchmark_names:
        benchmark_path = benchmarks_root / name

        if not benchmark_path.exists():
            logger.warning(f"Benchmark directory not found: {benchmark_path}")
            continue

        # Skip benchmarks that already have meta.yaml
        meta_yaml = GroundTruthPaths(benchmark_path).meta_yaml
        if meta_yaml.exists():
            logger.info(f"[{name}] meta.yaml already exists, skipping")
            continue

        if not is_oss_fuzz_project(benchmark_path):
            logger.warning(
                f"[{name}] Not a valid OSS-Fuzz project "
                "(missing project.yaml/Dockerfile/build.sh), skipping"
            )
            continue

        try:
            result_path = auto_generate_meta_yaml(
                benchmark_path,
                oss_fuzz_path,
                cpuset_cpus=cpuset_cpus,
            )

            # Count harnesses from generated meta.yaml
            import yaml

            with result_path.open() as f:
                meta = yaml.safe_load(f)
            harness_count = len(meta.get("harness_files", []))
            total_harnesses += harness_count
            initialized += 1

            logger.info(f"[{name}] Initialized: {harness_count} harnesses")

        except Exception:
            logger.exception(f"[{name}] Failed to initialize")
            continue

    # Print summary
    logger.info("=" * 60)
    logger.info(
        f"Initialized {initialized} benchmarks, {total_harnesses} total harnesses"
    )
    if config.trials and config.sanitizers:
        trial_count = total_harnesses * config.trials * len(config.sanitizers)
        logger.info(
            f"Estimated trials: {total_harnesses} harnesses × "
            f"{config.trials} trials × {len(config.sanitizers)} sanitizers = {trial_count}"
        )
    logger.info("=" * 60)

    return 0


def handle_benchmark_help(_args: argparse.Namespace) -> int:
    """Handle benchmark command without subcommand."""
    logger.error(
        "Please specify a subcommand. "
        "Run 'crsbench benchmark --help' for available subcommands."
    )
    return 1


def handle_validate(args: argparse.Namespace) -> int:
    """Handle 'crsbench benchmark validate' command."""
    from crsbench.benchmark.packaging.validate import validate_benchmark

    benchmark_path = Path(args.benchmark_path)

    if not benchmark_path.exists():
        logger.error(f"Benchmark path not found: {benchmark_path}")
        return 1

    result = validate_benchmark(benchmark_path)

    if result.errors:
        for error in result.errors:
            logger.error(f"ERROR: {error}")

    if result.warnings:
        for warning in result.warnings:
            logger.warning(f"WARNING: {warning}")

    if result.valid:
        logger.info(f"Validation passed: {benchmark_path.name}")
        return 0
    logger.error(f"Validation failed: {benchmark_path.name}")
    return 1


def handle_bundle(args: argparse.Namespace) -> int:
    """Handle 'crsbench benchmark bundle' command."""
    from crsbench.benchmark.packaging.bundle import bundle_benchmark

    benchmark_path = Path(args.benchmark_path)

    if not benchmark_path.is_dir():
        logger.error(f"Benchmark not found: {benchmark_path}")
        return 1

    try:
        pkgs_dir = bundle_benchmark(benchmark_path, force=args.force)
        logger.info(f"Successfully bundled: {benchmark_path.name}")
        logger.info(f"Output: {pkgs_dir}")
        return 0
    except ValueError as e:
        logger.error(f"Bundle validation error: {e}")
        return 1
    except Exception as e:
        logger.error(f"Bundle failed: {e}")
        return 1


def handle_bundle_all(args: argparse.Namespace) -> int:
    """Handle 'crsbench benchmark bundle-all' command."""
    import fnmatch

    from crsbench.benchmark.packaging.bundle import bundle_benchmark

    benchmarks_dir = Path(args.benchmarks_dir)

    if not benchmarks_dir.is_dir():
        logger.error(f"Benchmarks directory not found: {benchmarks_dir}")
        return 1

    # Discover all benchmarks
    benchmarks = []
    for path in sorted(benchmarks_dir.iterdir()):
        if not path.is_dir():
            continue
        # Skip hidden directories
        if path.name.startswith("."):
            continue
        # Check for .aixcc directory (indicates a benchmark)
        if not (path / ".aixcc").exists():
            continue
        # Apply filter
        if args.filter and not fnmatch.fnmatch(path.name, args.filter):
            continue
        benchmarks.append(path)

    if not benchmarks:
        logger.warning(f"No benchmarks found in {benchmarks_dir}")
        return 0

    # Categorize benchmarks
    from crsbench.benchmark.packaging.workdir_parser import get_expected_source_dir

    to_bundle = []
    already_bundled = []
    for bench in benchmarks:
        pkgs_dir = bench / "pkgs"
        dockerfile = bench / "Dockerfile"

        # Determine expected tarball name (same logic as bundle_benchmark)
        source_name = get_expected_source_dir(dockerfile)
        if not source_name:
            logger.error(f"Cannot parse WORKDIR from Dockerfile: {dockerfile}")
            return 1

        expected_tarball = pkgs_dir / f"{source_name}.tar.gz"
        if expected_tarball.exists() and not args.force:
            already_bundled.append(bench)
        else:
            to_bundle.append(bench)

    logger.info(f"Found {len(benchmarks)} benchmarks")
    logger.info(f"  Already bundled: {len(already_bundled)}")
    logger.info(f"  To bundle: {len(to_bundle)}")

    if args.dry_run:
        logger.info("\nDry run - would bundle:")
        for bench in to_bundle:
            logger.info(f"  {bench.name}")
        return 0

    if not to_bundle:
        logger.info("Nothing to bundle")
        return 0

    # Bundle in parallel
    results: dict[str, str] = {}  # name -> status
    total = len(to_bundle)
    completed_count = 0

    def bundle_one(bench_path: Path) -> tuple[str, str]:
        try:
            bundle_benchmark(bench_path, force=args.force)
            return bench_path.name, "success"
        except Exception as e:
            return bench_path.name, f"failed: {e}"

    logger.info(f"Bundling {total} benchmarks with {args.workers} workers...")

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(bundle_one, bench): bench for bench in to_bundle}

        for future in as_completed(futures):
            name, status = future.result()
            results[name] = status
            completed_count += 1
            progress = f"[{completed_count}/{total}]"
            if status == "success":
                logger.info(f"{progress} OK: {name}")
            else:
                logger.error(f"{progress} FAILED: {name} - {status}")

    # Summary
    success_count = sum(1 for s in results.values() if s == "success")
    failed_count = len(results) - success_count

    logger.info("\n" + "=" * 50)
    logger.info("BUNDLE SUMMARY")
    logger.info("=" * 50)
    logger.info(f"Total: {len(benchmarks)}")
    logger.info(f"Already bundled: {len(already_bundled)}")
    logger.info(f"Bundled: {success_count}")
    logger.info(f"Failed: {failed_count}")

    if failed_count > 0:
        logger.info("\nFailed benchmarks:")
        for name, status in results.items():
            if status != "success":
                logger.info(f"  {name}: {status}")
        return 1

    return 0


def handle_prepare_delta(args: argparse.Namespace) -> int:
    """Handle 'crsbench benchmark prepare-delta' command."""
    from crsbench.benchmark.packaging.bundle import prepare_delta

    benchmark_path = Path(args.benchmark_path)

    if not benchmark_path.is_dir():
        logger.error(f"Benchmark not found: {benchmark_path}")
        return 1

    try:
        ref_diff_path = prepare_delta(benchmark_path)
        logger.info(f"Successfully generated: {ref_diff_path}")
        return 0
    except ValueError as e:
        logger.error(f"Prepare-delta error: {e}")
        return 1
    except Exception as e:
        logger.error(f"Prepare-delta failed: {e}")
        return 1


def handle_dedup_povs(args: argparse.Namespace) -> int:
    """Handle 'crsbench benchmark dedup-povs' command."""
    import fnmatch as _fn
    import json as _json

    from crsbench.benchmark.packaging.dedup_povs import dedup_benchmark_povs

    if args.all:
        if args.benchmark_path:
            benchmarks_root = Path(args.benchmark_path)
        else:
            benchmarks_root = _benchmarks_root_from_cwd()
        if not benchmarks_root.is_dir():
            logger.error(f"Benchmarks root not found: {benchmarks_root}")
            return 1

        candidates = sorted(p for p in benchmarks_root.iterdir() if p.is_dir())
        if args.filter:
            candidates = [p for p in candidates if _fn.fnmatch(p.name, args.filter)]
        benchmark_paths = [p for p in candidates if (p / ".aixcc").is_dir()]
        if not benchmark_paths:
            logger.error(f"No benchmarks with .aixcc/ found under {benchmarks_root}")
            return 1
    else:
        if not args.benchmark_path:
            logger.error("Provide a benchmark path or pass --all")
            return 1
        if args.filter:
            logger.error("--filter only applies with --all")
            return 1
        bp = Path(args.benchmark_path)
        if not bp.is_dir():
            logger.error(f"Benchmark not found: {bp}")
            return 1
        benchmark_paths = [bp]

    dry_run = not args.no_dry_run
    mode = "DRY RUN" if dry_run else "LIVE"
    logger.info(f"Deduplicating POVs in {len(benchmark_paths)} benchmark(s) [{mode}]")

    summaries = []
    failures: list[tuple[str, str]] = []
    for bp in benchmark_paths:
        try:
            summary = dedup_benchmark_povs(
                bp,
                harness_filter=args.harness,
                cpv_filter=args.cpv,
                top_n=args.top_n,
                dry_run=dry_run,
            )
        except ValueError as e:
            logger.error(f"[{bp.name}] {e}")
            failures.append((bp.name, str(e)))
            continue
        summaries.append(summary)

    # Per-benchmark lines (only when running in --all mode)
    if args.all:
        action = "would remove" if dry_run else "removed"
        for s in summaries:
            logger.info(
                f"[{s.benchmark}] {s.total_povs} POVs, "
                f"{s.total_kept} kept, {s.total_removed} {action}"
            )

    # Aggregate summary
    total_povs = sum(s.total_povs for s in summaries)
    total_kept = sum(s.total_kept for s in summaries)
    total_removed = sum(s.total_removed for s in summaries)

    logger.info("\n" + "=" * 50)
    logger.info("DEDUP SUMMARY")
    logger.info("=" * 50)
    action = "Would remove" if dry_run else "Removed"
    logger.info(f"{'Benchmarks processed:':<22}{len(summaries)}")
    if failures:
        logger.info(f"{'Benchmarks failed:':<22}{len(failures)}")
    logger.info(f"{'Total POVs:':<22}{total_povs}")
    logger.info(f"{'Kept:':<22}{total_kept}")
    logger.info(f"{action + ':':<22}{total_removed}")

    if dry_run and total_removed > 0:
        logger.info("\nRun with --no-dry-run to actually delete files.")

    # Write JSON report if requested
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        if args.all:
            payload = {
                "benchmarks": [s.to_dict() for s in summaries],
                "totals": {
                    "benchmarks_processed": len(summaries),
                    "benchmarks_failed": len(failures),
                    "total_povs": total_povs,
                    "total_kept": total_kept,
                    "total_removed": total_removed,
                },
            }
            args.output.write_text(_json.dumps(payload, indent=2))
        else:
            args.output.write_text(summaries[0].to_json())
        logger.info(f"\nReport written to: {args.output}")

    return 1 if failures else 0


def handle_import_povs(args: argparse.Namespace) -> int:
    """Handle 'crsbench benchmark import-povs' command."""
    from crsbench.benchmark.packaging.import_povs import (
        import_many,
        summaries_to_json,
    )

    source_root = Path(args.source_dir)
    if not source_root.is_dir():
        logger.error(f"Source dir not found: {source_root}")
        return 1

    if args.all:
        if args.benchmark_path:
            logger.error("Pass either a benchmark path or --all, not both")
            return 1
        import fnmatch as _fn

        candidates = sorted(p for p in source_root.iterdir() if p.is_dir())
        if args.filter:
            candidates = [p for p in candidates if _fn.fnmatch(p.name, args.filter)]
        # Map source-dir benchmark names to actual benchmark paths
        # under benchmarks/ (skip ones that don't exist locally).
        benchmarks_root = _benchmarks_root_from_cwd()
        benchmark_paths: list[Path] = []
        for cand in candidates:
            bp = benchmarks_root / cand.name
            if bp.is_dir() and (bp / ".aixcc").is_dir():
                benchmark_paths.append(bp)
            else:
                logger.warning(f"Skipping {cand.name}: no matching benchmark at {bp}")
        if not benchmark_paths:
            logger.error("No matching benchmarks found")
            return 1
    else:
        if not args.benchmark_path:
            logger.error("Provide a benchmark path or pass --all")
            return 1
        bp = Path(args.benchmark_path)
        if not bp.is_dir():
            logger.error(f"Benchmark not found: {bp}")
            return 1
        benchmark_paths = [bp]

    dry_run = not args.apply
    mode = "DRY RUN" if dry_run else "APPLY"
    logger.info(
        f"Importing POVs from {source_root} into "
        f"{len(benchmark_paths)} benchmark(s) [{mode}]"
    )

    summaries = import_many(
        benchmark_paths,
        source_root,
        max_new_povs_per_cpv=args.max_povs_per_cpv,
        harness_filter=args.harness,
        cpv_filter=args.cpv,
        copy_logs=not args.no_logs,
        dry_run=dry_run,
    )

    total_imported = sum(s.total_imported for s in summaries)
    total_dups = sum(s.total_duplicates for s in summaries)
    total_capped = sum(s.total_capacity_skipped for s in summaries)
    total_candidates = sum(s.total_candidates for s in summaries)

    logger.info("\n" + "=" * 50)
    logger.info("IMPORT SUMMARY")
    logger.info("=" * 50)
    logger.info(f"Benchmarks processed:  {len(summaries)}")
    logger.info(f"Candidates discovered: {total_candidates}")
    logger.info(
        f"{'Would import' if dry_run else 'Imported'}:           {total_imported}"
    )
    logger.info(f"Skipped (duplicate):   {total_dups}")
    logger.info(f"Skipped (max-cap):     {total_capped}")
    if dry_run and total_imported > 0:
        logger.info("\nRun with --apply to actually copy files.")

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(summaries_to_json(summaries))
        logger.info(f"\nReport written to: {args.output}")

    return 0


def _benchmarks_root_from_cwd() -> Path:
    """Resolve benchmarks/ root by walking up from the working directory.

    Falls back to ``./benchmarks`` if the lookup fails.
    """
    import os

    if "BENCHMARKS_ROOT" in os.environ:
        return Path(os.environ["BENCHMARKS_ROOT"])
    cwd = Path.cwd()
    for parent in [cwd, *cwd.parents]:
        candidate = parent / "benchmarks"
        if candidate.is_dir():
            return candidate
    return cwd / "benchmarks"


def handle_inject_canary(args: argparse.Namespace) -> int:
    """Handle 'crsbench benchmark inject-canary' command.

    Injects canary strings into all benchmarks matching --filter.
    All matching benchmarks get the SAME UUID (per-prefix grouping).
    """
    import uuid

    from crsbench.benchmark.canary.generator import inject_canaries_by_prefix

    benchmarks_dir = Path(args.benchmarks_dir)

    if not benchmarks_dir.is_dir():
        logger.error(f"Benchmarks directory not found: {benchmarks_dir}")
        return 1

    # Parse optional UUID
    canary_uuid = None
    if args.uuid:
        try:
            canary_uuid = uuid.UUID(args.uuid)
        except ValueError:
            logger.error(f"Invalid UUID format: {args.uuid}")
            return 1

    # Parse optional registry path
    registry_path = Path(args.registry) if args.registry else None

    try:
        result = inject_canaries_by_prefix(
            benchmarks_dir,
            args.filter,
            canary_uuid=canary_uuid,
            registry_path=registry_path,
            force=args.force,
        )

        if result.matched_count == 0:
            logger.warning(f"No benchmarks found matching filter: {args.filter}")
            return 0

        # Summary
        logger.info("=" * 50)
        logger.info("CANARY INJECTION SUMMARY")
        logger.info("=" * 50)
        logger.info(f"Filter: {args.filter}")
        logger.info(f"UUID: {result.canary_uuid}")
        logger.info(f"Benchmarks matched: {result.matched_count}")
        logger.info(f"Files injected: {result.injected_count}")
        logger.info(f"Benchmarks skipped (existing): {result.skipped_count}")
        logger.info(f"Benchmarks processed: {len(result.benchmarks)}")

        if result.benchmarks:
            logger.info("\nProcessed benchmarks:")
            for name in result.benchmarks:
                logger.info(f"  - {name}")

        return 0

    except Exception as e:
        logger.error(f"Canary injection failed: {e}")
        return 1


def handle_list_canaries(args: argparse.Namespace) -> int:
    """Handle 'crsbench benchmark list-canaries' command."""
    from crsbench.benchmark.canary.detector import list_registered_canaries

    # Parse optional registry path
    registry_path = Path(args.registry) if args.registry else None

    canaries = list_registered_canaries(registry_path)

    if not canaries:
        logger.info("No canaries registered yet.")
        logger.info("Use 'crsbench benchmark inject-canary' to inject canaries.")
        return 0

    logger.info("=" * 50)
    logger.info("REGISTERED CANARIES")
    logger.info("=" * 50)

    for prefix, uuid_val in sorted(canaries.items()):
        logger.info(f"  {prefix}: {uuid_val}")

    logger.info(f"\nTotal: {len(canaries)} prefix groups")

    return 0


def handle_seed_import(args: argparse.Namespace) -> int:
    """Handle 'crsbench benchmark seed-import' command.

    Imports seed corpus files from experiment output and stores them
    deduplicated in each benchmark's .aixcc/{harness}/corpus/ directory.
    """
    from crsbench.benchmark.seed import CorpusCollector

    experiment_dir = Path(args.experiment_dir)
    benchmarks_dir = Path(args.benchmarks)

    if not experiment_dir.is_dir():
        logger.error(f"Experiment directory not found: {experiment_dir}")
        return 1

    if not benchmarks_dir.is_dir():
        logger.error(f"Benchmarks directory not found: {benchmarks_dir}")
        return 1

    try:
        collector = CorpusCollector(experiment_dir, benchmarks_dir)
        results = collector.collect(
            force=args.force,
            all_mode=args.all_mode,
            benchmark_filter=args.benchmark,
            harness_filter=args.harness,
            dry_run=args.dry_run,
        )
    except ValueError as e:
        logger.error(f"Import error: {e}")
        return 1
    except FileNotFoundError as e:
        logger.error(f"Import error: {e}")
        return 1
    except Exception as e:
        logger.error(f"Seed import failed: {e}")
        return 1

    prefix = "[dry-run] " if args.dry_run else ""
    verb = "would import" if args.dry_run else "imported"
    logger.info("=" * 50)
    logger.info(f"{prefix}SEED CORPUS IMPORT SUMMARY")
    logger.info("=" * 50)
    for result in results:
        for warning in result.warnings:
            logger.warning(warning)
        logger.info(
            f"{prefix}{result.benchmark_name}/{result.harness_name}: "
            f"{result.total_files} files total "
            f"(+{result.new_files} new, from {result.source_trials} trial(s)) "
            f"-> {result.output_dir}"
        )
    logger.info(f"{prefix}{verb} {len(results)} benchmark/harness group(s)")
    if args.dry_run:
        logger.info("[dry-run] no changes written to disk")
    return 0


def _discover_inc_build_benchmarks(
    benchmarks_dir: Path,
    filter_pattern: str | None,
) -> list[tuple[str, list[str]]]:
    """Discover benchmarks with inc_build: true.

    Args:
        benchmarks_dir: Directory containing benchmarks
        filter_pattern: Optional glob pattern to filter benchmarks

    Returns:
        List of (project_name, sanitizers) tuples
    """
    import fnmatch

    import yaml

    results = []

    for path in sorted(benchmarks_dir.iterdir()):
        if not path.is_dir():
            continue
        # Skip hidden directories
        if path.name.startswith("."):
            continue
        # Check for .aixcc directory (indicates a benchmark)
        if not (path / ".aixcc").exists():
            continue
        # Apply filter
        if filter_pattern and not fnmatch.fnmatch(path.name, filter_pattern):
            continue

        # Load project.yaml
        project_yaml = path / "project.yaml"
        if not project_yaml.exists():
            continue

        try:
            with project_yaml.open() as f:
                project_data = yaml.safe_load(f)
        except Exception as e:
            logger.warning(f"Failed to parse {project_yaml}: {e}")
            continue

        # Check inc_build flag (default: True per ProjectConfig schema)
        inc_build = project_data.get("inc_build", True)
        if not inc_build:
            continue

        # Get sanitizers (default: address)
        sanitizers = project_data.get("sanitizers", ["address"])
        if sanitizers:
            results.append((path.name, sanitizers))

    return results


def _iter_inc_targets(args: argparse.Namespace) -> tuple[Path, list[tuple[str, str]]]:
    """Resolve benchmark targets into (project, sanitizer) tuples."""
    benchmarks_dir = Path(args.benchmarks_dir)
    if not benchmarks_dir.is_dir():
        raise ValueError(f"Benchmarks directory not found: {benchmarks_dir}")

    benchmarks = _discover_inc_build_benchmarks(benchmarks_dir, args.filter)
    targets: list[tuple[str, str]] = []
    for project_name, sanitizers in benchmarks:
        for sanitizer in sanitizers:
            targets.append((project_name, sanitizer))
    return benchmarks_dir, targets


def handle_image_build(args: argparse.Namespace) -> int:
    """Handle `crsbench benchmark image build`."""
    from crsbench.builder.infrastructure import OSSFuzzInfrastructure
    from crsbench.utils.run_helper import ensure_oss_fuzz_root

    try:
        benchmarks_dir, targets = _iter_inc_targets(args)
    except ValueError as e:
        logger.error(str(e))
        return 1
    if not targets:
        logger.warning(f"No inc-build enabled benchmarks found in {benchmarks_dir}")
        return 0

    results: dict[str, str] = {}
    total = len(targets)
    completed = 0
    infra = OSSFuzzInfrastructure(Path(ensure_oss_fuzz_root()))

    def build_one(target: tuple[str, str]) -> tuple[str, str]:
        project_name, sanitizer = target
        key = f"{project_name}:{sanitizer}"
        benchmark_path = benchmarks_dir / project_name
        infra.create_variant_project(
            benchmark_path=benchmark_path,
            variant_name=project_name,
        )
        ok = infra.build_inc_build_image(
            project_name=project_name,
            sanitizer=sanitizer,
            benchmark_path=benchmark_path,
            local_prefix=args.local_prefix,
        )
        return key, "built" if ok else "build_failed"

    logger.info(f"Building {total} image targets with {args.workers} workers...")

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(build_one, target): target for target in targets}

        for future in as_completed(futures):
            key, status = future.result()
            results[key] = status
            completed += 1

            project_name, sanitizer = key.split(":", 1)
            progress = f"[{completed}/{total}]"
            if status == "built":
                logger.info(f"{progress} BUILT: {project_name} ({sanitizer})")
            else:
                logger.warning(f"{progress} FAILED: {project_name} ({sanitizer})")

    built = sum(1 for s in results.values() if s == "built")
    failed = total - built
    logger.info(f"Build summary: total={total} built={built} failed={failed}")
    return 0 if failed == 0 else 1


def handle_image_push(args: argparse.Namespace) -> int:
    """Handle `crsbench benchmark image push`."""
    from crsbench.benchmark.packaging.docker_utils import push_inc_build_image

    try:
        benchmarks_dir, targets = _iter_inc_targets(args)
    except ValueError as e:
        logger.error(str(e))
        return 1
    if not targets:
        logger.warning(f"No inc-build enabled benchmarks found in {benchmarks_dir}")
        return 0

    results: dict[str, str] = {}
    total = len(targets)
    completed = 0

    def push_one(target: tuple[str, str]) -> tuple[str, str]:
        project_name, sanitizer = target
        key = f"{project_name}:{sanitizer}"
        ok = push_inc_build_image(
            project_name,
            sanitizer,
            registry=args.registry,
            local_prefix=args.local_prefix,
            timeout=args.push_timeout,
        )
        return key, "pushed" if ok else "push_failed"

    logger.info(f"Pushing {total} image targets with {args.workers} workers...")
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(push_one, target): target for target in targets}
        for future in as_completed(futures):
            key, status = future.result()
            results[key] = status
            completed += 1
            project_name, sanitizer = key.split(":", 1)
            progress = f"[{completed}/{total}]"
            if status == "pushed":
                logger.info(f"{progress} PUSHED: {project_name} ({sanitizer})")
            else:
                logger.warning(f"{progress} FAILED: {project_name} ({sanitizer})")

    pushed = sum(1 for s in results.values() if s == "pushed")
    failed = total - pushed
    logger.info(f"Push summary: total={total} pushed={pushed} failed={failed}")
    return 0 if failed == 0 else 1


def handle_image_prepare(args: argparse.Namespace) -> int:
    """Handle `crsbench benchmark image prepare`."""
    from crsbench.builder.infrastructure import OSSFuzzInfrastructure
    from crsbench.utils.run_helper import ensure_oss_fuzz_root

    try:
        benchmarks_dir, targets = _iter_inc_targets(args)
    except ValueError as e:
        logger.error(str(e))
        return 1
    if not targets:
        logger.warning(f"No inc-build enabled benchmarks found in {benchmarks_dir}")
        return 0

    infra = OSSFuzzInfrastructure(Path(ensure_oss_fuzz_root()))
    results: dict[str, str] = {}
    total = len(targets)
    completed = 0

    def prepare_one(target: tuple[str, str]) -> tuple[str, str]:
        project_name, sanitizer = target
        key = f"{project_name}:{sanitizer}"
        benchmark_path = benchmarks_dir / project_name
        infra.create_variant_project(
            benchmark_path=benchmark_path,
            variant_name=project_name,
        )
        ok = infra.ensure_inc_image(
            project_name=project_name,
            sanitizer=sanitizer,
            benchmark_path=benchmark_path,
            registry=args.registry,
            policy=args.policy,
            max_pull_bytes=args.max_pull_bytes,
            pull_timeout=args.pull_timeout,
            local_prefix=args.local_prefix,
        )
        return key, "ready" if ok else "unavailable"

    logger.info(f"Preparing {total} image targets with {args.workers} workers...")
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(prepare_one, target): target for target in targets}
        for future in as_completed(futures):
            key, status = future.result()
            results[key] = status
            completed += 1
            project_name, sanitizer = key.split(":", 1)
            progress = f"[{completed}/{total}]"
            if status == "ready":
                logger.info(f"{progress} READY: {project_name} ({sanitizer})")
            else:
                logger.warning(f"{progress} UNAVAILABLE: {project_name} ({sanitizer})")

    ready = sum(1 for s in results.values() if s == "ready")
    unavailable = total - ready
    logger.info(
        f"Prepare summary: total={total} ready={ready} unavailable={unavailable}"
    )
    return 0 if unavailable == 0 else 1


def handle_image_check(args: argparse.Namespace) -> int:
    """Handle `crsbench benchmark image check`."""
    from crsbench.benchmark.packaging.docker_utils import (
        docker_image_exists,
        get_inc_build_image_name,
        get_local_image_digest,
        get_local_inc_image_name,
        get_remote_image_digest,
    )

    try:
        benchmarks_dir, targets = _iter_inc_targets(args)
    except ValueError as e:
        logger.error(str(e))
        return 1

    if not targets:
        logger.warning(f"No inc-build enabled benchmarks found in {benchmarks_dir}")
        return 0

    results: dict[str, str] = {}
    for project_name, sanitizer in targets:
        key = f"{project_name}:{sanitizer}"
        local_image = get_local_inc_image_name(
            project_name,
            sanitizer,
            local_prefix=args.local_prefix,
        )
        local_exists = docker_image_exists(local_image)

        if args.local_only:
            results[key] = "LOCAL_PRESENT" if local_exists else "LOCAL_MISSING"
            continue

        remote_image = get_inc_build_image_name(
            project_name,
            sanitizer,
            args.registry,
        )
        remote_digest = get_remote_image_digest(remote_image)

        if not local_exists and not remote_digest:
            results[key] = "NOT_FOUND"
        elif not local_exists:
            results[key] = "REMOTE_ONLY"
        elif not remote_digest:
            results[key] = "LOCAL_ONLY"
        else:
            local_digest = get_local_image_digest(local_image)
            if local_digest and local_digest == remote_digest:
                results[key] = "UP_TO_DATE"
            elif local_digest and remote_digest:
                results[key] = (
                    f"OUTDATED(local:{local_digest[:12]} remote:{remote_digest[:12]})"
                )
            else:
                results[key] = "UNKNOWN"

    logger.info("IMAGE CHECK SUMMARY")
    for key, status in sorted(results.items(), key=lambda x: (x[1], x[0])):
        logger.info(f"{key:40} {status}")

    return 0


def handle_upload(args: argparse.Namespace) -> int:
    """Handle 'crsbench benchmark upload' command."""
    from crsbench.utils.logger import configure_logger

    if hasattr(args, "verbose") and args.verbose:
        configure_logger(level="DEBUG")

    from crsbench.dataset.backends import check_hf_token

    is_valid, message = check_hf_token()
    if not is_valid:
        logger.error(message)
        return 1
    logger.info(message)

    from crsbench.dataset.upload import upload_dataset

    try:
        upload_dataset(
            args.dataset,
            args.benchmarks_dir,
            benchmarks=args.benchmarks,
            dry_run=args.dry_run,
            prune=args.prune,
        )
        return 0
    except Exception as e:
        logger.error(f"Upload failed: {e}")
        return 1


def run_benchmark_command(args: argparse.Namespace) -> int:
    """Entry point for benchmark command.

    Args:
        args: Parsed arguments with benchmark_command and other options

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    if hasattr(args, "func"):
        return args.func(args)
    return handle_benchmark_help(args)
