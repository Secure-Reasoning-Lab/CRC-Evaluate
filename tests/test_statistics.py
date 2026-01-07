"""Tests for the statistics module."""

from pathlib import Path

from crsbench.statistics import (
    BenchmarkInfo,
    BenchmarkStats,
    CategoryStats,
    VulnEntry,
    detect_mode,
    detect_source,
)


class TestDetectSource:
    """Tests for detect_source function."""

    def test_team_atlanta(self):
        assert detect_source("atlanta-curl-delta-01") == "Team-Atlanta"

    def test_afc(self):
        assert detect_source("afc-libxml2-delta-01") == "AFC"

    def test_asc(self):
        assert detect_source("asc-example-full-01") == "ASC"

    def test_sanity(self):
        assert detect_source("sanity-mock-c-delta-01") == "Sanity"

    def test_unknown(self):
        assert detect_source("other-project") == "Unknown"

    def test_case_insensitive(self):
        assert detect_source("ATLANTA-test") == "Team-Atlanta"
        assert detect_source("AFC-test") == "AFC"


class TestDetectMode:
    """Tests for detect_mode function."""

    def test_delta_mode(self):
        assert detect_mode({"delta_mode": {"base_commit": "abc"}}) == "delta"

    def test_full_mode(self):
        assert detect_mode({"full_mode": {"base_commit": "abc"}}) == "full"

    def test_unknown_mode(self):
        assert detect_mode({}) == "unknown"

    def test_delta_priority(self):
        # delta_mode takes priority if both exist
        meta = {"delta_mode": {"base_commit": "a"}, "full_mode": {"base_commit": "b"}}
        assert detect_mode(meta) == "delta"


class TestBenchmarkStats:
    """Tests for BenchmarkStats.from_benchmarks."""

    def test_empty_list(self):
        stats = BenchmarkStats.from_benchmarks([])
        assert stats.total_benchmarks == 0
        assert stats.total_vulns == 0

    def test_excludes_sanity(self):
        benchmarks = [
            BenchmarkInfo(name="atlanta-test", path=Path(), source="Team-Atlanta"),
            BenchmarkInfo(name="sanity-test", path=Path(), source="Sanity"),
        ]
        stats = BenchmarkStats.from_benchmarks(benchmarks)
        assert stats.total_benchmarks == 1
        assert stats.sanity_count == 1

    def test_counts_vulns(self):
        vuln = VulnEntry(
            benchmark_name="test",
            source="AFC",
            repo_url="",
            mode="delta",
            language="C/C++",
            harness_name="fuzz_test",
            vuln_id="CVE-2024-001",
            num_povs=2,
            num_patches=1,
        )
        benchmarks = [
            BenchmarkInfo(
                name="afc-test",
                path=Path(),
                source="AFC",
                vulns=[vuln, vuln],
            ),
        ]
        stats = BenchmarkStats.from_benchmarks(benchmarks)
        assert stats.total_vulns == 2
        assert stats.total_povs == 4
        assert stats.total_patches == 2

    def test_by_source(self):
        benchmarks = [
            BenchmarkInfo(name="afc-1", path=Path(), source="AFC"),
            BenchmarkInfo(name="afc-2", path=Path(), source="AFC"),
            BenchmarkInfo(name="asc-1", path=Path(), source="ASC"),
        ]
        stats = BenchmarkStats.from_benchmarks(benchmarks)
        assert stats.by_source["AFC"].benchmarks == 2
        assert stats.by_source["ASC"].benchmarks == 1

    def test_by_mode(self):
        benchmarks = [
            BenchmarkInfo(name="test-1", path=Path(), source="AFC", mode="delta"),
            BenchmarkInfo(name="test-2", path=Path(), source="AFC", mode="delta"),
            BenchmarkInfo(name="test-3", path=Path(), source="AFC", mode="full"),
        ]
        stats = BenchmarkStats.from_benchmarks(benchmarks)
        assert stats.by_mode["delta"].benchmarks == 2
        assert stats.by_mode["full"].benchmarks == 1

    def test_completeness_counts(self):
        vuln_with_yaml = VulnEntry(
            benchmark_name="test",
            source="AFC",
            repo_url="",
            mode="delta",
            language="C/C++",
            harness_name="fuzz",
            vuln_id="CVE-001",
            has_vuln_yaml=True,
        )
        vuln_without_yaml = VulnEntry(
            benchmark_name="test",
            source="AFC",
            repo_url="",
            mode="delta",
            language="C/C++",
            harness_name="fuzz",
            vuln_id="CVE-002",
            has_vuln_yaml=False,
        )
        benchmarks = [
            BenchmarkInfo(
                name="test-1",
                path=Path(),
                source="AFC",
                has_test_sh=True,
                vulns=[vuln_with_yaml],
            ),
            BenchmarkInfo(
                name="test-2",
                path=Path(),
                source="AFC",
                has_test_sh=False,
                vulns=[vuln_without_yaml],
            ),
        ]
        stats = BenchmarkStats.from_benchmarks(benchmarks)
        assert stats.benchmarks_with_test_sh == 1
        assert stats.vulns_with_vuln_yaml == 1

    def test_rts_and_inc_build_counts(self):
        benchmarks = [
            BenchmarkInfo(
                name="test-1",
                path=Path(),
                source="AFC",
                rts_mode="jcgeks",
                inc_build=True,
            ),
            BenchmarkInfo(
                name="test-2",
                path=Path(),
                source="AFC",
                rts_mode="openclover",
                inc_build=False,
            ),
            BenchmarkInfo(
                name="test-3",
                path=Path(),
                source="AFC",
                rts_mode=None,
                inc_build=False,
            ),
        ]
        stats = BenchmarkStats.from_benchmarks(benchmarks)
        assert stats.benchmarks_with_rts == 2
        assert stats.benchmarks_with_inc_build == 1

    def test_rts_and_inc_build_defaults(self):
        # Test that defaults are None/True when not specified
        benchmarks = [
            BenchmarkInfo(name="test-1", path=Path(), source="AFC"),
        ]
        stats = BenchmarkStats.from_benchmarks(benchmarks)
        assert stats.benchmarks_with_rts == 0
        assert stats.benchmarks_with_inc_build == 1  # inc_build defaults to True


class TestCategoryStats:
    """Tests for CategoryStats dataclass."""

    def test_defaults(self):
        cat = CategoryStats()
        assert cat.benchmarks == 0
        assert cat.vulns == 0

    def test_values(self):
        cat = CategoryStats(benchmarks=5, vulns=10)
        assert cat.benchmarks == 5
        assert cat.vulns == 10
