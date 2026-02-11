"""Tests for POV verification modules.

Tests cover:
- VerdictResolver (FULL and DELTA mode verdict logic)
- Deduplication strategies
- Verification models
"""

import pytest
from crsbench.builder.types import BenchmarkMode, VariantType
from crsbench.evaluation.verification.dedup import (
    NoOpDedup,
    PatchBasedDedup,
    StatusBasedDedup,
    get_dedup_strategy,
)
from crsbench.evaluation.verification.models import (
    PovVerificationRequest,
    PovVerificationResult,
    PovVerificationStatus,
)
from crsbench.evaluation.verification.pov.verdict import VerdictResolver


class TestVariantType:
    """Tests for VariantType enum."""

    def test_variant_type_values(self):
        """Test VariantType enum values."""
        assert VariantType.FULL_BASE.value == "fullbase"
        assert VariantType.DELTA_REF.value == "deltaref"
        assert VariantType.ALL_PATCHED.value == "allpatched"
        assert VariantType.CPV.value == "cpv"
        assert VariantType.COVERAGE.value == "coverage"

    def test_variant_type_exact_members(self):
        """Verify VariantType has exactly the expected members."""
        expected = {
            "FULL_BASE",
            "DELTA_REF",
            "ALL_PATCHED",
            "CPV",
            "PATCHED",
            "COVERAGE",
        }
        actual = {v.name for v in VariantType}
        assert actual == expected, (
            f"VariantType members mismatch: {actual - expected} extra, {expected - actual} missing"
        )


class TestBenchmarkMode:
    """Tests for BenchmarkMode enum."""

    def test_benchmark_mode_values(self):
        """Test BenchmarkMode enum values."""
        assert BenchmarkMode.FULL.value == "full"
        assert BenchmarkMode.DELTA.value == "delta"


class TestPovVerificationStatus:
    """Tests for PovVerificationStatus enum."""

    def test_status_exact_members(self):
        """Verify PovVerificationStatus has exactly the expected members."""
        expected = {"NOT_VULNERABLE", "CPV", "UNINTENDED_CRASH", "ERROR"}
        actual = {s.name for s in PovVerificationStatus}
        assert actual == expected, (
            f"Status members mismatch: {actual - expected} extra, {expected - actual} missing"
        )

    def test_status_values(self):
        """Test PovVerificationStatus enum values."""
        assert PovVerificationStatus.NOT_VULNERABLE.value == "not_vulnerable"
        assert PovVerificationStatus.CPV.value == "cpv"
        assert PovVerificationStatus.UNINTENDED_CRASH.value == "unintended_crash"
        assert PovVerificationStatus.ERROR.value == "error"


class TestVerificationModels:
    """Tests for verification models."""

    def test_verification_request(self):
        """Test PovVerificationRequest creation."""
        request = PovVerificationRequest(
            pov_data=b"test data",
            harness="test_harness",
            benchmark="test-bench",
            pov_id="pov_0",
        )
        assert request.pov_data == b"test data"
        assert request.harness == "test_harness"

    def test_verification_result(self):
        """Test PovVerificationResult creation."""
        result = PovVerificationResult(
            status=PovVerificationStatus.CPV,
            benchmark="test-bench",
            cpv_matched=["cpv_0", "cpv_1"],
        )
        assert result.status == PovVerificationStatus.CPV
        assert result.is_vulnerability
        assert "cpv_0" in str(result)

    def test_verification_result_to_dict(self):
        """Test PovVerificationResult serialization."""
        result = PovVerificationResult(
            status=PovVerificationStatus.UNINTENDED_CRASH,
            benchmark="test-bench",
        )
        d = result.to_dict()
        assert d["status"] == "unintended_crash"
        assert d["benchmark"] == "test-bench"


class TestVerdictResolverFullMode:
    """Tests for VerdictResolver in FULL mode."""

    def test_full_mode_not_vulnerable(self):
        """Base doesn't crash -> NOT_VULNERABLE."""
        result = VerdictResolver.resolve(
            mode=BenchmarkMode.FULL,
            crash_results={
                VariantType.FULL_BASE: False,
                VariantType.ALL_PATCHED: False,
            },
            cpv_crash_map={},
            benchmark_name="test",
        )
        assert result.status == PovVerificationStatus.NOT_VULNERABLE

    def test_full_mode_unintended_crash(self):
        """Base crashes, allpatched crashes -> UNINTENDED_CRASH."""
        result = VerdictResolver.resolve(
            mode=BenchmarkMode.FULL,
            crash_results={
                VariantType.FULL_BASE: True,
                VariantType.ALL_PATCHED: True,
            },
            cpv_crash_map={},
            benchmark_name="test",
        )
        assert result.status == PovVerificationStatus.UNINTENDED_CRASH

    def test_full_mode_cpv_single(self):
        """Base crashes, cpv0 crashes -> CPV with cpv_0."""
        result = VerdictResolver.resolve(
            mode=BenchmarkMode.FULL,
            crash_results={
                VariantType.FULL_BASE: True,
                VariantType.ALL_PATCHED: False,
            },
            cpv_crash_map={0: True},
            benchmark_name="test",
        )
        assert result.status == PovVerificationStatus.CPV
        assert result.cpv_matched == ["cpv_0"]

    def test_full_mode_cpv_multiple(self):
        """Base crashes, multiple cpvs crash -> CPV with all matched."""
        result = VerdictResolver.resolve(
            mode=BenchmarkMode.FULL,
            crash_results={
                VariantType.FULL_BASE: True,
                VariantType.ALL_PATCHED: False,
            },
            cpv_crash_map={0: True, 1: False, 2: True},
            benchmark_name="test",
        )
        assert result.status == PovVerificationStatus.CPV
        assert result.cpv_matched == ["cpv_0", "cpv_2"]

    def test_full_mode_no_cpv_match_unintended(self):
        """Base crashes, allpatched ok, no cpv crashes -> UNINTENDED_CRASH."""
        result = VerdictResolver.resolve(
            mode=BenchmarkMode.FULL,
            crash_results={
                VariantType.FULL_BASE: True,
                VariantType.ALL_PATCHED: False,
            },
            cpv_crash_map={0: False, 1: False},
            benchmark_name="test",
        )
        assert result.status == PovVerificationStatus.UNINTENDED_CRASH


class TestVerdictResolverDeltaMode:
    """Tests for VerdictResolver in DELTA mode.

    DELTA mode uses DELTA_REF as the vulnerable version (same logic as FULL mode).
    """

    def test_delta_mode_not_vulnerable(self):
        """Ref doesn't crash -> NOT_VULNERABLE."""
        result = VerdictResolver.resolve(
            mode=BenchmarkMode.DELTA,
            crash_results={
                VariantType.DELTA_REF: False,
                VariantType.ALL_PATCHED: False,
            },
            cpv_crash_map={},
            benchmark_name="test",
        )
        assert result.status == PovVerificationStatus.NOT_VULNERABLE

    def test_delta_mode_unintended_crash_allpatched(self):
        """Ref crashes, allpatched crashes -> UNINTENDED_CRASH."""
        result = VerdictResolver.resolve(
            mode=BenchmarkMode.DELTA,
            crash_results={
                VariantType.DELTA_REF: True,
                VariantType.ALL_PATCHED: True,
            },
            cpv_crash_map={},
            benchmark_name="test",
        )
        assert result.status == PovVerificationStatus.UNINTENDED_CRASH

    def test_delta_mode_cpv(self):
        """Ref crashes, cpv crashes -> CPV."""
        result = VerdictResolver.resolve(
            mode=BenchmarkMode.DELTA,
            crash_results={
                VariantType.DELTA_REF: True,
                VariantType.ALL_PATCHED: False,
            },
            cpv_crash_map={0: True, 1: False},
            benchmark_name="test",
        )
        assert result.status == PovVerificationStatus.CPV
        assert result.cpv_matched == ["cpv_0"]

    def test_delta_mode_no_cpv_unintended(self):
        """Ref crashes, allpatched ok, no cpv -> UNINTENDED_CRASH (same as FULL mode)."""
        result = VerdictResolver.resolve(
            mode=BenchmarkMode.DELTA,
            crash_results={
                VariantType.DELTA_REF: True,
                VariantType.ALL_PATCHED: False,
            },
            cpv_crash_map={0: False, 1: False},
            benchmark_name="test",
        )
        assert result.status == PovVerificationStatus.UNINTENDED_CRASH


class TestDeduplication:
    """Tests for deduplication strategies."""

    def test_patch_based_dedup(self):
        """Test PatchBasedDedup removes duplicates by CPV set."""
        dedup = PatchBasedDedup()
        results = [
            PovVerificationResult(
                status=PovVerificationStatus.CPV,
                benchmark="test",
                cpv_matched=["cpv_0"],
                pov_id="pov1",
            ),
            PovVerificationResult(
                status=PovVerificationStatus.CPV,
                benchmark="test",
                cpv_matched=["cpv_0"],  # Duplicate
                pov_id="pov2",
            ),
            PovVerificationResult(
                status=PovVerificationStatus.CPV,
                benchmark="test",
                cpv_matched=["cpv_1"],
                pov_id="pov3",
            ),
        ]

        unique = dedup.deduplicate(results)
        assert len(unique) == 2
        assert unique[0].pov_id == "pov1"
        assert unique[1].pov_id == "pov3"

    def test_patch_based_dedup_preserves_non_cpv(self):
        """Test PatchBasedDedup keeps non-CPV results."""
        dedup = PatchBasedDedup()
        results = [
            PovVerificationResult(
                status=PovVerificationStatus.UNINTENDED_CRASH,
                benchmark="test",
                pov_id="pov1",
            ),
            PovVerificationResult(
                status=PovVerificationStatus.UNINTENDED_CRASH,
                benchmark="test",
                pov_id="pov2",
            ),
        ]

        unique = dedup.deduplicate(results)
        assert len(unique) == 2  # Both kept (non-CPV)

    def test_noop_dedup(self):
        """Test NoOpDedup keeps all results."""
        dedup = NoOpDedup()
        results = [
            PovVerificationResult(
                status=PovVerificationStatus.CPV,
                benchmark="test",
                cpv_matched=["cpv_0"],
            ),
            PovVerificationResult(
                status=PovVerificationStatus.CPV,
                benchmark="test",
                cpv_matched=["cpv_0"],
            ),
        ]

        unique = dedup.deduplicate(results)
        assert len(unique) == 2

    def test_status_based_dedup(self):
        """Test StatusBasedDedup keeps one per status."""
        dedup = StatusBasedDedup()
        results = [
            PovVerificationResult(
                status=PovVerificationStatus.UNINTENDED_CRASH,
                benchmark="test",
                pov_id="pov1",
            ),
            PovVerificationResult(
                status=PovVerificationStatus.UNINTENDED_CRASH,
                benchmark="test",
                pov_id="pov2",
            ),
            PovVerificationResult(
                status=PovVerificationStatus.NOT_VULNERABLE,
                benchmark="test",
                pov_id="pov3",
            ),
        ]

        unique = dedup.deduplicate(results)
        assert len(unique) == 2

    def test_get_dedup_strategy(self):
        """Test get_dedup_strategy factory function."""
        assert isinstance(get_dedup_strategy("patch-based"), PatchBasedDedup)
        assert isinstance(get_dedup_strategy("none"), NoOpDedup)
        assert isinstance(get_dedup_strategy("status-based"), StatusBasedDedup)

        with pytest.raises(ValueError):
            get_dedup_strategy("unknown")


class TestVerdictResolverEdgeCases:
    """Edge case tests for VerdictResolver."""

    def test_missing_allpatched_returns_error(self):
        """Missing ALL_PATCHED should return ERROR (build/reproduce failed)."""
        result = VerdictResolver.resolve(
            mode=BenchmarkMode.FULL,
            crash_results={VariantType.FULL_BASE: True},  # No ALL_PATCHED
            cpv_crash_map={0: True},
            benchmark_name="test",
        )
        # Should be ERROR since required variant is missing
        assert result.status == PovVerificationStatus.ERROR
        assert "allpatched" in result.details

    def test_empty_cpv_map(self):
        """Empty CPV map should result in UNINTENDED_CRASH."""
        result = VerdictResolver.resolve(
            mode=BenchmarkMode.FULL,
            crash_results={
                VariantType.FULL_BASE: True,
                VariantType.ALL_PATCHED: False,
            },
            cpv_crash_map={},  # Empty
            benchmark_name="test",
        )
        assert result.status == PovVerificationStatus.UNINTENDED_CRASH

    def test_pov_id_preserved(self):
        """POV ID should be preserved in result."""
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


class TestVerificationEngineForceRebuild:
    """Tests for VerificationEngine force_rebuild behavior."""

    def test_get_or_build_results_skips_cache_with_force_rebuild(self):
        """force_rebuild=True should skip in-memory cache."""
        from unittest.mock import MagicMock, patch

        with patch(
            "crsbench.evaluation.verification.pov.engine.VerificationEngine.__init__",
            return_value=None,
        ):
            from crsbench.evaluation.verification.pov.engine import VerificationEngine

            engine = VerificationEngine.__new__(VerificationEngine)
            engine._built_results = {}
            engine.builder = MagicMock()
            engine.builder.create_build_plan.return_value = MagicMock()
            engine.builder.execute_plan.return_value = {"variant1": MagicMock()}

            adapter = MagicMock()
            adapter.benchmark_name = "test-benchmark"
            adapter.get_mode.return_value.value = "delta"

            # First call - should build
            engine.get_or_build_results(adapter, force_rebuild=False)
            assert engine.builder.execute_plan.call_count == 1

            # Second call without force_rebuild - should use cache
            engine.get_or_build_results(adapter, force_rebuild=False)
            assert engine.builder.execute_plan.call_count == 1  # No new call

            # Third call with force_rebuild - should rebuild
            engine.get_or_build_results(adapter, force_rebuild=True)
            assert engine.builder.execute_plan.call_count == 2  # New call

    def test_get_or_build_results_passes_force_rebuild_to_builder(self):
        """force_rebuild should be passed to execute_plan."""
        from unittest.mock import MagicMock, patch

        with patch(
            "crsbench.evaluation.verification.pov.engine.VerificationEngine.__init__",
            return_value=None,
        ):
            from crsbench.evaluation.verification.pov.engine import VerificationEngine

            engine = VerificationEngine.__new__(VerificationEngine)
            engine._built_results = {}
            engine.builder = MagicMock()
            mock_plan = MagicMock()
            engine.builder.create_build_plan.return_value = mock_plan
            engine.builder.execute_plan.return_value = {}

            adapter = MagicMock()
            adapter.benchmark_name = "test-benchmark"
            adapter.get_mode.return_value.value = "delta"

            # Call with force_rebuild=True
            engine.get_or_build_results(adapter, force_rebuild=True)

            # Verify force_rebuild=True was passed to execute_plan
            engine.builder.execute_plan.assert_called_once_with(
                mock_plan, force_rebuild=True
            )

    def test_get_or_build_results_default_no_force_rebuild(self):
        """Default should be force_rebuild=False."""
        from unittest.mock import MagicMock, patch

        with patch(
            "crsbench.evaluation.verification.pov.engine.VerificationEngine.__init__",
            return_value=None,
        ):
            from crsbench.evaluation.verification.pov.engine import VerificationEngine

            engine = VerificationEngine.__new__(VerificationEngine)
            engine._built_results = {}
            engine.builder = MagicMock()
            mock_plan = MagicMock()
            engine.builder.create_build_plan.return_value = mock_plan
            engine.builder.execute_plan.return_value = {}

            adapter = MagicMock()
            adapter.benchmark_name = "test-benchmark"
            adapter.get_mode.return_value.value = "delta"

            # Call without force_rebuild (should default to False)
            engine.get_or_build_results(adapter)

            # Verify force_rebuild=False was passed to execute_plan
            engine.builder.execute_plan.assert_called_once_with(
                mock_plan, force_rebuild=False
            )
