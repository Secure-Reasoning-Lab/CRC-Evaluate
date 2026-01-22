"""CLI commands for benchmark management operations.

Provides:
- crsbench benchmark validate <path> - Validate benchmark structure
- crsbench benchmark bundle <path> - Create pkgs/ tarball
- crsbench benchmark bundle-all <dir> - Bundle all benchmarks in directory
- crsbench benchmark prepare-delta <path> - Generate ref.diff
- crsbench benchmark inject-canary <dir> --filter <pattern> - Add canary for contamination detection
- crsbench benchmark list-canaries - List registered canary UUIDs
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


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

    benchmark_parser.set_defaults(command="benchmark", func=handle_benchmark_help)


def handle_benchmark_help(_args: argparse.Namespace) -> int:
    """Handle benchmark command without subcommand."""
    logger.error("Please specify a subcommand: validate, bundle, or prepare-delta")
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
    to_bundle = []
    already_bundled = []
    for bench in benchmarks:
        if (bench / "pkgs").exists() and not args.force:
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

    def bundle_one(bench_path: Path) -> tuple[str, str]:
        try:
            bundle_benchmark(bench_path, force=args.force)
            return bench_path.name, "success"
        except Exception as e:
            return bench_path.name, f"failed: {e}"

    logger.info(
        f"\nBundling {len(to_bundle)} benchmarks with {args.workers} workers..."
    )

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(bundle_one, bench): bench for bench in to_bundle}

        for future in as_completed(futures):
            name, status = future.result()
            results[name] = status
            if status == "success":
                logger.info(f"  [OK] {name}")
            else:
                logger.error(f"  [FAILED] {name}: {status}")

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
