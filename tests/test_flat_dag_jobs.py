"""Tests for flat DAG jobs (BuildSingleVariantJob, BuildVariantsJob, VerifyCpvPovJob, etc.).

Tests verify:
- Job IDs follow expected naming conventions
- Job types match typed concurrency limits
- depends_on chains are correct
- DAG construction produces proper edges
"""

from pathlib import Path

from crsbench.benchmark_ci.jobs.base import JobContext
from crsbench.benchmark_ci.jobs.flat import (
    BuildPatchVariantJob,
    BuildSingleVariantJob,
    BuildVariantsJob,
    FlatCollectCoverageJob,
    PatchVariantTestJob,
    VerifyCpvPovJob,
)
from crsbench.builder.types import BenchmarkMode, VariantType


class TestBuildSingleVariantJob:
    """Tests for BuildSingleVariantJob per-variant build job."""

    def test_job_id_deltaref(self) -> None:
        """Test job_id for DELTA_REF variant."""
        job = BuildSingleVariantJob(
            benchmark_path=Path("/bench/test-proj"),
            benchmark_name="test-proj",
            variant_type=VariantType.DELTA_REF,
            commit="abc123",
            main_repo="https://github.com/test/repo",
            mode=BenchmarkMode.DELTA,
        )
        assert job.job_id == "build-single:test-proj:test-proj-deltaref"

    def test_job_id_allpatched(self) -> None:
        """Test job_id for ALL_PATCHED variant."""
        job = BuildSingleVariantJob(
            benchmark_path=Path("/bench/test-proj"),
            benchmark_name="test-proj",
            variant_type=VariantType.ALL_PATCHED,
            commit="abc123",
            main_repo="https://github.com/test/repo",
            mode=BenchmarkMode.DELTA,
        )
        assert job.job_id == "build-single:test-proj:test-proj-delta-allpatched"

    def test_job_id_fullbase(self) -> None:
        """Test job_id for FULL_BASE variant."""
        job = BuildSingleVariantJob(
            benchmark_path=Path("/bench/test-proj"),
            benchmark_name="test-proj",
            variant_type=VariantType.FULL_BASE,
            commit="abc123",
            main_repo="https://github.com/test/repo",
            mode=BenchmarkMode.FULL,
        )
        assert job.job_id == "build-single:test-proj:test-proj-fullbase"

    def test_job_id_coverage(self) -> None:
        """Test job_id for COVERAGE variant."""
        job = BuildSingleVariantJob(
            benchmark_path=Path("/bench/test-proj"),
            benchmark_name="test-proj",
            variant_type=VariantType.COVERAGE,
            commit="abc123",
            main_repo="https://github.com/test/repo",
            mode=BenchmarkMode.DELTA,
            sanitizer="coverage",
        )
        assert job.job_id == "build-single:test-proj:test-proj-delta-coverage"

    def test_job_id_cpv(self) -> None:
        """Test job_id for CPV variant (all patches except one)."""
        job = BuildSingleVariantJob(
            benchmark_path=Path("/bench/test-proj"),
            benchmark_name="test-proj",
            variant_type=VariantType.CPV,
            commit="abc123",
            main_repo="https://github.com/test/repo",
            mode=BenchmarkMode.FULL,
            cpv_num=0,
            patches=[Path("/patch1.diff")],  # All patches except cpv_0
        )
        assert job.job_id == "build-single:test-proj:test-proj-full-cpv0"

    def test_job_type(self) -> None:
        """Verify job_type is 'build' for DAGExecutor typed limits."""
        job = BuildSingleVariantJob(
            benchmark_path=Path("/bench/test-proj"),
            benchmark_name="test-proj",
            variant_type=VariantType.DELTA_REF,
            commit="abc123",
            main_repo="https://github.com/test/repo",
            mode=BenchmarkMode.DELTA,
        )
        assert job.job_type == "build"

    def test_no_dependencies(self) -> None:
        """BuildSingleVariantJob has no dependencies."""
        job = BuildSingleVariantJob(
            benchmark_path=Path("/bench/test-proj"),
            benchmark_name="test-proj",
            variant_type=VariantType.DELTA_REF,
            commit="abc123",
            main_repo="https://github.com/test/repo",
            mode=BenchmarkMode.DELTA,
        )
        assert job.depends_on == []


class TestBuildVariantsJob:
    def test_job_id(self) -> None:
        job = BuildVariantsJob(
            benchmark_path=Path("/bench/test-proj"),
            benchmark_name="test-proj",
        )
        assert job.job_id == "build-variants:test-proj"

    def test_job_type(self) -> None:
        job = BuildVariantsJob(
            benchmark_path=Path("/bench/test-proj"),
            benchmark_name="test-proj",
        )
        assert job.job_type == "build"

    def test_no_dependencies(self) -> None:
        job = BuildVariantsJob(
            benchmark_path=Path("/bench/test-proj"),
            benchmark_name="test-proj",
        )
        assert job.depends_on == []


class TestVerifyCpvPovJob:
    def test_job_id(self) -> None:
        job = VerifyCpvPovJob(
            benchmark_name="test-proj",
            cpv_id="cpv_0",
            harness="fuzz_target",
            build_job_id="build-variants:test-proj",
        )
        assert job.job_id == "verify-cpv-pov:test-proj:cpv_0"

    def test_job_type(self) -> None:
        job = VerifyCpvPovJob(
            benchmark_name="test-proj",
            cpv_id="cpv_0",
            harness="fuzz_target",
            build_job_id="build-variants:test-proj",
        )
        assert job.job_type == "verify"

    def test_depends_on_build(self) -> None:
        """Test legacy single build_job_id dependency."""
        job = VerifyCpvPovJob(
            benchmark_name="test-proj",
            cpv_id="cpv_0",
            harness="fuzz_target",
            build_job_id="build-variants:test-proj",
        )
        assert job.depends_on == ["build-variants:test-proj"]

    def test_depends_on_multiple_builds(self) -> None:
        """Test new build_job_ids list dependency."""
        job = VerifyCpvPovJob(
            benchmark_name="test-proj",
            cpv_id="cpv_0",
            harness="fuzz_target",
            build_job_ids=[
                "build-single:test-proj:test-proj-deltaref",
                "build-single:test-proj:test-proj-delta-allpatched",
            ],
        )
        assert len(job.depends_on) == 2
        assert "build-single:test-proj:test-proj-deltaref" in job.depends_on
        assert "build-single:test-proj:test-proj-delta-allpatched" in job.depends_on

    def test_build_job_ids_takes_precedence(self) -> None:
        """build_job_ids should take precedence over build_job_id."""
        job = VerifyCpvPovJob(
            benchmark_name="test-proj",
            cpv_id="cpv_0",
            harness="fuzz_target",
            build_job_id="build-variants:test-proj",  # legacy
            build_job_ids=[
                "build-single:test-proj:test-proj-deltaref",
            ],  # new (takes precedence)
        )
        assert job.depends_on == ["build-single:test-proj:test-proj-deltaref"]

    def test_empty_pov_paths_succeeds(self) -> None:
        job = VerifyCpvPovJob(
            benchmark_name="test-proj",
            cpv_id="cpv_0",
            harness="fuzz_target",
            pov_paths=[],
            build_job_id="build-variants:test-proj",
        )
        context = JobContext()
        result = job.execute(context)
        assert result.success
        assert result.details["pov_count"] == 0


class TestBuildPatchVariantJob:
    def test_job_id(self) -> None:
        job = BuildPatchVariantJob(
            benchmark_path=Path("/bench/test-proj"),
            benchmark_name="test-proj",
            cpv_id="cpv_0",
            patch_id="patch_0",
            patch_path=Path("/bench/test-proj/patch_0.diff"),
            build_job_id="build-variants:test-proj",
        )
        assert job.job_id == "build-patch:test-proj:cpv_0:patch_0"

    def test_job_type(self) -> None:
        job = BuildPatchVariantJob(
            benchmark_path=Path("/bench/test-proj"),
            benchmark_name="test-proj",
            cpv_id="cpv_0",
            patch_id="patch_0",
            patch_path=Path("/patch.diff"),
        )
        assert job.job_type == "build"

    def test_depends_on_build(self) -> None:
        job = BuildPatchVariantJob(
            benchmark_path=Path("/bench/test-proj"),
            benchmark_name="test-proj",
            cpv_id="cpv_0",
            patch_id="patch_0",
            patch_path=Path("/patch.diff"),
            build_job_id="build-variants:test-proj",
        )
        assert job.depends_on == ["build-variants:test-proj"]

    def test_no_build_dep(self) -> None:
        job = BuildPatchVariantJob(
            benchmark_path=Path("/bench/test-proj"),
            benchmark_name="test-proj",
            cpv_id="cpv_0",
            patch_id="patch_0",
            patch_path=Path("/patch.diff"),
        )
        assert job.depends_on == []


class TestPatchVariantTestJob:
    def test_job_id_full(self) -> None:
        job = PatchVariantTestJob(
            benchmark_path=Path("/bench/test-proj"),
            benchmark_name="test-proj",
            cpv_id="cpv_0",
            patch_id="patch_0",
            harness="fuzz_target",
            test_mode="FULL",
            build_patch_job_id="build-patch:test-proj:cpv_0:patch_0",
        )
        assert job.job_id == "test-patch:test-proj:cpv_0:patch_0:FULL"

    def test_job_id_rts(self) -> None:
        job = PatchVariantTestJob(
            benchmark_path=Path("/bench/test-proj"),
            benchmark_name="test-proj",
            cpv_id="cpv_0",
            patch_id="patch_0",
            harness="fuzz_target",
            test_mode="RTS",
            build_patch_job_id="build-patch:test-proj:cpv_0:patch_0",
        )
        assert job.job_id == "test-patch:test-proj:cpv_0:patch_0:RTS"

    def test_job_type(self) -> None:
        job = PatchVariantTestJob(
            benchmark_path=Path("/bench/test-proj"),
            benchmark_name="test-proj",
            cpv_id="cpv_0",
            patch_id="patch_0",
            harness="fuzz_target",
        )
        assert job.job_type == "verify"

    def test_depends_on_build_patch(self) -> None:
        job = PatchVariantTestJob(
            benchmark_path=Path("/bench/test-proj"),
            benchmark_name="test-proj",
            cpv_id="cpv_0",
            patch_id="patch_0",
            harness="fuzz_target",
            build_patch_job_id="build-patch:test-proj:cpv_0:patch_0",
        )
        assert job.depends_on == ["build-patch:test-proj:cpv_0:patch_0"]


class TestFlatCollectCoverageJob:
    def test_job_id(self) -> None:
        job = FlatCollectCoverageJob(
            benchmark_path=Path("/bench/test-proj"),
            benchmark_name="test-proj",
            harness="fuzz_target",
            build_job_id="build-variants:test-proj",
        )
        assert job.job_id == "collect-coverage:test-proj"

    def test_job_type(self) -> None:
        job = FlatCollectCoverageJob(
            benchmark_path=Path("/bench/test-proj"),
            benchmark_name="test-proj",
            harness="fuzz_target",
        )
        assert job.job_type == "verify"

    def test_depends_on_build(self) -> None:
        """Test legacy single build_job_id dependency."""
        job = FlatCollectCoverageJob(
            benchmark_path=Path("/bench/test-proj"),
            benchmark_name="test-proj",
            harness="fuzz_target",
            build_job_id="build-variants:test-proj",
        )
        assert job.depends_on == ["build-variants:test-proj"]

    def test_depends_on_build_job_ids(self) -> None:
        """Test new build_job_ids list dependency."""
        job = FlatCollectCoverageJob(
            benchmark_path=Path("/bench/test-proj"),
            benchmark_name="test-proj",
            harness="fuzz_target",
            build_job_ids=["build-single:test-proj:test-proj-delta-coverage"],
        )
        assert job.depends_on == ["build-single:test-proj:test-proj-delta-coverage"]


class TestFlatDAGConstruction:
    """Test that a full flat DAG has proper structure."""

    def test_pov_dag_structure(self) -> None:
        """BuildVariantsJob -> VerifyCpvPovJob per CPV."""
        build = BuildVariantsJob(
            benchmark_path=Path("/bench/proj"),
            benchmark_name="proj",
        )
        verify_0 = VerifyCpvPovJob(
            benchmark_name="proj",
            cpv_id="cpv_0",
            harness="fuzz_target",
            pov_paths=[Path("/pov_0.blob")],
            build_job_id=build.job_id,
        )
        verify_1 = VerifyCpvPovJob(
            benchmark_name="proj",
            cpv_id="cpv_1",
            harness="fuzz_target",
            pov_paths=[Path("/pov_0.blob")],
            build_job_id=build.job_id,
        )

        jobs = [build, verify_0, verify_1]
        graph = {j.job_id: set(j.depends_on) for j in jobs}

        # Verify structure
        assert graph["build-variants:proj"] == set()
        assert graph["verify-cpv-pov:proj:cpv_0"] == {"build-variants:proj"}
        assert graph["verify-cpv-pov:proj:cpv_1"] == {"build-variants:proj"}

    def test_patch_dag_structure(self) -> None:
        """BuildVariantsJob -> BuildPatchVariantJob -> PatchVariantTestJob."""
        build = BuildVariantsJob(
            benchmark_path=Path("/bench/proj"),
            benchmark_name="proj",
        )
        build_patch = BuildPatchVariantJob(
            benchmark_path=Path("/bench/proj"),
            benchmark_name="proj",
            cpv_id="cpv_0",
            patch_id="patch_0",
            patch_path=Path("/patch.diff"),
            build_job_id=build.job_id,
        )
        test_patch = PatchVariantTestJob(
            benchmark_path=Path("/bench/proj"),
            benchmark_name="proj",
            cpv_id="cpv_0",
            patch_id="patch_0",
            harness="fuzz_target",
            test_mode="FULL",
            build_patch_job_id=build_patch.job_id,
        )

        jobs = [build, build_patch, test_patch]
        graph = {j.job_id: set(j.depends_on) for j in jobs}

        assert graph["build-variants:proj"] == set()
        assert graph["build-patch:proj:cpv_0:patch_0"] == {"build-variants:proj"}
        assert graph["test-patch:proj:cpv_0:patch_0:FULL"] == {
            "build-patch:proj:cpv_0:patch_0"
        }

    def test_all_cmd_shared_build(self) -> None:
        """ci all: ONE BuildVariantsJob shared by POV, patch, coverage (legacy)."""
        build = BuildVariantsJob(
            benchmark_path=Path("/bench/proj"),
            benchmark_name="proj",
        )
        verify_pov = VerifyCpvPovJob(
            benchmark_name="proj",
            cpv_id="cpv_0",
            harness="fuzz_target",
            pov_paths=[Path("/pov.blob")],
            build_job_id=build.job_id,
        )
        build_patch = BuildPatchVariantJob(
            benchmark_path=Path("/bench/proj"),
            benchmark_name="proj",
            cpv_id="cpv_0",
            patch_id="patch_0",
            patch_path=Path("/patch.diff"),
            build_job_id=build.job_id,
        )
        coverage = FlatCollectCoverageJob(
            benchmark_path=Path("/bench/proj"),
            benchmark_name="proj",
            harness="fuzz_target",
            build_job_id=build.job_id,
        )

        jobs = [build, verify_pov, build_patch, coverage]

        # All depend on the same build job
        build_dependents = [j for j in jobs if build.job_id in j.depends_on]
        assert len(build_dependents) == 3
        assert verify_pov in build_dependents
        assert build_patch in build_dependents
        assert coverage in build_dependents

        # Only ONE build job exists
        build_jobs = [j for j in jobs if j.job_type == "build"]
        assert len(build_jobs) == 2  # BuildVariantsJob + BuildPatchVariantJob
        build_variant_jobs = [j for j in jobs if isinstance(j, BuildVariantsJob)]
        assert len(build_variant_jobs) == 1

    def test_per_variant_dag_structure(self) -> None:
        """ci all: Per-variant BuildSingleVariantJob enables parallel builds."""
        # Create per-variant build jobs
        vulnerable_build = BuildSingleVariantJob(
            benchmark_path=Path("/bench/proj"),
            benchmark_name="proj",
            variant_type=VariantType.DELTA_REF,
            commit="abc123",
            main_repo="https://github.com/test/repo",
            mode=BenchmarkMode.DELTA,
        )
        allpatched_build = BuildSingleVariantJob(
            benchmark_path=Path("/bench/proj"),
            benchmark_name="proj",
            variant_type=VariantType.ALL_PATCHED,
            commit="abc123",
            main_repo="https://github.com/test/repo",
            mode=BenchmarkMode.DELTA,
        )
        coverage_build = BuildSingleVariantJob(
            benchmark_path=Path("/bench/proj"),
            benchmark_name="proj",
            variant_type=VariantType.COVERAGE,
            commit="abc123",
            main_repo="https://github.com/test/repo",
            mode=BenchmarkMode.DELTA,
            sanitizer="coverage",
        )

        # Verify job depends on both vulnerable and allpatched
        verify_pov = VerifyCpvPovJob(
            benchmark_name="proj",
            cpv_id="cpv_0",
            harness="fuzz_target",
            pov_paths=[Path("/pov.blob")],
            build_job_ids=[vulnerable_build.job_id, allpatched_build.job_id],
        )

        # Patch job depends on vulnerable build only
        build_patch = BuildPatchVariantJob(
            benchmark_path=Path("/bench/proj"),
            benchmark_name="proj",
            cpv_id="cpv_0",
            patch_id="patch_0",
            patch_path=Path("/patch.diff"),
            build_job_id=vulnerable_build.job_id,
        )

        # Coverage depends on coverage build
        coverage = FlatCollectCoverageJob(
            benchmark_path=Path("/bench/proj"),
            benchmark_name="proj",
            harness="fuzz_target",
            build_job_ids=[coverage_build.job_id],
        )

        jobs = [
            vulnerable_build,
            allpatched_build,
            coverage_build,
            verify_pov,
            build_patch,
            coverage,
        ]
        graph = {j.job_id: set(j.depends_on) for j in jobs}

        # All BuildSingleVariantJob have no dependencies (can run in parallel)
        assert graph[vulnerable_build.job_id] == set()
        assert graph[allpatched_build.job_id] == set()
        assert graph[coverage_build.job_id] == set()

        # Verify depends on both build jobs
        assert graph[verify_pov.job_id] == {
            vulnerable_build.job_id,
            allpatched_build.job_id,
        }

        # Patch build depends on vulnerable only
        assert graph[build_patch.job_id] == {vulnerable_build.job_id}

        # Coverage depends on coverage build
        assert graph[coverage.job_id] == {coverage_build.job_id}

        # Count build jobs by type
        build_single_jobs = [j for j in jobs if isinstance(j, BuildSingleVariantJob)]
        assert len(build_single_jobs) == 3  # vulnerable, allpatched, coverage
