"""Simplified CLI for benchmark CI validation.

Usage:
    crsbench ci --benchmarks bench1,bench2
    crsbench ci --all
    crsbench ci --all --include-coverage

This CLI uses BenchmarkValidator which delegates to existing engines:
- VerificationEngine for POV checks
- PatchVerificationEngine for patch checks
- CoverageEngine for coverage checks

No custom build/verify logic - just orchestration.
"""

import argparse
import fnmatch
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional, Union

from crsbench.benchmark_ci.models import (
    BenchmarkValidationResult,
    CheckResult,
    CheckStatus,
    ValidationSummary,
)
from crsbench.benchmark_ci.validator import BenchmarkValidator
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


def cli_print(*args, **kwargs) -> None:  # noqa: T201
    """Print to stdout for CLI user-facing output."""
    print(*args, **kwargs)  # noqa: T201


def get_benchmarks_root() -> Path:
    """Get the root benchmarks directory."""
    if "BENCHMARKS_ROOT" in os.environ:
        return Path(os.environ["BENCHMARKS_ROOT"])

    # Default to benchmarks/ relative to project root
    current = Path(__file__).resolve()
    for parent in current.parents:
        benchmarks_dir = parent / "benchmarks"
        if benchmarks_dir.is_dir():
            return benchmarks_dir

    raise RuntimeError("Could not find benchmarks directory")


def discover_benchmarks(
    benchmarks_root: Path, filter_pattern: Optional[str] = None
) -> list[Path]:
    """Discover benchmark directories.

    Args:
        benchmarks_root: Root directory containing benchmarks
        filter_pattern: Optional glob pattern to filter benchmarks

    Returns:
        List of benchmark paths
    """
    benchmarks = []
    for path in sorted(benchmarks_root.iterdir()):
        if not path.is_dir():
            continue
        # Skip hidden directories
        if path.name.startswith("."):
            continue
        # Check for .aixcc directory (indicates valid benchmark)
        if not (path / ".aixcc").exists():
            continue
        # Apply filter if specified
        if filter_pattern and not fnmatch.fnmatch(path.name, filter_pattern):
            continue
        benchmarks.append(path)
    return benchmarks


def add_ci_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Add 'ci' subcommand to the CLI.

    Args:
        subparsers: Subparsers object from argparse
    """
    ci_parser = subparsers.add_parser(
        "ci",
        help="Validate benchmarks (format, POV, patch verification)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Validate specific benchmarks
  crsbench ci --benchmarks sanity-mock-c-delta-01,sanity-mock-c-full-01

  # Validate all benchmarks
  crsbench ci --all

  # Filter benchmarks by pattern
  crsbench ci --all --filter "afc-*"

  # Include coverage check (slower)
  crsbench ci --all --include-coverage

  # Force rebuild Docker images
  crsbench ci --benchmarks bench1 --force-rebuild

  # Export results to JSON
  crsbench ci --all --output results.json
        """,
    )

    # Benchmark selection
    ci_parser.add_argument(
        "--benchmarks",
        "-b",
        type=str,
        help="Comma-separated list of benchmark names to validate",
    )
    ci_parser.add_argument(
        "--all",
        action="store_true",
        help="Validate all benchmarks in the benchmarks/ directory",
    )
    ci_parser.add_argument(
        "--filter",
        "-f",
        type=str,
        help="Filter benchmarks by glob pattern (e.g., 'afc-*', 'sanity-*')",
    )

    # Check options
    ci_parser.add_argument(
        "--include-coverage",
        action="store_true",
        help="Include coverage check (slower)",
    )
    ci_parser.add_argument(
        "--format-only",
        action="store_true",
        help="Only run format validation (fast, no Docker)",
    )

    # Skip options
    ci_parser.add_argument(
        "--skip-format",
        action="store_true",
        help="Skip format validation",
    )
    ci_parser.add_argument(
        "--skip-verify",
        action="store_true",
        help="Skip POV verification",
    )
    ci_parser.add_argument(
        "--skip-patch-verify",
        action="store_true",
        help="Skip patch verification",
    )

    # Build options
    ci_parser.add_argument(
        "--force-rebuild",
        action="store_true",
        help="Force rebuild Docker images even if cached",
    )
    ci_parser.add_argument(
        "--inc-build",
        action="store_true",
        help="Enable incremental builds (pull pre-built images from registry)",
    )

    # Output
    ci_parser.add_argument(
        "--output",
        "-o",
        type=str,
        help="Path to save results JSON",
    )
    ci_parser.add_argument(
        "--output-dir",
        type=str,
        help="Directory for detailed logs (creates per-benchmark subdirs with logs)",
    )

    # Display options
    ci_parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable colored output",
    )

    ci_parser.set_defaults(command="ci")


def format_time(seconds: float) -> str:
    """Format time in human-readable form."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    secs = seconds % 60
    if minutes < 60:
        return f"{minutes}m{secs:.0f}s"
    hours = minutes // 60
    mins = minutes % 60
    return f"{hours}h{mins}m"


def print_results_table(summary: ValidationSummary, *, use_color: bool = True) -> None:
    """Print results in a formatted table.

    Args:
        summary: ValidationSummary with results
        use_color: Whether to use ANSI color codes
    """
    # Header
    cli_print()
    cli_print("=" * 100)
    cli_print("BENCHMARK VALIDATION REPORT")
    cli_print("=" * 100)
    cli_print()

    # Column headers
    header = (
        f"{'Benchmark':<45} {'Format':<8} {'POV':<8} {'Patch':<8} "
        f"{'Coverage':<8} {'Total':<8} {'Time':<10}"
    )
    cli_print(header)
    cli_print("-" * 100)

    # Status formatting - accepts CheckResult, CheckStatus, or None
    def status_str(
        check_or_status: Union[CheckResult, CheckStatus, None],
        *,
        with_color: bool = True,
    ) -> str:
        if check_or_status is None:
            status = "SKIP"
        elif isinstance(check_or_status, CheckStatus):
            status = check_or_status.value.upper()
            if status == "ERROR":
                status = "ERR"
        elif check_or_status.status == CheckStatus.PASS:
            status = "PASS"
        elif check_or_status.status == CheckStatus.FAIL:
            status = "FAIL"
        elif check_or_status.status == CheckStatus.SKIP:
            status = "SKIP"
        else:
            status = "ERR"

        if not with_color:
            return status

        # Add ANSI color codes
        colors = {
            "PASS": "\033[92m",  # Green
            "FAIL": "\033[91m",  # Red
            "SKIP": "\033[93m",  # Yellow
            "ERR": "\033[91m",  # Red
        }
        reset = "\033[0m"
        return f"{colors.get(status, '')}{status}{reset}"

    # Results
    for r in summary.results:
        total_status = status_str(r.total_status, with_color=use_color)
        cov_status = status_str(r.coverage_check, with_color=use_color)

        # Adjust column width for ANSI codes (they don't take visual space)
        col_width = 17 if use_color else 8

        row = (
            f"{r.benchmark:<45} "
            f"{status_str(r.format_check, with_color=use_color):<{col_width}} "
            f"{status_str(r.pov_check, with_color=use_color):<{col_width}} "
            f"{status_str(r.patch_check, with_color=use_color):<{col_width}} "
            f"{cov_status:<{col_width}} "
            f"{total_status:<{col_width}} "
            f"{format_time(r.total_time):<10}"
        )
        cli_print(row)

    # Summary
    cli_print("-" * 100)
    cli_print(
        f"Summary: {summary.passed} passed, {summary.failed} failed, "
        f"{summary.errors} errors, {summary.total} total"
    )
    cli_print()


def _save_benchmark_logs(
    result: BenchmarkValidationResult, output_dir: Optional[Path]
) -> None:
    """Save detailed logs for a benchmark validation result.

    Args:
        result: Validation result for a benchmark
        output_dir: Output directory for logs (None to skip)
    """
    if output_dir is None:
        return

    benchmark_dir = output_dir / result.benchmark
    benchmark_dir.mkdir(parents=True, exist_ok=True)

    # Save summary using model's to_dict() method
    summary_path = benchmark_dir / "summary.json"
    with summary_path.open("w") as f:
        json.dump(result.to_dict(), f, indent=2)

    # Save detailed error logs if any failures
    if result.total_status in (CheckStatus.FAIL, CheckStatus.ERROR):
        errors_path = benchmark_dir / "errors.txt"
        with errors_path.open("w") as f:
            f.write(f"Benchmark: {result.benchmark}\n")
            f.write(f"Status: {result.total_status.value}\n\n")

            for name, check in [
                ("Format", result.format_check),
                ("POV", result.pov_check),
                ("Patch", result.patch_check),
                ("Coverage", result.coverage_check),
            ]:
                if check and check.status in (CheckStatus.FAIL, CheckStatus.ERROR):
                    f.write(f"=== {name} Check ===\n")
                    f.write(f"Status: {check.status.value}\n")
                    if check.error:
                        f.write(f"Error: {check.error}\n")
                    if check.details:
                        f.write(f"Details: {json.dumps(check.details, indent=2)}\n")
                    f.write("\n")


def run_ci(args: argparse.Namespace) -> int:
    """Run benchmark CI validation.

    Args:
        args: Parsed command line arguments

    Returns:
        Exit code (0 for success, 1 for failure)
    """
    # Get benchmarks to validate
    benchmarks_root = get_benchmarks_root()

    if args.all:
        benchmark_paths = discover_benchmarks(benchmarks_root, args.filter)
    elif args.benchmarks:
        benchmark_names = [b.strip() for b in args.benchmarks.split(",")]
        benchmark_paths = [benchmarks_root / name for name in benchmark_names]
        # Validate paths exist
        for path in benchmark_paths:
            if not path.exists():
                logger.error(f"Benchmark not found: {path}")
                return 1
    else:
        logger.error("No benchmarks specified. Use --benchmarks or --all")
        return 1

    if not benchmark_paths:
        logger.error("No benchmarks found matching criteria")
        return 1

    logger.info(f"Validating {len(benchmark_paths)} benchmarks")

    # Setup output directory for logs if requested
    output_dir = Path(args.output_dir) if args.output_dir else None
    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"Logs will be saved to: {output_dir}")

    # Create validator
    validator = BenchmarkValidator()

    # Determine color usage
    use_color = not args.no_color

    # Run validation
    if args.format_only:
        # Fast path - format validation only
        summary = ValidationSummary(started_at=datetime.now())
        for i, path in enumerate(benchmark_paths, 1):
            logger.info(f"[{i}/{len(benchmark_paths)}] {path.name}")
            format_result = validator.validate_format(path)
            result = BenchmarkValidationResult(
                benchmark=path.name,
                benchmark_path=path,
                format_check=format_result,
                pov_check=CheckResult.skip("format-only mode"),
                patch_check=CheckResult.skip("format-only mode"),
                started_at=datetime.now(),
                finished_at=datetime.now(),
            )
            summary.add_result(result)
            _save_benchmark_logs(result, output_dir)
        summary.finished_at = datetime.now()
    else:
        summary = ValidationSummary(started_at=datetime.now())
        for i, path in enumerate(benchmark_paths, 1):
            logger.info(f"[{i}/{len(benchmark_paths)}] {path.name}")
            result = validator.validate_benchmark(
                path,
                include_coverage=args.include_coverage,
                force_rebuild=args.force_rebuild,
                use_inc_build=args.inc_build,
                skip_format=args.skip_format,
                skip_verify=args.skip_verify,
                skip_patch_verify=args.skip_patch_verify,
            )
            summary.add_result(result)
            _save_benchmark_logs(result, output_dir)
        summary.finished_at = datetime.now()

    # Print results
    print_results_table(summary, use_color=use_color)

    # Save results if requested
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w") as f:
            json.dump(summary.to_dict(), f, indent=2)
        logger.info(f"Results saved to: {output_path}")

    # Return exit code
    if summary.failed > 0 or summary.errors > 0:
        return 1
    return 0


def main(args: Optional[list[str]] = None) -> int:
    """Main entry point for benchmark CI CLI."""
    parser = argparse.ArgumentParser(
        description="Benchmark CI validation for CRSBench",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Benchmark selection
    parser.add_argument(
        "--benchmarks",
        "-b",
        type=str,
        help="Comma-separated list of benchmark names to validate",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Validate all benchmarks",
    )
    parser.add_argument(
        "--filter",
        "-f",
        type=str,
        help="Filter benchmarks by glob pattern",
    )

    # Check options
    parser.add_argument(
        "--include-coverage",
        action="store_true",
        help="Include coverage check",
    )
    parser.add_argument(
        "--format-only",
        action="store_true",
        help="Only run format validation",
    )

    # Skip options
    parser.add_argument(
        "--skip-format",
        action="store_true",
        help="Skip format validation",
    )
    parser.add_argument(
        "--skip-verify",
        action="store_true",
        help="Skip POV verification",
    )
    parser.add_argument(
        "--skip-patch-verify",
        action="store_true",
        help="Skip patch verification",
    )

    # Build options
    parser.add_argument(
        "--force-rebuild",
        action="store_true",
        help="Force rebuild Docker images",
    )
    parser.add_argument(
        "--inc-build",
        action="store_true",
        help="Enable incremental builds (pull pre-built images from registry)",
    )

    # Output options
    parser.add_argument(
        "--output",
        "-o",
        type=str,
        help="Path to save results JSON",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        help="Directory for detailed logs (creates per-benchmark subdirs)",
    )

    # Display options
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable colored output",
    )

    parsed_args = parser.parse_args(args)
    return run_ci(parsed_args)


if __name__ == "__main__":
    sys.exit(main())
