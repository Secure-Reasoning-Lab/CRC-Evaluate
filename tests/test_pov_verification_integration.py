"""Integration tests for POV verification with real benchmark data.

Tests verify:
1. POV from cpv_0 should match only cpv_0
2. POV from cpv_1 should match only cpv_1
3. Cross-CPV POVs should not match wrong CPVs
"""

from pathlib import Path

import pytest
from crsbench.builder.types import BenchmarkMode, VariantType
from crsbench.evaluation.verification.models import VerificationStatus
from crsbench.evaluation.verification.pov.verdict import VerdictResolver
from crsbench.validation.meta_adapter import MetaYamlAdapter


class TestSanityMockCDeltaValidation:
    """Integration tests using sanity-mock-c-delta-01 benchmark."""

    @pytest.fixture
    def benchmark_path(self) -> Path:
        """Path to sanity-mock-c-delta-01 benchmark."""
        return Path("benchmarks/sanity-mock-c-delta-01")

    @pytest.fixture
    def meta_yaml_path(self, benchmark_path) -> Path:
        """Path to meta.yaml."""
        return benchmark_path / ".aixcc" / "meta.yaml"

    @pytest.fixture
    def adapter(self, meta_yaml_path) -> MetaYamlAdapter:
        """Create MetaYamlAdapter for the benchmark."""
        if not meta_yaml_path.exists():
            pytest.skip(f"Benchmark not found: {meta_yaml_path}")

        return MetaYamlAdapter.from_meta_yaml(
            meta_yaml_path=meta_yaml_path,
            benchmark_name="sanity-mock-c-delta-01",
            lang="c",
            main_repo="https://github.com/example/mock-c.git",
        )

    def test_benchmark_mode_is_delta(self, adapter):
        """Verify benchmark is in DELTA mode."""
        assert adapter.get_mode() == BenchmarkMode.DELTA

    def test_benchmark_has_two_cpvs(self, adapter):
        """Verify benchmark has exactly 2 CPVs (0 and 1)."""
        cpv_numbers = adapter.get_cpv_numbers()
        assert cpv_numbers == [0, 1]

    def test_cpv0_pov_location_exists(self, benchmark_path):
        """Verify CPV_0 POV blob exists."""
        pov_path = (
            benchmark_path
            / ".aixcc"
            / "fuzz_process_input_header"
            / "cpv_0"
            / "blobs"
            / "pov_0.blob"
        )
        if not pov_path.exists():
            pytest.skip(f"POV not found: {pov_path}")
        assert pov_path.exists()
        assert pov_path.stat().st_size > 0

    def test_cpv1_pov_location_exists(self, benchmark_path):
        """Verify CPV_1 POV blob exists."""
        pov_path = (
            benchmark_path
            / ".aixcc"
            / "fuzz_parse_buffer_section"
            / "cpv_1"
            / "blobs"
            / "pov_0.blob"
        )
        if not pov_path.exists():
            pytest.skip(f"POV not found: {pov_path}")
        assert pov_path.exists()
        assert pov_path.stat().st_size > 0

    def test_cpv0_pov_matches_only_cpv0(self, adapter):
        """POV from cpv_0 should trigger only cpv_0.

        Simulated scenario:
        - DELTA_BASE: no crash (bug not in base)
        - DELTA_REF: crash (bug in ref)
        - ALL_PATCHED: no crash (all patches fix it)
        - CPV_0: crash (missing cpv_0 patch exposes bug)
        - CPV_1: no crash (cpv_0 patch applied)
        """
        result = VerdictResolver.resolve(
            mode=BenchmarkMode.DELTA,
            crash_results={
                VariantType.DELTA_BASE: False,  # Base doesn't crash
                VariantType.DELTA_REF: True,  # Ref crashes (has the bug)
                VariantType.ALL_PATCHED: False,  # All patched doesn't crash
            },
            cpv_crash_map={
                0: True,  # CPV_0 variant crashes (missing cpv_0 patch)
                1: False,  # CPV_1 variant doesn't crash (cpv_0 patch applied)
            },
            benchmark_name="sanity-mock-c-delta-01",
            pov_id="cpv_0_pov_0",
        )

        assert result.status == VerificationStatus.CPV
        assert result.cpv_matched == ["cpv_0"]
        assert "cpv_1" not in result.cpv_matched

    def test_cpv1_pov_matches_only_cpv1(self, adapter):
        """POV from cpv_1 should trigger only cpv_1.

        Simulated scenario:
        - DELTA_BASE: no crash (bug not in base)
        - DELTA_REF: crash (bug in ref)
        - ALL_PATCHED: no crash (all patches fix it)
        - CPV_0: no crash (cpv_1 patch applied)
        - CPV_1: crash (missing cpv_1 patch exposes bug)
        """
        result = VerdictResolver.resolve(
            mode=BenchmarkMode.DELTA,
            crash_results={
                VariantType.DELTA_BASE: False,
                VariantType.DELTA_REF: True,
                VariantType.ALL_PATCHED: False,
            },
            cpv_crash_map={
                0: False,  # CPV_0 variant doesn't crash (cpv_1 patch applied)
                1: True,  # CPV_1 variant crashes (missing cpv_1 patch)
            },
            benchmark_name="sanity-mock-c-delta-01",
            pov_id="cpv_1_pov_0",
        )

        assert result.status == VerificationStatus.CPV
        assert result.cpv_matched == ["cpv_1"]
        assert "cpv_0" not in result.cpv_matched

    def test_pov_matching_both_cpvs(self, adapter):
        """POV that triggers both CPVs (edge case).

        This could happen if a POV exploits multiple vulnerabilities.
        """
        result = VerdictResolver.resolve(
            mode=BenchmarkMode.DELTA,
            crash_results={
                VariantType.DELTA_BASE: False,
                VariantType.DELTA_REF: True,
                VariantType.ALL_PATCHED: False,
            },
            cpv_crash_map={
                0: True,  # Both CPV variants crash
                1: True,
            },
            benchmark_name="sanity-mock-c-delta-01",
            pov_id="multi_cpv_pov",
        )

        assert result.status == VerificationStatus.CPV
        assert "cpv_0" in result.cpv_matched
        assert "cpv_1" in result.cpv_matched

    def test_pov_matching_no_cpvs_is_unintended(self, adapter):
        """POV that crashes ref but no CPV variants = UNINTENDED_CRASH.

        This happens when POV crashes the program but not via known vulnerabilities.
        """
        result = VerdictResolver.resolve(
            mode=BenchmarkMode.DELTA,
            crash_results={
                VariantType.DELTA_BASE: False,
                VariantType.DELTA_REF: True,
                VariantType.ALL_PATCHED: False,
            },
            cpv_crash_map={
                0: False,
                1: False,
            },
            benchmark_name="sanity-mock-c-delta-01",
            pov_id="unintended_pov",
        )

        assert result.status == VerificationStatus.UNINTENDED_CRASH

    def test_pov_not_crashing_ref_is_not_vulnerable(self, adapter):
        """POV that doesn't crash ref = NOT_VULNERABLE.

        The POV doesn't actually trigger any crash.
        """
        result = VerdictResolver.resolve(
            mode=BenchmarkMode.DELTA,
            crash_results={
                VariantType.DELTA_BASE: False,
                VariantType.DELTA_REF: False,  # Ref doesn't crash
                VariantType.ALL_PATCHED: False,
            },
            cpv_crash_map={
                0: False,
                1: False,
            },
            benchmark_name="sanity-mock-c-delta-01",
            pov_id="non_crashing_pov",
        )

        assert result.status == VerificationStatus.NOT_VULNERABLE

    def test_pov_crashing_base_is_zeroday(self, adapter):
        """POV that crashes base = ZERODAY (pre-existing bug)."""
        result = VerdictResolver.resolve(
            mode=BenchmarkMode.DELTA,
            crash_results={
                VariantType.DELTA_BASE: True,  # Base crashes (pre-existing bug)
            },
            cpv_crash_map={},
            benchmark_name="sanity-mock-c-delta-01",
            pov_id="zeroday_pov",
        )

        assert result.status == VerificationStatus.ZERODAY


class TestMetaYamlAdapterIntegration:
    """Test MetaYamlAdapter with real benchmark data."""

    @pytest.fixture
    def meta_yaml_path(self) -> Path:
        """Path to meta.yaml."""
        return Path("benchmarks/sanity-mock-c-delta-01/.aixcc/meta.yaml")

    def test_adapter_loads_correctly(self, meta_yaml_path):
        """Test MetaYamlAdapter loads the benchmark correctly."""
        if not meta_yaml_path.exists():
            pytest.skip(f"Benchmark not found: {meta_yaml_path}")

        adapter = MetaYamlAdapter.from_meta_yaml(
            meta_yaml_path=meta_yaml_path,
            benchmark_name="sanity-mock-c-delta-01",
            lang="c",
            main_repo="https://github.com/example/mock-c.git",
        )

        # Verify basic properties
        assert adapter.benchmark_name == "sanity-mock-c-delta-01"
        assert adapter.lang == "c"
        assert adapter.get_mode() == BenchmarkMode.DELTA

    def test_adapter_variant_names(self, meta_yaml_path):
        """Test variant name generation."""
        if not meta_yaml_path.exists():
            pytest.skip(f"Benchmark not found: {meta_yaml_path}")

        adapter = MetaYamlAdapter.from_meta_yaml(
            meta_yaml_path=meta_yaml_path,
            benchmark_name="sanity-mock-c-delta-01",
            lang="c",
            main_repo="https://github.com/example/mock-c.git",
        )

        # Test variant naming
        assert (
            adapter.get_variant_name(VariantType.DELTA_BASE)
            == "sanity-mock-c-delta-01-deltabase"
        )
        assert (
            adapter.get_variant_name(VariantType.DELTA_REF)
            == "sanity-mock-c-delta-01-deltaref"
        )
        assert (
            adapter.get_variant_name(VariantType.ALL_PATCHED)
            == "sanity-mock-c-delta-01-allpatched"
        )
        assert (
            adapter.get_variant_name(VariantType.CPV, cpv_num=0)
            == "sanity-mock-c-delta-01-cpv0"
        )
        assert (
            adapter.get_variant_name(VariantType.CPV, cpv_num=1)
            == "sanity-mock-c-delta-01-cpv1"
        )

    def test_adapter_harness_names(self, meta_yaml_path):
        """Test harness name extraction."""
        if not meta_yaml_path.exists():
            pytest.skip(f"Benchmark not found: {meta_yaml_path}")

        adapter = MetaYamlAdapter.from_meta_yaml(
            meta_yaml_path=meta_yaml_path,
            benchmark_name="sanity-mock-c-delta-01",
            lang="c",
            main_repo="https://github.com/example/mock-c.git",
        )

        harness_names = adapter.get_harness_names()
        assert "fuzz_process_input_header" in harness_names
        assert "fuzz_parse_buffer_section" in harness_names
