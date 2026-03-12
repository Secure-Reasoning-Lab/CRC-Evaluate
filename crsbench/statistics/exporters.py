"""Export and output functions for benchmark statistics."""

import csv
from collections.abc import Sequence
from pathlib import Path

from crsbench.statistics.cwe_utils import CWE_TOP_25_2025, get_pillar_name
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
                harness_name = (
                    ";".join(info.harnesses)
                    if info.harnesses
                    else "(no vulnerable harness)"
                )
                writer.writerow(
                    [
                        info.name,
                        info.source,
                        info.repo_url,
                        info.mode,
                        info.language,
                        harness_name,
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
        writer.writerow(
            [
                "Section",
                "Category",
                "Subcategory",
                "Primary Value",
                "Secondary Value",
                "Tertiary Value",
            ]
        )
        writer.writerow(
            [
                "Overall",
                "Total",
                "",
                stats.total_benchmarks,
                stats.unique_projects,
                stats.total_vulns,
            ]
        )
        writer.writerow(["Overall", "Harnesses", "", stats.total_harnesses, "", ""])
        writer.writerow(
            [
                "Overall",
                "Vulnerable Harnesses",
                "",
                stats.total_vulnerable_harnesses,
                "",
                "",
            ]
        )
        writer.writerow(["Overall", "POVs", "", stats.total_povs, "", ""])
        writer.writerow(["Overall", "Patches", "", stats.total_patches, "", ""])
        writer.writerow([])

        # File completeness
        writer.writerow(
            [
                "File Completeness",
                "test.sh",
                "",
                stats.benchmarks_with_test_sh,
                "",
                f"{stats.benchmarks_with_test_sh}/{stats.total_benchmarks}",
            ]
        )
        writer.writerow(
            [
                "File Completeness",
                "vuln.yaml",
                "",
                stats.vulns_with_vuln_yaml,
                "",
                f"{stats.vulns_with_vuln_yaml}/{stats.total_vulns}",
            ]
        )
        writer.writerow([])

        # Feature support
        writer.writerow(
            [
                "Feature Support",
                "RTS mode declared",
                "",
                stats.benchmarks_with_rts,
                "",
                f"{stats.benchmarks_with_rts}/{stats.total_benchmarks}",
            ]
        )
        writer.writerow(
            [
                "Feature Support",
                "Incremental build",
                "",
                stats.benchmarks_with_inc_build,
                "",
                f"{stats.benchmarks_with_inc_build}/{stats.total_benchmarks}",
            ]
        )
        writer.writerow([])

        # Vulns per vulnerable harness
        writer.writerow(
            [
                "Vulns per Vulnerable Harness",
                "Average",
                "",
                f"{stats.avg_vulns_per_harness:.2f}",
                "",
                "",
            ]
        )
        writer.writerow(
            [
                "Vulns per Vulnerable Harness",
                "Min",
                "",
                stats.min_vulns_per_harness,
                "",
                "",
            ]
        )
        writer.writerow(
            [
                "Vulns per Vulnerable Harness",
                "Max",
                "",
                stats.max_vulns_per_harness,
                "",
                "",
            ]
        )
        # Histogram
        for vuln_count in sorted(stats.vulns_per_harness_histogram.keys()):
            num_harnesses = stats.vulns_per_harness_histogram[vuln_count]
            writer.writerow(
                [
                    "Vulns per Vulnerable Harness Histogram",
                    f"{vuln_count} vulns",
                    "",
                    num_harnesses,
                    "",
                    "",
                ]
            )
        writer.writerow([])

        # By Source
        for source in sorted(stats.by_source):
            if source in stats.by_source:
                cat = stats.by_source[source]
                writer.writerow(
                    [
                        "By Source",
                        source,
                        "",
                        cat.benchmarks,
                        cat.unique_projects,
                        cat.vulns,
                    ]
                )
        writer.writerow([])

        # By Mode
        for mode in sorted(stats.by_mode):
            if mode in stats.by_mode:
                cat = stats.by_mode[mode]
                writer.writerow(
                    [
                        "By Mode",
                        mode,
                        "",
                        cat.benchmarks,
                        cat.unique_projects,
                        cat.vulns,
                    ]
                )
        writer.writerow([])

        # By Language
        for lang, cat in sorted(stats.by_language.items()):
            writer.writerow(
                [
                    "By Language",
                    lang,
                    "",
                    cat.benchmarks,
                    cat.unique_projects,
                    cat.vulns,
                ]
            )
        writer.writerow([])

        # By Source x Mode
        for source, mode in sorted(stats.source_mode):
            key = (source, mode)
            if key in stats.source_mode:
                cat = stats.source_mode[key]
                writer.writerow(
                    [
                        "By Source x Mode",
                        source,
                        mode,
                        cat.benchmarks,
                        cat.unique_projects,
                        cat.vulns,
                    ]
                )

        # By Source x Language x Mode
        for source, lang, mode in sorted(stats.source_language_mode):
            key = (source, lang, mode)
            if key in stats.source_language_mode:
                cat = stats.source_language_mode[key]
                writer.writerow(
                    [
                        "By Source x Language x Mode",
                        source,
                        f"{lang} {mode}",
                        cat.benchmarks,
                        cat.unique_projects,
                        cat.vulns,
                    ]
                )
        writer.writerow([])

        # By CWE (sorted by count descending)
        for cwe, count in sorted(stats.by_cwe.items(), key=lambda x: (-x[1], x[0])):
            writer.writerow(["By CWE", cwe, "", "", "", count])
        writer.writerow([])

        # By Pillar (sorted by count descending)
        for pillar_id, count in sorted(
            stats.by_pillar.items(), key=lambda x: (-x[1], x[0])
        ):
            if pillar_id == "Other":
                pillar_display = "Other (unmapped)"
            else:
                pillar_name = get_pillar_name(pillar_id)
                pillar_display = f"CWE-{pillar_id}: {pillar_name}"
            writer.writerow(["By Pillar", pillar_display, "", "", "", count])
        writer.writerow([])

        # 2025 CWE Top 25 (in rank order)
        for rank, (cwe_id, name) in enumerate(CWE_TOP_25_2025, 1):
            count = stats.by_top25.get(cwe_id, 0)
            writer.writerow(["Top 25", f"#{rank}", f"CWE-{cwe_id}", name, "", count])

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
    logger.info(f"  # of Benchmarks:      {stats.total_benchmarks}")
    logger.info(f"  # of Unique Projects: {stats.unique_projects}")
    logger.info(f"  # of Vulns:           {stats.total_vulns}")
    logger.info(f"  # of Harnesses:       {stats.total_harnesses}")
    logger.info(f"  # of Vuln Harnesses:  {stats.total_vulnerable_harnesses}")
    logger.info(f"  # of POVs:            {stats.total_povs}")
    logger.info(f"  # of Patches:         {stats.total_patches}")
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
        f"  RTS mode declared:       "
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
    logger.info(
        f"  {'Source':<15} {'# Benchmarks':>12} {'# Unique':>10} {'# Vulns':>10}"
    )
    logger.info(f"  {'-' * 15} {'-' * 12} {'-' * 10} {'-' * 10}")
    for source in sorted(stats.by_source):
        if source in stats.by_source:
            cat = stats.by_source[source]
            logger.info(
                f"  {source:<15} {cat.benchmarks:>12} {cat.unique_projects:>10} {cat.vulns:>10}"
            )
    logger.info(f"  {'-' * 15} {'-' * 12} {'-' * 10} {'-' * 10}")
    logger.info(
        f"  {'Total':<15} {stats.total_benchmarks:>12} {stats.unique_projects:>10} {stats.total_vulns:>10}"
    )
    logger.info("")

    logger.info("-" * 70)
    logger.info("By Mode:")
    logger.info("-" * 70)
    logger.info(f"  {'Mode':<15} {'# Benchmarks':>12} {'# Unique':>10} {'# Vulns':>10}")
    logger.info(f"  {'-' * 15} {'-' * 12} {'-' * 10} {'-' * 10}")
    for mode in sorted(stats.by_mode):
        if mode in stats.by_mode:
            cat = stats.by_mode[mode]
            logger.info(
                f"  {mode:<15} {cat.benchmarks:>12} {cat.unique_projects:>10} {cat.vulns:>10}"
            )
    logger.info(f"  {'-' * 15} {'-' * 12} {'-' * 10} {'-' * 10}")
    logger.info(
        f"  {'Total':<15} {stats.total_benchmarks:>12} {stats.unique_projects:>10} {stats.total_vulns:>10}"
    )
    logger.info("")

    logger.info("-" * 70)
    logger.info("By Language:")
    logger.info("-" * 70)
    logger.info(
        f"  {'Language':<15} {'# Benchmarks':>12} {'# Unique':>10} {'# Vulns':>10}"
    )
    logger.info(f"  {'-' * 15} {'-' * 12} {'-' * 10} {'-' * 10}")
    for lang, cat in sorted(stats.by_language.items()):
        logger.info(
            f"  {lang:<15} {cat.benchmarks:>12} {cat.unique_projects:>10} {cat.vulns:>10}"
        )
    logger.info(f"  {'-' * 15} {'-' * 12} {'-' * 10} {'-' * 10}")
    logger.info(
        f"  {'Total':<15} {stats.total_benchmarks:>12} {stats.unique_projects:>10} {stats.total_vulns:>10}"
    )
    logger.info("")

    logger.info("-" * 70)
    logger.info("By Source x Mode:")
    logger.info("-" * 70)
    logger.info(
        f"  {'Source':<15} {'Mode':<10} {'# Benchmarks':>12} {'# Unique':>10} {'# Vulns':>10}"
    )
    logger.info(f"  {'-' * 15} {'-' * 10} {'-' * 12} {'-' * 10} {'-' * 10}")
    for source, mode in sorted(stats.source_mode):
        key = (source, mode)
        if key in stats.source_mode:
            cat = stats.source_mode[key]
            logger.info(
                f"  {source:<15} {mode:<10} {cat.benchmarks:>12} {cat.unique_projects:>10} {cat.vulns:>10}"
            )
    logger.info(f"  {'-' * 15} {'-' * 10} {'-' * 12} {'-' * 10} {'-' * 10}")
    logger.info(
        f"  {'Total':<15} {'':<10} {stats.total_benchmarks:>12} {stats.unique_projects:>10} {stats.total_vulns:>10}"
    )
    logger.info("")

    logger.info("-" * 70)
    logger.info("By Source x Language x Mode:")
    logger.info("-" * 70)
    logger.info(
        f"  {'Source':<15} {'Language':<10} {'Mode':<8} {'# Benchmarks':>12} {'# Unique':>10} {'# Vulns':>10}"
    )
    logger.info(f"  {'-' * 15} {'-' * 10} {'-' * 8} {'-' * 12} {'-' * 10} {'-' * 10}")
    for source, lang, mode in sorted(stats.source_language_mode):
        key = (source, lang, mode)
        if key in stats.source_language_mode:
            cat = stats.source_language_mode[key]
            logger.info(
                f"  {source:<15} {lang:<10} {mode:<8} "
                f"{cat.benchmarks:>12} {cat.unique_projects:>10} {cat.vulns:>10}"
            )
    logger.info(f"  {'-' * 15} {'-' * 10} {'-' * 8} {'-' * 12} {'-' * 10} {'-' * 10}")
    logger.info(
        f"  {'Total':<15} {'':<10} {'':<8} "
        f"{stats.total_benchmarks:>12} {stats.unique_projects:>10} {stats.total_vulns:>10}"
    )
    logger.info("")

    logger.info("-" * 70)
    logger.info("Vulns per Vulnerable Harness:")
    logger.info("-" * 70)
    logger.info(f"  Average: {stats.avg_vulns_per_harness:.2f}")
    logger.info(f"  Min:     {stats.min_vulns_per_harness}")
    logger.info(f"  Max:     {stats.max_vulns_per_harness}")
    logger.info("")
    logger.info("  Histogram (vulns -> # vulnerable harnesses):")
    if stats.vulns_per_harness_histogram:
        max_count = max(stats.vulns_per_harness_histogram.values())
        max_bar_width = 40
        for vuln_count in sorted(stats.vulns_per_harness_histogram.keys()):
            num_harnesses = stats.vulns_per_harness_histogram[vuln_count]
            bar_len = (
                int((num_harnesses / max_count) * max_bar_width) if max_count else 0
            )
            bar = "█" * bar_len
            logger.info(f"    {vuln_count:>3} vulns: {bar} ({num_harnesses})")
    logger.info("")

    logger.info("-" * 70)
    logger.info("By CWE:")
    logger.info("-" * 70)
    logger.info(f"  {'CWE':<15} {'# Vulns':>10}")
    logger.info(f"  {'-' * 15} {'-' * 10}")
    # Sort by count descending, then by CWE name
    for cwe, count in sorted(stats.by_cwe.items(), key=lambda x: (-x[1], x[0])):
        logger.info(f"  {cwe:<15} {count:>10}")
    logger.info(f"  {'-' * 15} {'-' * 10}")
    total_cwe_refs = sum(stats.by_cwe.values())
    logger.info(f"  {'Total':<15} {total_cwe_refs:>10}")
    logger.info(f"  (Unique CWEs: {len(stats.by_cwe)})")
    logger.info("")

    logger.info("-" * 70)
    logger.info("By Pillar (CWE Root Category):")
    logger.info("-" * 70)
    logger.info(f"  {'Pillar':<60} {'# Vulns':>8}")
    logger.info(f"  {'-' * 60} {'-' * 8}")
    # Sort by count descending
    for pillar_id, count in sorted(
        stats.by_pillar.items(), key=lambda x: (-x[1], x[0])
    ):
        if pillar_id == "Other":
            pillar_display = "Other (unmapped)"
        else:
            pillar_name = get_pillar_name(pillar_id)
            pillar_display = f"CWE-{pillar_id}: {pillar_name}"
        logger.info(f"  {pillar_display:<60} {count:>8}")
    logger.info(f"  {'-' * 60} {'-' * 8}")
    total_pillar_refs = sum(stats.by_pillar.values())
    logger.info(f"  {'Total':<60} {total_pillar_refs:>8}")
    logger.info("")

    logger.info("-" * 70)
    logger.info("2025 CWE Top 25 Coverage:")
    logger.info("-" * 70)
    logger.info(f"  {'Rank':<5} {'CWE':<10} {'Name':<43} {'# Vulns':>8}")
    logger.info(f"  {'-' * 5} {'-' * 10} {'-' * 43} {'-' * 8}")
    top25_covered = 0
    top25_total_vulns = 0
    for rank, (cwe_id, name) in enumerate(CWE_TOP_25_2025, 1):
        count = stats.by_top25.get(cwe_id, 0)
        status = "" if count > 0 else "✗"
        if count > 0:
            top25_covered += 1
            top25_total_vulns += count
        # Truncate name if too long
        display_name = name[:40] + "..." if len(name) > 43 else name
        logger.info(
            f"  {rank:<5} CWE-{cwe_id:<6} {display_name:<43} {count:>7} {status}"
        )
    logger.info(f"  {'-' * 5} {'-' * 10} {'-' * 43} {'-' * 8}")
    logger.info(f"  Coverage: {top25_covered}/25 ({top25_covered * 100 // 25}%)")
    logger.info(f"  Total vulns matching Top 25: {top25_total_vulns}")
    logger.info("=" * 70)
