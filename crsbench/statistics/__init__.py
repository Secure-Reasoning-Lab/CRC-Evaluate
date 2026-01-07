"""CRSBench statistics module for benchmark analysis and reporting.

This module provides functionality for collecting benchmark statistics,
generating summary reports, and exporting data to CSV format.

Main Components:
    - Models: BenchmarkInfo, VulnEntry, BenchmarkStats, CategoryStats
    - Collector: collect_benchmark_info, collect_benchmark_stats
    - Exporters: export_benchmarks_csv, export_summary_csv, print_summary

Usage:
    from crsbench.statistics import (
        collect_benchmark_stats,
        export_benchmarks_csv,
        print_summary,
        BenchmarkStats,
    )

    # Collect benchmark data
    benchmarks = collect_benchmark_stats(benchmarks_dir)

    # Calculate aggregate statistics
    stats = BenchmarkStats.from_benchmarks(benchmarks)

    # Export to CSV
    export_benchmarks_csv(benchmarks, output_path)

    # Print summary to console
    print_summary(benchmarks)

CLI Usage:
    crsbench stats --summary-only
    crsbench stats --output benchmarks.csv
"""

from crsbench.statistics.collector import (
    collect_benchmark_info,
    collect_benchmark_stats,
    count_files_in_dir,
    detect_mode,
    detect_source,
    find_project_root,
    get_default_benchmarks_dir,
)
from crsbench.statistics.exporters import (
    export_benchmarks_csv,
    export_summary_csv,
    print_summary,
)
from crsbench.statistics.models import (
    BenchmarkInfo,
    BenchmarkStats,
    CategoryStats,
    VulnEntry,
)

__all__ = [
    # Models
    "BenchmarkInfo",
    "BenchmarkStats",
    "CategoryStats",
    "VulnEntry",
    # Collector
    "collect_benchmark_info",
    "collect_benchmark_stats",
    # Exporters
    "export_benchmarks_csv",
    "export_summary_csv",
    "print_summary",
    # Utils (from collector)
    "count_files_in_dir",
    "detect_mode",
    "detect_source",
    "find_project_root",
    "get_default_benchmarks_dir",
]
