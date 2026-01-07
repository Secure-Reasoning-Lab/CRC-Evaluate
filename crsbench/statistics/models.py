"""Data models for benchmark statistics."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


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
    rts_mode: str | None = None  # None, "jcgeks", "openclover", "binary_rts"
    inc_build: bool = True
    harnesses: list[str] = field(default_factory=list)
    vulns: list[VulnEntry] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class CategoryStats:
    """Statistics for a category (source, mode, language)."""

    benchmarks: int = 0
    vulns: int = 0


@dataclass
class BenchmarkStats:
    """Aggregated benchmark statistics.

    This dataclass eliminates the duplicate calculation logic between
    print_summary() and export_summary_csv().
    """

    # Core counts
    total_benchmarks: int = 0
    total_vulns: int = 0
    total_harnesses: int = 0
    total_povs: int = 0
    total_patches: int = 0

    # Completeness counts
    benchmarks_with_test_sh: int = 0
    vulns_with_vuln_yaml: int = 0

    # RTS and incremental build support
    benchmarks_with_rts: int = 0
    benchmarks_with_inc_build: int = 0

    # Sanity benchmarks (excluded from stats but tracked)
    sanity_count: int = 0

    # Categorized counts
    by_source: dict[str, CategoryStats] = field(default_factory=dict)
    by_mode: dict[str, CategoryStats] = field(default_factory=dict)
    by_language: dict[str, CategoryStats] = field(default_factory=dict)
    source_mode: dict[tuple[str, str], CategoryStats] = field(default_factory=dict)

    @classmethod
    def from_benchmarks(cls, benchmarks: Sequence[BenchmarkInfo]) -> BenchmarkStats:
        """Calculate statistics from a list of BenchmarkInfo objects.

        This is the SINGLE source of truth for statistics calculation,
        replacing the duplicate logic in print_summary() and export_summary_csv().

        Args:
            benchmarks: List of BenchmarkInfo objects

        Returns:
            BenchmarkStats with calculated statistics
        """
        # Filter out Sanity benchmarks
        filtered = [b for b in benchmarks if b.source != "Sanity"]
        sanity_count = len(benchmarks) - len(filtered)

        stats = cls(
            total_benchmarks=len(filtered),
            total_vulns=sum(len(b.vulns) for b in filtered),
            total_harnesses=sum(len(b.harnesses) for b in filtered),
            benchmarks_with_test_sh=sum(1 for b in filtered if b.has_test_sh),
            vulns_with_vuln_yaml=sum(
                1 for b in filtered for v in b.vulns if v.has_vuln_yaml
            ),
            benchmarks_with_rts=sum(1 for b in filtered if b.rts_mode),
            benchmarks_with_inc_build=sum(1 for b in filtered if b.inc_build),
            total_povs=sum(v.num_povs for b in filtered for v in b.vulns),
            total_patches=sum(v.num_patches for b in filtered for v in b.vulns),
            sanity_count=sanity_count,
        )

        # Calculate categorized statistics
        for b in filtered:
            # By source
            if b.source not in stats.by_source:
                stats.by_source[b.source] = CategoryStats()
            stats.by_source[b.source].benchmarks += 1
            stats.by_source[b.source].vulns += len(b.vulns)

            # By mode
            if b.mode not in stats.by_mode:
                stats.by_mode[b.mode] = CategoryStats()
            stats.by_mode[b.mode].benchmarks += 1
            stats.by_mode[b.mode].vulns += len(b.vulns)

            # By language
            lang = b.language or "Unknown"
            if lang not in stats.by_language:
                stats.by_language[lang] = CategoryStats()
            stats.by_language[lang].benchmarks += 1
            stats.by_language[lang].vulns += len(b.vulns)

            # Source x Mode cross-tabulation
            key = (b.source, b.mode)
            if key not in stats.source_mode:
                stats.source_mode[key] = CategoryStats()
            stats.source_mode[key].benchmarks += 1
            stats.source_mode[key].vulns += len(b.vulns)

        return stats
