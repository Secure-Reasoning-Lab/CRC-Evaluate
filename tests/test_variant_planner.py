"""Tests for VariantPlanner centralized build job creation."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from crsbench.benchmark_ci.jobs.flat import BuildSingleVariantJob
from crsbench.builder.types import BenchmarkMode, VariantType
from crsbench.executor.variant_planner import VariantPlanner


@pytest.fixture
def planner() -> VariantPlanner:
    return VariantPlanner(oss_fuzz_path=Path("oss-fuzz"), source_mode="pkgs")


def _mock_adapter(
    *,
    ref_commit: str = "abc123",
    base_commit: str = "",
    main_repo: str = "https://github.com/test/repo",
    lang: str = "c",
    repo_name: str = "test-repo",
    sanitizers: list[str] | None = None,
    cpv_sanitizers: dict[str, str] | None = None,
) -> MagicMock:
    """Create a mock MetaYamlAdapter."""
    adapter = MagicMock()
    adapter.get_ref_commit.return_value = ref_commit or None
    adapter.get_base_commit.return_value = base_commit or None
    adapter.main_repo = main_repo
    adapter.lang = lang
    adapter.repo_name = repo_name
    adapter.get_all_cpv_sanitizers.return_value = sanitizers or ["address"]
    if cpv_sanitizers:
        adapter.get_cpv_sanitizer.side_effect = lambda _h, c: cpv_sanitizers.get(
            c, "address"
        )
    else:
        adapter.get_cpv_sanitizer.return_value = "address"
    return adapter


class TestVariantPlannerDelta:
    """Delta mode benchmark planning."""

    @patch(
        "crsbench.executor.variant_planner.VariantPlanner._check_inc_build_support",
        return_value=True,
    )
    @patch("crsbench.builder.infrastructure.OSSFuzzInfrastructure")
    @patch("crsbench.executor.variant_planner.VariantPlanner._load_adapter")
    def test_basic_delta_benchmark(
        self, mock_load_adapter, mock_infra_cls, mock_inc, planner
    ) -> None:
        """Delta benchmark produces vulnerable, allpatched, and CPV jobs."""
        adapter = _mock_adapter(ref_commit="abc123")
        mock_load_adapter.return_value = adapter

        infra = MagicMock()
        infra.get_all_patches.return_value = [Path("/p1.diff"), Path("/p2.diff")]
        infra.get_patches_except.return_value = [Path("/p1.diff")]
        mock_infra_cls.return_value = infra

        # Mock discovery: one harness, one CPV
        with (
            patch(
                "crsbench.benchmark_ci.cli.benchmark_discovery.discover_harness_names",
                return_value=["fuzz_target"],
            ),
            patch(
                "crsbench.benchmark_ci.cli.benchmark_discovery.discover_cpv_ids",
                return_value=["cpv_0"],
            ),
        ):
            jobs = planner.plan_builds(Path("/benchmarks/test-proj"))

        # Should produce: 1 vulnerable + 1 allpatched + 1 CPV = 3 jobs
        assert len(jobs) == 3

        # All should be BuildSingleVariantJob
        assert all(isinstance(j, BuildSingleVariantJob) for j in jobs)

        # Check variant types
        types = [j.variant_type for j in jobs]
        assert VariantType.DELTA_REF in types
        assert VariantType.ALL_PATCHED in types
        assert VariantType.CPV in types

        # Check inc-build is enabled
        assert all(j.use_inc_build for j in jobs)

    @patch(
        "crsbench.executor.variant_planner.VariantPlanner._check_inc_build_support",
        return_value=True,
    )
    @patch("crsbench.builder.infrastructure.OSSFuzzInfrastructure")
    @patch("crsbench.executor.variant_planner.VariantPlanner._load_adapter")
    def test_full_build_mode(
        self, mock_load_adapter, mock_infra_cls, mock_inc, planner
    ) -> None:
        """use_inc_build=False disables inc-build for all jobs."""
        adapter = _mock_adapter(ref_commit="abc123")
        mock_load_adapter.return_value = adapter

        infra = MagicMock()
        infra.get_all_patches.return_value = []
        infra.get_patches_except.return_value = []
        mock_infra_cls.return_value = infra

        with (
            patch(
                "crsbench.benchmark_ci.cli.benchmark_discovery.discover_harness_names",
                return_value=["fuzz_target"],
            ),
            patch(
                "crsbench.benchmark_ci.cli.benchmark_discovery.discover_cpv_ids",
                return_value=["cpv_0"],
            ),
        ):
            jobs = planner.plan_builds(
                Path("/benchmarks/test-proj"), use_inc_build=False
            )

        assert all(not j.use_inc_build for j in jobs)


class TestVariantPlannerFull:
    """Full mode benchmark planning."""

    @patch(
        "crsbench.executor.variant_planner.VariantPlanner._check_inc_build_support",
        return_value=True,
    )
    @patch("crsbench.builder.infrastructure.OSSFuzzInfrastructure")
    @patch("crsbench.executor.variant_planner.VariantPlanner._load_adapter")
    def test_full_mode_uses_full_base(
        self, mock_load_adapter, mock_infra_cls, mock_inc, planner
    ) -> None:
        """Full mode benchmark uses FULL_BASE instead of DELTA_REF."""
        adapter = _mock_adapter(ref_commit="", base_commit="def456")
        mock_load_adapter.return_value = adapter

        infra = MagicMock()
        infra.get_all_patches.return_value = []
        infra.get_patches_except.return_value = []
        mock_infra_cls.return_value = infra

        with (
            patch(
                "crsbench.benchmark_ci.cli.benchmark_discovery.discover_harness_names",
                return_value=["fuzz_target"],
            ),
            patch(
                "crsbench.benchmark_ci.cli.benchmark_discovery.discover_cpv_ids",
                return_value=["cpv_0"],
            ),
        ):
            jobs = planner.plan_builds(Path("/benchmarks/test-proj"))

        vulnerable_jobs = [j for j in jobs if j.variant_type == VariantType.FULL_BASE]
        assert len(vulnerable_jobs) == 1
        assert vulnerable_jobs[0].mode == BenchmarkMode.FULL


class TestVariantPlannerMultiSanitizer:
    """Multi-sanitizer benchmark planning."""

    @patch(
        "crsbench.executor.variant_planner.VariantPlanner._check_inc_build_support",
        return_value=True,
    )
    @patch("crsbench.builder.infrastructure.OSSFuzzInfrastructure")
    @patch("crsbench.executor.variant_planner.VariantPlanner._load_adapter")
    def test_multi_sanitizer_creates_per_sanitizer_shared_variants(
        self, mock_load_adapter, mock_infra_cls, mock_inc, planner
    ) -> None:
        """Each sanitizer gets its own vulnerable and allpatched variants."""
        adapter = _mock_adapter(
            ref_commit="abc123",
            sanitizers=["address", "undefined"],
            cpv_sanitizers={"cpv_0": "address", "cpv_1": "undefined"},
        )
        mock_load_adapter.return_value = adapter

        infra = MagicMock()
        infra.get_all_patches.return_value = [Path("/p.diff")]
        infra.get_patches_except.return_value = []
        mock_infra_cls.return_value = infra

        with (
            patch(
                "crsbench.benchmark_ci.cli.benchmark_discovery.discover_harness_names",
                return_value=["fuzz_target"],
            ),
            patch(
                "crsbench.benchmark_ci.cli.benchmark_discovery.discover_cpv_ids",
                return_value=["cpv_0", "cpv_1"],
            ),
        ):
            jobs = planner.plan_builds(Path("/benchmarks/test-proj"))

        # 2 sanitizers * (1 vulnerable + 1 allpatched) + 2 CPVs = 6
        assert len(jobs) == 6

        vulnerable_jobs = [j for j in jobs if j.variant_type == VariantType.DELTA_REF]
        assert len(vulnerable_jobs) == 2
        sanitizers = {j.sanitizer for j in vulnerable_jobs}
        assert sanitizers == {"address", "undefined"}


class TestVariantPlannerCoverage:
    """Coverage variant inclusion."""

    @patch(
        "crsbench.executor.variant_planner.VariantPlanner._check_inc_build_support",
        return_value=True,
    )
    @patch("crsbench.builder.infrastructure.OSSFuzzInfrastructure")
    @patch("crsbench.executor.variant_planner.VariantPlanner._load_adapter")
    def test_include_coverage(
        self, mock_load_adapter, mock_infra_cls, mock_inc, planner
    ) -> None:
        adapter = _mock_adapter(ref_commit="abc123")
        mock_load_adapter.return_value = adapter

        infra = MagicMock()
        infra.get_all_patches.return_value = []
        infra.get_patches_except.return_value = []
        mock_infra_cls.return_value = infra

        with (
            patch(
                "crsbench.benchmark_ci.cli.benchmark_discovery.discover_harness_names",
                return_value=["fuzz_target"],
            ),
            patch(
                "crsbench.benchmark_ci.cli.benchmark_discovery.discover_cpv_ids",
                return_value=["cpv_0"],
            ),
        ):
            jobs = planner.plan_builds(
                Path("/benchmarks/test-proj"), include_coverage=True
            )

        coverage_jobs = [j for j in jobs if j.variant_type == VariantType.COVERAGE]
        assert len(coverage_jobs) == 1
        assert not coverage_jobs[0].use_inc_build
        assert coverage_jobs[0].sanitizer == "coverage"


class TestVariantPlannerPatched:
    """Patched variant creation for patch verification."""

    @patch(
        "crsbench.executor.variant_planner.VariantPlanner._check_inc_build_support",
        return_value=True,
    )
    @patch("crsbench.builder.infrastructure.OSSFuzzInfrastructure")
    @patch("crsbench.executor.variant_planner.VariantPlanner._load_adapter")
    def test_include_patched(
        self, mock_load_adapter, mock_infra_cls, mock_inc, planner
    ) -> None:
        adapter = _mock_adapter(ref_commit="abc123")
        mock_load_adapter.return_value = adapter

        infra = MagicMock()
        infra.get_all_patches.return_value = []
        infra.get_patches_except.return_value = []
        mock_infra_cls.return_value = infra

        with (
            patch(
                "crsbench.benchmark_ci.cli.benchmark_discovery.discover_harness_names",
                return_value=["fuzz_target"],
            ),
            patch(
                "crsbench.benchmark_ci.cli.benchmark_discovery.discover_cpv_ids",
                return_value=["cpv_0"],
            ),
            patch(
                "crsbench.benchmark_ci.cli.benchmark_discovery.discover_patch_paths",
                return_value=[("patch_0", Path("/patches/patch_0.diff"))],
            ),
        ):
            jobs = planner.plan_builds(
                Path("/benchmarks/test-proj"), include_patched=True
            )

        patched_jobs = [j for j in jobs if j.variant_type == VariantType.PATCHED]
        assert len(patched_jobs) == 1
        assert patched_jobs[0].patch_id == "patch_0"
        assert patched_jobs[0].pov_id == "cpv_0"
        assert patched_jobs[0].patches == [Path("/patches/patch_0.diff")]


class TestVariantPlannerEdgeCases:
    """Edge cases and error handling."""

    @patch("crsbench.executor.variant_planner.VariantPlanner._load_adapter")
    def test_adapter_load_failure(self, mock_load_adapter, planner) -> None:
        """Returns empty list when adapter fails to load."""
        mock_load_adapter.return_value = None
        jobs = planner.plan_builds(Path("/benchmarks/broken"))
        assert jobs == []

    @patch("crsbench.executor.variant_planner.VariantPlanner._load_adapter")
    def test_no_commit_returns_empty(self, mock_load_adapter, planner) -> None:
        """Returns empty list when no commit is found."""
        adapter = _mock_adapter(ref_commit="", base_commit="")
        mock_load_adapter.return_value = adapter
        jobs = planner.plan_builds(Path("/benchmarks/no-commit"))
        assert jobs == []


class TestVariantPlannerMultiBenchmark:
    """plan_all_builds across multiple benchmarks."""

    @patch(
        "crsbench.executor.variant_planner.VariantPlanner._check_inc_build_support",
        return_value=True,
    )
    @patch("crsbench.builder.infrastructure.OSSFuzzInfrastructure")
    @patch("crsbench.executor.variant_planner.VariantPlanner._load_adapter")
    def test_plan_all_builds(
        self, mock_load_adapter, mock_infra_cls, mock_inc, planner
    ) -> None:
        adapter = _mock_adapter(ref_commit="abc123")
        mock_load_adapter.return_value = adapter

        infra = MagicMock()
        infra.get_all_patches.return_value = []
        infra.get_patches_except.return_value = []
        mock_infra_cls.return_value = infra

        with (
            patch(
                "crsbench.benchmark_ci.cli.benchmark_discovery.discover_harness_names",
                return_value=["fuzz_target"],
            ),
            patch(
                "crsbench.benchmark_ci.cli.benchmark_discovery.discover_cpv_ids",
                return_value=["cpv_0"],
            ),
        ):
            jobs = planner.plan_all_builds(
                [
                    Path("/benchmarks/bench-a"),
                    Path("/benchmarks/bench-b"),
                ]
            )

        # 2 benchmarks * 3 jobs each = 6
        assert len(jobs) == 6


class TestBuildSingleVariantJobPatched:
    """Verify BuildSingleVariantJob handles PATCHED variant type."""

    def test_patched_job_id(self) -> None:
        """PATCHED variant job_id includes pov_id and patch_id."""
        job = BuildSingleVariantJob(
            benchmark_path=Path("/bench/test-proj"),
            benchmark_name="test-proj",
            variant_type=VariantType.PATCHED,
            commit="abc123",
            main_repo="https://github.com/test/repo",
            mode=BenchmarkMode.DELTA,
            cpv_num=0,
            patch_id="patch_0",
            pov_id="cpv_0",
            patches=[Path("/patches/patch_0.diff")],
        )
        expected = "build-single/test-proj/test-proj-asan-delta-patched-cpv_0-patch_0"
        assert job.job_id == expected

    def test_patched_job_type(self) -> None:
        job = BuildSingleVariantJob(
            benchmark_path=Path("/bench/test-proj"),
            benchmark_name="test-proj",
            variant_type=VariantType.PATCHED,
            commit="abc123",
            main_repo="https://github.com/test/repo",
            mode=BenchmarkMode.DELTA,
            patch_id="patch_0",
            pov_id="cpv_0",
            patches=[Path("/patches/patch_0.diff")],
        )
        assert job.job_type == "build"

    def test_patched_no_depends(self) -> None:
        """PATCHED jobs have no dependencies (all builds are independent)."""
        job = BuildSingleVariantJob(
            benchmark_path=Path("/bench/test-proj"),
            benchmark_name="test-proj",
            variant_type=VariantType.PATCHED,
            commit="abc123",
            main_repo="https://github.com/test/repo",
            mode=BenchmarkMode.DELTA,
            patch_id="patch_0",
            pov_id="cpv_0",
            patches=[Path("/patches/patch_0.diff")],
        )
        assert job.depends_on == []
