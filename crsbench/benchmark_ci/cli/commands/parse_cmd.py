"""Parse and display CI results subcommand.

Extracts full parse functionality from main.py. Loads summary.json from
an output directory and displays results in table, JSON, or CSV format.
"""

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from rich.console import Console

from crsbench.benchmark_ci.cli.output import print_results_table
from crsbench.benchmark_ci.models import (
    BenchmarkValidationResult,
    CheckMode,
    CheckResult,
    CheckStatus,
    ValidationSummary,
)
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


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
        build_time=data.get("build_time", 0.0),
        verify_time=data.get("verify_time", 0.0),
        error=data.get("error", ""),
        details=data.get("details", {}),
        fallback_used=data.get("fallback_used", False),
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
        # Parse check results (None if not present in JSON)
        format_check = _parse_check_result(r.get("format_check"))
        pov_check = _parse_check_result(r.get("pov_check"))
        patch_check = _parse_check_result(r.get("patch_check"))
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


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register the parse subcommand."""
    parser = subparsers.add_parser(
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
    parser.add_argument(
        "--output-dir",
        "-d",
        type=str,
        required=True,
        help="Directory containing CI results (summary.json)",
    )
    parser.add_argument(
        "--format",
        "-f",
        type=str,
        choices=["table", "json", "csv"],
        default="table",
        help="Output format (default: table)",
    )
    parser.add_argument(
        "--failed-only",
        action="store_true",
        help="Only show failed benchmarks",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable colored output",
    )
    parser.set_defaults(ci_func=run_parse)


def run_parse(args: argparse.Namespace) -> int:
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
    console = Console(no_color=args.no_color)

    if output_format == "table":
        print_results_table(summary, check_mode=check_mode, no_color=args.no_color)
    elif output_format == "json":
        console.print(json.dumps(summary.to_dict(), indent=2))
    elif output_format == "csv":
        console.print("benchmark,status,format,pov,patch,coverage,time_seconds")
        for r in summary.results:
            fmt_s = r.format_check.status.value if r.format_check else "-"
            pov_s = r.pov_check.status.value if r.pov_check else "-"
            pat_s = r.patch_check.status.value if r.patch_check else "-"
            cov_s = r.coverage_check.status.value if r.coverage_check else "-"
            console.print(
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
