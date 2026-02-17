"""Parse and display CI results subcommand.

Extracts full parse functionality from main.py. Loads summary.json from
an output directory and displays results in table, JSON, or CSV format.

Also re-parses job logs to extract split patch columns (build, POV, unittest)
and writes updated summary_reparsed.csv and results_reparsed.txt.
"""

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.table import Table

from crsbench.benchmark_ci.cli.output import (
    _add_all_columns,
    _add_all_rows,
    print_results_table,
    write_summary_csv,
)
from crsbench.benchmark_ci.models import (
    BenchmarkValidationResult,
    CheckMode,
    CheckResult,
    CheckStatus,
    ValidationSummary,
)
from crsbench.benchmark_ci.storage import format_storage_size
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


def _parse_job_log(log_path: Path) -> Optional[dict]:
    """Parse a job log file and extract the JSON details.

    Args:
        log_path: Path to the .log file

    Returns:
        Parsed JSON dict or None if parsing fails
    """
    try:
        content = log_path.read_text()
        # Find JSON block after the header
        json_match = re.search(r"\{[\s\S]*\}", content)
        if json_match:
            return json.loads(json_match.group())
    except Exception:
        pass
    return None


def _reparse_split_pov_results(
    output_dir: Path, benchmark_name: str
) -> tuple[Optional[CheckResult], Optional[CheckResult]]:
    """Re-parse job logs to extract split POV results.

    Scans build/ directory for build-single logs:
    - success -> pov_build_check (sum of all variant builds)

    Scans verify/ directory for verify-cpv-pov logs:
    - success -> pov_pov_check (POV verification only)

    Args:
        output_dir: CI output directory
        benchmark_name: Name of the benchmark

    Returns:
        Tuple of (pov_build_check, pov_pov_check)
    """
    benchmark_dir = output_dir / benchmark_name
    build_dir = benchmark_dir / "build"
    verify_dir = benchmark_dir / "verify"

    # Track build results
    build_results: list[bool] = []
    build_errors: list[str] = []
    build_time = 0.0
    fallback_used = False

    # Track POV verify results
    pov_results: list[bool] = []
    pov_errors: list[str] = []
    pov_time = 0.0

    # Parse build-single logs (base variant builds)
    if build_dir.exists():
        for log_path in build_dir.glob("build-single-*.log"):
            content = log_path.read_text()

            # Extract elapsed time from header
            elapsed_match = re.search(r"Elapsed:\s*([\d.]+)s", content)
            if elapsed_match:
                build_time += float(elapsed_match.group(1))

            # Check status
            if "Status:  SUCCESS" in content:
                build_results.append(True)
                # Check for fallback
                details = _parse_job_log(log_path)
                if details and details.get("fallback_used", False):
                    fallback_used = True
            elif "Status:  FAILED" in content or "Status:  ERROR" in content:
                build_results.append(False)
                variant_match = re.search(
                    r"build-single-[^:]+:(.+)\.log", log_path.name
                )
                variant_name = variant_match.group(1) if variant_match else "unknown"
                build_errors.append(f"{variant_name}: build failed")

    # Parse verify-cpv-pov logs (POV verification)
    if verify_dir.exists():
        for log_path in verify_dir.glob("verify-cpv-pov-*.log"):
            # Skip stdout/stderr files
            if ".stdout" in log_path.name or ".stderr" in log_path.name:
                continue

            content = log_path.read_text()

            # Extract elapsed time from header
            elapsed_match = re.search(r"Elapsed:\s*([\d.]+)s", content)
            if elapsed_match:
                pov_time += float(elapsed_match.group(1))

            # Check status
            if "Status:  SUCCESS" in content:
                pov_results.append(True)
            elif "Status:  FAILED" in content:
                pov_results.append(False)
                cpv_match = re.search(r"cpv_\d+", log_path.name)
                cpv_id = cpv_match.group() if cpv_match else "unknown"
                pov_errors.append(f"{cpv_id}: POV not detected")

    # Build CheckResults
    pov_build_check = None
    if build_results:
        if all(build_results):
            pov_build_check = CheckResult(
                status=CheckStatus.PASS,
                time_seconds=build_time,
                build_time=build_time,
                fallback_used=fallback_used,
            )
        else:
            pov_build_check = CheckResult(
                status=CheckStatus.FAIL,
                time_seconds=build_time,
                build_time=build_time,
                error="; ".join(build_errors[:3]),
                details={"failures": build_errors},
                fallback_used=fallback_used,
            )

    pov_pov_check = None
    if pov_results:
        if all(pov_results):
            pov_pov_check = CheckResult(
                status=CheckStatus.PASS,
                time_seconds=pov_time,
                verify_time=pov_time,
                details={"cpv_count": len(pov_results)},
            )
        else:
            pov_pov_check = CheckResult(
                status=CheckStatus.FAIL,
                time_seconds=pov_time,
                verify_time=pov_time,
                error="; ".join(pov_errors[:3]),
                details={"failures": pov_errors},
            )

    return pov_build_check, pov_pov_check


def _reparse_pov_var_results(
    output_dir: Path, benchmark_name: str
) -> Optional[CheckResult]:
    """Re-parse job logs to extract POV variant results (pov_1+).

    Scans verify/ directory for verify-cpv-var logs.

    Args:
        output_dir: CI output directory
        benchmark_name: Name of the benchmark

    Returns:
        pov_var_check CheckResult or None
    """
    verify_dir = output_dir / benchmark_name / "verify"
    if not verify_dir.exists():
        return None

    var_passed = 0
    var_total = 0
    failures: list[str] = []
    verify_time = 0.0
    has_results = False

    for log_path in verify_dir.glob("verify-cpv-var-*.log"):
        if ".stdout" in log_path.name or ".stderr" in log_path.name:
            continue

        content = log_path.read_text()
        elapsed_match = re.search(r"Elapsed:\s*([\d.]+)s", content)
        if elapsed_match:
            verify_time += float(elapsed_match.group(1))

        details = _parse_job_log(log_path)
        if details is None:
            continue

        cpv_var_passed = details.get("var_passed", 0)
        cpv_var_total = details.get("var_total", 0)
        if cpv_var_total > 0:
            has_results = True
            var_passed += cpv_var_passed
            var_total += cpv_var_total
            if cpv_var_passed < cpv_var_total:
                cpv_match = re.search(r"cpv_\d+", log_path.name)
                cpv_id = cpv_match.group() if cpv_match else "unknown"
                failures.append(f"{cpv_id}: {cpv_var_passed}/{cpv_var_total} variants")

    if not has_results or var_total == 0:
        return None

    if failures:
        return CheckResult(
            status=CheckStatus.FAIL,
            time_seconds=verify_time,
            verify_time=verify_time,
            error="; ".join(failures[:3]),
            details={
                "var_passed": var_passed,
                "var_total": var_total,
                "failures": failures,
            },
        )

    return CheckResult(
        status=CheckStatus.PASS,
        time_seconds=verify_time,
        verify_time=verify_time,
        details={"var_passed": var_passed, "var_total": var_total},
    )


def _reparse_split_patch_results(
    output_dir: Path, benchmark_name: str
) -> tuple[Optional[CheckResult], Optional[CheckResult], Optional[CheckResult]]:
    """Re-parse job logs to extract split patch results.

    Scans verify/ directory for test-patch logs and extracts:
    - pov_test_passed -> patch_pov_check
    - unit_tests_passed -> patch_unittest_check

    Scans build/ directory for build-patch logs:
    - success -> patch_build_check

    Args:
        output_dir: CI output directory
        benchmark_name: Name of the benchmark

    Returns:
        Tuple of (patch_build_check, patch_pov_check, patch_unittest_check)
    """
    benchmark_dir = output_dir / benchmark_name
    build_dir = benchmark_dir / "build"
    verify_dir = benchmark_dir / "verify"

    # Track results across all CPVs/patches
    build_results: list[bool] = []
    build_errors: list[str] = []
    build_time = 0.0

    pov_results: list[bool] = []
    pov_errors: list[str] = []
    pov_time = 0.0

    unittest_results: list[bool] = []
    unittest_errors: list[str] = []
    unittest_time = 0.0

    # Parse build-patch logs
    if build_dir.exists():
        for log_path in build_dir.glob("build-patch-*.log"):
            details = _parse_job_log(log_path)
            if details is None:
                # Check if the log indicates failure
                content = log_path.read_text()
                if "Status:  FAILED" in content or "Status:  ERROR" in content:
                    build_results.append(False)
                    # Extract error from log
                    cpv_match = re.search(r"cpv_\d+", log_path.name)
                    cpv_id = cpv_match.group() if cpv_match else "unknown"
                    build_errors.append(f"{cpv_id}: build failed")
                continue

            # Extract build time from header
            content = log_path.read_text()
            elapsed_match = re.search(r"Elapsed:\s*([\d.]+)s", content)
            if elapsed_match:
                build_time += float(elapsed_match.group(1))

            # Check status in header
            if "Status:  SUCCESS" in content:
                build_results.append(True)
            else:
                build_results.append(False)
                cpv_id = details.get("cpv_id", "unknown")
                build_errors.append(f"{cpv_id}: build failed")

    # Parse patch test logs from verify/ directory
    if verify_dir.exists():
        # Try new split format first: test-patch-pov-*.log
        pov_logs = list(verify_dir.glob("test-patch-pov-*.log"))
        unittest_logs = list(verify_dir.glob("test-patch-unittest-*-FULL.log"))

        if pov_logs:
            # New split format
            for log_path in pov_logs:
                if ".stdout" in log_path.name or ".stderr" in log_path.name:
                    continue
                details = _parse_job_log(log_path)
                if details is None:
                    continue

                content = log_path.read_text()
                elapsed_match = re.search(r"Elapsed:\s*([\d.]+)s", content)
                elapsed = float(elapsed_match.group(1)) if elapsed_match else 0.0

                cpv_id = details.get("cpv_id", "unknown")
                pov_passed = details.get("pov_0_passed", False)
                pov_results.append(pov_passed)
                if not pov_passed:
                    pov_errors.append(f"{cpv_id}: POV still triggers")
                pov_test_time = details.get("pov_test_time", 0.0)
                pov_time += pov_test_time if pov_test_time > 0 else elapsed

            for log_path in unittest_logs:
                if ".stdout" in log_path.name or ".stderr" in log_path.name:
                    continue
                details = _parse_job_log(log_path)
                if details is None:
                    continue

                content = log_path.read_text()
                elapsed_match = re.search(r"Elapsed:\s*([\d.]+)s", content)
                elapsed = float(elapsed_match.group(1)) if elapsed_match else 0.0

                cpv_id = details.get("cpv_id", "unknown")
                unittest_value = details.get("unit_tests_passed")
                if unittest_value is not None:
                    unittest_results.append(unittest_value)
                    if not unittest_value:
                        unittest_errors.append(f"{cpv_id}: unit tests failed")
                    unit_test_time = details.get("unit_test_time", 0.0)
                    unittest_time += unit_test_time if unit_test_time > 0 else elapsed
        else:
            # Fallback: old combined format test-patch-*-FULL.log
            for log_path in verify_dir.glob("test-patch-*-FULL.log"):
                details = _parse_job_log(log_path)
                if details is None:
                    continue

                content = log_path.read_text()
                elapsed_match = re.search(r"Elapsed:\s*([\d.]+)s", content)
                elapsed = float(elapsed_match.group(1)) if elapsed_match else 0.0

                cpv_id = details.get("cpv_id", "unknown")

                pov_passed = details.get("pov_test_passed", False)
                pov_results.append(pov_passed)
                if not pov_passed:
                    pov_errors.append(f"{cpv_id}: POV still triggers")
                pov_test_time = details.get("pov_test_time", 0.0)
                pov_time += pov_test_time if pov_test_time > 0 else elapsed

                unittest_value = details.get("unit_tests_passed")
                if unittest_value is not None:
                    unittest_results.append(unittest_value)
                    if not unittest_value:
                        unittest_errors.append(f"{cpv_id}: unit tests failed")
                    unit_test_time = details.get("unit_test_time", 0.0)
                    unittest_time += unit_test_time if unit_test_time > 0 else elapsed

    # Build CheckResults from aggregated data
    patch_build_check = None
    if build_results:
        if all(build_results):
            patch_build_check = CheckResult(
                status=CheckStatus.PASS,
                time_seconds=build_time,
                build_time=build_time,
                details={"patch_count": len(build_results)},
            )
        else:
            patch_build_check = CheckResult(
                status=CheckStatus.FAIL,
                time_seconds=build_time,
                build_time=build_time,
                error="; ".join(build_errors[:3]),
                details={"failures": build_errors},
            )

    patch_pov_check = None
    if pov_results:
        if all(pov_results):
            patch_pov_check = CheckResult(
                status=CheckStatus.PASS,
                time_seconds=pov_time,
                verify_time=pov_time,
                details={"patch_count": len(pov_results)},
            )
        else:
            patch_pov_check = CheckResult(
                status=CheckStatus.FAIL,
                time_seconds=pov_time,
                verify_time=pov_time,
                error="; ".join(pov_errors[:3]),
                details={"failures": pov_errors},
            )

    patch_unittest_check = None
    if unittest_results:
        if all(unittest_results):
            patch_unittest_check = CheckResult(
                status=CheckStatus.PASS,
                time_seconds=unittest_time,
                verify_time=unittest_time,
                details={"patch_count": len(unittest_results)},
            )
        else:
            patch_unittest_check = CheckResult(
                status=CheckStatus.FAIL,
                time_seconds=unittest_time,
                verify_time=unittest_time,
                error="; ".join(unittest_errors[:3]),
                details={"failures": unittest_errors},
            )
    elif pov_results and not all(pov_results):
        # POV failed, unit tests weren't run
        patch_unittest_check = CheckResult.skip("POV failed, unit tests skipped")

    return patch_build_check, patch_pov_check, patch_unittest_check


def _reparse_patch_var_results(
    output_dir: Path, benchmark_name: str
) -> Optional[CheckResult]:
    """Re-parse job logs to extract patch variant results (pov_1+).

    Scans verify/ directory for test-patch-var logs.

    Args:
        output_dir: CI output directory
        benchmark_name: Name of the benchmark

    Returns:
        patch_var_check CheckResult or None
    """
    verify_dir = output_dir / benchmark_name / "verify"
    if not verify_dir.exists():
        return None

    var_passed = 0
    var_total = 0
    failures: list[str] = []
    verify_time = 0.0
    has_results = False

    for log_path in verify_dir.glob("test-patch-var-*.log"):
        if ".stdout" in log_path.name or ".stderr" in log_path.name:
            continue

        content = log_path.read_text()
        elapsed_match = re.search(r"Elapsed:\s*([\d.]+)s", content)
        if elapsed_match:
            verify_time += float(elapsed_match.group(1))

        details = _parse_job_log(log_path)
        if details is None:
            continue

        cpv_var_passed = details.get("var_passed", 0)
        cpv_var_total = details.get("var_total", 0)
        if cpv_var_total > 0:
            has_results = True
            var_passed += cpv_var_passed
            var_total += cpv_var_total
            if cpv_var_passed < cpv_var_total:
                cpv_id = details.get("cpv_id", "unknown")
                patch_id = details.get("patch_id", "unknown")
                failures.append(
                    f"{cpv_id}/{patch_id}: {cpv_var_passed}/{cpv_var_total} variants"
                )

    if not has_results or var_total == 0:
        return None

    if failures:
        return CheckResult(
            status=CheckStatus.FAIL,
            time_seconds=verify_time,
            verify_time=verify_time,
            error="; ".join(failures[:3]),
            details={
                "var_passed": var_passed,
                "var_total": var_total,
                "failures": failures,
            },
        )

    return CheckResult(
        status=CheckStatus.PASS,
        time_seconds=verify_time,
        verify_time=verify_time,
        details={"var_passed": var_passed, "var_total": var_total},
    )


def _load_summary_from_output_dir(
    output_dir: Path, *, reparse_logs: bool = True
) -> Optional[ValidationSummary]:
    """Load ValidationSummary from an output directory.

    Args:
        output_dir: Directory containing summary.json
        reparse_logs: If True, re-parse job logs to populate split patch fields

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
        benchmark_name = r["benchmark"]

        # Parse check results (None if not present in JSON)
        format_check = _parse_check_result(r.get("format_check"))
        pov_check = _parse_check_result(r.get("pov_check"))
        patch_check = _parse_check_result(r.get("patch_check"))
        coverage_check = _parse_check_result(r.get("coverage_check"))

        # Parse split POV check results from JSON first
        pov_build_check = _parse_check_result(r.get("pov_build_check"))
        pov_pov_check = _parse_check_result(r.get("pov_pov_check"))

        # Re-parse from job logs if split POV fields are missing
        if reparse_logs and (pov_build_check is None or pov_pov_check is None):
            reparsed_pov = _reparse_split_pov_results(output_dir, benchmark_name)
            if pov_build_check is None:
                pov_build_check = reparsed_pov[0]
            if pov_pov_check is None:
                pov_pov_check = reparsed_pov[1]

        # Parse split patch check results from JSON first
        patch_build_check = _parse_check_result(r.get("patch_build_check"))
        patch_pov_check = _parse_check_result(r.get("patch_pov_check"))
        patch_unittest_check = _parse_check_result(r.get("patch_unittest_check"))

        # Re-parse from job logs if split patch fields are missing
        if reparse_logs and (
            patch_build_check is None
            or patch_pov_check is None
            or patch_unittest_check is None
        ):
            reparsed_patch = _reparse_split_patch_results(output_dir, benchmark_name)
            if patch_build_check is None:
                patch_build_check = reparsed_patch[0]
            if patch_pov_check is None:
                patch_pov_check = reparsed_patch[1]
            if patch_unittest_check is None:
                patch_unittest_check = reparsed_patch[2]

        # Parse POV variant check result from JSON, reparse if missing
        pov_var_check = _parse_check_result(r.get("pov_var_check"))
        if reparse_logs and pov_var_check is None:
            pov_var_check = _reparse_pov_var_results(output_dir, benchmark_name)

        patch_var_check = _parse_check_result(r.get("patch_var_check"))
        if reparse_logs and patch_var_check is None:
            patch_var_check = _reparse_patch_var_results(output_dir, benchmark_name)

        # Parse variant check results
        pov_inc_check = _parse_check_result(r.get("pov_inc_check"))
        patch_inc_check = _parse_check_result(r.get("patch_inc_check"))
        patch_rts_check = _parse_check_result(r.get("patch_rts_check"))
        patch_inc_rts_check = _parse_check_result(r.get("patch_inc_rts_check"))
        coverage_inc_check = _parse_check_result(r.get("coverage_inc_check"))

        result = BenchmarkValidationResult(
            benchmark=benchmark_name,
            benchmark_path=Path(r["benchmark_path"]),
            format_check=format_check,
            pov_check=pov_check,
            pov_build_check=pov_build_check,
            pov_pov_check=pov_pov_check,
            pov_var_check=pov_var_check,
            patch_check=patch_check,
            patch_build_check=patch_build_check,
            patch_pov_check=patch_pov_check,
            patch_var_check=patch_var_check,
            patch_unittest_check=patch_unittest_check,
            coverage_check=coverage_check,
            pov_inc_check=pov_inc_check,
            patch_inc_check=patch_inc_check,
            patch_rts_check=patch_rts_check,
            patch_inc_rts_check=patch_inc_rts_check,
            coverage_inc_check=coverage_inc_check,
            shared_build_time=r.get("shared_build_time", 0.0),
            storage_bytes=r.get("storage_bytes", 0),
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

    # Always use ALL mode — shows full column set regardless of stored value
    check_mode = CheckMode.ALL

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


def _write_reparsed_files(
    summary: ValidationSummary, output_dir: Path, check_mode: CheckMode
) -> None:
    """Write reparsed summary.csv and results.txt files.

    Args:
        summary: ValidationSummary with reparsed data
        output_dir: Output directory
        check_mode: Check mode for formatting
    """
    # Write summary_reparsed.csv
    csv_path = output_dir / "summary_reparsed.csv"
    with csv_path.open("w") as f:
        write_summary_csv(summary, f, check_mode=check_mode)
    logger.info(f"Wrote {csv_path}")

    # Write results_reparsed.txt
    txt_path = output_dir / "results_reparsed.txt"
    with txt_path.open("w") as f:
        console = Console(file=f, no_color=True, width=10_000)
        table = Table(title="Benchmark Validation Report", show_lines=False)
        table.add_column("Benchmark", min_width=25)
        _add_all_columns(table)
        _add_all_rows(table, summary)
        console.print(table)

        # Calculate total storage across all benchmarks
        total_storage = sum(r.storage_bytes for r in summary.results)
        storage_str = format_storage_size(total_storage) if total_storage > 0 else "0 B"

        console.print(
            f"\nSummary: {summary.passed} passed, {summary.failed} failed, "
            f"{summary.errors} errors, {summary.total} total | Storage: {storage_str}"
        )
    logger.info(f"Wrote {txt_path}")


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

  # Skip writing reparsed files
  crsbench ci parse --output-dir ./results --no-rewrite
        """,
    )
    parser.add_argument(
        "--output-dir",
        "-d",
        type=Path,
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
    parser.add_argument(
        "--no-rewrite",
        action="store_true",
        help="Skip writing summary_reparsed.csv and results_reparsed.txt",
    )
    parser.set_defaults(ci_func=run_parse)


def run_parse(args: argparse.Namespace) -> int:
    """Parse and display CI results from an output directory.

    Args:
        args: Parsed command line arguments

    Returns:
        Exit code (0 for success, 1 for failure)
    """
    output_dir = args.output_dir
    if not output_dir.exists():
        logger.error(f"Output directory not found: {output_dir}")
        return 1

    summary = _load_summary_from_output_dir(output_dir, reparse_logs=True)
    if summary is None:
        return 1

    # Use check_mode from saved summary
    check_mode = summary.check_mode

    # Write reparsed files unless disabled
    if not getattr(args, "no_rewrite", False):
        _write_reparsed_files(summary, output_dir, check_mode)

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

    if output_format == "table":
        print_results_table(summary, check_mode=check_mode, no_color=args.no_color)
    elif output_format == "json":
        console = Console(no_color=args.no_color)
        console.print(json.dumps(summary.to_dict(), indent=2))
    elif output_format == "csv":
        write_summary_csv(summary, sys.stdout, check_mode=check_mode)

    # Print summary stats
    if output_format == "table":
        if summary.failed > 0 or summary.errors > 0:
            logger.info(f"Found {summary.failed} failures, {summary.errors} errors")
        else:
            logger.info(f"All {summary.passed} benchmarks passed")

    return 0 if summary.failed == 0 and summary.errors == 0 else 1
