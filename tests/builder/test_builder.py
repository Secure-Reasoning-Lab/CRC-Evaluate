"""Tests for the OSSFuzzBuilder module."""

from pathlib import Path
from unittest.mock import patch

import pytest
from crsbench.builder import (
    BenchmarkMode,
    BuildConfig,
    BuildPlan,
    BuildResult,
    OSSFuzzBuilder,
    VariantType,
)


class TestVariantType:
    """Tests for VariantType enum."""

    def test_variant_type_values(self):
        """Test variant type values."""
        assert VariantType.FULL_BASE.value == "fullbase"
        assert VariantType.DELTA_REF.value == "deltaref"
        assert VariantType.ALL_PATCHED.value == "allpatched"
        assert VariantType.CPV.value == "cpv"
        assert VariantType.COVERAGE.value == "coverage"

    def test_is_validation_variant(self):
        """Test is_validation_variant method."""
        assert VariantType.FULL_BASE.is_validation_variant()
        assert VariantType.DELTA_REF.is_validation_variant()
        assert VariantType.CPV.is_validation_variant()
        assert not VariantType.COVERAGE.is_validation_variant()

    def test_supports_inc_build(self):
        """Test supports_inc_build method.

        All variant types except COVERAGE support incremental builds.
        """
        # Validation variants support inc-build
        assert VariantType.FULL_BASE.supports_inc_build()
        assert VariantType.DELTA_REF.supports_inc_build()
        assert VariantType.ALL_PATCHED.supports_inc_build()
        assert VariantType.CPV.supports_inc_build()

        # Patch variant supports inc-build
        assert VariantType.PATCHED.supports_inc_build()

        # Coverage does NOT support inc-build (different instrumentation)
        assert not VariantType.COVERAGE.supports_inc_build()


class TestBuildConfig:
    """Tests for BuildConfig dataclass."""

    def test_basic_config(self):
        """Test basic configuration."""
        config = BuildConfig(
            benchmark_name="test-benchmark",
            variant_type=VariantType.DELTA_REF,
            commit="abc123",
            main_repo="https://github.com/test/repo",
            benchmark_path=Path("/tmp/benchmark"),
            language="c",
        )
        assert config.benchmark_name == "test-benchmark"
        assert config.variant_type == VariantType.DELTA_REF
        # Variant name includes sanitizer (defaults to "address" -> "asan")
        assert config.variant_name == "test-benchmark-asan-deltaref"

    def test_cpv_variant_name(self):
        """Test CPV variant naming (includes sanitizer and mode)."""
        config = BuildConfig(
            benchmark_name="test-benchmark",
            variant_type=VariantType.CPV,
            commit="abc123",
            main_repo="https://github.com/test/repo",
            benchmark_path=Path("/tmp/benchmark"),
            mode=BenchmarkMode.DELTA,
            cpv_num=0,
        )
        # CPV variants include sanitizer and mode: {benchmark}-{san_short}-{mode}-cpv{N}
        assert config.variant_name == "test-benchmark-asan-delta-cpv0"

    def test_cpv_requires_cpv_num(self):
        """Test that CPV variants require cpv_num."""
        with pytest.raises(ValueError, match="cpv_num is required"):
            BuildConfig(
                benchmark_name="test-benchmark",
                variant_type=VariantType.CPV,
                commit="abc123",
                main_repo="https://github.com/test/repo",
                benchmark_path=Path("/tmp/benchmark"),
            )

    def test_coverage_sets_sanitizer(self):
        """Test that coverage variant sets sanitizer to coverage."""
        config = BuildConfig(
            benchmark_name="test-benchmark",
            variant_type=VariantType.COVERAGE,
            commit="abc123",
            main_repo="https://github.com/test/repo",
            benchmark_path=Path("/tmp/benchmark"),
        )
        assert config.sanitizer == "coverage"

    def test_string_benchmark_path_converted(self):
        """Test that string benchmark_path is converted to Path."""
        config = BuildConfig(
            benchmark_name="test-benchmark",
            variant_type=VariantType.DELTA_REF,
            commit="abc123",
            main_repo="https://github.com/test/repo",
            benchmark_path="/tmp/benchmark",  # type: ignore
        )
        assert isinstance(config.benchmark_path, Path)


class TestBuildResult:
    """Tests for BuildResult dataclass."""

    def test_from_cache(self):
        """Test creating result from cache."""
        config = BuildConfig(
            benchmark_name="test",
            variant_type=VariantType.DELTA_REF,
            commit="abc",
            main_repo="https://example.com",
            benchmark_path=Path("/tmp"),
        )
        result = BuildResult.from_cache(config, Path("/tmp/build"))
        assert result.success
        assert result.cached
        assert result.build_path == Path("/tmp/build")

    def test_from_error(self):
        """Test creating error result."""
        config = BuildConfig(
            benchmark_name="test",
            variant_type=VariantType.DELTA_REF,
            commit="abc",
            main_repo="https://example.com",
            benchmark_path=Path("/tmp"),
        )
        result = BuildResult.from_error(config, "Build failed", 10.5)
        assert not result.success
        assert result.error == "Build failed"
        assert result.elapsed_seconds == 10.5


class TestBuildPlan:
    """Tests for BuildPlan dataclass."""

    def test_empty_plan(self):
        """Test empty build plan."""
        plan = BuildPlan(benchmark_name="test")
        assert plan.total_count == 0
        assert plan.cached_count == 0
        assert plan.build_count == 0

    def test_add_config(self):
        """Test adding configurations."""
        plan = BuildPlan(benchmark_name="test")
        config = BuildConfig(
            benchmark_name="test",
            variant_type=VariantType.DELTA_REF,
            commit="abc",
            main_repo="https://example.com",
            benchmark_path=Path("/tmp"),
        )
        plan.add_config(config)
        assert plan.total_count == 1
        assert plan.build_count == 1

    def test_mark_cached(self):
        """Test marking variants as cached."""
        plan = BuildPlan(benchmark_name="test")
        config = BuildConfig(
            benchmark_name="test",
            variant_type=VariantType.DELTA_REF,
            commit="abc",
            main_repo="https://example.com",
            benchmark_path=Path("/tmp"),
        )
        plan.add_config(config)
        # Variant name includes sanitizer: test-asan-deltaref
        plan.mark_cached("test-asan-deltaref")

        assert plan.total_count == 1
        assert plan.cached_count == 1
        assert plan.build_count == 0
        assert plan.configs_to_build == []


class TestOSSFuzzBuilder:
    """Tests for OSSFuzzBuilder (with mocked infrastructure)."""

    @pytest.fixture
    def mock_oss_fuzz_path(self, tmp_path: Path) -> Path:
        """Create a mock oss-fuzz directory."""
        oss_fuzz = tmp_path / "oss-fuzz"
        oss_fuzz.mkdir()
        (oss_fuzz / "infra").mkdir()
        (oss_fuzz / "infra" / "helper.py").touch()
        (oss_fuzz / "projects").mkdir()
        (oss_fuzz / "build" / "out").mkdir(parents=True)
        return oss_fuzz

    def test_init(self, mock_oss_fuzz_path: Path):
        """Test builder initialization."""
        builder = OSSFuzzBuilder(mock_oss_fuzz_path, max_workers=4)
        assert builder.max_workers == 4

    def test_create_build_plan_delta_mode(
        self, mock_oss_fuzz_path: Path, tmp_path: Path
    ):
        """Test creating build plan for delta mode benchmark."""
        builder = OSSFuzzBuilder(mock_oss_fuzz_path)
        benchmark_path = tmp_path / "benchmark"
        benchmark_path.mkdir()

        plan = builder.create_build_plan(
            benchmark_name="test-delta-01",
            benchmark_path=benchmark_path,
            main_repo="https://github.com/test/repo",
            mode=BenchmarkMode.DELTA,
            base_commit="base123",
            ref_commit="ref456",
            cpv_numbers=[0, 1],
            language="c",
        )

        # Should have: deltaref, allpatched, cpv0, cpv1
        # Variant names include sanitizer: {benchmark}-{san_short}-{suffix}
        assert plan.total_count == 4
        variant_names = [c.variant_name for c in plan.configs]
        assert "test-delta-01-asan-deltaref" in variant_names
        assert "test-delta-01-asan-delta-allpatched" in variant_names
        assert "test-delta-01-asan-delta-cpv0" in variant_names
        assert "test-delta-01-asan-delta-cpv1" in variant_names

    def test_create_build_plan_full_mode(
        self, mock_oss_fuzz_path: Path, tmp_path: Path
    ):
        """Test creating build plan for full mode benchmark."""
        builder = OSSFuzzBuilder(mock_oss_fuzz_path)
        benchmark_path = tmp_path / "benchmark"
        benchmark_path.mkdir()

        plan = builder.create_build_plan(
            benchmark_name="test-full-01",
            benchmark_path=benchmark_path,
            main_repo="https://github.com/test/repo",
            mode=BenchmarkMode.FULL,
            base_commit="base123",
            ref_commit=None,
            cpv_numbers=[0],
            language="c",
        )

        # Should have: fullbase, allpatched, cpv0
        # Variant names include sanitizer: {benchmark}-{san_short}-{suffix}
        assert plan.total_count == 3
        variant_names = [c.variant_name for c in plan.configs]
        assert "test-full-01-asan-fullbase" in variant_names
        assert "test-full-01-asan-full-allpatched" in variant_names
        assert "test-full-01-asan-full-cpv0" in variant_names

    def test_create_build_plan_with_coverage(
        self, mock_oss_fuzz_path: Path, tmp_path: Path
    ):
        """Test creating build plan with coverage variant."""
        builder = OSSFuzzBuilder(mock_oss_fuzz_path)
        benchmark_path = tmp_path / "benchmark"
        benchmark_path.mkdir()

        plan = builder.create_build_plan(
            benchmark_name="test",
            benchmark_path=benchmark_path,
            main_repo="https://github.com/test/repo",
            mode=BenchmarkMode.DELTA,
            base_commit="base",
            ref_commit="ref",
            cpv_numbers=[0],
            include_coverage=True,
        )

        # Coverage variant: {benchmark}-coverage-{mode}-coverage
        # Note: Coverage sanitizer maps to "coverage" (not "cov")
        variant_names = [c.variant_name for c in plan.configs]
        assert "test-coverage-delta-coverage" in variant_names

    def test_is_variant_built(self, mock_oss_fuzz_path: Path):
        """Test checking if variant is built."""
        builder = OSSFuzzBuilder(mock_oss_fuzz_path)

        # Create fake built variant
        variant_name = "test-deltaref"
        (mock_oss_fuzz_path / "projects" / variant_name).mkdir()
        build_out = mock_oss_fuzz_path / "build" / "out" / variant_name
        build_out.mkdir(parents=True)
        (build_out / "fuzzer").touch()

        assert builder.is_variant_built(variant_name)
        assert not builder.is_variant_built("nonexistent-variant")


class TestOSSFuzzBuilderForceRebuild:
    """Tests for force_rebuild cleanup behavior in OSSFuzzBuilder."""

    @pytest.fixture
    def mock_oss_fuzz_path(self, tmp_path: Path) -> Path:
        """Create a mock oss-fuzz directory."""
        oss_fuzz = tmp_path / "oss-fuzz"
        oss_fuzz.mkdir()
        (oss_fuzz / "infra").mkdir()
        (oss_fuzz / "infra" / "helper.py").touch()
        (oss_fuzz / "projects").mkdir()
        (oss_fuzz / "build" / "out").mkdir(parents=True)
        return oss_fuzz

    @pytest.fixture
    def builder(self, mock_oss_fuzz_path: Path) -> OSSFuzzBuilder:
        """Create builder with mocked infrastructure."""
        return OSSFuzzBuilder(mock_oss_fuzz_path, max_workers=1)

    @pytest.fixture
    def config(self, tmp_path: Path) -> BuildConfig:
        """Create a sample build config."""
        return BuildConfig(
            benchmark_name="test",
            variant_type=VariantType.DELTA_REF,
            commit="abc123",
            main_repo="https://example.com",
            benchmark_path=tmp_path / "benchmark",
        )

    def test_build_single_force_rebuild_calls_cleanup(
        self, builder: OSSFuzzBuilder, config: BuildConfig
    ):
        """Test that build_single with force_rebuild=True calls cleanup methods."""
        with (
            patch.object(
                builder.infra, "cleanup_build_outputs"
            ) as mock_cleanup_outputs,
            patch.object(builder.infra, "cleanup_source") as mock_cleanup_source,
            patch.object(builder.infra, "is_variant_built", return_value=True),
            patch.object(
                builder,
                "_build_single",
                return_value=BuildResult.from_cache(config, Path("/tmp/build")),
            ),
        ):
            builder.build_single(config, force_rebuild=True)

            # Cleanup should be called
            mock_cleanup_outputs.assert_called_once_with(config.variant_name)
            mock_cleanup_source.assert_called_once_with(config.variant_name)

    def test_build_single_no_force_rebuild_skips_cleanup(
        self, builder: OSSFuzzBuilder, config: BuildConfig
    ):
        """Test that build_single without force_rebuild does not call cleanup."""
        with (
            patch.object(
                builder.infra, "cleanup_build_outputs"
            ) as mock_cleanup_outputs,
            patch.object(builder.infra, "cleanup_source") as mock_cleanup_source,
            patch.object(builder.infra, "is_variant_built", return_value=True),
            patch.object(
                builder.infra, "get_build_output_path", return_value=Path("/tmp/build")
            ),
        ):
            builder.build_single(config, force_rebuild=False)

            # Cleanup should NOT be called
            mock_cleanup_outputs.assert_not_called()
            mock_cleanup_source.assert_not_called()

    def test_build_single_force_rebuild_bypasses_cache(
        self, builder: OSSFuzzBuilder, config: BuildConfig
    ):
        """Test that force_rebuild=True bypasses cache even if variant is built."""
        with (
            patch.object(builder.infra, "cleanup_build_outputs"),
            patch.object(builder.infra, "cleanup_source"),
            patch.object(builder.infra, "is_variant_built", return_value=True),
            patch.object(
                builder,
                "_build_single",
                return_value=BuildResult(
                    config=config,
                    success=True,
                    variant_name=config.variant_name,
                    build_path=Path("/tmp/build"),
                ),
            ) as mock_build,
        ):
            result = builder.build_single(config, force_rebuild=True)

            # _build_single should be called (not cache)
            mock_build.assert_called_once()
            assert result.success
            assert not result.cached

    def test_build_variants_force_rebuild_calls_cleanup_for_all(
        self, builder: OSSFuzzBuilder, tmp_path: Path
    ):
        """Test that build_variants with force_rebuild=True calls cleanup for all."""
        configs = [
            BuildConfig(
                benchmark_name="test",
                variant_type=VariantType.DELTA_REF,
                commit="abc",
                main_repo="https://example.com",
                benchmark_path=tmp_path / "benchmark",
            ),
            BuildConfig(
                benchmark_name="test",
                variant_type=VariantType.DELTA_REF,
                commit="def",
                main_repo="https://example.com",
                benchmark_path=tmp_path / "benchmark",
            ),
        ]

        def mock_build_single(config: BuildConfig) -> BuildResult:
            return BuildResult(
                config=config,
                success=True,
                variant_name=config.variant_name,
                build_path=Path("/tmp/build"),
            )

        with (
            patch.object(
                builder.infra, "cleanup_build_outputs"
            ) as mock_cleanup_outputs,
            patch.object(builder.infra, "cleanup_source") as mock_cleanup_source,
            patch.object(builder, "_ensure_repos_cached"),
            patch.object(builder, "_build_single", side_effect=mock_build_single),
        ):
            builder.build_variants(configs, force_rebuild=True)

            # Cleanup should be called for all configs
            assert mock_cleanup_outputs.call_count == 2
            assert mock_cleanup_source.call_count == 2

    def test_build_variants_no_force_rebuild_uses_cache(
        self, builder: OSSFuzzBuilder, tmp_path: Path
    ):
        """Test that build_variants without force_rebuild uses cached results."""
        configs = [
            BuildConfig(
                benchmark_name="test",
                variant_type=VariantType.DELTA_REF,
                commit="abc",
                main_repo="https://example.com",
                benchmark_path=tmp_path / "benchmark",
            ),
        ]

        with (
            patch.object(
                builder.infra, "cleanup_build_outputs"
            ) as mock_cleanup_outputs,
            patch.object(builder.infra, "cleanup_source") as mock_cleanup_source,
            patch.object(builder.infra, "is_variant_built", return_value=True),
            patch.object(
                builder.infra, "get_build_output_path", return_value=Path("/tmp/build")
            ),
        ):
            results = builder.build_variants(configs, force_rebuild=False)

            # Cleanup should NOT be called
            mock_cleanup_outputs.assert_not_called()
            mock_cleanup_source.assert_not_called()
            # Should return cached result
            assert results[configs[0].variant_name].cached


class TestBuildConfigVariantNames:
    """Tests for variant name generation.

    Naming convention (with multi-sanitizer support):
    - Format: {benchmark}-{san_short}-{suffix}
    - Base/ref variants: {benchmark}-{san_short}-fullbase, {benchmark}-{san_short}-deltaref
    - Shared variants: {benchmark}-{san_short}-delta-allpatched
    """

    @pytest.mark.parametrize(
        ("variant_type", "cpv_num", "mode", "expected_suffix"),
        [
            # Base/ref variants: mode is in the type name
            (VariantType.FULL_BASE, None, BenchmarkMode.FULL, "fullbase"),
            (VariantType.DELTA_REF, None, BenchmarkMode.DELTA, "deltaref"),
            # Shared variants: mode prefix required
            (VariantType.ALL_PATCHED, None, BenchmarkMode.DELTA, "delta-allpatched"),
            (VariantType.ALL_PATCHED, None, BenchmarkMode.FULL, "full-allpatched"),
            (VariantType.CPV, 0, BenchmarkMode.DELTA, "delta-cpv0"),
            (VariantType.CPV, 1, BenchmarkMode.DELTA, "delta-cpv1"),
            (VariantType.CPV, 10, BenchmarkMode.FULL, "full-cpv10"),
            (VariantType.COVERAGE, None, BenchmarkMode.DELTA, "delta-coverage"),
            (VariantType.COVERAGE, None, BenchmarkMode.FULL, "full-coverage"),
        ],
    )
    def test_variant_name_generation(
        self,
        variant_type: VariantType,
        cpv_num: int | None,
        mode: BenchmarkMode,
        expected_suffix: str,
    ):
        """Test variant name generation for different types."""
        config = BuildConfig(
            benchmark_name="my-benchmark",
            variant_type=variant_type,
            commit="abc",
            main_repo="https://example.com",
            benchmark_path=Path("/tmp"),
            mode=mode,
            cpv_num=cpv_num,
        )
        # Variant names now include sanitizer
        # Coverage variants use "coverage" as sanitizer, others default to "address" -> "asan"
        if variant_type == VariantType.COVERAGE:
            assert config.variant_name == f"my-benchmark-coverage-{expected_suffix}"
        else:
            assert config.variant_name == f"my-benchmark-asan-{expected_suffix}"


class TestIncBuildSupport:
    """Tests for incremental build support in validation variants."""

    @pytest.fixture
    def mock_oss_fuzz_path(self, tmp_path: Path) -> Path:
        """Create a mock oss-fuzz directory."""
        oss_fuzz = tmp_path / "oss-fuzz"
        oss_fuzz.mkdir()
        (oss_fuzz / "infra").mkdir()
        (oss_fuzz / "infra" / "helper.py").touch()
        (oss_fuzz / "projects").mkdir()
        (oss_fuzz / "build" / "out").mkdir(parents=True)
        return oss_fuzz

    @pytest.fixture
    def builder(self, mock_oss_fuzz_path: Path) -> OSSFuzzBuilder:
        """Create builder with mocked infrastructure."""
        return OSSFuzzBuilder(mock_oss_fuzz_path, max_workers=1)

    def test_create_build_plan_with_use_inc_build(
        self, builder: OSSFuzzBuilder, tmp_path: Path
    ):
        """Test that use_inc_build flag is passed to all validation configs."""
        benchmark_path = tmp_path / "benchmark"
        benchmark_path.mkdir()

        plan = builder.create_build_plan(
            benchmark_name="test-delta-01",
            benchmark_path=benchmark_path,
            main_repo="https://github.com/test/repo",
            mode=BenchmarkMode.DELTA,
            base_commit="base123",
            ref_commit="ref456",
            cpv_numbers=[0, 1],
            language="c",
            use_inc_build=True,
        )

        # All configs should have use_inc_build=True
        for config in plan.configs:
            assert config.use_inc_build is True

    def test_create_build_plan_default_uses_inc_build(
        self, builder: OSSFuzzBuilder, tmp_path: Path
    ):
        """Test that use_inc_build defaults to False."""
        benchmark_path = tmp_path / "benchmark"
        benchmark_path.mkdir()

        plan = builder.create_build_plan(
            benchmark_name="test-delta-01",
            benchmark_path=benchmark_path,
            main_repo="https://github.com/test/repo",
            mode=BenchmarkMode.DELTA,
            base_commit="base123",
            ref_commit="ref456",
            cpv_numbers=[0],
            language="c",
        )

        # All configs should have use_inc_build=False (default)
        for config in plan.configs:
            assert config.use_inc_build is False

    def test_build_single_uses_inc_build_when_image_available(
        self, builder: OSSFuzzBuilder, tmp_path: Path
    ):
        """Test that _build_single uses inc-build path when image is available."""
        config = BuildConfig(
            benchmark_name="test-benchmark",
            variant_type=VariantType.DELTA_REF,
            commit="abc123",
            main_repo="https://example.com",
            benchmark_path=tmp_path / "benchmark",
            use_inc_build=True,
        )

        with (
            patch.object(
                builder.infra, "ensure_inc_image", return_value=True
            ) as mock_ensure,
            patch.object(
                builder,
                "_build_with_inc_image",
                return_value=BuildResult(
                    config=config,
                    success=True,
                    variant_name=config.variant_name,
                    build_path=Path("/tmp/build"),
                ),
            ) as mock_inc_build,
            patch.object(builder, "_build_standard") as mock_standard,
        ):
            builder._build_single(config)

            # Should call ensure_inc_image and _build_with_inc_image
            mock_ensure.assert_called_once_with("test-benchmark", "address")
            mock_inc_build.assert_called_once()
            mock_standard.assert_not_called()

    def test_build_single_falls_back_when_image_unavailable(
        self, builder: OSSFuzzBuilder, tmp_path: Path
    ):
        """Test that _build_single falls back to standard build when no image."""
        config = BuildConfig(
            benchmark_name="test-benchmark",
            variant_type=VariantType.DELTA_REF,
            commit="abc123",
            main_repo="https://example.com",
            benchmark_path=tmp_path / "benchmark",
            use_inc_build=True,
        )

        with (
            patch.object(
                builder.infra, "ensure_inc_image", return_value=False
            ) as mock_ensure,
            patch.object(builder, "_build_with_inc_image") as mock_inc_build,
            patch.object(
                builder,
                "_build_standard",
                return_value=BuildResult(
                    config=config,
                    success=True,
                    variant_name=config.variant_name,
                    build_path=Path("/tmp/build"),
                ),
            ) as mock_standard,
        ):
            builder._build_single(config)

            # Should fall back to standard build
            mock_ensure.assert_called_once()
            mock_inc_build.assert_not_called()
            mock_standard.assert_called_once()
            # Image unavailable → fallback signals "prepare the inc-build image"
            call_kwargs = mock_standard.call_args.kwargs
            assert call_kwargs.get("fallback_from_inc") is True

    def test_build_single_skips_inc_build_for_coverage(
        self, builder: OSSFuzzBuilder, tmp_path: Path
    ):
        """Test that coverage variant always uses standard build."""
        config = BuildConfig(
            benchmark_name="test-benchmark",
            variant_type=VariantType.COVERAGE,
            commit="abc123",
            main_repo="https://example.com",
            benchmark_path=tmp_path / "benchmark",
            use_inc_build=True,  # Even if set, should not use inc-build
        )

        with (
            patch.object(builder.infra, "ensure_inc_image") as mock_ensure,
            patch.object(builder, "_build_with_inc_image") as mock_inc_build,
            patch.object(
                builder,
                "_build_standard",
                return_value=BuildResult(
                    config=config,
                    success=True,
                    variant_name=config.variant_name,
                    build_path=Path("/tmp/build"),
                ),
            ) as mock_standard,
        ):
            builder._build_single(config)

            # Should use standard build (coverage doesn't support inc-build)
            mock_ensure.assert_not_called()
            mock_inc_build.assert_not_called()
            mock_standard.assert_called_once()
            # Verify fallback_from_inc=False (not a fallback, just not supported)
            call_kwargs = mock_standard.call_args.kwargs
            assert call_kwargs.get("fallback_from_inc") is False

    def test_build_single_skips_inc_build_when_disabled(
        self, builder: OSSFuzzBuilder, tmp_path: Path
    ):
        """Test that use_inc_build=False skips inc-build check."""
        config = BuildConfig(
            benchmark_name="test-benchmark",
            variant_type=VariantType.DELTA_REF,
            commit="abc123",
            main_repo="https://example.com",
            benchmark_path=tmp_path / "benchmark",
            use_inc_build=False,  # Disabled
        )

        with (
            patch.object(builder.infra, "ensure_inc_image") as mock_ensure,
            patch.object(builder, "_build_with_inc_image") as mock_inc_build,
            patch.object(
                builder,
                "_build_standard",
                return_value=BuildResult(
                    config=config,
                    success=True,
                    variant_name=config.variant_name,
                    build_path=Path("/tmp/build"),
                ),
            ) as mock_standard,
        ):
            builder._build_single(config)

            # Should use standard build (inc-build disabled)
            mock_ensure.assert_not_called()
            mock_inc_build.assert_not_called()
            mock_standard.assert_called_once()
            # Verify fallback_from_inc=False (not a fallback, just disabled)
            call_kwargs = mock_standard.call_args.kwargs
            assert call_kwargs.get("fallback_from_inc") is False


class TestBuildMetadataCaching:
    """Tests for build metadata caching and inc-build cache validation."""

    @pytest.fixture
    def oss_fuzz_path(self, tmp_path: Path) -> Path:
        """Create a mock oss-fuzz directory structure."""
        oss_fuzz = tmp_path / "oss-fuzz"
        (oss_fuzz / "infra").mkdir(parents=True)
        (oss_fuzz / "projects").mkdir(parents=True)
        (oss_fuzz / "build" / "out").mkdir(parents=True)

        # Create a mock helper.py
        helper = oss_fuzz / "infra" / "helper.py"
        helper.write_text("# mock helper")

        return oss_fuzz

    @pytest.fixture
    def infra(self, oss_fuzz_path: Path):
        """Create OSSFuzzInfrastructure instance."""
        from crsbench.builder.infrastructure import OSSFuzzInfrastructure

        return OSSFuzzInfrastructure(oss_fuzz_path)

    def _create_mock_build(
        self,
        infra,
        variant_name: str,
        *,
        inc_build: bool = False,
        with_metadata: bool = True,
    ) -> Path:
        """Create a mock build output with optional metadata."""
        build_path = infra.get_build_output_path(variant_name)
        build_path.mkdir(parents=True, exist_ok=True)

        # Create a mock fuzzer binary
        (build_path / "fuzz_target").write_text("mock binary")

        # Create project symlink
        project_path = infra.projects_base / variant_name
        project_path.mkdir(parents=True, exist_ok=True)

        # Write metadata if requested
        if with_metadata:
            infra.write_build_metadata(
                variant_name, inc_build=inc_build, sanitizer="address"
            )

        return build_path

    def test_is_variant_built_without_require_inc_build(self, infra):
        """Test is_variant_built accepts any cache when require_inc_build is None."""
        # Create non-inc build
        self._create_mock_build(infra, "test-variant", inc_build=False)

        # Should accept without inc_build requirement
        assert infra.is_variant_built("test-variant") is True
        assert infra.is_variant_built("test-variant", require_inc_build=None) is True

    def test_is_variant_built_rejects_non_inc_cache_when_inc_required(self, infra):
        """Test is_variant_built rejects non-inc cache when require_inc_build=True."""
        # Create non-inc build
        self._create_mock_build(infra, "test-variant", inc_build=False)

        # Should reject because cached build is not inc-build
        assert infra.is_variant_built("test-variant", require_inc_build=True) is False

    def test_is_variant_built_accepts_inc_cache_when_inc_required(self, infra):
        """Test is_variant_built accepts inc cache when require_inc_build=True."""
        # Create inc-build
        self._create_mock_build(infra, "test-variant", inc_build=True)

        # Should accept because cached build is inc-build
        assert infra.is_variant_built("test-variant", require_inc_build=True) is True

    def test_is_variant_built_rejects_inc_cache_when_non_inc_required(self, infra):
        """Test is_variant_built rejects inc cache when require_inc_build=False."""
        # Create inc-build
        self._create_mock_build(infra, "test-variant", inc_build=True)

        # Should reject because cached build is inc-build but non-inc required
        assert infra.is_variant_built("test-variant", require_inc_build=False) is False

    def test_is_variant_built_treats_no_metadata_as_non_inc(self, infra):
        """Test legacy builds without metadata are treated as non-inc."""
        # Create build without metadata (legacy)
        self._create_mock_build(
            infra, "test-variant", inc_build=False, with_metadata=False
        )

        # Should accept without requirement
        assert infra.is_variant_built("test-variant") is True

        # Should reject when inc-build required (no metadata = non-inc)
        assert infra.is_variant_built("test-variant", require_inc_build=True) is False

        # Should accept when non-inc required
        assert infra.is_variant_built("test-variant", require_inc_build=False) is True

    def test_builder_uses_cache_when_inc_build_matches(
        self, oss_fuzz_path: Path, infra, tmp_path: Path
    ):
        """Test builder uses cache when inc-build mode matches."""
        builder = OSSFuzzBuilder(oss_fuzz_path)

        # Create inc-build cache (variant name includes sanitizer)
        variant_name = "test-benchmark-asan-deltaref"
        self._create_mock_build(infra, variant_name, inc_build=True)

        config = BuildConfig(
            benchmark_name="test-benchmark",
            variant_type=VariantType.DELTA_REF,
            commit="abc123",
            main_repo="https://example.com",
            benchmark_path=tmp_path / "benchmark",
            use_inc_build=True,  # Matches cached build
        )

        # Mock _build_single to track if it's called
        with patch.object(builder, "_build_single") as mock_build:
            results = builder.build_variants([config])

            # Should use cache, not call _build_single
            mock_build.assert_not_called()
            assert config.variant_name in results
            assert results[config.variant_name].cached is True

    def test_builder_rebuilds_when_inc_build_mismatches(
        self, oss_fuzz_path: Path, infra, tmp_path: Path
    ):
        """Test builder rebuilds when inc-build mode doesn't match cache."""
        builder = OSSFuzzBuilder(oss_fuzz_path)

        # Create non-inc cache (variant name includes sanitizer)
        variant_name = "test-benchmark-asan-deltaref"
        self._create_mock_build(infra, variant_name, inc_build=False)

        config = BuildConfig(
            benchmark_name="test-benchmark",
            variant_type=VariantType.DELTA_REF,
            commit="abc123",
            main_repo="https://example.com",
            benchmark_path=tmp_path / "benchmark",
            use_inc_build=True,  # Doesn't match cached build
        )

        # Mock _build_single to return success
        mock_result = BuildResult(
            config=config,
            success=True,
            variant_name=config.variant_name,
            build_path=Path("/tmp/build"),
        )
        with patch.object(builder, "_build_single", return_value=mock_result) as mock:
            results = builder.build_variants([config])

            # Should rebuild because cache doesn't match inc-build requirement
            mock.assert_called_once()
            assert config.variant_name in results
            assert results[config.variant_name].cached is False
