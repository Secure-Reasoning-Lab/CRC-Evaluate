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
from typing import Optional, TypeAlias, Union

from crsbench.benchmark_ci.models import (
    BenchmarkValidationResult,
    CheckMode,
    CheckResult,
    CheckStatus,
    ValidationSummary,
)
from crsbench.benchmark_ci.validator import BenchmarkValidator
from crsbench.utils.logger import get_logger

# Type alias for status display (Python 3.11 compatible)
StatusInput: TypeAlias = Union[CheckResult, CheckStatus, None]

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

    # Check mode options (orthogonal flags for inc-build and RTS variants)
    ci_parser.add_argument(
        "--check-inc",
        action="store_true",
        help="Run only inc-build variants (POV:inc, Patch:inc)",
    )
    ci_parser.add_argument(
        "--check-rts",
        action="store_true",
        help="Run only RTS variants (Patch:rts). Requires benchmark rts_mode support.",
    )
    ci_parser.add_argument(
        "--check-all",
        action="store_true",
        help="Run full variant matrix (standard + inc + RTS + combined)",
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
    # Note: CI always rebuilds from scratch - no caching between commands
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


def _status_str(
    check_or_status: StatusInput,
    *,
    with_color: bool = True,
    show_time: bool = False,
) -> str:
    """Format status for display.

    Args:
        check_or_status: Check result, status, or None
        with_color: Whether to use ANSI color codes
        show_time: If True, append time in seconds for CheckResult

    Returns:
        Formatted status string (e.g., "PASS(2m)", "PASS-FB(8m)", "FAIL", "-")
        None → "-" (check not requested)
        SKIP → "SKIP" (benchmark doesn't support it)
    """
    # Handle None (check not requested by command)
    if check_or_status is None:
        return "-"

    # Handle CheckStatus enum
    if isinstance(check_or_status, CheckStatus):
        status = check_or_status.value.upper()
        if status == "ERROR":
            status = "ERR"
        if not with_color:
            return status
        colors = {
            "PASS": "\033[92m",  # Green
            "FAIL": "\033[91m",  # Red
            "SKIP": "\033[93m",  # Yellow
            "ERR": "\033[91m",  # Red
        }
        reset = "\033[0m"
        return f"{colors.get(status, '')}{status}{reset}"

    # Handle CheckResult - use model's format_status() for PASS with fallback
    if show_time:
        formatted = check_or_status.format_status()
    else:
        if check_or_status.status == CheckStatus.SKIP:
            formatted = "SKIP"
        elif check_or_status.status == CheckStatus.ERROR:
            formatted = "ERR"
        else:
            formatted = check_or_status.status.value.upper()

    # Extract base status for coloring (PASS, FBPASS, FAIL, ERR, SKIP, -)
    base_status = formatted.split("(")[0]  # Get "PASS" from "PASS(2m)"

    if not with_color or base_status == "-":
        return formatted

    # Add ANSI color codes
    colors = {
        "PASS": "\033[92m",  # Green
        "PASS-FB": "\033[93m",  # Yellow (fallback worked but worth noting)
        "FAIL": "\033[91m",  # Red
        "SKIP": "\033[93m",  # Yellow
        "ERR": "\033[91m",  # Red
        "ERROR": "\033[91m",  # Red
    }
    reset = "\033[0m"

    # Color the base status, keep the time part uncolored
    if "(" in formatted:
        time_part = "(" + formatted.split("(")[1]
        return f"{colors.get(base_status, '')}{base_status}{reset}{time_part}"
    return f"{colors.get(base_status, '')}{formatted}{reset}"


def _format_results_table(
    summary: ValidationSummary,
    *,
    use_color: bool = True,
    check_mode: CheckMode = CheckMode.DEFAULT,
) -> list[str]:
    """Format results as a table and return lines.

    Args:
        summary: ValidationSummary with results
        use_color: Whether to use ANSI color codes
        check_mode: Check mode used for validation

    Returns:
        List of formatted lines
    """
    lines: list[str] = []

    # Header
    lines.append("")
    lines.append("=" * 120)
    lines.append("BENCHMARK VALIDATION REPORT")
    lines.append("=" * 120)
    lines.append("")

    # Determine columns based on check_mode
    col_width = 17 if use_color else 8

    if check_mode == CheckMode.DEFAULT:
        # Standard columns with time per check
        col_w = 14 if use_color else 12  # Wider columns for time
        header = (
            f"{'Benchmark':<40} {'Format':<12} {'POV':<14} {'Patch':<14} "
            f"{'Coverage':<14} {'Total':<8}"
        )
        lines.append(header)
        lines.append("-" * 110)

        for r in summary.results:
            row = (
                f"{r.benchmark:<40} "
                f"{_status_str(r.format_check, with_color=use_color, show_time=True):<{col_w + 9 if use_color else col_w}} "
                f"{_status_str(r.pov_check, with_color=use_color, show_time=True):<{col_w + 9 if use_color else col_w}} "
                f"{_status_str(r.patch_check, with_color=use_color, show_time=True):<{col_w + 9 if use_color else col_w}} "
                f"{_status_str(r.coverage_check, with_color=use_color, show_time=True):<{col_w + 9 if use_color else col_w}} "
                f"{_status_str(r.total_status, with_color=use_color):<{col_width}}"
            )
            lines.append(row)

    elif check_mode == CheckMode.INC:
        # Inc-build variant columns with time per check
        col_w = 14 if use_color else 12
        header = (
            f"{'Benchmark':<35} {'inc':<4} {'Format':<12} {'POV(inc)':<14} "
            f"{'Patch(inc)':<14} {'Cov(inc)':<14} {'Total':<8}"
        )
        lines.append(header)
        lines.append("-" * 120)

        for r in summary.results:
            inc_marker = "Y" if r.supports_inc_build else "-"
            row = (
                f"{r.benchmark:<35} "
                f"{inc_marker:<4} "
                f"{_status_str(r.format_check, with_color=use_color, show_time=True):<{col_w + 9 if use_color else col_w}} "
                f"{_status_str(r.pov_inc_check, with_color=use_color, show_time=True):<{col_w + 9 if use_color else col_w}} "
                f"{_status_str(r.patch_inc_check, with_color=use_color, show_time=True):<{col_w + 9 if use_color else col_w}} "
                f"{_status_str(r.coverage_inc_check, with_color=use_color, show_time=True):<{col_w + 9 if use_color else col_w}} "
                f"{_status_str(r.total_status, with_color=use_color):<{col_width}}"
            )
            lines.append(row)

    elif check_mode == CheckMode.RTS:
        # RTS variant columns (no POV for RTS) with time per check
        col_w = 14 if use_color else 12
        header = (
            f"{'Benchmark':<35} {'rts':<10} {'Format':<12} {'Patch(rts)':<14} "
            f"{'Total':<8}"
        )
        lines.append(header)
        lines.append("-" * 90)

        for r in summary.results:
            rts_mode = r.rts_mode or "-"
            row = (
                f"{r.benchmark:<35} "
                f"{rts_mode:<10} "
                f"{_status_str(r.format_check, with_color=use_color, show_time=True):<{col_w + 9 if use_color else col_w}} "
                f"{_status_str(r.patch_rts_check, with_color=use_color, show_time=True):<{col_w + 9 if use_color else col_w}} "
                f"{_status_str(r.total_status, with_color=use_color):<{col_width}}"
            )
            lines.append(row)

    else:
        # ALL or INC_RTS - full matrix view with time per check
        col_w = 12 if use_color else 10
        header = (
            f"{'Benchmark':<30} {'inc':<4} {'rts':<8} {'POV':<12} {'POV(i)':<12} "
            f"{'Patch':<12} {'P(inc)':<12} {'P(rts)':<12} {'P(i+r)':<12} {'Total':<6}"
        )
        lines.append(header)
        lines.append("-" * 140)

        for r in summary.results:
            inc_marker = "Y" if r.supports_inc_build else "-"
            rts_mode = r.rts_mode[:6] if r.rts_mode else "-"
            row = (
                f"{r.benchmark:<30} "
                f"{inc_marker:<4} "
                f"{rts_mode:<8} "
                f"{_status_str(r.pov_check, with_color=use_color, show_time=True):<{col_w + 9 if use_color else col_w}} "
                f"{_status_str(r.pov_inc_check, with_color=use_color, show_time=True):<{col_w + 9 if use_color else col_w}} "
                f"{_status_str(r.patch_check, with_color=use_color, show_time=True):<{col_w + 9 if use_color else col_w}} "
                f"{_status_str(r.patch_inc_check, with_color=use_color, show_time=True):<{col_w + 9 if use_color else col_w}} "
                f"{_status_str(r.patch_rts_check, with_color=use_color, show_time=True):<{col_w + 9 if use_color else col_w}} "
                f"{_status_str(r.patch_inc_rts_check, with_color=use_color, show_time=True):<{col_w + 9 if use_color else col_w}} "
                f"{_status_str(r.total_status, with_color=use_color):<{col_width - 2}}"
            )
            lines.append(row)

    # Summary
    lines.append("-" * 120)
    lines.append(
        f"Summary: {summary.passed} passed, {summary.failed} failed, "
        f"{summary.errors} errors, {summary.total} total"
    )
    lines.append("")

    return lines


def print_results_table(
    summary: ValidationSummary,
    *,
    use_color: bool = True,
    check_mode: CheckMode = CheckMode.DEFAULT,
) -> None:
    """Print results in a formatted table.

    Args:
        summary: ValidationSummary with results
        use_color: Whether to use ANSI color codes
        check_mode: Check mode used for validation
    """
    lines = _format_results_table(summary, use_color=use_color, check_mode=check_mode)
    for line in lines:
        cli_print(line)


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


def _parse_check_result(data: Optional[dict]) -> Optional[CheckResult]:
    """Parse a CheckResult from JSON data.

    Args:
        data: JSON dict or None

    Returns:
        CheckResult if data present, None otherwise
    """
    if not data:
        return None
    return CheckResult(
        status=CheckStatus(data["status"]),
        time_seconds=data.get("time_seconds", 0),
        error=data.get("error", ""),
        details=data.get("details", {}),
    )


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
        format_check = _parse_check_result(r.get("format_check"))
        if not format_check:
            format_check = CheckResult.skip("Missing format_check")

        pov_check = _parse_check_result(r.get("pov_check"))
        if not pov_check:
            pov_check = CheckResult.skip("Missing pov_check")

        patch_check = _parse_check_result(r.get("patch_check"))
        if not patch_check:
            patch_check = CheckResult.skip("Missing patch_check")

        coverage_check = _parse_check_result(r.get("coverage_check"))

        # Parse variant check results
        pov_inc_check = _parse_check_result(r.get("pov_inc_check"))
        patch_inc_check = _parse_check_result(r.get("patch_inc_check"))
        patch_rts_check = _parse_check_result(r.get("patch_rts_check"))
        patch_inc_rts_check = _parse_check_result(r.get("patch_inc_rts_check"))
        coverage_inc_check = _parse_check_result(r.get("coverage_inc_check"))

        result = BenchmarkValidationResult(
            benchmark=r["benchmark"],
            benchmark_path=Path(r["benchmark_path"]),
            format_check=format_check,
            pov_check=pov_check,
            patch_check=patch_check,
            coverage_check=coverage_check,
            pov_inc_check=pov_inc_check,
            patch_inc_check=patch_inc_check,
            patch_rts_check=patch_rts_check,
            patch_inc_rts_check=patch_inc_rts_check,
            coverage_inc_check=coverage_inc_check,
            supports_inc_build=r.get("supports_inc_build", True),
            rts_mode=r.get("rts_mode"),
            started_at=datetime.fromisoformat(r["started_at"])
            if r.get("started_at")
            else None,
            finished_at=datetime.fromisoformat(r["finished_at"])
            if r.get("finished_at")
            else None,
        )
        results.append(result)

    # Parse check_mode (default to DEFAULT for backward compatibility)
    check_mode_value = data.get("check_mode", "default")
    try:
        check_mode = CheckMode(check_mode_value)
    except ValueError:
        check_mode = CheckMode.DEFAULT

    summary = ValidationSummary(
        check_mode=check_mode,
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


def _save_aggregated_summary(
    summary: ValidationSummary,
    output_dir: Path,
    check_mode: CheckMode = CheckMode.DEFAULT,
) -> None:
    """Save aggregated summary at top level of output directory.

    Creates:
    - summary.json: Full JSON summary
    - summary.csv: CSV format for easy analysis (with per-check timing)
    - RESULTS.txt: Human-readable summary (same as stdout table)

    Args:
        summary: ValidationSummary with all results
        output_dir: Output directory
        check_mode: Check mode used for validation
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    # Save JSON summary
    json_path = output_dir / "summary.json"
    with json_path.open("w") as f:
        json.dump(summary.to_dict(), f, indent=2)

    # Save CSV summary with per-check timing
    csv_path = output_dir / "summary.csv"
    with csv_path.open("w") as f:
        # Header based on check_mode
        if check_mode == CheckMode.DEFAULT:
            f.write(
                "benchmark,total_status,format_status,format_time,"
                "pov_status,pov_time,patch_status,patch_time,"
                "coverage_status,coverage_time,total_time\n"
            )
            for r in summary.results:
                fmt_s = r.format_check.status.value if r.format_check else ""
                fmt_t = f"{r.format_check.time_seconds:.1f}" if r.format_check else ""
                pov_s = r.pov_check.status.value if r.pov_check else ""
                pov_t = f"{r.pov_check.time_seconds:.1f}" if r.pov_check else ""
                pat_s = r.patch_check.status.value if r.patch_check else ""
                pat_t = f"{r.patch_check.time_seconds:.1f}" if r.patch_check else ""
                cov_s = r.coverage_check.status.value if r.coverage_check else ""
                cov_t = (
                    f"{r.coverage_check.time_seconds:.1f}" if r.coverage_check else ""
                )
                f.write(
                    f"{r.benchmark},{r.total_status.value},"
                    f"{fmt_s},{fmt_t},{pov_s},{pov_t},{pat_s},{pat_t},"
                    f"{cov_s},{cov_t},{r.total_time:.1f}\n"
                )
        elif check_mode == CheckMode.INC:
            f.write(
                "benchmark,inc_build,total_status,format_status,format_time,"
                "pov_inc_status,pov_inc_time,patch_inc_status,patch_inc_time,"
                "cov_inc_status,cov_inc_time,total_time\n"
            )
            for r in summary.results:
                inc = "Y" if r.supports_inc_build else "N"
                pov_inc_status = r.pov_inc_check.status.value if r.pov_inc_check else ""
                pov_inc_time = (
                    f"{r.pov_inc_check.time_seconds:.1f}" if r.pov_inc_check else ""
                )
                patch_inc_status = (
                    r.patch_inc_check.status.value if r.patch_inc_check else ""
                )
                patch_inc_time = (
                    f"{r.patch_inc_check.time_seconds:.1f}" if r.patch_inc_check else ""
                )
                cov_inc_status = (
                    r.coverage_inc_check.status.value if r.coverage_inc_check else ""
                )
                cov_inc_time = (
                    f"{r.coverage_inc_check.time_seconds:.1f}"
                    if r.coverage_inc_check
                    else ""
                )
                fmt_s = r.format_check.status.value if r.format_check else ""
                fmt_t = f"{r.format_check.time_seconds:.1f}" if r.format_check else ""
                f.write(
                    f"{r.benchmark},{inc},{r.total_status.value},"
                    f"{fmt_s},{fmt_t},"
                    f"{pov_inc_status},{pov_inc_time},"
                    f"{patch_inc_status},{patch_inc_time},"
                    f"{cov_inc_status},{cov_inc_time},{r.total_time:.1f}\n"
                )
        elif check_mode == CheckMode.RTS:
            f.write(
                "benchmark,rts_mode,total_status,format_status,format_time,"
                "patch_rts_status,patch_rts_time,total_time\n"
            )
            for r in summary.results:
                rts = r.rts_mode or ""
                patch_rts_status = (
                    r.patch_rts_check.status.value if r.patch_rts_check else ""
                )
                patch_rts_time = (
                    f"{r.patch_rts_check.time_seconds:.1f}" if r.patch_rts_check else ""
                )
                fmt_s = r.format_check.status.value if r.format_check else ""
                fmt_t = f"{r.format_check.time_seconds:.1f}" if r.format_check else ""
                f.write(
                    f"{r.benchmark},{rts},{r.total_status.value},"
                    f"{fmt_s},{fmt_t},"
                    f"{patch_rts_status},{patch_rts_time},{r.total_time:.1f}\n"
                )
        else:
            # ALL or INC_RTS - full matrix
            f.write(
                "benchmark,inc_build,rts_mode,total_status,"
                "pov_status,pov_time,pov_inc_status,pov_inc_time,"
                "patch_status,patch_time,patch_inc_status,patch_inc_time,"
                "patch_rts_status,patch_rts_time,patch_inc_rts_status,patch_inc_rts_time,"
                "total_time\n"
            )
            for r in summary.results:
                inc = "Y" if r.supports_inc_build else "N"
                rts = r.rts_mode or ""
                pov_status = r.pov_check.status.value if r.pov_check else ""
                pov_time = f"{r.pov_check.time_seconds:.1f}" if r.pov_check else ""
                pov_inc_status = r.pov_inc_check.status.value if r.pov_inc_check else ""
                pov_inc_time = (
                    f"{r.pov_inc_check.time_seconds:.1f}" if r.pov_inc_check else ""
                )
                patch_status = r.patch_check.status.value if r.patch_check else ""
                patch_time = (
                    f"{r.patch_check.time_seconds:.1f}" if r.patch_check else ""
                )
                patch_inc_status = (
                    r.patch_inc_check.status.value if r.patch_inc_check else ""
                )
                patch_inc_time = (
                    f"{r.patch_inc_check.time_seconds:.1f}" if r.patch_inc_check else ""
                )
                patch_rts_status = (
                    r.patch_rts_check.status.value if r.patch_rts_check else ""
                )
                patch_rts_time = (
                    f"{r.patch_rts_check.time_seconds:.1f}" if r.patch_rts_check else ""
                )
                patch_inc_rts_status = (
                    r.patch_inc_rts_check.status.value if r.patch_inc_rts_check else ""
                )
                patch_inc_rts_time = (
                    f"{r.patch_inc_rts_check.time_seconds:.1f}"
                    if r.patch_inc_rts_check
                    else ""
                )
                f.write(
                    f"{r.benchmark},{inc},{rts},{r.total_status.value},"
                    f"{pov_status},{pov_time},{pov_inc_status},{pov_inc_time},"
                    f"{patch_status},{patch_time},{patch_inc_status},{patch_inc_time},"
                    f"{patch_rts_status},{patch_rts_time},"
                    f"{patch_inc_rts_status},{patch_inc_rts_time},{r.total_time:.1f}\n"
                )

    # Save human-readable summary (same as stdout table)
    results_path = output_dir / "RESULTS.txt"
    with results_path.open("w") as f:
        # Capture table output
        lines = _format_results_table(summary, use_color=False, check_mode=check_mode)
        f.write("\n".join(lines))
        f.write("\n")

    logger.debug(f"Aggregated results saved to: {output_dir}/")
    logger.debug("  - summary.json (full JSON)")
    logger.debug("  - summary.csv (CSV format)")
    logger.debug("  - RESULTS.txt (human-readable)")


def _get_check_mode(args: argparse.Namespace) -> CheckMode:
    """Convert CLI flags to CheckMode.

    Args:
        args: Parsed command line arguments

    Returns:
        CheckMode based on flag combination
    """
    check_inc = getattr(args, "check_inc", False)
    check_rts = getattr(args, "check_rts", False)
    check_all = getattr(args, "check_all", False)

    if check_all:
        return CheckMode.ALL
    if check_inc and check_rts:
        return CheckMode.INC_RTS
    if check_inc:
        return CheckMode.INC
    if check_rts:
        return CheckMode.RTS
    return CheckMode.DEFAULT


def run_ci(args: argparse.Namespace) -> int:
    """Run benchmark CI validation.

    Args:
        args: Parsed command line arguments

    Returns:
        Exit code (0 for success, 1 for failure)
    """
    # Determine check mode from flags
    check_mode = _get_check_mode(args)
    if check_mode != CheckMode.DEFAULT:
        logger.debug(f"Check mode: {check_mode.value}")

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
        logger.debug(f"Per-phase logs will be saved to: {output_dir}/<benchmark>/")

    # Calculate worker distribution based on CPU count
    # When running N benchmarks in parallel, each gets cpu_count/N workers (min 1)
    # Cap at 4 to avoid DoS-ing package servers with too many concurrent downloads
    cpu_count = os.cpu_count() or 8
    benchmark_workers = max(1, args.workers)
    per_benchmark_workers = min(4, max(1, cpu_count // benchmark_workers))

    logger.debug(
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
            check_mode=check_mode,
            include_coverage=args.include_coverage,
            use_inc_build=args.inc_build,
            skip_format=args.skip_format,
            skip_verify=args.skip_verify,
            skip_patch_verify=args.skip_patch_verify,
            log_dir=output_dir,
        )

    # Run validation
    summary = ValidationSummary(started_at=datetime.now(), check_mode=check_mode)

    if benchmark_workers == 1:
        # Sequential execution
        for i, path in enumerate(benchmark_paths, 1):
            logger.debug(f"[{i}/{len(benchmark_paths)}] {path.name}")
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
                    logger.debug(
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
    print_results_table(summary, use_color=use_color, check_mode=check_mode)

    # Save aggregated summary at top level if output_dir specified
    if output_dir:
        _save_aggregated_summary(summary, output_dir, check_mode=check_mode)

    # Save results if requested
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w") as f:
            json.dump(summary.to_dict(), f, indent=2)
        logger.debug(f"Results saved to: {output_path}")

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

    # Use check_mode from saved summary
    check_mode = summary.check_mode

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
        print_results_table(summary, use_color=use_color, check_mode=check_mode)
    elif output_format == "json":
        cli_print(json.dumps(summary.to_dict(), indent=2))
    elif output_format == "csv":
        # Header
        cli_print("benchmark,status,format,pov,patch,coverage,time_seconds")
        # Rows
        for r in summary.results:
            fmt_s = r.format_check.status.value if r.format_check else "-"
            pov_s = r.pov_check.status.value if r.pov_check else "-"
            pat_s = r.patch_check.status.value if r.patch_check else "-"
            cov_s = r.coverage_check.status.value if r.coverage_check else "-"
            cli_print(
                f"{r.benchmark},{r.total_status.value},"
                f"{fmt_s},{pov_s},{pat_s},{cov_s},"
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

    # Check mode options
    parser.add_argument(
        "--check-inc",
        action="store_true",
        help="Run only inc-build variants",
    )
    parser.add_argument(
        "--check-rts",
        action="store_true",
        help="Run only RTS variants",
    )
    parser.add_argument(
        "--check-all",
        action="store_true",
        help="Run full variant matrix",
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
    # Note: CI always rebuilds from scratch - no caching between commands
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
