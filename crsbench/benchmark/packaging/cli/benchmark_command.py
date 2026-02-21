"""CLI commands for benchmark management operations.

Provides:
- crsbench benchmark validate <path> - Validate benchmark structure
- crsbench benchmark bundle <path> - Create pkgs/ tarball
- crsbench benchmark bundle-all <dir> - Bundle all benchmarks in directory
- crsbench benchmark prepare-delta <path> - Generate ref.diff
- crsbench benchmark inject-canary <dir> --filter <pattern> - Add canary for contamination detection
- crsbench benchmark list-canaries - List registered canary UUIDs
- crsbench benchmark pull-image <dir> - Pull inc-build Docker images
- crsbench benchmark check-image <dir> - Check local vs remote image digests
- crsbench benchmark dedup-povs <path> - Deduplicate POVs by crash signature
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
        help="Import corpus from experiment output as seed for benchmark",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Collects corpus files from CRS experiment output and stores them
with metadata in the benchmark's .aixcc/{harness}/corpus/ directory.

Files are named by content hash (deduplication). A manifest.json
contains metadata including relative_time (seconds since CRS start).

Output structure:
  .aixcc/{harness}/corpus/
  ├── manifest.json  # Metadata: crs_run_start_time, files info
  ├── {hash1}        # Corpus files (named by content hash)
  ├── {hash2}
  └── ...

Manifest format:
  {
    "crs_run_start_time": 1234567890.0,
    "files": {
      "abc123...": {"relative_time": 500.0, "original_name": "...", "size": 1234}
    }
  }

Examples:
  # Import corpus from experiment output
  %(prog)s ./experiment-output/

  # Specify custom benchmarks directory
  %(prog)s ./experiment-output/ --benchmarks ./my-benchmarks/

  # Force overwrite existing corpus
  %(prog)s ./experiment-output/ --force
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
        "--force",
        action="store_true",
        help="Overwrite existing corpus directory",
    )
    seed_import_parser.set_defaults(func=handle_seed_import)

    # crsbench benchmark pull-image
    pull_image_parser = benchmark_subparsers.add_parser(
        "pull-image",
        help="Pull inc-build Docker images for benchmarks",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Pre-pull inc-build Docker images for faster incremental builds.
Only benchmarks with inc_build: true in project.yaml are processed.

Examples:
  # Pull all inc-build images
  %(prog)s benchmarks/

  # Filter by glob pattern
  %(prog)s benchmarks/ --filter "afc-*"

  # Dry-run to see what would be pulled
  %(prog)s benchmarks/ --dry-run

  # Use more parallel workers
  %(prog)s benchmarks/ --workers 8
        """,
    )
    pull_image_parser.add_argument(
        "benchmarks_dir",
        type=str,
        nargs="?",
        default="./benchmarks",
        help="Directory containing benchmarks (default: ./benchmarks)",
    )
    pull_image_parser.add_argument(
        "--filter",
        type=str,
        default=None,
        help="Filter benchmarks by glob pattern (e.g., 'afc-*')",
    )
    pull_image_parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Number of parallel workers (default: 4)",
    )
    pull_image_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be pulled without actually pulling",
    )
    pull_image_parser.set_defaults(func=handle_pull_image)

    # crsbench benchmark check-image
    check_image_parser = benchmark_subparsers.add_parser(
        "check-image",
        help="Check local vs remote inc-build image digests",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Compare local inc-build images against remote registry to detect outdated images.

Status values:
  UP-TO-DATE   - Local image matches remote
  OUTDATED     - Local image differs from remote (needs pull)
  LOCAL-ONLY   - Image exists locally but not in registry
  REMOTE-ONLY  - Image exists in registry but not locally

Examples:
  # Check all inc-build images
  %(prog)s benchmarks/

  # Filter by glob pattern
  %(prog)s benchmarks/ --filter "afc-*"
        """,
    )
    check_image_parser.add_argument(
        "benchmarks_dir",
        type=str,
        nargs="?",
        default="./benchmarks",
        help="Directory containing benchmarks (default: ./benchmarks)",
    )
    check_image_parser.add_argument(
        "--filter",
        type=str,
        default=None,
        help="Filter benchmarks by glob pattern (e.g., 'afc-*')",
    )
    check_image_parser.set_defaults(func=handle_check_image)

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
        help="Only list what would be uploaded, don't actually upload",
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
        """,
    )
    dedup_povs_parser.add_argument(
        "benchmark_path",
        type=str,
        help="Path to benchmark directory",
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
    from crsbench.benchmark.packaging.dedup_povs import dedup_benchmark_povs

    benchmark_path = Path(args.benchmark_path)

    if not benchmark_path.is_dir():
        logger.error(f"Benchmark not found: {benchmark_path}")
        return 1

    dry_run = not args.no_dry_run
    mode = "DRY RUN" if dry_run else "LIVE"
    logger.info(f"Deduplicating POVs in {benchmark_path.name} [{mode}]")

    try:
        summary = dedup_benchmark_povs(
            benchmark_path,
            harness_filter=args.harness,
            cpv_filter=args.cpv,
            top_n=args.top_n,
            dry_run=dry_run,
        )
    except ValueError as e:
        logger.error(str(e))
        return 1

    # Print summary
    logger.info("\n" + "=" * 50)
    logger.info("DEDUP SUMMARY")
    logger.info("=" * 50)
    logger.info(f"Benchmark: {summary.benchmark}")
    logger.info(f"Total POVs: {summary.total_povs}")
    logger.info(f"Kept: {summary.total_kept}")

    action = "Would remove" if dry_run else "Removed"
    logger.info(f"{action}: {summary.total_removed}")

    if dry_run and summary.total_removed > 0:
        logger.info("\nRun with --no-dry-run to actually delete files.")

    # Write JSON report if requested
    if args.output:
        args.output.write_text(summary.to_json())
        logger.info(f"\nReport written to: {args.output}")

    return 0


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

        if not result.benchmarks and result.skipped_count == 0:
            logger.warning(f"No benchmarks found matching filter: {args.filter}")
            return 0

        # Summary
        logger.info("=" * 50)
        logger.info("CANARY INJECTION SUMMARY")
        logger.info("=" * 50)
        logger.info(f"Filter: {args.filter}")
        logger.info(f"UUID: {result.canary_uuid}")
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

    Collects corpus files from experiment output and stores them
    in the benchmark's .aixcc/{harness}/corpus/ directory with a manifest.
    """
    from crsbench.benchmark.seed import CollectionResult, CorpusCollector

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
        result: CollectionResult = collector.collect(force=args.force)

        # Display warnings
        for warning in result.warnings:
            logger.warning(warning)

        # Summary
        logger.info("=" * 50)
        logger.info("SEED CORPUS IMPORT SUMMARY")
        logger.info("=" * 50)
        logger.info(f"Benchmark: {result.benchmark_name}")
        logger.info(f"Harness: {result.harness_name}")
        logger.info(f"Output: {result.output_dir}")
        logger.info(f"Total files: {result.total_files}")

        return 0

    except FileExistsError as e:
        logger.error(f"Corpus already exists: {e}")
        logger.info("Use --force to overwrite existing corpus")
        return 1
    except ValueError as e:
        logger.error(f"Import error: {e}")
        return 1
    except Exception as e:
        logger.error(f"Seed import failed: {e}")
        return 1


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


def handle_pull_image(args: argparse.Namespace) -> int:
    """Handle 'crsbench benchmark pull-image' command.

    Pre-pull inc-build Docker images for faster incremental builds.
    Compares local and remote digests to detect outdated images.
    """
    from crsbench.benchmark.packaging.docker_utils import (
        docker_image_exists,
        docker_pull,
        docker_retag,
        get_inc_build_image_name,
        get_local_image_digest,
        get_ossfuzz_image_name,
        get_remote_image_digest,
    )

    benchmarks_dir = Path(args.benchmarks_dir)

    if not benchmarks_dir.is_dir():
        logger.error(f"Benchmarks directory not found: {benchmarks_dir}")
        return 1

    # Discover inc-build benchmarks
    benchmarks = _discover_inc_build_benchmarks(benchmarks_dir, args.filter)

    if not benchmarks:
        logger.warning(f"No inc-build enabled benchmarks found in {benchmarks_dir}")
        return 0

    # Build list of (project_name, sanitizer) pairs to pull
    pull_targets: list[tuple[str, str]] = []
    for project_name, sanitizers in benchmarks:
        for sanitizer in sanitizers:
            pull_targets.append((project_name, sanitizer))

    logger.info(f"Found {len(benchmarks)} inc-build enabled benchmarks")
    logger.info(f"Total images to process: {len(pull_targets)}")

    if args.dry_run:
        logger.info("\nDry run - would pull:")
        for project_name, sanitizer in pull_targets:
            image_name = get_inc_build_image_name(project_name, sanitizer)
            logger.info(f"  {image_name}")
        return 0

    # Pull in parallel
    results: dict[str, str] = {}  # "project:sanitizer" -> status
    total = len(pull_targets)
    completed_count = 0

    def pull_one(target: tuple[str, str]) -> tuple[str, str]:
        project_name, sanitizer = target
        key = f"{project_name}:{sanitizer}"
        inc_image = get_inc_build_image_name(project_name, sanitizer)
        ossfuzz_image = get_ossfuzz_image_name(project_name, sanitizer)

        # Get remote digest first
        remote_digest = get_remote_image_digest(inc_image)
        if not remote_digest:
            return key, "not_available"

        # Check if local image exists and compare digest
        local_exists = docker_image_exists(ossfuzz_image) or docker_image_exists(
            inc_image
        )
        if local_exists:
            local_digest = get_local_image_digest(inc_image)
            if local_digest and local_digest == remote_digest:
                # Ensure OSS-Fuzz format exists
                if not docker_image_exists(ossfuzz_image):
                    docker_retag(inc_image, ossfuzz_image)
                return key, "up_to_date"

        # Pull from registry (new or outdated)
        if docker_pull(inc_image):
            if docker_retag(inc_image, ossfuzz_image):
                if local_exists:
                    return key, "updated"
                return key, "pulled"
            return key, "retag_failed"

        return key, "pull_failed"

    logger.info(f"Pulling images with {args.workers} workers...")

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(pull_one, target): target for target in pull_targets}

        for future in as_completed(futures):
            key, status = future.result()
            results[key] = status
            completed_count += 1

            project_name, sanitizer = key.split(":", 1)
            progress = f"[{completed_count}/{total}]"

            if status == "pulled":
                logger.info(f"{progress} PULLED: {project_name} ({sanitizer})")
            elif status == "updated":
                logger.info(f"{progress} UPDATED: {project_name} ({sanitizer})")
            elif status == "up_to_date":
                logger.info(f"{progress} UP-TO-DATE: {project_name} ({sanitizer})")
            elif status == "not_available":
                logger.warning(
                    f"{progress} NOT IN REGISTRY: {project_name} ({sanitizer})"
                )
            else:
                logger.warning(f"{progress} FAILED: {project_name} ({sanitizer})")

    # Summary
    pulled = sum(1 for s in results.values() if s == "pulled")
    updated = sum(1 for s in results.values() if s == "updated")
    up_to_date = sum(1 for s in results.values() if s == "up_to_date")
    not_available = sum(1 for s in results.values() if s == "not_available")
    failed = sum(1 for s in results.values() if s in ("retag_failed", "pull_failed"))

    logger.info("\n" + "=" * 50)
    logger.info("PULL SUMMARY")
    logger.info("=" * 50)
    logger.info(f"Total: {total}")
    logger.info(f"Pulled (new): {pulled}")
    logger.info(f"Updated (outdated): {updated}")
    logger.info(f"Up-to-date (skipped): {up_to_date}")
    logger.info(f"Not in registry: {not_available}")
    if failed > 0:
        logger.info(f"Failed: {failed}")

    return 0


def handle_check_image(args: argparse.Namespace) -> int:
    """Handle 'crsbench benchmark check-image' command.

    Compare local inc-build images against remote registry.
    """
    from crsbench.benchmark.packaging.docker_utils import (
        docker_image_exists,
        get_inc_build_image_name,
        get_local_image_digest,
        get_ossfuzz_image_name,
        get_remote_image_digest,
    )

    benchmarks_dir = Path(args.benchmarks_dir)

    if not benchmarks_dir.is_dir():
        logger.error(f"Benchmarks directory not found: {benchmarks_dir}")
        return 1

    # Discover inc-build benchmarks
    benchmarks = _discover_inc_build_benchmarks(benchmarks_dir, args.filter)

    if not benchmarks:
        logger.warning(f"No inc-build enabled benchmarks found in {benchmarks_dir}")
        return 0

    # Build list of (project_name, sanitizer) pairs to check
    check_targets: list[tuple[str, str]] = []
    for project_name, sanitizers in benchmarks:
        for sanitizer in sanitizers:
            check_targets.append((project_name, sanitizer))

    logger.info(f"Checking {len(check_targets)} images...")

    # Check each image
    results: dict[str, str] = {}  # "project:sanitizer" -> status

    for project_name, sanitizer in check_targets:
        key = f"{project_name}:{sanitizer}"
        inc_image = get_inc_build_image_name(project_name, sanitizer)
        ossfuzz_image = get_ossfuzz_image_name(project_name, sanitizer)

        # Check local image (prefer OSS-Fuzz format)
        local_exists = docker_image_exists(ossfuzz_image) or docker_image_exists(
            inc_image
        )

        # Get remote digest
        remote_digest = get_remote_image_digest(inc_image)

        if not local_exists and not remote_digest:
            results[key] = "NOT_FOUND"
        elif not local_exists:
            results[key] = "REMOTE-ONLY"
        elif not remote_digest:
            results[key] = "LOCAL-ONLY"
        else:
            # Compare digests
            local_digest = get_local_image_digest(inc_image)
            if local_digest and remote_digest:
                if local_digest == remote_digest:
                    results[key] = "UP-TO-DATE"
                else:
                    results[key] = (
                        f"OUTDATED (local: {local_digest[:16]}..., remote: {remote_digest[:16]}...)"
                    )
            else:
                # Can't compare digests, assume outdated if we can't verify
                results[key] = "UP-TO-DATE"  # Assume OK if local exists

    # Output results
    logger.info("\n" + "=" * 60)
    logger.info("CHECK IMAGE SUMMARY")
    logger.info("=" * 60)

    # Sort by status for readability
    for key, status in sorted(results.items(), key=lambda x: (x[1], x[0])):
        project_name, sanitizer = key.split(":", 1)
        image_display = f"{project_name}:inc-{sanitizer}"
        logger.info(f"{image_display:50} {status}")

    # Count by status
    up_to_date = sum(1 for s in results.values() if s == "UP-TO-DATE")
    outdated = sum(1 for s in results.values() if s.startswith("OUTDATED"))
    remote_only = sum(1 for s in results.values() if s == "REMOTE-ONLY")
    local_only = sum(1 for s in results.values() if s == "LOCAL-ONLY")
    not_found = sum(1 for s in results.values() if s == "NOT_FOUND")

    logger.info("\n" + "-" * 40)
    logger.info(f"Total: {len(results)}")
    logger.info(f"Up-to-date: {up_to_date}")
    logger.info(f"Outdated: {outdated}")
    logger.info(f"Remote-only: {remote_only}")
    logger.info(f"Local-only: {local_only}")
    if not_found > 0:
        logger.info(f"Not found: {not_found}")

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
