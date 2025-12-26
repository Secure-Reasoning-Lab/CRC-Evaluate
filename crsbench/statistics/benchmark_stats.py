#!/usr/bin/env python3
"""
Benchmark statistics collection and CSV export.

Collects information from all benchmarks in the benchmarks/ directory
and exports to CSV format similar to migration reports.
"""

import argparse
import csv
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import yaml

from crsbench.utils.logger import configure_logger, get_logger

logger = get_logger(__name__)


def find_project_root() -> Path:
    """Find the CRSBench project root directory.

    Searches upward from the current directory for a directory containing
    both 'benchmarks' and 'crsbench' directories.

    Returns:
        Path to project root, or current directory if not found
    """
    current = Path.cwd()

    # Check current directory first
    if (current / "benchmarks").exists() and (current / "crsbench").exists():
        return current

    # Search upward
    for parent in current.parents:
        if (parent / "benchmarks").exists() and (parent / "crsbench").exists():
            return parent

    # Fallback: check if we're inside the crsbench package
    # e.g., /path/to/crsbench/crsbench/statistics -> /path/to/crsbench
    script_path = Path(__file__).resolve()
    for parent in script_path.parents:
        if (parent / "benchmarks").exists() and (parent / "crsbench").exists():
            return parent

    return current


def get_default_benchmarks_dir() -> Path:
    """Get the default benchmarks directory.

    Returns:
        Path to benchmarks directory
    """
    return find_project_root() / "benchmarks"


@dataclass
class VulnEntry:
    """Single vulnerability entry for CSV output."""

    benchmark_name: str
    source: str
    repo_url: str
    mode: str
    language: str
    harness_name: str
    vuln_id: str
    num_povs: int = 0
    num_patches: int = 0
    has_test_sh: bool = False
    has_vuln_yaml: bool = False


@dataclass
class BenchmarkInfo:
    """Information about a single benchmark."""

    name: str
    path: Path
    source: str = ""
    repo_url: str = ""
    mode: str = ""
    language: str = ""
    has_test_sh: bool = False
    harnesses: List[str] = field(default_factory=list)
    vulns: List[VulnEntry] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


def detect_source(benchmark_name: str) -> str:
    """Detect benchmark source from naming convention.

    Args:
        benchmark_name: Name of the benchmark

    Returns:
        Source identifier (Team-Atlanta, AFC, ASC, Sanity, etc.)
    """
    name_lower = benchmark_name.lower()

    if name_lower.startswith("atlanta-"):
        return "Team-Atlanta"
    if name_lower.startswith("afc-"):
        return "AFC"
    if name_lower.startswith("asc-"):
        return "ASC"
    if name_lower.startswith("sanity-"):
        return "Sanity"
    return "Unknown"


def detect_mode(meta_data: dict) -> str:
    """Detect benchmark mode from meta.yaml data.

    Args:
        meta_data: Parsed meta.yaml content

    Returns:
        Mode string (delta, full, or unknown)
    """
    # Check delta_mode first (takes priority if both exist)
    if meta_data.get("delta_mode"):
        return "delta"
    if meta_data.get("full_mode"):
        return "full"
    return "unknown"


def count_files_in_dir(directory: Path, pattern: str) -> int:
    """Count files matching pattern in a directory.

    Args:
        directory: Directory to search
        pattern: Glob pattern (e.g., "*.blob", "*.diff")

    Returns:
        Number of matching files
    """
    if not directory.exists():
        return 0
    return len(list(directory.glob(pattern)))


def collect_benchmark_info(benchmark_dir: Path) -> Optional[BenchmarkInfo]:
    """Collect information from a single benchmark directory.

    Args:
        benchmark_dir: Path to benchmark directory

    Returns:
        BenchmarkInfo object or None if not a valid benchmark
    """
    benchmark_name = benchmark_dir.name
    info = BenchmarkInfo(name=benchmark_name, path=benchmark_dir)

    # Detect source from name
    info.source = detect_source(benchmark_name)

    # Check for test.sh
    info.has_test_sh = (benchmark_dir / "test.sh").exists()

    # Read project.yaml for repo URL and language
    project_yaml = benchmark_dir / "project.yaml"
    if project_yaml.exists():
        try:
            with project_yaml.open() as f:
                project_data = yaml.safe_load(f) or {}

            info.repo_url = project_data.get("main_repo", "")
            lang = project_data.get("language", "").upper()
            # Normalize C and C++ to C/C++
            if lang in ("C", "C++"):
                lang = "C/C++"
            info.language = lang
        except Exception as e:
            info.errors.append(f"Error reading project.yaml: {e}")
    else:
        info.errors.append("Missing project.yaml")

    # Read meta.yaml for mode and vulnerability info
    meta_yaml = benchmark_dir / ".aixcc" / "meta.yaml"
    if not meta_yaml.exists():
        info.errors.append("Missing .aixcc/meta.yaml")
        return info

    try:
        with meta_yaml.open() as f:
            meta_data = yaml.safe_load(f) or {}

        # Detect mode
        info.mode = detect_mode(meta_data)

        # Extract harness and vulnerability information
        harness_files = meta_data.get("harness_files", [])
        for harness in harness_files:
            harness_name = harness.get("name", "unknown")
            info.harnesses.append(harness_name)

            # Get vulnerabilities for this harness
            vulns = harness.get("vulns", [])
            for vuln in vulns:
                vuln_id = vuln.get("vuln_keyword", "unknown")

                # CPV directory path: .aixcc/{harness_name}/{vuln_id}/
                cpv_dir = benchmark_dir / ".aixcc" / harness_name / vuln_id

                # Count actual POV blobs
                blobs_dir = cpv_dir / "blobs"
                num_povs = count_files_in_dir(blobs_dir, "*.blob")

                # Count actual patches
                patches_dir = cpv_dir / "patches"
                num_patches = count_files_in_dir(patches_dir, "*.diff")

                # Check vuln.yaml existence
                has_vuln_yaml = (cpv_dir / "vuln.yaml").exists()

                vuln_entry = VulnEntry(
                    benchmark_name=benchmark_name,
                    source=info.source,
                    repo_url=info.repo_url,
                    mode=info.mode,
                    language=info.language,
                    harness_name=harness_name,
                    vuln_id=vuln_id,
                    num_povs=num_povs,
                    num_patches=num_patches,
                    has_test_sh=info.has_test_sh,
                    has_vuln_yaml=has_vuln_yaml,
                )
                info.vulns.append(vuln_entry)

    except Exception as e:
        info.errors.append(f"Error reading meta.yaml: {e}")

    return info


def collect_benchmark_stats(
    benchmarks_dir: Path, specific_benchmarks: Optional[List[str]] = None
) -> List[BenchmarkInfo]:
    """Collect statistics from all benchmarks.

    Args:
        benchmarks_dir: Path to benchmarks directory
        specific_benchmarks: Optional list of specific benchmark names to process

    Returns:
        List of BenchmarkInfo objects
    """
    if not benchmarks_dir.exists():
        logger.error(f"Benchmarks directory does not exist: {benchmarks_dir}")
        return []

    results = []

    for item in sorted(benchmarks_dir.iterdir()):
        if not item.is_dir():
            continue

        # Skip if specific benchmarks are requested and this isn't one
        if specific_benchmarks and item.name not in specific_benchmarks:
            continue

        # Check if it's a valid benchmark (has .aixcc directory)
        if not (item / ".aixcc").exists():
            logger.debug(f"Skipping {item.name}: no .aixcc directory")
            continue

        info = collect_benchmark_info(item)
        if info:
            results.append(info)
            logger.debug(
                f"Collected {item.name}: {len(info.vulns)} vulns, "
                f"{len(info.harnesses)} harnesses"
            )

    return results


def export_benchmarks_csv(
    benchmarks: List[BenchmarkInfo],
    output_path: Path,
    *,
    include_no_vulns: bool = False,
) -> None:
    """Export benchmark information to CSV file.

    Args:
        benchmarks: List of BenchmarkInfo objects
        output_path: Path to output CSV file
        include_no_vulns: If True, include benchmarks with no vulnerabilities
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)

        # Write header
        writer.writerow(
            [
                "Benchmark Name",
                "Source",
                "Repo URL",
                "Mode",
                "Language",
                "Harness Name",
                "Vuln ID",
                "Num POVs",
                "Num Patches",
                "Has test.sh",
                "Has vuln.yaml",
            ]
        )

        for info in benchmarks:
            if info.vulns:
                # Write one row per vulnerability
                for vuln in info.vulns:
                    writer.writerow(
                        [
                            vuln.benchmark_name,
                            vuln.source,
                            vuln.repo_url,
                            vuln.mode,
                            vuln.language,
                            vuln.harness_name,
                            vuln.vuln_id,
                            vuln.num_povs,
                            vuln.num_patches,
                            "Yes" if vuln.has_test_sh else "No",
                            "Yes" if vuln.has_vuln_yaml else "No",
                        ]
                    )
            elif include_no_vulns:
                # Write single row for benchmark with no vulnerabilities
                writer.writerow(
                    [
                        info.name,
                        info.source,
                        info.repo_url,
                        info.mode,
                        info.language,
                        "(no harness)",
                        "(no vuln)",
                        0,
                        0,
                        "Yes" if info.has_test_sh else "No",
                        "N/A",
                    ]
                )

    logger.info(f"Exported benchmark statistics to: {output_path}")


def print_summary(benchmarks: List[BenchmarkInfo]) -> None:
    """Print summary statistics to console.

    Args:
        benchmarks: List of BenchmarkInfo objects
    """
    # Filter out Sanity benchmarks for main statistics
    filtered = [b for b in benchmarks if b.source != "Sanity"]
    sanity_count = len(benchmarks) - len(filtered)

    total_benchmarks = len(filtered)
    total_vulns = sum(len(b.vulns) for b in filtered)
    total_harnesses = sum(len(b.harnesses) for b in filtered)

    # Count test.sh and vuln.yaml
    benchmarks_with_test_sh = sum(1 for b in filtered if b.has_test_sh)
    vulns_with_vuln_yaml = sum(1 for b in filtered for v in b.vulns if v.has_vuln_yaml)
    total_povs = sum(v.num_povs for b in filtered for v in b.vulns)
    total_patches = sum(v.num_patches for b in filtered for v in b.vulns)

    # Count by source
    by_source: dict = {}
    for b in filtered:
        by_source.setdefault(b.source, {"benchmarks": 0, "vulns": 0})
        by_source[b.source]["benchmarks"] += 1
        by_source[b.source]["vulns"] += len(b.vulns)

    # Count by mode
    by_mode: dict = {}
    for b in filtered:
        by_mode.setdefault(b.mode, {"benchmarks": 0, "vulns": 0})
        by_mode[b.mode]["benchmarks"] += 1
        by_mode[b.mode]["vulns"] += len(b.vulns)

    # Count by language
    by_language: dict = {}
    for b in filtered:
        lang = b.language or "Unknown"
        by_language.setdefault(lang, {"benchmarks": 0, "vulns": 0})
        by_language[lang]["benchmarks"] += 1
        by_language[lang]["vulns"] += len(b.vulns)

    # Cross-tabulation: source x mode
    source_mode: dict = {}
    for b in filtered:
        key = (b.source, b.mode)
        source_mode.setdefault(key, {"benchmarks": 0, "vulns": 0})
        source_mode[key]["benchmarks"] += 1
        source_mode[key]["vulns"] += len(b.vulns)

    print("=" * 70)
    print("BENCHMARK STATISTICS SUMMARY")
    print(f"(Excluding {sanity_count} Sanity benchmarks)")
    print("=" * 70)
    print()

    print("Overall:")
    print(f"  # of Projects:  {total_benchmarks}")
    print(f"  # of Vulns:     {total_vulns}")
    print(f"  # of Harnesses: {total_harnesses}")
    print(f"  # of POVs:      {total_povs}")
    print(f"  # of Patches:   {total_patches}")
    print()

    print("File Completeness:")
    print(f"  Benchmarks with test.sh: {benchmarks_with_test_sh}/{total_benchmarks}")
    print(f"  Vulns with vuln.yaml:    {vulns_with_vuln_yaml}/{total_vulns}")
    print()

    print("-" * 70)
    print("By Source:")
    print("-" * 70)
    print(f"  {'Source':<15} {'# Projects':>12} {'# Vulns':>12}")
    print(f"  {'-' * 15} {'-' * 12} {'-' * 12}")
    for source in ["AFC", "ASC", "Team-Atlanta"]:
        if source in by_source:
            stats = by_source[source]
            print(f"  {source:<15} {stats['benchmarks']:>12} {stats['vulns']:>12}")
    print(f"  {'-' * 15} {'-' * 12} {'-' * 12}")
    print(f"  {'Total':<15} {total_benchmarks:>12} {total_vulns:>12}")
    print()

    print("-" * 70)
    print("By Mode:")
    print("-" * 70)
    print(f"  {'Mode':<15} {'# Projects':>12} {'# Vulns':>12}")
    print(f"  {'-' * 15} {'-' * 12} {'-' * 12}")
    for mode in ["delta", "full"]:
        if mode in by_mode:
            stats = by_mode[mode]
            print(f"  {mode:<15} {stats['benchmarks']:>12} {stats['vulns']:>12}")
    print(f"  {'-' * 15} {'-' * 12} {'-' * 12}")
    print(f"  {'Total':<15} {total_benchmarks:>12} {total_vulns:>12}")
    print()

    print("-" * 70)
    print("By Language:")
    print("-" * 70)
    print(f"  {'Language':<15} {'# Projects':>12} {'# Vulns':>12}")
    print(f"  {'-' * 15} {'-' * 12} {'-' * 12}")
    for lang, stats in sorted(by_language.items()):
        print(f"  {lang:<15} {stats['benchmarks']:>12} {stats['vulns']:>12}")
    print(f"  {'-' * 15} {'-' * 12} {'-' * 12}")
    print(f"  {'Total':<15} {total_benchmarks:>12} {total_vulns:>12}")
    print()

    print("-" * 70)
    print("By Source x Mode:")
    print("-" * 70)
    print(f"  {'Source':<15} {'Mode':<10} {'# Projects':>12} {'# Vulns':>12}")
    print(f"  {'-' * 15} {'-' * 10} {'-' * 12} {'-' * 12}")
    for source in ["AFC", "ASC", "Team-Atlanta"]:
        for mode in ["delta", "full"]:
            key = (source, mode)
            if key in source_mode:
                stats = source_mode[key]
                print(
                    f"  {source:<15} {mode:<10} {stats['benchmarks']:>12} {stats['vulns']:>12}"
                )
    print(f"  {'-' * 15} {'-' * 10} {'-' * 12} {'-' * 12}")
    print(f"  {'Total':<15} {'':<10} {total_benchmarks:>12} {total_vulns:>12}")
    print("=" * 70)


def export_summary_csv(benchmarks: List[BenchmarkInfo], output_path: Path) -> None:
    """Export summary statistics to CSV file.

    Args:
        benchmarks: List of BenchmarkInfo objects
        output_path: Path to output CSV file
    """
    # Filter out Sanity benchmarks
    filtered = [b for b in benchmarks if b.source != "Sanity"]

    total_benchmarks = len(filtered)
    total_vulns = sum(len(b.vulns) for b in filtered)
    total_harnesses = sum(len(b.harnesses) for b in filtered)
    benchmarks_with_test_sh = sum(1 for b in filtered if b.has_test_sh)
    vulns_with_vuln_yaml = sum(1 for b in filtered for v in b.vulns if v.has_vuln_yaml)
    total_povs = sum(v.num_povs for b in filtered for v in b.vulns)
    total_patches = sum(v.num_patches for b in filtered for v in b.vulns)

    # Count by source
    by_source: dict = {}
    for b in filtered:
        by_source.setdefault(b.source, {"benchmarks": 0, "vulns": 0})
        by_source[b.source]["benchmarks"] += 1
        by_source[b.source]["vulns"] += len(b.vulns)

    # Count by mode
    by_mode: dict = {}
    for b in filtered:
        by_mode.setdefault(b.mode, {"benchmarks": 0, "vulns": 0})
        by_mode[b.mode]["benchmarks"] += 1
        by_mode[b.mode]["vulns"] += len(b.vulns)

    # Count by language
    by_language: dict = {}
    for b in filtered:
        lang = b.language or "Unknown"
        by_language.setdefault(lang, {"benchmarks": 0, "vulns": 0})
        by_language[lang]["benchmarks"] += 1
        by_language[lang]["vulns"] += len(b.vulns)

    # Cross-tabulation: source x mode
    source_mode: dict = {}
    for b in filtered:
        key = (b.source, b.mode)
        source_mode.setdefault(key, {"benchmarks": 0, "vulns": 0})
        source_mode[key]["benchmarks"] += 1
        source_mode[key]["vulns"] += len(b.vulns)

    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)

        # Overall section
        writer.writerow(["Section", "Category", "Subcategory", "# Projects", "# Vulns"])
        writer.writerow(["Overall", "Total", "", total_benchmarks, total_vulns])
        writer.writerow(["Overall", "Harnesses", "", total_harnesses, ""])
        writer.writerow(["Overall", "POVs", "", total_povs, ""])
        writer.writerow(["Overall", "Patches", "", total_patches, ""])
        writer.writerow([])

        # File completeness
        writer.writerow(
            [
                "File Completeness",
                "test.sh",
                "",
                benchmarks_with_test_sh,
                f"{benchmarks_with_test_sh}/{total_benchmarks}",
            ]
        )
        writer.writerow(
            [
                "File Completeness",
                "vuln.yaml",
                "",
                vulns_with_vuln_yaml,
                f"{vulns_with_vuln_yaml}/{total_vulns}",
            ]
        )
        writer.writerow([])

        # By Source
        for source in ["AFC", "ASC", "Team-Atlanta"]:
            if source in by_source:
                stats = by_source[source]
                writer.writerow(
                    ["By Source", source, "", stats["benchmarks"], stats["vulns"]]
                )
        writer.writerow([])

        # By Mode
        for mode in ["delta", "full"]:
            if mode in by_mode:
                stats = by_mode[mode]
                writer.writerow(
                    ["By Mode", mode, "", stats["benchmarks"], stats["vulns"]]
                )
        writer.writerow([])

        # By Language
        for lang, stats in sorted(by_language.items()):
            writer.writerow(
                ["By Language", lang, "", stats["benchmarks"], stats["vulns"]]
            )
        writer.writerow([])

        # By Source x Mode
        for source in ["AFC", "ASC", "Team-Atlanta"]:
            for mode in ["delta", "full"]:
                key = (source, mode)
                if key in source_mode:
                    stats = source_mode[key]
                    writer.writerow(
                        [
                            "By Source x Mode",
                            source,
                            mode,
                            stats["benchmarks"],
                            stats["vulns"],
                        ]
                    )

    logger.info(f"Exported summary statistics to: {output_path}")


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Collect and export benchmark statistics to CSV",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Export all benchmarks to CSV (auto-detects benchmarks directory)
  python -m crsbench.statistics.benchmark_stats --output benchmarks.csv

  # Export specific benchmarks
  python -m crsbench.statistics.benchmark_stats \\
    --benchmarks atlanta-curl-delta-01,afc-libxml2-delta-01 \\
    --output selected.csv

  # Include benchmarks with no vulnerabilities
  python -m crsbench.statistics.benchmark_stats \\
    --output all.csv \\
    --include-no-vulns

  # Summary only (no CSV export)
  python -m crsbench.statistics.benchmark_stats --summary-only
        """,
    )

    parser.add_argument(
        "--benchmarks-dir",
        type=Path,
        default=None,
        help="Path to benchmarks directory (default: auto-detect from project root)",
    )

    parser.add_argument(
        "--benchmarks",
        type=str,
        help="Comma-separated list of specific benchmark names to process",
    )

    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=Path("benchmark_stats.csv"),
        help="Output CSV file path (default: benchmark_stats.csv)",
    )

    parser.add_argument(
        "--include-no-vulns",
        action="store_true",
        help="Include benchmarks with no vulnerabilities in output",
    )

    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="Print summary only, don't export CSV",
    )

    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Enable verbose logging"
    )

    args = parser.parse_args()

    # Setup logging
    level = "DEBUG" if args.verbose else "INFO"
    configure_logger(level=level)

    # Parse specific benchmarks
    specific_benchmarks = None
    if args.benchmarks:
        specific_benchmarks = [b.strip() for b in args.benchmarks.split(",")]

    # Get benchmarks directory (auto-detect if not specified)
    benchmarks_dir = args.benchmarks_dir or get_default_benchmarks_dir()

    # Collect statistics
    logger.info(f"Collecting benchmark statistics from: {benchmarks_dir}")
    benchmarks = collect_benchmark_stats(benchmarks_dir, specific_benchmarks)

    if not benchmarks:
        logger.warning("No benchmarks found")
        sys.exit(1)

    # Print summary
    print_summary(benchmarks)

    # Export CSV
    if not args.summary_only:
        export_benchmarks_csv(
            benchmarks, args.output, include_no_vulns=args.include_no_vulns
        )
        logger.info(f"CSV exported to: {args.output}")

        # Export summary CSV (auto-generate filename from output)
        summary_path = (
            args.output.parent / f"{args.output.stem}_summary{args.output.suffix}"
        )
        export_summary_csv(benchmarks, summary_path)
        logger.info(f"Summary CSV exported to: {summary_path}")


if __name__ == "__main__":
    main()
