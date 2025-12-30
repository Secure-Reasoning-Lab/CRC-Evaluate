"""Tests for the OSSFuzzBuilder module."""

from pathlib import Path

import pytest
from crsbench.builder import (
    BenchmarkMode,
    BuildConfig,
    BuildPlan,
    BuildResult,
    OSSFuzzBuilder,
    ParallelExecutor,
    VariantType,
)


class TestVariantType:
    """Tests for VariantType enum."""

    def test_variant_type_values(self):
        """Test variant type values."""
        assert VariantType.FULL_BASE.value == "fullbase"
        assert VariantType.DELTA_BASE.value == "deltabase"
        assert VariantType.DELTA_REF.value == "deltaref"
        assert VariantType.ALL_PATCHED.value == "allpatched"
        assert VariantType.CPV.value == "cpv"
        assert VariantType.COVERAGE.value == "coverage"

    def test_is_validation_variant(self):
        """Test is_validation_variant method."""
        assert VariantType.FULL_BASE.is_validation_variant()
        assert VariantType.DELTA_BASE.is_validation_variant()
        assert VariantType.CPV.is_validation_variant()
        assert not VariantType.COVERAGE.is_validation_variant()


class TestBuildConfig:
    """Tests for BuildConfig dataclass."""

    def test_basic_config(self):
        """Test basic configuration."""
        config = BuildConfig(
            benchmark_name="test-benchmark",
            variant_type=VariantType.DELTA_BASE,
            commit="abc123",
            main_repo="https://github.com/test/repo",
            benchmark_path=Path("/tmp/benchmark"),
            language="c",
        )
        assert config.benchmark_name == "test-benchmark"
        assert config.variant_type == VariantType.DELTA_BASE
        assert config.variant_name == "test-benchmark-deltabase"

    def test_cpv_variant_name(self):
        """Test CPV variant naming."""
        config = BuildConfig(
            benchmark_name="test-benchmark",
            variant_type=VariantType.CPV,
            commit="abc123",
            main_repo="https://github.com/test/repo",
            benchmark_path=Path("/tmp/benchmark"),
            cpv_num=0,
        )
        assert config.variant_name == "test-benchmark-cpv0"

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
            variant_type=VariantType.DELTA_BASE,
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
            variant_type=VariantType.DELTA_BASE,
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
            variant_type=VariantType.DELTA_BASE,
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
            variant_type=VariantType.DELTA_BASE,
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
            variant_type=VariantType.DELTA_BASE,
            commit="abc",
            main_repo="https://example.com",
            benchmark_path=Path("/tmp"),
        )
        plan.add_config(config)
        plan.mark_cached("test-deltabase")

        assert plan.total_count == 1
        assert plan.cached_count == 1
        assert plan.build_count == 0
        assert plan.configs_to_build == []


class TestParallelExecutor:
    """Tests for ParallelExecutor."""

    def test_invalid_workers(self):
        """Test that invalid worker count raises error."""
        with pytest.raises(ValueError, match="max_workers must be >= 1"):
            ParallelExecutor(max_workers=0)

    def test_execute_empty_configs(self):
        """Test executing with empty config list."""
        executor = ParallelExecutor(max_workers=2)
        results = executor.execute_builds([], lambda _c: None)  # type: ignore  # noqa: ARG005
        assert results == {}

    def test_execute_single_success(self):
        """Test executing a single successful build."""
        executor = ParallelExecutor(max_workers=1)
        config = BuildConfig(
            benchmark_name="test",
            variant_type=VariantType.DELTA_BASE,
            commit="abc",
            main_repo="https://example.com",
            benchmark_path=Path("/tmp"),
        )

        def mock_build(c: BuildConfig) -> BuildResult:
            return BuildResult(
                config=c,
                success=True,
                variant_name=c.variant_name,
                build_path=Path("/tmp/build"),
            )

        result = executor.execute_single(config, mock_build)
        assert result.success

    def test_execute_builds_parallel(self):
        """Test parallel execution of multiple builds."""
        executor = ParallelExecutor(max_workers=2)
        configs = [
            BuildConfig(
                benchmark_name="test",
                variant_type=VariantType.DELTA_BASE,
                commit="abc",
                main_repo="https://example.com",
                benchmark_path=Path("/tmp"),
            ),
            BuildConfig(
                benchmark_name="test",
                variant_type=VariantType.DELTA_REF,
                commit="def",
                main_repo="https://example.com",
                benchmark_path=Path("/tmp"),
            ),
        ]

        def mock_build(c: BuildConfig) -> BuildResult:
            return BuildResult(
                config=c,
                success=True,
                variant_name=c.variant_name,
                build_path=Path("/tmp/build"),
            )

        results = executor.execute_builds(configs, mock_build)
        assert len(results) == 2
        assert all(r.success for r in results.values())

    def test_execute_handles_exception(self):
        """Test that exceptions in build function are handled."""
        executor = ParallelExecutor(max_workers=1)
        config = BuildConfig(
            benchmark_name="test",
            variant_type=VariantType.DELTA_BASE,
            commit="abc",
            main_repo="https://example.com",
            benchmark_path=Path("/tmp"),
        )

        def failing_build(_c: BuildConfig) -> BuildResult:  # noqa: ARG001
            raise RuntimeError("Build exploded")

        result = executor.execute_single(config, failing_build)
        assert not result.success
        assert "Unexpected error" in result.error  # type: ignore


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

    def test_create_build_plan_delta_mode(self, mock_oss_fuzz_path: Path, tmp_path: Path):
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

        # Should have: deltabase, deltaref, allpatched, cpv0, cpv1
        assert plan.total_count == 5
        variant_names = [c.variant_name for c in plan.configs]
        assert "test-delta-01-deltabase" in variant_names
        assert "test-delta-01-deltaref" in variant_names
        assert "test-delta-01-allpatched" in variant_names
        assert "test-delta-01-cpv0" in variant_names
        assert "test-delta-01-cpv1" in variant_names

    def test_create_build_plan_full_mode(self, mock_oss_fuzz_path: Path, tmp_path: Path):
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
        assert plan.total_count == 3
        variant_names = [c.variant_name for c in plan.configs]
        assert "test-full-01-fullbase" in variant_names
        assert "test-full-01-allpatched" in variant_names
        assert "test-full-01-cpv0" in variant_names

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

        variant_names = [c.variant_name for c in plan.configs]
        assert "test-coverage" in variant_names

    def test_is_variant_built(self, mock_oss_fuzz_path: Path):
        """Test checking if variant is built."""
        builder = OSSFuzzBuilder(mock_oss_fuzz_path)

        # Create fake built variant
        variant_name = "test-deltabase"
        (mock_oss_fuzz_path / "projects" / variant_name).mkdir()
        build_out = mock_oss_fuzz_path / "build" / "out" / variant_name
        build_out.mkdir(parents=True)
        (build_out / "fuzzer").touch()

        assert builder.is_variant_built(variant_name)
        assert not builder.is_variant_built("nonexistent-variant")


class TestBuildConfigVariantNames:
    """Tests for variant name generation."""

    @pytest.mark.parametrize(
        ("variant_type", "cpv_num", "expected_suffix"),
        [
            (VariantType.FULL_BASE, None, "fullbase"),
            (VariantType.DELTA_BASE, None, "deltabase"),
            (VariantType.DELTA_REF, None, "deltaref"),
            (VariantType.ALL_PATCHED, None, "allpatched"),
            (VariantType.CPV, 0, "cpv0"),
            (VariantType.CPV, 1, "cpv1"),
            (VariantType.CPV, 10, "cpv10"),
            (VariantType.COVERAGE, None, "coverage"),
        ],
    )
    def test_variant_name_generation(
        self,
        variant_type: VariantType,
        cpv_num: int | None,
        expected_suffix: str,
    ):
        """Test variant name generation for different types."""
        config = BuildConfig(
            benchmark_name="my-benchmark",
            variant_type=variant_type,
            commit="abc",
            main_repo="https://example.com",
            benchmark_path=Path("/tmp"),
            cpv_num=cpv_num,
        )
        assert config.variant_name == f"my-benchmark-{expected_suffix}"
