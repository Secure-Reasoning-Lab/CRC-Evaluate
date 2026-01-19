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
from concurrent.futures import ThreadPoolExecutor, as_completed
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

    Supports two subcommands:
    - ci run: Run validation (default if no subcommand given)
    - ci parse: Parse and display results from output directory

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

  # Parse existing results
  crsbench ci parse --output-dir ./results --format table
        """,
    )

    # Add subparsers for ci subcommands
    ci_subparsers = ci_parser.add_subparsers(dest="ci_subcommand")

    # Add parse subcommand
    add_ci_parse_subparser(ci_subparsers)

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
    ci_parser.add_argument(
        "--source",
        type=str,
        choices=["pkgs", "main_repo"],
        default="main_repo",
        help="Source mode: 'pkgs' (bundled tarballs) or 'main_repo' (git clone, default)",
    )

    # Parallelism options
    ci_parser.add_argument(
        "--workers",
        "-j",
        type=int,
        default=4,
        help="Number of benchmarks to validate in parallel (default: 4)",
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

    # Execution options
    ci_parser.add_argument(
        "--exit-on-error",
        action="store_true",
        help="Exit immediately on first benchmark failure (for fast feedback)",
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


def _load_summary_from_output_dir(output_dir: Path) -> Optional[ValidationSummary]:
    """Load ValidationSummary from an output directory.

    Args:
        output_dir: Directory containing summary.json

    Returns:
        ValidationSummary if found, None otherwise
    """
    summary_path = output_dir / "summary.json"
    if not summary_path.exists():
        logger.error(f"summary.json not found in {output_dir}")
        return None

    with summary_path.open() as f:
        data = json.load(f)

    # Parse the summary data
    results = []
    for r in data.get("results", []):
        # Parse check results
        format_check = CheckResult(
            status=CheckStatus(r["format_check"]["status"]),
            time_seconds=r["format_check"].get("time_seconds", 0),
            error=r["format_check"].get("error", ""),
            details=r["format_check"].get("details", {}),
        )
        pov_check = CheckResult(
            status=CheckStatus(r["pov_check"]["status"]),
            time_seconds=r["pov_check"].get("time_seconds", 0),
            error=r["pov_check"].get("error", ""),
            details=r["pov_check"].get("details", {}),
        )
        patch_check = CheckResult(
            status=CheckStatus(r["patch_check"]["status"]),
            time_seconds=r["patch_check"].get("time_seconds", 0),
            error=r["patch_check"].get("error", ""),
            details=r["patch_check"].get("details", {}),
        )
        coverage_check = None
        if r.get("coverage_check"):
            coverage_check = CheckResult(
                status=CheckStatus(r["coverage_check"]["status"]),
                time_seconds=r["coverage_check"].get("time_seconds", 0),
                error=r["coverage_check"].get("error", ""),
                details=r["coverage_check"].get("details", {}),
            )

        result = BenchmarkValidationResult(
            benchmark=r["benchmark"],
            benchmark_path=Path(r["benchmark_path"]),
            format_check=format_check,
            pov_check=pov_check,
            patch_check=patch_check,
            coverage_check=coverage_check,
            started_at=datetime.fromisoformat(r["started_at"])
            if r.get("started_at")
            else None,
            finished_at=datetime.fromisoformat(r["finished_at"])
            if r.get("finished_at")
            else None,
        )
        results.append(result)

    summary = ValidationSummary(
        started_at=datetime.fromisoformat(data["started_at"])
        if data.get("started_at")
        else None,
        finished_at=datetime.fromisoformat(data["finished_at"])
        if data.get("finished_at")
        else None,
    )
    for r in results:
        summary.add_result(r)

    return summary


def _save_aggregated_summary(summary: ValidationSummary, output_dir: Path) -> None:
    """Save aggregated summary at top level of output directory.

    Creates:
    - summary.json: Full JSON summary
    - summary.csv: CSV format for easy analysis
    - RESULTS.txt: Human-readable summary

    Args:
        summary: ValidationSummary with all results
        output_dir: Output directory
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    # Save JSON summary
    json_path = output_dir / "summary.json"
    with json_path.open("w") as f:
        json.dump(summary.to_dict(), f, indent=2)

    # Save CSV summary
    csv_path = output_dir / "summary.csv"
    with csv_path.open("w") as f:
        # Header
        f.write("benchmark,status,format,pov,patch,coverage,time_seconds\n")
        # Rows
        for r in summary.results:
            cov_status = r.coverage_check.status.value if r.coverage_check else "skip"
            f.write(
                f"{r.benchmark},{r.total_status.value},"
                f"{r.format_check.status.value},{r.pov_check.status.value},"
                f"{r.patch_check.status.value},{cov_status},"
                f"{r.total_time:.1f}\n"
            )

    # Save human-readable summary
    results_path = output_dir / "RESULTS.txt"
    with results_path.open("w") as f:
        f.write("=" * 60 + "\n")
        f.write("BENCHMARK CI RESULTS\n")
        f.write("=" * 60 + "\n\n")

        if summary.started_at:
            f.write(f"Started: {summary.started_at.isoformat()}\n")
        if summary.finished_at:
            f.write(f"Finished: {summary.finished_at.isoformat()}\n")
            if summary.started_at:
                duration = (summary.finished_at - summary.started_at).total_seconds()
                f.write(f"Duration: {format_time(duration)}\n")
        f.write("\n")

        f.write(f"Total: {summary.total}\n")
        f.write(f"Passed: {summary.passed}\n")
        f.write(f"Failed: {summary.failed}\n")
        f.write(f"Errors: {summary.errors}\n")
        f.write("\n")

        # List failures
        failed_benchmarks = [
            r
            for r in summary.results
            if r.total_status in (CheckStatus.FAIL, CheckStatus.ERROR)
        ]
        if failed_benchmarks:
            f.write("-" * 60 + "\n")
            f.write("FAILED BENCHMARKS:\n")
            f.write("-" * 60 + "\n")
            for r in failed_benchmarks:
                f.write(f"\n{r.benchmark}: {r.total_status.value}\n")
                for name, check in [
                    ("Format", r.format_check),
                    ("POV", r.pov_check),
                    ("Patch", r.patch_check),
                ]:
                    if check.status in (CheckStatus.FAIL, CheckStatus.ERROR):
                        f.write(
                            f"  - {name}: {check.error[:100] if check.error else 'unknown'}\n"
                        )

    logger.info(f"Aggregated results saved to: {output_dir}/")
    logger.info("  - summary.json (full JSON)")
    logger.info("  - summary.csv (CSV format)")
    logger.info("  - RESULTS.txt (human-readable)")


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
        logger.info(f"Per-phase logs will be saved to: {output_dir}/<benchmark>/")

    # Calculate worker distribution based on CPU count
    # When running N benchmarks in parallel, each gets cpu_count/N workers (min 1)
    # Cap at 4 to avoid DoS-ing package servers with too many concurrent downloads
    cpu_count = os.cpu_count() or 8
    benchmark_workers = max(1, args.workers)
    per_benchmark_workers = min(4, max(1, cpu_count // benchmark_workers))

    logger.info(
        f"Running with {benchmark_workers} benchmark worker(s), "
        f"{per_benchmark_workers} internal worker(s) each"
    )

    # Create validator with adjusted workers
    source_mode = getattr(args, "source", "pkgs")
    validator = BenchmarkValidator(
        build_workers=per_benchmark_workers,
        verify_workers=per_benchmark_workers,
        source_mode=source_mode,
    )

    # Determine color usage
    use_color = not args.no_color

    # Check for exit-on-error option
    exit_on_error = getattr(args, "exit_on_error", False)

    # Helper function for parallel execution
    def validate_single_benchmark(path: Path) -> BenchmarkValidationResult:
        """Validate a single benchmark (used for parallel execution)."""
        if args.format_only:
            format_result = validator.validate_format(path)
            return BenchmarkValidationResult(
                benchmark=path.name,
                benchmark_path=path,
                format_check=format_result,
                pov_check=CheckResult.skip("format-only mode"),
                patch_check=CheckResult.skip("format-only mode"),
                started_at=datetime.now(),
                finished_at=datetime.now(),
            )
        return validator.validate_benchmark(
            path,
            include_coverage=args.include_coverage,
            force_rebuild=args.force_rebuild,
            use_inc_build=args.inc_build,
            skip_format=args.skip_format,
            skip_verify=args.skip_verify,
            skip_patch_verify=args.skip_patch_verify,
            log_dir=output_dir,
        )

    # Run validation
    summary = ValidationSummary(started_at=datetime.now())

    if benchmark_workers == 1:
        # Sequential execution
        for i, path in enumerate(benchmark_paths, 1):
            logger.info(f"[{i}/{len(benchmark_paths)}] {path.name}")
            result = validate_single_benchmark(path)
            summary.add_result(result)
            _save_benchmark_logs(result, output_dir)
            # Exit early if requested and there's a failure
            if exit_on_error and result.total_status in (
                CheckStatus.FAIL,
                CheckStatus.ERROR,
            ):
                logger.warning(
                    f"Exiting early due to failure: {path.name} "
                    f"(--exit-on-error enabled)"
                )
                break
    else:
        # Parallel execution
        completed = 0
        total = len(benchmark_paths)
        should_exit = False
        with ThreadPoolExecutor(max_workers=benchmark_workers) as executor:
            future_to_path = {
                executor.submit(validate_single_benchmark, path): path
                for path in benchmark_paths
            }
            for future in as_completed(future_to_path):
                path = future_to_path[future]
                completed += 1
                try:
                    result = future.result()
                    logger.info(
                        f"[{completed}/{total}] {path.name}: "
                        f"{result.total_status.value}"
                    )
                    summary.add_result(result)
                    _save_benchmark_logs(result, output_dir)
                    # Check for early exit
                    if exit_on_error and result.total_status in (
                        CheckStatus.FAIL,
                        CheckStatus.ERROR,
                    ):
                        logger.warning(
                            f"Failure detected: {path.name} "
                            "(--exit-on-error enabled, waiting for running tasks)"
                        )
                        should_exit = True
                except Exception as e:
                    logger.error(f"[{completed}/{total}] {path.name}: ERROR - {e}")
                    # Create error result
                    error_result = BenchmarkValidationResult(
                        benchmark=path.name,
                        benchmark_path=path,
                        format_check=CheckResult.make_error(str(e), 0),
                        pov_check=CheckResult.skip("Error in validation"),
                        patch_check=CheckResult.skip("Error in validation"),
                        started_at=datetime.now(),
                        finished_at=datetime.now(),
                    )
                    summary.add_result(error_result)
                    if exit_on_error:
                        should_exit = True
            # Cancel remaining futures if exiting early
            if should_exit:
                for f in future_to_path:
                    f.cancel()

    summary.finished_at = datetime.now()

    # Print results
    print_results_table(summary, use_color=use_color)

    # Save aggregated summary at top level if output_dir specified
    if output_dir:
        _save_aggregated_summary(summary, output_dir)

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


def add_ci_parse_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Add 'ci parse' subcommand to the CLI.

    Args:
        subparsers: Subparsers object from argparse
    """
    parse_parser = subparsers.add_parser(
        "parse",
        help="Parse and display CI results from an output directory",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Display results as table (default)
  crsbench ci parse --output-dir ./results

  # Export as JSON
  crsbench ci parse --output-dir ./results --format json

  # Export as CSV
  crsbench ci parse --output-dir ./results --format csv

  # Show only failed benchmarks
  crsbench ci parse --output-dir ./results --failed-only
        """,
    )

    parse_parser.add_argument(
        "--output-dir",
        "-d",
        type=str,
        required=True,
        help="Directory containing CI results (summary.json)",
    )
    parse_parser.add_argument(
        "--format",
        "-f",
        type=str,
        choices=["table", "json", "csv"],
        default="table",
        help="Output format (default: table)",
    )
    parse_parser.add_argument(
        "--failed-only",
        action="store_true",
        help="Only show failed benchmarks",
    )
    parse_parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable colored output",
    )

    parse_parser.set_defaults(command="ci_parse")


def run_ci_parse(args: argparse.Namespace) -> int:
    """Parse and display CI results from an output directory.

    Args:
        args: Parsed command line arguments

    Returns:
        Exit code (0 for success, 1 for failure)
    """
    output_dir = Path(args.output_dir)
    if not output_dir.exists():
        logger.error(f"Output directory not found: {output_dir}")
        return 1

    summary = _load_summary_from_output_dir(output_dir)
    if summary is None:
        return 1

    # Filter to failed only if requested
    if args.failed_only:
        failed_results = [
            r
            for r in summary.results
            if r.total_status in (CheckStatus.FAIL, CheckStatus.ERROR)
        ]
        summary = ValidationSummary(
            started_at=summary.started_at,
            finished_at=summary.finished_at,
        )
        for r in failed_results:
            summary.add_result(r)

    # Output based on format
    output_format = args.format
    use_color = not args.no_color

    if output_format == "table":
        print_results_table(summary, use_color=use_color)
    elif output_format == "json":
        cli_print(json.dumps(summary.to_dict(), indent=2))
    elif output_format == "csv":
        # Header
        cli_print("benchmark,status,format,pov,patch,coverage,time_seconds")
        # Rows
        for r in summary.results:
            cov_status = r.coverage_check.status.value if r.coverage_check else "skip"
            cli_print(
                f"{r.benchmark},{r.total_status.value},"
                f"{r.format_check.status.value},{r.pov_check.status.value},"
                f"{r.patch_check.status.value},{cov_status},"
                f"{r.total_time:.1f}"
            )

    # Print summary stats
    if output_format == "table":
        if summary.failed > 0 or summary.errors > 0:
            logger.info(f"Found {summary.failed} failures, {summary.errors} errors")
        else:
            logger.info(f"All {summary.passed} benchmarks passed")

    return 0 if summary.failed == 0 and summary.errors == 0 else 1


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
    parser.add_argument(
        "--source",
        type=str,
        choices=["pkgs", "main_repo"],
        default="main_repo",
        help="Source mode: 'pkgs' (bundled tarballs) or 'main_repo' (git clone, default)",
    )

    # Parallelism options
    parser.add_argument(
        "--workers",
        "-j",
        type=int,
        default=4,
        help="Number of benchmarks to validate in parallel (default: 4)",
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

    # Execution options
    parser.add_argument(
        "--exit-on-error",
        action="store_true",
        help="Exit immediately on first benchmark failure (for fast feedback)",
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
