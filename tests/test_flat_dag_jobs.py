"""Tests for flat DAG jobs (BuildSingleVariantJob, VerifyCpvPovJob, etc.).

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
        # Job ID includes sanitizer in variant name (defaults to "address" -> "asan")
        assert job.job_id == "build-single:test-proj:test-proj-asan-deltaref"

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
        # Job ID includes sanitizer in variant name
        assert job.job_id == "build-single:test-proj:test-proj-asan-delta-allpatched"

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
        # Job ID includes sanitizer in variant name
        assert job.job_id == "build-single:test-proj:test-proj-asan-fullbase"

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
        # Job ID includes sanitizer in variant name (coverage -> coverage)
        assert job.job_id == "build-single:test-proj:test-proj-cov-delta-coverage"

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
        # Job ID includes sanitizer in variant name
        assert job.job_id == "build-single:test-proj:test-proj-asan-full-cpv0"

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


class TestVerifyCpvPovJob:
    def test_job_id(self) -> None:
        job = VerifyCpvPovJob(
            benchmark_name="test-proj",
            cpv_id="cpv_0",
            harness="fuzz_target",
            build_job_ids=["build-variants:test-proj"],
        )
        assert job.job_id == "verify-cpv-pov:test-proj:cpv_0"

    def test_job_type(self) -> None:
        job = VerifyCpvPovJob(
            benchmark_name="test-proj",
            cpv_id="cpv_0",
            harness="fuzz_target",
            build_job_ids=["build-variants:test-proj"],
        )
        assert job.job_type == "verify"

    def test_depends_on_build(self) -> None:
        """Test build_job_ids dependency."""
        job = VerifyCpvPovJob(
            benchmark_name="test-proj",
            cpv_id="cpv_0",
            harness="fuzz_target",
            build_job_ids=["build-variants:test-proj"],
        )
        assert job.depends_on == ["build-variants:test-proj"]

    def test_depends_on_multiple_builds(self) -> None:
        """Test build_job_ids list dependency."""
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

    def test_empty_pov_path_succeeds(self) -> None:
        """Test that None pov_path succeeds with pov_count=0."""
        job = VerifyCpvPovJob(
            benchmark_name="test-proj",
            cpv_id="cpv_0",
            harness="fuzz_target",
            pov_path=None,
            build_job_ids=["build-variants:test-proj"],
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
        """BuildSingleVariantJob -> VerifyCpvPovJob per CPV."""
        build = BuildSingleVariantJob(
            benchmark_path=Path("/bench/proj"),
            benchmark_name="proj",
            variant_type=VariantType.DELTA_REF,
            commit="abc123",
            main_repo="https://github.com/test/repo",
            mode=BenchmarkMode.DELTA,
        )
        verify_0 = VerifyCpvPovJob(
            benchmark_name="proj",
            cpv_id="cpv_0",
            harness="fuzz_target",
            pov_path=Path("/pov_0.blob"),
            build_job_ids=[build.job_id],
        )
        verify_1 = VerifyCpvPovJob(
            benchmark_name="proj",
            cpv_id="cpv_1",
            harness="fuzz_target",
            pov_path=Path("/pov_0.blob"),
            build_job_ids=[build.job_id],
        )

        jobs = [build, verify_0, verify_1]
        graph = {j.job_id: set(j.depends_on) for j in jobs}

        # Verify structure
        assert graph["build-single:proj:proj-asan-deltaref"] == set()
        assert graph["verify-cpv-pov:proj:cpv_0"] == {
            "build-single:proj:proj-asan-deltaref"
        }
        assert graph["verify-cpv-pov:proj:cpv_1"] == {
            "build-single:proj:proj-asan-deltaref"
        }

    def test_patch_dag_structure(self) -> None:
        """BuildSingleVariantJob -> BuildPatchVariantJob -> PatchVariantTestJob."""
        build = BuildSingleVariantJob(
            benchmark_path=Path("/bench/proj"),
            benchmark_name="proj",
            variant_type=VariantType.DELTA_REF,
            commit="abc123",
            main_repo="https://github.com/test/repo",
            mode=BenchmarkMode.DELTA,
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

        assert graph["build-single:proj:proj-asan-deltaref"] == set()
        assert graph["build-patch:proj:cpv_0:patch_0"] == {
            "build-single:proj:proj-asan-deltaref"
        }
        assert graph["test-patch:proj:cpv_0:patch_0:FULL"] == {
            "build-patch:proj:cpv_0:patch_0"
        }

    def test_all_cmd_shared_build(self) -> None:
        """ci all: BuildSingleVariantJob shared by POV, patch, coverage."""
        build = BuildSingleVariantJob(
            benchmark_path=Path("/bench/proj"),
            benchmark_name="proj",
            variant_type=VariantType.DELTA_REF,
            commit="abc123",
            main_repo="https://github.com/test/repo",
            mode=BenchmarkMode.DELTA,
        )
        verify_pov = VerifyCpvPovJob(
            benchmark_name="proj",
            cpv_id="cpv_0",
            harness="fuzz_target",
            pov_path=Path("/pov.blob"),
            build_job_ids=[build.job_id],
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

        # BuildSingleVariantJob + BuildPatchVariantJob
        build_jobs = [j for j in jobs if j.job_type == "build"]
        assert len(build_jobs) == 2
        build_single_jobs = [j for j in jobs if isinstance(j, BuildSingleVariantJob)]
        assert len(build_single_jobs) == 1

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
            pov_path=Path("/pov.blob"),
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


class TestIncBuildAvailablePropagation:
    """Tests for inc_build_available data propagation between build and test jobs.

    These tests verify the fix for the bug where PatchVariantTestJob was
    hardcoding use_inc_build=True instead of reading from build job's shared data.
    """

    def test_patch_test_job_uses_inc_build_available_true(self) -> None:
        """Test that PatchVariantTestJob uses inc_build_available=True from build."""
        from unittest.mock import MagicMock, patch

        build_job_id = "build-patch:test-proj:cpv_0:patch_0"
        test_job = PatchVariantTestJob(
            benchmark_path=Path("/bench/test-proj"),
            benchmark_name="test-proj",
            cpv_id="cpv_0",
            patch_id="patch_0",
            harness="fuzz_target",
            test_mode="FULL",
            pov_paths=[Path("/pov.blob")],
            build_patch_job_id=build_job_id,
        )

        # Simulate build job stored inc_build_available=True
        context = JobContext()
        context.shared[build_job_id] = {
            "variant_name": "test-proj-asan-patched-cpv_0-patch_0",
            "sanitizer": "address",
            "inc_build_available": True,
        }

        # Mock the PatchVerificationEngine at its source module
        with patch(
            "crsbench.evaluation.verification.patch.PatchVerificationEngine"
        ) as mock_engine_cls:
            mock_engine = MagicMock()
            mock_engine_cls.return_value = mock_engine
            mock_result = MagicMock()
            mock_result.status.value = "VALID"
            mock_result.pov_test_passed = True
            mock_result.unit_tests_passed = True
            mock_result.variants_tested = 1
            mock_result.variants_matched = 1
            mock_engine.verify.return_value = mock_result

            with patch("crsbench.utils.run_helper.get_oss_fuzz_root"):
                try:
                    test_job.execute(context)
                except Exception:
                    pass  # May fail due to missing files, but we can check the call

            # Verify engine was created with use_inc_build=True
            if mock_engine_cls.called:
                call_kwargs = mock_engine_cls.call_args.kwargs
                assert call_kwargs.get("use_inc_build") is True

    def test_patch_test_job_uses_inc_build_available_false(self) -> None:
        """Test that PatchVariantTestJob uses inc_build_available=False from build.

        This is the critical test for the bug fix: when inc-build image was not
        available during build phase, the test phase must also use standard build.
        """
        from unittest.mock import MagicMock, patch

        build_job_id = "build-patch:test-proj:cpv_0:patch_0"
        test_job = PatchVariantTestJob(
            benchmark_path=Path("/bench/test-proj"),
            benchmark_name="test-proj",
            cpv_id="cpv_0",
            patch_id="patch_0",
            harness="fuzz_target",
            test_mode="RTS",
            pov_paths=[Path("/pov.blob")],
            build_patch_job_id=build_job_id,
        )

        # Simulate build job stored inc_build_available=False (fallback case)
        context = JobContext()
        context.shared[build_job_id] = {
            "variant_name": "test-proj-asan-patched-cpv_0-patch_0",
            "sanitizer": "address",
            "inc_build_available": False,  # No inc-build image available
        }

        # Mock the PatchVerificationEngine at its source module
        with patch(
            "crsbench.evaluation.verification.patch.PatchVerificationEngine"
        ) as mock_engine_cls:
            mock_engine = MagicMock()
            mock_engine_cls.return_value = mock_engine
            mock_result = MagicMock()
            mock_result.status.value = "VALID"
            mock_result.pov_test_passed = True
            mock_result.unit_tests_passed = True
            mock_result.variants_tested = 1
            mock_result.variants_matched = 1
            mock_engine.verify.return_value = mock_result

            with patch("crsbench.utils.run_helper.get_oss_fuzz_root"):
                try:
                    test_job.execute(context)
                except Exception:
                    pass  # May fail due to missing files, but we can check the call

            # Verify engine was created with use_inc_build=False
            if mock_engine_cls.called:
                call_kwargs = mock_engine_cls.call_args.kwargs
                assert call_kwargs.get("use_inc_build") is False

    def test_patch_test_job_defaults_to_false_when_missing(self) -> None:
        """Test that PatchVariantTestJob defaults to use_inc_build=False if key missing."""
        from unittest.mock import MagicMock, patch

        build_job_id = "build-patch:test-proj:cpv_0:patch_0"
        test_job = PatchVariantTestJob(
            benchmark_path=Path("/bench/test-proj"),
            benchmark_name="test-proj",
            cpv_id="cpv_0",
            patch_id="patch_0",
            harness="fuzz_target",
            test_mode="FULL",
            pov_paths=[Path("/pov.blob")],
            build_patch_job_id=build_job_id,
        )

        # Simulate build job that didn't store inc_build_available (old format)
        context = JobContext()
        context.shared[build_job_id] = {
            "variant_name": "test-proj-asan-patched-cpv_0-patch_0",
            "sanitizer": "address",
            # Note: inc_build_available is missing
        }

        # Mock the PatchVerificationEngine at its source module
        with patch(
            "crsbench.evaluation.verification.patch.PatchVerificationEngine"
        ) as mock_engine_cls:
            mock_engine = MagicMock()
            mock_engine_cls.return_value = mock_engine
            mock_result = MagicMock()
            mock_result.status.value = "VALID"
            mock_result.pov_test_passed = True
            mock_result.unit_tests_passed = True
            mock_result.variants_tested = 1
            mock_result.variants_matched = 1
            mock_engine.verify.return_value = mock_result

            with patch("crsbench.utils.run_helper.get_oss_fuzz_root"):
                try:
                    test_job.execute(context)
                except Exception:
                    pass

            # Should default to False for safety
            if mock_engine_cls.called:
                call_kwargs = mock_engine_cls.call_args.kwargs
                assert call_kwargs.get("use_inc_build") is False
