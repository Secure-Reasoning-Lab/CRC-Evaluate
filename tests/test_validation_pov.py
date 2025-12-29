"""Tests for POV validation modules.

Tests cover:
- VerdictResolver (FULL and DELTA mode verdict logic)
- Deduplication strategies
- Variant models
- Verification models
"""

import pytest
from crsbench.validation.variant.models import (
    BenchmarkMode,
    BuildTag,
    BuildVersion,
)
from crsbench.validation.verification.dedup import (
    NoOpDedup,
    PatchBasedDedup,
    StatusBasedDedup,
    get_dedup_strategy,
)
from crsbench.validation.verification.models import (
    VerificationRequest,
    VerificationResult,
    VerificationStatus,
)
from crsbench.validation.verification.verdict import VerdictResolver


class TestBuildTag:
    """Tests for BuildTag enum."""

    def test_build_tag_values(self):
        """Test BuildTag enum values."""
        assert BuildTag.FULL_BASE.value == "fullbase"
        assert BuildTag.DELTA_BASE.value == "deltabase"
        assert BuildTag.DELTA_REF.value == "deltaref"
        assert BuildTag.ALL_PATCHED.value == "allpatched"
        assert BuildTag.CPV.value == "cpv"


class TestBenchmarkMode:
    """Tests for BenchmarkMode enum."""

    def test_benchmark_mode_values(self):
        """Test BenchmarkMode enum values."""
        assert BenchmarkMode.FULL.value == "full"
        assert BenchmarkMode.DELTA.value == "delta"


class TestBuildVersion:
    """Tests for BuildVersion dataclass."""

    def test_build_version_creation(self):
        """Test BuildVersion creation."""
        version = BuildVersion(
            benchmark_name="test-bench",
            lang="c",
            mode=BenchmarkMode.DELTA,
            build_tag=BuildTag.DELTA_REF,
            commit="abc123",
            variant_project_name="test-bench-deltaref",
        )
        assert version.benchmark_name == "test-bench"
        assert version.lang == "c"
        assert version.project_path == "test-bench-deltaref"

    def test_build_version_cpv(self):
        """Test BuildVersion with CPV."""
        version = BuildVersion(
            benchmark_name="test-bench",
            lang="c",
            mode=BenchmarkMode.DELTA,
            build_tag=BuildTag.CPV,
            commit="abc123",
            variant_project_name="test-bench-cpv0",
            cpv_num=0,
        )
        assert version.cpv_num == 0
        assert str(version) == "test-bench:cpv0"


class TestVerificationModels:
    """Tests for verification models."""

    def test_verification_request(self):
        """Test VerificationRequest creation."""
        request = VerificationRequest(
            pov_data=b"test data",
            harness="test_harness",
            benchmark="test-bench",
            pov_id="pov_0",
        )
        assert request.pov_data == b"test data"
        assert request.harness == "test_harness"

    def test_verification_result(self):
        """Test VerificationResult creation."""
        result = VerificationResult(
            status=VerificationStatus.CPV,
            benchmark="test-bench",
            cpv_matched=["cpv_0", "cpv_1"],
        )
        assert result.status == VerificationStatus.CPV
        assert result.is_vulnerability
        assert "cpv_0" in str(result)

    def test_verification_result_to_dict(self):
        """Test VerificationResult serialization."""
        result = VerificationResult(
            status=VerificationStatus.ZERODAY,
            benchmark="test-bench",
        )
        d = result.to_dict()
        assert d["status"] == "zeroday"
        assert d["benchmark"] == "test-bench"


class TestVerdictResolverFullMode:
    """Tests for VerdictResolver in FULL mode."""

    def test_full_mode_not_vulnerable(self):
        """Base doesn't crash -> NOT_VULNERABLE."""
        result = VerdictResolver.resolve(
            mode=BenchmarkMode.FULL,
            crash_results={BuildTag.FULL_BASE: False},
            cpv_crash_map={},
            benchmark_name="test",
        )
        assert result.status == VerificationStatus.NOT_VULNERABLE

    def test_full_mode_unintended_crash(self):
        """Base crashes, allpatched crashes -> UNINTENDED_CRASH."""
        result = VerdictResolver.resolve(
            mode=BenchmarkMode.FULL,
            crash_results={
                BuildTag.FULL_BASE: True,
                BuildTag.ALL_PATCHED: True,
            },
            cpv_crash_map={},
            benchmark_name="test",
        )
        assert result.status == VerificationStatus.UNINTENDED_CRASH

    def test_full_mode_cpv_single(self):
        """Base crashes, cpv0 crashes -> CPV with cpv_0."""
        result = VerdictResolver.resolve(
            mode=BenchmarkMode.FULL,
            crash_results={
                BuildTag.FULL_BASE: True,
                BuildTag.ALL_PATCHED: False,
            },
            cpv_crash_map={0: True},
            benchmark_name="test",
        )
        assert result.status == VerificationStatus.CPV
        assert result.cpv_matched == ["cpv_0"]

    def test_full_mode_cpv_multiple(self):
        """Base crashes, multiple cpvs crash -> CPV with all matched."""
        result = VerdictResolver.resolve(
            mode=BenchmarkMode.FULL,
            crash_results={
                BuildTag.FULL_BASE: True,
                BuildTag.ALL_PATCHED: False,
            },
            cpv_crash_map={0: True, 1: False, 2: True},
            benchmark_name="test",
        )
        assert result.status == VerificationStatus.CPV
        assert result.cpv_matched == ["cpv_0", "cpv_2"]

    def test_full_mode_zeroday(self):
        """Base crashes, allpatched ok, no cpv crashes -> ZERODAY."""
        result = VerdictResolver.resolve(
            mode=BenchmarkMode.FULL,
            crash_results={
                BuildTag.FULL_BASE: True,
                BuildTag.ALL_PATCHED: False,
            },
            cpv_crash_map={0: False, 1: False},
            benchmark_name="test",
        )
        assert result.status == VerificationStatus.ZERODAY


class TestVerdictResolverDeltaMode:
    """Tests for VerdictResolver in DELTA mode."""

    def test_delta_mode_zeroday_base_crashes(self):
        """Base crashes in delta mode -> ZERODAY (pre-existing bug)."""
        result = VerdictResolver.resolve(
            mode=BenchmarkMode.DELTA,
            crash_results={BuildTag.DELTA_BASE: True},
            cpv_crash_map={},
            benchmark_name="test",
        )
        assert result.status == VerificationStatus.ZERODAY

    def test_delta_mode_not_vulnerable(self):
        """Base ok, ref doesn't crash -> NOT_VULNERABLE."""
        result = VerdictResolver.resolve(
            mode=BenchmarkMode.DELTA,
            crash_results={
                BuildTag.DELTA_BASE: False,
                BuildTag.DELTA_REF: False,
            },
            cpv_crash_map={},
            benchmark_name="test",
        )
        assert result.status == VerificationStatus.NOT_VULNERABLE

    def test_delta_mode_unintended_crash_allpatched(self):
        """Base ok, ref crashes, allpatched crashes -> UNINTENDED_CRASH."""
        result = VerdictResolver.resolve(
            mode=BenchmarkMode.DELTA,
            crash_results={
                BuildTag.DELTA_BASE: False,
                BuildTag.DELTA_REF: True,
                BuildTag.ALL_PATCHED: True,
            },
            cpv_crash_map={},
            benchmark_name="test",
        )
        assert result.status == VerificationStatus.UNINTENDED_CRASH

    def test_delta_mode_cpv(self):
        """Base ok, ref crashes, cpv crashes -> CPV."""
        result = VerdictResolver.resolve(
            mode=BenchmarkMode.DELTA,
            crash_results={
                BuildTag.DELTA_BASE: False,
                BuildTag.DELTA_REF: True,
                BuildTag.ALL_PATCHED: False,
            },
            cpv_crash_map={0: True, 1: False},
            benchmark_name="test",
        )
        assert result.status == VerificationStatus.CPV
        assert result.cpv_matched == ["cpv_0"]

    def test_delta_mode_unintended_no_cpv(self):
        """Base ok, ref crashes, allpatched ok, no cpv -> UNINTENDED_CRASH."""
        result = VerdictResolver.resolve(
            mode=BenchmarkMode.DELTA,
            crash_results={
                BuildTag.DELTA_BASE: False,
                BuildTag.DELTA_REF: True,
                BuildTag.ALL_PATCHED: False,
            },
            cpv_crash_map={0: False, 1: False},
            benchmark_name="test",
        )
        assert result.status == VerificationStatus.UNINTENDED_CRASH


class TestDeduplication:
    """Tests for deduplication strategies."""

    def test_patch_based_dedup(self):
        """Test PatchBasedDedup removes duplicates by CPV set."""
        dedup = PatchBasedDedup()
        results = [
            VerificationResult(
                status=VerificationStatus.CPV,
                benchmark="test",
                cpv_matched=["cpv_0"],
                pov_id="pov1",
            ),
            VerificationResult(
                status=VerificationStatus.CPV,
                benchmark="test",
                cpv_matched=["cpv_0"],  # Duplicate
                pov_id="pov2",
            ),
            VerificationResult(
                status=VerificationStatus.CPV,
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
            VerificationResult(
                status=VerificationStatus.ZERODAY,
                benchmark="test",
                pov_id="pov1",
            ),
            VerificationResult(
                status=VerificationStatus.ZERODAY,
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
            VerificationResult(
                status=VerificationStatus.CPV,
                benchmark="test",
                cpv_matched=["cpv_0"],
            ),
            VerificationResult(
                status=VerificationStatus.CPV,
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
            VerificationResult(
                status=VerificationStatus.ZERODAY,
                benchmark="test",
                pov_id="pov1",
            ),
            VerificationResult(
                status=VerificationStatus.ZERODAY,
                benchmark="test",
                pov_id="pov2",
            ),
            VerificationResult(
                status=VerificationStatus.NOT_VULNERABLE,
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

    def test_missing_allpatched_defaults_to_true(self):
        """Missing ALL_PATCHED should default to True (crashed)."""
        result = VerdictResolver.resolve(
            mode=BenchmarkMode.FULL,
            crash_results={BuildTag.FULL_BASE: True},  # No ALL_PATCHED
            cpv_crash_map={0: True},
            benchmark_name="test",
        )
        # Should be UNINTENDED_CRASH since allpatched defaults to True
        assert result.status == VerificationStatus.UNINTENDED_CRASH

    def test_empty_cpv_map(self):
        """Empty CPV map should result in appropriate status."""
        result = VerdictResolver.resolve(
            mode=BenchmarkMode.FULL,
            crash_results={
                BuildTag.FULL_BASE: True,
                BuildTag.ALL_PATCHED: False,
            },
            cpv_crash_map={},  # Empty
            benchmark_name="test",
        )
        assert result.status == VerificationStatus.ZERODAY

    def test_pov_id_preserved(self):
        """POV ID should be preserved in result."""
        result = VerdictResolver.resolve(
            mode=BenchmarkMode.FULL,
            crash_results={BuildTag.FULL_BASE: False},
            cpv_crash_map={},
            benchmark_name="test",
            pov_id="test_pov_123",
        )
        assert result.pov_id == "test_pov_123"


class TestVerificationEngineForceRebuild:
    """Tests for VerificationEngine force_rebuild behavior."""

    def test_get_or_build_versions_skips_cache_with_force_rebuild(self):
        """force_rebuild=True should skip in-memory cache."""
        from unittest.mock import MagicMock, patch

        with patch(
            "crsbench.validation.verification.engine.VerificationEngine.__init__",
            return_value=None,
        ):
            from crsbench.validation.verification.engine import VerificationEngine

            engine = VerificationEngine.__new__(VerificationEngine)
            engine._built_versions = {}
            engine.builder = MagicMock()
            engine.builder.build_all_variants.return_value = ["version1", "version2"]

            adapter = MagicMock()
            adapter.benchmark_name = "test-benchmark"

            # First call - should build
            result1 = engine._get_or_build_versions(adapter, force_rebuild=False)
            assert engine.builder.build_all_variants.call_count == 1

            # Second call without force_rebuild - should use cache
            result2 = engine._get_or_build_versions(adapter, force_rebuild=False)
            assert engine.builder.build_all_variants.call_count == 1  # No new call

            # Third call with force_rebuild - should rebuild
            result3 = engine._get_or_build_versions(adapter, force_rebuild=True)
            assert engine.builder.build_all_variants.call_count == 2  # New call

    def test_get_or_build_versions_passes_force_rebuild_to_builder(self):
        """force_rebuild should be passed to builder.build_all_variants."""
        from unittest.mock import MagicMock, patch

        with patch(
            "crsbench.validation.verification.engine.VerificationEngine.__init__",
            return_value=None,
        ):
            from crsbench.validation.verification.engine import VerificationEngine

            engine = VerificationEngine.__new__(VerificationEngine)
            engine._built_versions = {}
            engine.builder = MagicMock()
            engine.builder.build_all_variants.return_value = []

            adapter = MagicMock()
            adapter.benchmark_name = "test-benchmark"

            # Call with force_rebuild=True
            engine._get_or_build_versions(adapter, force_rebuild=True)

            # Verify force_rebuild=True was passed to builder
            engine.builder.build_all_variants.assert_called_once_with(
                adapter, force_rebuild=True
            )

    def test_get_or_build_versions_default_no_force_rebuild(self):
        """Default should be force_rebuild=False."""
        from unittest.mock import MagicMock, patch

        with patch(
            "crsbench.validation.verification.engine.VerificationEngine.__init__",
            return_value=None,
        ):
            from crsbench.validation.verification.engine import VerificationEngine

            engine = VerificationEngine.__new__(VerificationEngine)
            engine._built_versions = {}
            engine.builder = MagicMock()
            engine.builder.build_all_variants.return_value = []

            adapter = MagicMock()
            adapter.benchmark_name = "test-benchmark"

            # Call without force_rebuild (should default to False)
            engine._get_or_build_versions(adapter)

            # Verify force_rebuild=False was passed to builder
            engine.builder.build_all_variants.assert_called_once_with(
                adapter, force_rebuild=False
            )
