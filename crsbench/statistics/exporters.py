"""Export and output functions for benchmark statistics."""

import csv
from collections.abc import Sequence
from pathlib import Path

from crsbench.statistics.models import BenchmarkInfo, BenchmarkStats
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


def export_benchmarks_csv(
    benchmarks: Sequence[BenchmarkInfo],
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


def export_summary_csv(benchmarks: Sequence[BenchmarkInfo], output_path: Path) -> None:
    """Export summary statistics to CSV file.

    Args:
        benchmarks: List of BenchmarkInfo objects
        output_path: Path to output CSV file
    """
    # Calculate stats once using the shared dataclass
    stats = BenchmarkStats.from_benchmarks(benchmarks)

    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)

        # Overall section
        writer.writerow(["Section", "Category", "Subcategory", "# Projects", "# Vulns"])
        writer.writerow(
            ["Overall", "Total", "", stats.total_benchmarks, stats.total_vulns]
        )
        writer.writerow(["Overall", "Harnesses", "", stats.total_harnesses, ""])
        writer.writerow(["Overall", "POVs", "", stats.total_povs, ""])
        writer.writerow(["Overall", "Patches", "", stats.total_patches, ""])
        writer.writerow([])

        # File completeness
        writer.writerow(
            [
                "File Completeness",
                "test.sh",
                "",
                stats.benchmarks_with_test_sh,
                f"{stats.benchmarks_with_test_sh}/{stats.total_benchmarks}",
            ]
        )
        writer.writerow(
            [
                "File Completeness",
                "vuln.yaml",
                "",
                stats.vulns_with_vuln_yaml,
                f"{stats.vulns_with_vuln_yaml}/{stats.total_vulns}",
            ]
        )
        writer.writerow([])

        # Feature support
        writer.writerow(
            [
                "Feature Support",
                "RTS mode",
                "",
                stats.benchmarks_with_rts,
                f"{stats.benchmarks_with_rts}/{stats.total_benchmarks}",
            ]
        )
        writer.writerow(
            [
                "Feature Support",
                "Incremental build",
                "",
                stats.benchmarks_with_inc_build,
                f"{stats.benchmarks_with_inc_build}/{stats.total_benchmarks}",
            ]
        )
        writer.writerow([])

        # By Source
        for source in ["AFC", "ASC", "Team-Atlanta"]:
            if source in stats.by_source:
                cat = stats.by_source[source]
                writer.writerow(["By Source", source, "", cat.benchmarks, cat.vulns])
        writer.writerow([])

        # By Mode
        for mode in ["delta", "full"]:
            if mode in stats.by_mode:
                cat = stats.by_mode[mode]
                writer.writerow(["By Mode", mode, "", cat.benchmarks, cat.vulns])
        writer.writerow([])

        # By Language
        for lang, cat in sorted(stats.by_language.items()):
            writer.writerow(["By Language", lang, "", cat.benchmarks, cat.vulns])
        writer.writerow([])

        # By Source x Mode
        for source in ["AFC", "ASC", "Team-Atlanta"]:
            for mode in ["delta", "full"]:
                key = (source, mode)
                if key in stats.source_mode:
                    cat = stats.source_mode[key]
                    writer.writerow(
                        [
                            "By Source x Mode",
                            source,
                            mode,
                            cat.benchmarks,
                            cat.vulns,
                        ]
                    )

    logger.info(f"Exported summary statistics to: {output_path}")


def print_summary(benchmarks: Sequence[BenchmarkInfo]) -> None:
    """Print summary statistics to console.

    Args:
        benchmarks: List of BenchmarkInfo objects
    """
    stats = BenchmarkStats.from_benchmarks(benchmarks)

    logger.info("=" * 70)
    logger.info("BENCHMARK STATISTICS SUMMARY")
    logger.info(f"(Excluding {stats.sanity_count} Sanity benchmarks)")
    logger.info("=" * 70)
    logger.info("")

    logger.info("Overall:")
    logger.info(f"  # of Projects:  {stats.total_benchmarks}")
    logger.info(f"  # of Vulns:     {stats.total_vulns}")
    logger.info(f"  # of Harnesses: {stats.total_harnesses}")
    logger.info(f"  # of POVs:      {stats.total_povs}")
    logger.info(f"  # of Patches:   {stats.total_patches}")
    logger.info("")

    logger.info("File Completeness:")
    logger.info(
        f"  Benchmarks with test.sh: "
        f"{stats.benchmarks_with_test_sh}/{stats.total_benchmarks}"
    )
    logger.info(
        f"  Vulns with vuln.yaml:    {stats.vulns_with_vuln_yaml}/{stats.total_vulns}"
    )
    logger.info("")

    logger.info("Feature Support:")
    logger.info(
        f"  RTS mode enabled:        "
        f"{stats.benchmarks_with_rts}/{stats.total_benchmarks}"
    )
    logger.info(
        f"  Incremental build:       "
        f"{stats.benchmarks_with_inc_build}/{stats.total_benchmarks}"
    )
    logger.info("")

    logger.info("-" * 70)
    logger.info("By Source:")
    logger.info("-" * 70)
    logger.info(f"  {'Source':<15} {'# Projects':>12} {'# Vulns':>12}")
    logger.info(f"  {'-' * 15} {'-' * 12} {'-' * 12}")
    for source in ["AFC", "ASC", "Team-Atlanta"]:
        if source in stats.by_source:
            cat = stats.by_source[source]
            logger.info(f"  {source:<15} {cat.benchmarks:>12} {cat.vulns:>12}")
    logger.info(f"  {'-' * 15} {'-' * 12} {'-' * 12}")
    logger.info(f"  {'Total':<15} {stats.total_benchmarks:>12} {stats.total_vulns:>12}")
    logger.info("")

    logger.info("-" * 70)
    logger.info("By Mode:")
    logger.info("-" * 70)
    logger.info(f"  {'Mode':<15} {'# Projects':>12} {'# Vulns':>12}")
    logger.info(f"  {'-' * 15} {'-' * 12} {'-' * 12}")
    for mode in ["delta", "full"]:
        if mode in stats.by_mode:
            cat = stats.by_mode[mode]
            logger.info(f"  {mode:<15} {cat.benchmarks:>12} {cat.vulns:>12}")
    logger.info(f"  {'-' * 15} {'-' * 12} {'-' * 12}")
    logger.info(f"  {'Total':<15} {stats.total_benchmarks:>12} {stats.total_vulns:>12}")
    logger.info("")

    logger.info("-" * 70)
    logger.info("By Language:")
    logger.info("-" * 70)
    logger.info(f"  {'Language':<15} {'# Projects':>12} {'# Vulns':>12}")
    logger.info(f"  {'-' * 15} {'-' * 12} {'-' * 12}")
    for lang, cat in sorted(stats.by_language.items()):
        logger.info(f"  {lang:<15} {cat.benchmarks:>12} {cat.vulns:>12}")
    logger.info(f"  {'-' * 15} {'-' * 12} {'-' * 12}")
    logger.info(f"  {'Total':<15} {stats.total_benchmarks:>12} {stats.total_vulns:>12}")
    logger.info("")

    logger.info("-" * 70)
    logger.info("By Source x Mode:")
    logger.info("-" * 70)
    logger.info(f"  {'Source':<15} {'Mode':<10} {'# Projects':>12} {'# Vulns':>12}")
    logger.info(f"  {'-' * 15} {'-' * 10} {'-' * 12} {'-' * 12}")
    for source in ["AFC", "ASC", "Team-Atlanta"]:
        for mode in ["delta", "full"]:
            key = (source, mode)
            if key in stats.source_mode:
                cat = stats.source_mode[key]
                logger.info(
                    f"  {source:<15} {mode:<10} {cat.benchmarks:>12} {cat.vulns:>12}"
                )
    logger.info(f"  {'-' * 15} {'-' * 10} {'-' * 12} {'-' * 12}")
    logger.info(
        f"  {'Total':<15} {'':<10} {stats.total_benchmarks:>12} {stats.total_vulns:>12}"
    )
    logger.info("=" * 70)
