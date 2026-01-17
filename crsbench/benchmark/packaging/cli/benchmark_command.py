"""CLI commands for benchmark management operations.

Provides:
- crsbench benchmark validate <path> - Validate benchmark structure
- crsbench benchmark bundle <path> - Create pkgs/ tarball
- crsbench benchmark prepare-delta <path> - Generate ref.diff
"""

import argparse
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
