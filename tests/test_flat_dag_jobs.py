"""Tests for flat DAG jobs (BuildVariantsJob, VerifyCpvPovJob, etc.).

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
    BuildVariantsJob,
    FlatCollectCoverageJob,
    TestPatchVariantJob,
    VerifyCpvPovJob,
)


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
        job = VerifyCpvPovJob(
            benchmark_name="test-proj",
            cpv_id="cpv_0",
            harness="fuzz_target",
            build_job_id="build-variants:test-proj",
        )
        assert job.depends_on == ["build-variants:test-proj"]

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


class TestTestPatchVariantJob:
    def test_job_id_full(self) -> None:
        job = TestPatchVariantJob(
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
        job = TestPatchVariantJob(
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
        job = TestPatchVariantJob(
            benchmark_path=Path("/bench/test-proj"),
            benchmark_name="test-proj",
            cpv_id="cpv_0",
            patch_id="patch_0",
            harness="fuzz_target",
        )
        assert job.job_type == "verify"

    def test_depends_on_build_patch(self) -> None:
        job = TestPatchVariantJob(
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
        job = FlatCollectCoverageJob(
            benchmark_path=Path("/bench/test-proj"),
            benchmark_name="test-proj",
            harness="fuzz_target",
            build_job_id="build-variants:test-proj",
        )
        assert job.depends_on == ["build-variants:test-proj"]


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
        """BuildVariantsJob -> BuildPatchVariantJob -> TestPatchVariantJob."""
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
        test_patch = TestPatchVariantJob(
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
        """ci all: ONE BuildVariantsJob shared by POV, patch, coverage."""
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
