"""Rich-based table output and file saving for benchmark CI results.

Replaces manual ANSI formatting with Rich Console and Table for
terminal-aware, automatically-sized output with proper color handling.
Also provides output-dir saving for per-benchmark logs and aggregated summaries.
"""

import json
from pathlib import Path

from rich.console import Console
from rich.table import Table

from crsbench.benchmark_ci.models import (
    BenchmarkValidationResult,
    CheckMode,
    CheckResult,
    CheckStatus,
    ValidationSummary,
)
from crsbench.benchmark_ci.storage import format_storage_size


def format_status(check: CheckResult | None) -> str:
    """Format a check result for Rich table display.

    Returns:
        Rich markup string for the status.
        None → "-" (check not requested by command)
        SKIP → "SKIP" (requested but benchmark doesn't support it)
        PASS → "PASS(Xs)" (green) or "PASS-FB(Xs)" (yellow)
        FAIL → "FAIL" (red)
        ERROR → "ERR" (red)
    """
    if check is None:
        return "[dim]-[/dim]"

    if check.status == CheckStatus.PASS:
        time_str = check.format_status()
        if check.fallback_used:
            return f"[yellow]{time_str}[/yellow]"
        return f"[green]{time_str}[/green]"

    if check.status == CheckStatus.FAIL:
        return "[red]FAIL[/red]"

    if check.status == CheckStatus.ERROR:
        return "[red]ERR[/red]"

    # SKIP status — benchmark doesn't support this check
    return "[dim]SKIP[/dim]"


def _format_total_status(status: CheckStatus) -> str:
    """Format a total_status CheckStatus enum for Rich display.

    Args:
        status: CheckStatus enum value

    Returns:
        Rich markup string
    """
    if status == CheckStatus.PASS:
        return "[green]PASS[/green]"
    if status == CheckStatus.FAIL:
        return "[red]FAIL[/red]"
    if status == CheckStatus.ERROR:
        return "[red]ERR[/red]"
    return "[dim]SKIP[/dim]"


def _add_detail_rows(
    table: Table, result: BenchmarkValidationResult, num_columns: int
) -> None:
    """Add per-CPV detail rows for failed checks.

    For each check that has FAIL status with a non-empty "failures" list
    in its details dict, adds indented dimmed sub-rows showing individual
    failure descriptions.

    Args:
        table: Rich Table to add rows to
        result: BenchmarkValidationResult with check results
        num_columns: Number of data columns (after Benchmark) to fill with ""
    """
    checks = [
        result.pov_check,
        result.patch_check,
        result.patch_rts_check,
    ]
    for check in checks:
        if check is None:
            continue
        if check.status != CheckStatus.FAIL:
            continue
        failures = check.details.get("failures", [])
        for failure in failures:
            table.add_row(
                f"  [dim]{failure}[/dim]",
                *[""] * num_columns,
            )


def _format_storage(result: BenchmarkValidationResult) -> str:
    """Format storage size for table display.

    Returns:
        Right-aligned storage size string, or "-" if no storage data
    """
    if result.storage_bytes <= 0:
        return "[dim]-[/dim]"
    return format_storage_size(result.storage_bytes)


def _add_format_columns(table: Table) -> None:
    """Add columns for FORMAT check mode (per-check columns)."""
    table.add_column("Struct", justify="center")
    table.add_column("Schema", justify="center")
    table.add_column("Harness", justify="center")
    table.add_column("CPV", justify="center")
    table.add_column("Blob", justify="center")
    table.add_column("Patch", justify="center")
    table.add_column("Storage", justify="right")
    table.add_column("Total", justify="center")


def _add_format_rows(table: Table, summary: ValidationSummary) -> None:
    """Add rows for FORMAT check mode (per-check columns)."""
    check_names = ("struct", "schema", "harness", "cpv", "blob", "patch")
    for r in summary.results:
        cols = [format_status(r.format_checks.get(n)) for n in check_names]
        table.add_row(
            r.benchmark, *cols, _format_storage(r), _format_total_status(r.total_status)
        )


def _add_default_columns(table: Table) -> None:
    """Add columns for DEFAULT check mode."""
    table.add_column("Format", justify="center")
    table.add_column("POV", justify="center")
    table.add_column("Patch", justify="center")
    table.add_column("Coverage", justify="center")
    table.add_column("Storage", justify="right")
    table.add_column("Total", justify="center")


def _add_default_rows(table: Table, summary: ValidationSummary) -> None:
    """Add rows for DEFAULT check mode."""
    for r in summary.results:
        table.add_row(
            r.benchmark,
            format_status(r.format_check),
            format_status(r.pov_check),
            format_status(r.patch_check),
            format_status(r.coverage_check),
            _format_storage(r),
            _format_total_status(r.total_status),
        )
        _add_detail_rows(table, r, 6)


def _add_inc_columns(table: Table) -> None:
    """Add columns for INC check mode."""
    table.add_column("inc", justify="center")
    table.add_column("Format", justify="center")
    table.add_column("POV(inc)", justify="center")
    table.add_column("Patch(inc)", justify="center")
    table.add_column("Cov(inc)", justify="center")
    table.add_column("Storage", justify="right")
    table.add_column("Total", justify="center")


def _add_inc_rows(table: Table, summary: ValidationSummary) -> None:
    """Add rows for INC check mode."""
    for r in summary.results:
        inc_marker = "[green]Y[/green]" if r.supports_inc_build else "[dim]-[/dim]"
        table.add_row(
            r.benchmark,
            inc_marker,
            format_status(r.format_check),
            format_status(r.pov_inc_check),
            format_status(r.patch_inc_check),
            format_status(r.coverage_inc_check),
            _format_storage(r),
            _format_total_status(r.total_status),
        )
        _add_detail_rows(table, r, 7)


def _add_rts_columns(table: Table) -> None:
    """Add columns for RTS check mode."""
    table.add_column("rts", justify="center")
    table.add_column("Format", justify="center")
    table.add_column("Patch(rts)", justify="center")
    table.add_column("Storage", justify="right")
    table.add_column("Total", justify="center")


def _add_rts_rows(table: Table, summary: ValidationSummary) -> None:
    """Add rows for RTS check mode."""
    for r in summary.results:
        rts_mode = r.rts_mode or "[dim]-[/dim]"
        table.add_row(
            r.benchmark,
            rts_mode,
            format_status(r.format_check),
            format_status(r.patch_rts_check),
            _format_storage(r),
            _format_total_status(r.total_status),
        )


def _add_all_columns(table: Table) -> None:
    """Add columns for ALL check mode (single build mode)."""
    table.add_column("inc", justify="center")
    table.add_column("rts", justify="center")
    table.add_column("Fmt", justify="center")
    table.add_column("POV", justify="center")
    table.add_column("Patch", justify="center")
    table.add_column("P(rts)", justify="center")
    table.add_column("Cov", justify="center")
    table.add_column("Storage", justify="right")
    table.add_column("Total", justify="center")


def _add_all_rows(table: Table, summary: ValidationSummary) -> None:
    """Add rows for ALL check mode (single build mode)."""
    for r in summary.results:
        inc_marker = "[green]Y[/green]" if r.supports_inc_build else "[dim]-[/dim]"
        rts_mode = r.rts_mode[:6] if r.rts_mode else "[dim]-[/dim]"
        table.add_row(
            r.benchmark,
            inc_marker,
            rts_mode,
            format_status(r.format_check),
            format_status(r.pov_check),
            format_status(r.patch_check),
            format_status(r.patch_rts_check),
            format_status(r.coverage_check),
            _format_storage(r),
            _format_total_status(r.total_status),
        )
        _add_detail_rows(table, r, 9)


def print_results_table(
    summary: ValidationSummary,
    *,
    check_mode: CheckMode = CheckMode.DEFAULT,
    no_color: bool = False,
) -> None:
    """Print validation results using Rich table.

    Creates a formatted table appropriate for the check mode, with
    colored status indicators and a summary line.

    Args:
        summary: ValidationSummary containing benchmark results
        check_mode: Determines which columns to display
        no_color: If True, disable colored output
    """
    console = Console(no_color=no_color)
    table = Table(title="Benchmark Validation Report", show_lines=False)
    table.add_column("Benchmark", style="cyan", min_width=25)

    if check_mode == CheckMode.FORMAT:
        _add_format_columns(table)
        _add_format_rows(table, summary)
    elif check_mode == CheckMode.DEFAULT:
        _add_default_columns(table)
        _add_default_rows(table, summary)
    elif check_mode == CheckMode.INC:
        _add_inc_columns(table)
        _add_inc_rows(table, summary)
    elif check_mode == CheckMode.RTS:
        _add_rts_columns(table)
        _add_rts_rows(table, summary)
    else:
        # ALL or INC_RTS - full matrix
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


def save_output_dir(
    summary: ValidationSummary,
    output_dir: Path,
    *,
    check_mode: CheckMode = CheckMode.ALL,
) -> None:
    """Save validation results to output directory.

    Creates:
    - <output_dir>/results.txt: Human-readable table (same as terminal output)
    - <output_dir>/summary.csv: CSV format for analysis
    - <output_dir>/summary.json: Aggregated JSON summary
    - <output_dir>/<benchmark>/summary.json: Per-benchmark details
    - <output_dir>/<benchmark>/errors.txt: Error details (failures only)

    Args:
        summary: ValidationSummary with all results
        output_dir: Output directory path
        check_mode: Check mode for table rendering
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    # Save human-readable results table
    _save_results_txt(summary, output_dir, check_mode=check_mode)

    # Save CSV summary
    _save_summary_csv(summary, output_dir, check_mode=check_mode)

    # Save per-benchmark results
    for result in summary.results:
        _save_benchmark_result(result, output_dir)

    # Save top-level aggregated summary
    summary_path = output_dir / "summary.json"
    summary_path.write_text(json.dumps(summary.to_dict(), indent=2))


def _save_results_txt(
    summary: ValidationSummary, output_dir: Path, *, check_mode: CheckMode
) -> None:
    """Render the Rich table to results.txt without color codes.

    Args:
        summary: ValidationSummary with all results
        output_dir: Output directory
        check_mode: Check mode for column layout
    """
    results_path = output_dir / "results.txt"
    with results_path.open("w") as f:
        console = Console(file=f, no_color=True, width=10_000)
        table = Table(title="Benchmark Validation Report", show_lines=False)
        table.add_column("Benchmark", min_width=25)

        if check_mode == CheckMode.FORMAT:
            _add_format_columns(table)
            _add_format_rows(table, summary)
        elif check_mode == CheckMode.DEFAULT:
            _add_default_columns(table)
            _add_default_rows(table, summary)
        elif check_mode == CheckMode.INC:
            _add_inc_columns(table)
            _add_inc_rows(table, summary)
        elif check_mode == CheckMode.RTS:
            _add_rts_columns(table)
            _add_rts_rows(table, summary)
        else:
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


def _plain_status(check: CheckResult | None) -> str:
    """Format check result as plain text for CSV.

    Args:
        check: CheckResult or None

    Returns:
        Plain status string (PASS, PASS-FB, FAIL, ERR, SKIP, or empty)
    """
    if check is None:
        return ""
    if check.status == CheckStatus.PASS:
        if check.fallback_used:
            return "PASS-FB"
        return "PASS"
    if check.status == CheckStatus.FAIL:
        return "FAIL"
    if check.status == CheckStatus.ERROR:
        return "ERR"
    return "SKIP"


def _check_build_time(check: CheckResult | None) -> float:
    """Extract build time from a check result, defaulting to 0."""
    if check is None:
        return 0.0
    return check.build_time


def _check_verify_time(check: CheckResult | None) -> float:
    """Extract verify time from a check result, defaulting to 0."""
    if check is None:
        return 0.0
    return check.verify_time


def write_summary_csv(
    summary: ValidationSummary,
    f,
    *,
    check_mode: CheckMode,
) -> None:
    """Write CSV summary to a file-like object.

    Args:
        summary: ValidationSummary with all results
        f: File-like object to write to (file or sys.stdout)
        check_mode: Check mode for column layout
    """
    if check_mode == CheckMode.FORMAT:
        f.write(
            "benchmark,struct,schema,harness,cpv,blob,patch,"
            "total,time_s,storage_bytes\n"
        )
        for r in summary.results:
            fmt_time = r.format_check.time_seconds if r.format_check else 0.0
            check_names = ("struct", "schema", "harness", "cpv", "blob", "patch")
            cols = [_plain_status(r.format_checks.get(n)) for n in check_names]
            f.write(
                f"{r.benchmark},{','.join(cols)},"
                f"{r.total_status.value},{fmt_time:.1f},{r.storage_bytes}\n"
            )
    elif check_mode == CheckMode.DEFAULT:
        f.write(
            "benchmark,format,pov,patch,coverage,"
            "total,time_s,build_time_s,verify_time_s,storage_bytes\n"
        )
        for r in summary.results:
            build_t = r.patch_check.build_time if r.patch_check else 0.0
            verify_t = r.patch_check.verify_time if r.patch_check else 0.0
            f.write(
                f"{r.benchmark},"
                f"{_plain_status(r.format_check)},"
                f"{_plain_status(r.pov_check)},"
                f"{_plain_status(r.patch_check)},"
                f"{_plain_status(r.coverage_check)},"
                f"{r.total_status.value},{r.total_time:.1f},"
                f"{build_t:.1f},{verify_t:.1f},{r.storage_bytes}\n"
            )
    elif check_mode == CheckMode.INC:
        f.write(
            "benchmark,inc,format,pov_inc,patch_inc,cov_inc,"
            "total,time_s,build_time_s,verify_time_s,storage_bytes\n"
        )
        for r in summary.results:
            inc = "Y" if r.supports_inc_build else "N"
            build_t = r.patch_inc_check.build_time if r.patch_inc_check else 0.0
            verify_t = r.patch_inc_check.verify_time if r.patch_inc_check else 0.0
            f.write(
                f"{r.benchmark},{inc},"
                f"{_plain_status(r.format_check)},"
                f"{_plain_status(r.pov_inc_check)},"
                f"{_plain_status(r.patch_inc_check)},"
                f"{_plain_status(r.coverage_inc_check)},"
                f"{r.total_status.value},{r.total_time:.1f},"
                f"{build_t:.1f},{verify_t:.1f},{r.storage_bytes}\n"
            )
    elif check_mode == CheckMode.RTS:
        f.write(
            "benchmark,rts_mode,format,patch_rts,"
            "total,time_s,build_time_s,verify_time_s,storage_bytes\n"
        )
        for r in summary.results:
            rts = r.rts_mode or ""
            build_t = r.patch_rts_check.build_time if r.patch_rts_check else 0.0
            verify_t = r.patch_rts_check.verify_time if r.patch_rts_check else 0.0
            f.write(
                f"{r.benchmark},{rts},"
                f"{_plain_status(r.format_check)},"
                f"{_plain_status(r.patch_rts_check)},"
                f"{r.total_status.value},{r.total_time:.1f},"
                f"{build_t:.1f},{verify_t:.1f},{r.storage_bytes}\n"
            )
    else:
        # ALL mode — per-check build + verify times
        f.write(
            "benchmark,inc,rts,fmt,"
            "pov,pov_build_s,pov_verify_s,"
            "patch,patch_build_s,patch_verify_s,"
            "patch_rts,patch_rts_verify_s,"
            "cov,cov_verify_s,"
            "total,total_time_s,storage_bytes\n"
        )
        for r in summary.results:
            inc = "Y" if r.supports_inc_build else "N"
            rts = r.rts_mode or ""
            f.write(
                f"{r.benchmark},{inc},{rts},"
                f"{_plain_status(r.format_check)},"
                f"{_plain_status(r.pov_check)},"
                f"{r.shared_build_time:.1f},"
                f"{_check_verify_time(r.pov_check):.1f},"
                f"{_plain_status(r.patch_check)},"
                f"{_check_build_time(r.patch_check):.1f},"
                f"{_check_verify_time(r.patch_check):.1f},"
                f"{_plain_status(r.patch_rts_check)},"
                f"{_check_verify_time(r.patch_rts_check):.1f},"
                f"{_plain_status(r.coverage_check)},"
                f"{_check_verify_time(r.coverage_check):.1f},"
                f"{r.total_status.value},{r.total_time:.1f},{r.storage_bytes}\n"
            )


def _save_summary_csv(
    summary: ValidationSummary, output_dir: Path, *, check_mode: CheckMode
) -> None:
    """Save results as CSV file in the output directory."""
    csv_path = output_dir / "summary.csv"
    with csv_path.open("w") as f:
        write_summary_csv(summary, f, check_mode=check_mode)


def _save_benchmark_result(result: BenchmarkValidationResult, output_dir: Path) -> None:
    """Save per-benchmark result to subdirectory.

    Args:
        result: Validation result for a single benchmark
        output_dir: Parent output directory
    """
    benchmark_dir = output_dir / result.benchmark
    benchmark_dir.mkdir(parents=True, exist_ok=True)

    # Save summary JSON
    summary_path = benchmark_dir / "summary.json"
    summary_path.write_text(json.dumps(result.to_dict(), indent=2))

    # Save error details for failures
    if result.total_status in (CheckStatus.FAIL, CheckStatus.ERROR):
        errors_path = benchmark_dir / "errors.txt"
        with errors_path.open("w") as f:
            f.write(f"Benchmark: {result.benchmark}\n")
            f.write(f"Status: {result.total_status.value}\n\n")

            # Format sub-checks (detailed per-check output)
            if result.format_checks:
                for name, check in result.format_checks.items():
                    if check.status not in (CheckStatus.FAIL, CheckStatus.ERROR):
                        continue
                    f.write(f"=== Format:{name} ===\n")
                    f.write(f"Status: {check.status.value}\n")
                    if check.error:
                        f.write(f"Error: {check.error}\n")
                    if check.details and check.details.get("issues"):
                        for issue in check.details["issues"]:
                            f.write(f"  - {issue}\n")
                    f.write("\n")

            # Non-format checks
            checks = [
                ("POV", result.pov_check),
                ("Patch", result.patch_check),
                ("Patch(rts)", result.patch_rts_check),
                ("Coverage", result.coverage_check),
            ]
            for name, check in checks:
                if check is None:
                    continue
                if check.status not in (CheckStatus.FAIL, CheckStatus.ERROR):
                    continue
                f.write(f"=== {name} Check ===\n")
                f.write(f"Status: {check.status.value}\n")
                if check.error:
                    f.write(f"Error: {check.error}\n")
                if check.details:
                    f.write(f"Details: {json.dumps(check.details, indent=2)}\n")
                f.write("\n")
