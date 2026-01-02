#!/usr/bin/env python3
"""Parse benchmark status JSON and output as table or CSV.

Usage:
    python parse_benchmark_status.py status.json
    python parse_benchmark_status.py status.json --format csv
    python parse_benchmark_status.py status.json --format csv --output results.csv

Examples:
    python parse_benchmark_status.py status.json
    python parse_benchmark_status.py status.json --format csv > results.csv
    python parse_benchmark_status.py status.json --failed-only
"""

import argparse
import csv
import json
import sys
from pathlib import Path


def format_time(seconds: float) -> str:
    """Format time in human-readable format."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    secs = seconds % 60
    return f"{minutes}m{secs:.0f}s"


def format_status(status: str, use_color: bool = True) -> str:
    """Format status with color codes for terminal."""
    if not use_color:
        return status.upper()
    if status == "pass":
        return "\033[92mPASS\033[0m"
    elif status == "fail":
        return "\033[91mFAIL\033[0m"
    return "\033[93mSKIP\033[0m"


def print_table(data: dict, use_color: bool = True, failed_only: bool = False):
    """Print results as a formatted table."""
    results = data.get("results", [])

    if failed_only:
        results = [r for r in results if r.get("total_status") == "fail"]

    if not results:
        print("No results to display")
        return

    # Header
    headers = [
        "Benchmark",
        "Format", "F.Time",
        "Verify", "V.Time",
        "Patch", "P.Time",
        "Coverage", "C.Time",
        "Total", "Total Time",
    ]

    # Calculate column widths
    max_name = max(len(r.get("name", "unknown")) for r in results)
    max_name = max(max_name, len("Benchmark"))

    # Print header
    header_fmt = f"{{:<{max_name}}}  {{:^6}} {{:>6}}  {{:^6}} {{:>7}}  {{:^6}} {{:>7}}  {{:^8}} {{:>7}}  {{:^6}} {{:>10}}"
    print(header_fmt.format(*headers))
    print("-" * (max_name + 85))

    # Print rows
    for r in results:
        name = r.get("name", "unknown")
        fmt = r.get("format_validate", {})
        verify = r.get("verify", {})
        patch = r.get("patch_verify", {})
        coverage = r.get("coverage", {})

        if use_color:
            row = [
                name,
                format_status(fmt.get("status", "skip"), True),
                format_time(fmt.get("time", 0)),
                format_status(verify.get("status", "skip"), True),
                format_time(verify.get("time", 0)),
                format_status(patch.get("status", "skip"), True),
                format_time(patch.get("time", 0)),
                format_status(coverage.get("status", "skip"), True),
                format_time(coverage.get("time", 0)),
                format_status(r.get("total_status", "skip"), True),
                format_time(r.get("total_time", 0)),
            ]
            print(f"{row[0]:<{max_name}}  {row[1]:^15} {row[2]:>6}  {row[3]:^15} {row[4]:>7}  {row[5]:^15} {row[6]:>7}  {row[7]:^17} {row[8]:>7}  {row[9]:^15} {row[10]:>10}")
        else:
            row = [
                name,
                fmt.get("status", "skip").upper(),
                format_time(fmt.get("time", 0)),
                verify.get("status", "skip").upper(),
                format_time(verify.get("time", 0)),
                patch.get("status", "skip").upper(),
                format_time(patch.get("time", 0)),
                coverage.get("status", "skip").upper(),
                format_time(coverage.get("time", 0)),
                r.get("total_status", "skip").upper(),
                format_time(r.get("total_time", 0)),
            ]
            print(header_fmt.format(*row))

    # Print summary
    print("-" * (max_name + 85))
    summary = data.get("summary", {})
    print(f"Summary: {summary.get('passed', 0)} passed, {summary.get('failed', 0)} failed, {summary.get('total', 0)} total")
    print(f"Total time: {format_time(summary.get('total_time', 0))}")


def output_csv(data: dict, output_file: Path | None = None, failed_only: bool = False):
    """Output results as CSV."""
    results = data.get("results", [])

    if failed_only:
        results = [r for r in results if r.get("total_status") == "fail"]

    headers = [
        "benchmark",
        "format_status", "format_time", "format_error",
        "verify_status", "verify_time", "verify_error",
        "patch_status", "patch_time", "patch_error",
        "coverage_status", "coverage_time", "coverage_error",
        "total_status", "total_time",
    ]

    rows = []
    for r in results:
        name = r.get("name", "unknown")
        fmt = r.get("format_validate", {})
        verify = r.get("verify", {})
        patch = r.get("patch_verify", {})
        coverage = r.get("coverage", {})

        rows.append([
            name,
            fmt.get("status", "skip"),
            f"{fmt.get('time', 0):.1f}",
            fmt.get("error", ""),
            verify.get("status", "skip"),
            f"{verify.get('time', 0):.1f}",
            verify.get("error", ""),
            patch.get("status", "skip"),
            f"{patch.get('time', 0):.1f}",
            patch.get("error", ""),
            coverage.get("status", "skip"),
            f"{coverage.get('time', 0):.1f}",
            coverage.get("error", ""),
            r.get("total_status", "skip"),
            f"{r.get('total_time', 0):.1f}",
        ])

    if output_file:
        with open(output_file, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(headers)
            writer.writerows(rows)
        print(f"CSV saved to: {output_file}", file=sys.stderr)
    else:
        writer = csv.writer(sys.stdout)
        writer.writerow(headers)
        writer.writerows(rows)


def output_markdown(data: dict, failed_only: bool = False):
    """Output results as Markdown table."""
    results = data.get("results", [])

    if failed_only:
        results = [r for r in results if r.get("total_status") == "fail"]

    if not results:
        print("No results to display")
        return

    # Header
    print("| Benchmark | Format | F.Time | Verify | V.Time | Patch | P.Time | Coverage | C.Time | Total | Total Time |")
    print("|-----------|--------|--------|--------|--------|-------|--------|----------|--------|-------|------------|")

    # Rows
    for r in results:
        name = r.get("name", "unknown")
        fmt = r.get("format_validate", {})
        verify = r.get("verify", {})
        patch = r.get("patch_verify", {})
        coverage = r.get("coverage", {})

        f_status = "✅" if fmt.get("status") == "pass" else "❌" if fmt.get("status") == "fail" else "⏭️"
        v_status = "✅" if verify.get("status") == "pass" else "❌" if verify.get("status") == "fail" else "⏭️"
        p_status = "✅" if patch.get("status") == "pass" else "❌" if patch.get("status") == "fail" else "⏭️"
        c_status = "✅" if coverage.get("status") == "pass" else "❌" if coverage.get("status") == "fail" else "⏭️"
        t_status = "✅" if r.get("total_status") == "pass" else "❌" if r.get("total_status") == "fail" else "⏭️"

        print(f"| {name} | {f_status} | {format_time(fmt.get('time', 0))} | {v_status} | {format_time(verify.get('time', 0))} | {p_status} | {format_time(patch.get('time', 0))} | {c_status} | {format_time(coverage.get('time', 0))} | {t_status} | {format_time(r.get('total_time', 0))} |")

    # Summary
    summary = data.get("summary", {})
    print()
    print(f"**Summary:** {summary.get('passed', 0)} passed, {summary.get('failed', 0)} failed, {summary.get('total', 0)} total")
    print(f"**Total time:** {format_time(summary.get('total_time', 0))}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Parse benchmark status JSON")
    parser.add_argument(
        "input",
        type=Path,
        help="Input JSON file from benchmark_status.py",
    )
    parser.add_argument(
        "--format",
        choices=["table", "csv", "markdown"],
        default="table",
        help="Output format (default: table)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Output file (for CSV format)",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable colored output (for table format)",
    )
    parser.add_argument(
        "--failed-only",
        action="store_true",
        help="Show only failed benchmarks",
    )

    args = parser.parse_args()

    if not args.input.exists():
        print(f"ERROR: Input file not found: {args.input}")
        return 1

    try:
        with open(args.input) as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON file: {e}")
        return 1

    # Validate JSON structure
    if "results" not in data:
        print("ERROR: JSON missing 'results' key")
        return 1

    if args.format == "table":
        print_table(data, use_color=not args.no_color, failed_only=args.failed_only)
    elif args.format == "csv":
        output_csv(data, args.output, failed_only=args.failed_only)
    elif args.format == "markdown":
        output_markdown(data, failed_only=args.failed_only)

    return 0


if __name__ == "__main__":
    sys.exit(main())
