"""Tests for VerdictResolver.

Tests verdict logic for FULL and DELTA modes (same logic, different base variant):
- Check base/ref crash → allpatched crash → CPV matches → UNINTENDED_CRASH
- FULL mode uses FULL_BASE, DELTA mode uses DELTA_REF
"""

from crsbench.builder.types import BenchmarkMode, VariantType
from crsbench.evaluation.verification.models import PovVerificationStatus
from crsbench.evaluation.verification.pov.verdict import VerdictResolver


class TestVerdictResolverFullMode:
    """Tests for FULL mode verdict resolution."""

    def test_base_no_crash_returns_not_vulnerable(self):
        """If base doesn't crash, POV is not vulnerable."""
        result = VerdictResolver.resolve(
            mode=BenchmarkMode.FULL,
            crash_results={
                VariantType.FULL_BASE: False,
                VariantType.ALL_PATCHED: False,
            },
            cpv_crash_map={},
            benchmark_name="test-bench",
            pov_id="pov_1",
        )
        assert result.status == PovVerificationStatus.NOT_VULNERABLE
        assert result.cpv_matched == []
        assert "does not crash" in result.details.lower()

    def test_allpatched_crashes_returns_unintended(self):
        """If allpatched crashes, it's an unintended crash."""
        result = VerdictResolver.resolve(
            mode=BenchmarkMode.FULL,
            crash_results={
                VariantType.FULL_BASE: True,
                VariantType.ALL_PATCHED: True,
            },
            cpv_crash_map={},
            benchmark_name="test-bench",
            pov_id="pov_1",
        )
        assert result.status == PovVerificationStatus.UNINTENDED_CRASH
        assert result.cpv_matched == []

    def test_cpv_match_single(self):
        """If base crashes and CPV variant crashes, return CPV."""
        result = VerdictResolver.resolve(
            mode=BenchmarkMode.FULL,
            crash_results={
                VariantType.FULL_BASE: True,
                VariantType.ALL_PATCHED: False,
            },
            cpv_crash_map={0: True, 1: False},
            benchmark_name="test-bench",
            pov_id="pov_1",
        )
        assert result.status == PovVerificationStatus.CPV
        assert result.cpv_matched == ["cpv_0"]

    def test_cpv_match_multiple(self):
        """If multiple CPV variants crash, all are matched."""
        result = VerdictResolver.resolve(
            mode=BenchmarkMode.FULL,
            crash_results={
                VariantType.FULL_BASE: True,
                VariantType.ALL_PATCHED: False,
            },
            cpv_crash_map={0: True, 1: True, 2: False},
            benchmark_name="test-bench",
            pov_id="pov_1",
        )
        assert result.status == PovVerificationStatus.CPV
        assert result.cpv_matched == ["cpv_0", "cpv_1"]

    def test_unintended_crash_when_base_crashes_no_cpv_match(self):
        """If base crashes but no CPV matches, it's an unintended crash."""
        result = VerdictResolver.resolve(
            mode=BenchmarkMode.FULL,
            crash_results={
                VariantType.FULL_BASE: True,
                VariantType.ALL_PATCHED: False,
            },
            cpv_crash_map={0: False, 1: False},
            benchmark_name="test-bench",
            pov_id="pov_1",
        )
        assert result.status == PovVerificationStatus.UNINTENDED_CRASH
        assert result.cpv_matched == []


class TestVerdictResolverDeltaMode:
    """Tests for DELTA mode verdict resolution.

    DELTA mode uses DELTA_REF as the vulnerable version (same logic as FULL mode).
    """

    def test_delta_ref_no_crash_returns_not_vulnerable(self):
        """In DELTA mode, if ref doesn't crash, POV is not vulnerable."""
        result = VerdictResolver.resolve(
            mode=BenchmarkMode.DELTA,
            crash_results={
                VariantType.DELTA_REF: False,
                VariantType.ALL_PATCHED: False,
            },
            cpv_crash_map={},
            benchmark_name="test-bench",
            pov_id="pov_1",
        )
        assert result.status == PovVerificationStatus.NOT_VULNERABLE

    def test_delta_allpatched_crashes_returns_unintended(self):
        """In DELTA mode, if allpatched crashes, it's an unintended crash."""
        result = VerdictResolver.resolve(
            mode=BenchmarkMode.DELTA,
            crash_results={
                VariantType.DELTA_REF: True,
                VariantType.ALL_PATCHED: True,
            },
            cpv_crash_map={},
            benchmark_name="test-bench",
            pov_id="pov_1",
        )
        assert result.status == PovVerificationStatus.UNINTENDED_CRASH

    def test_delta_cpv_match(self):
        """In DELTA mode, CPV matches are returned correctly."""
        result = VerdictResolver.resolve(
            mode=BenchmarkMode.DELTA,
            crash_results={
                VariantType.DELTA_REF: True,
                VariantType.ALL_PATCHED: False,
            },
            cpv_crash_map={0: True, 1: False},
            benchmark_name="test-bench",
            pov_id="pov_1",
        )
        assert result.status == PovVerificationStatus.CPV
        assert result.cpv_matched == ["cpv_0"]

    def test_delta_no_cpv_match_returns_unintended(self):
        """In DELTA mode, crashes ref but no CPV match is unintended crash."""
        result = VerdictResolver.resolve(
            mode=BenchmarkMode.DELTA,
            crash_results={
                VariantType.DELTA_REF: True,
                VariantType.ALL_PATCHED: False,
            },
            cpv_crash_map={0: False, 1: False},
            benchmark_name="test-bench",
            pov_id="pov_1",
        )
        assert result.status == PovVerificationStatus.UNINTENDED_CRASH


class TestVerdictResolverMetadata:
    """Test metadata in verdict results."""

    def test_benchmark_name_preserved(self):
        """Benchmark name should be in result."""
        result = VerdictResolver.resolve(
            mode=BenchmarkMode.FULL,
            crash_results={
                VariantType.FULL_BASE: False,
                VariantType.ALL_PATCHED: False,
            },
            cpv_crash_map={},
            benchmark_name="my-benchmark",
            pov_id="pov_1",
        )
        assert result.benchmark == "my-benchmark"

    def test_pov_id_preserved(self):
        """POV ID should be in result."""
        result = VerdictResolver.resolve(
            mode=BenchmarkMode.FULL,
            crash_results={
                VariantType.FULL_BASE: False,
                VariantType.ALL_PATCHED: False,
            },
            cpv_crash_map={},
            benchmark_name="test",
            pov_id="test_pov_123",
        )
        assert result.pov_id == "test_pov_123"

    def test_pov_id_optional(self):
        """POV ID can be None."""
        result = VerdictResolver.resolve(
            mode=BenchmarkMode.FULL,
            crash_results={
                VariantType.FULL_BASE: False,
                VariantType.ALL_PATCHED: False,
            },
            cpv_crash_map={},
            benchmark_name="test",
            pov_id=None,
        )
        assert result.pov_id is None

    def test_cpv_matched_sorted(self):
        """CPV matches should be sorted."""
        result = VerdictResolver.resolve(
            mode=BenchmarkMode.FULL,
            crash_results={
                VariantType.FULL_BASE: True,
                VariantType.ALL_PATCHED: False,
            },
            cpv_crash_map={2: True, 0: True, 1: True},
            benchmark_name="test",
            pov_id="pov_1",
        )
        assert result.cpv_matched == ["cpv_0", "cpv_1", "cpv_2"]
