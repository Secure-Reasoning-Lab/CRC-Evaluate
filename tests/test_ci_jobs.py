"""Tests for CI verify/test job serialization and execution."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest


class TestSerializeCiJob:
    """Test serialize_ci_job() for all job types."""

    def _make_verify_cpv_pov_job(self):
        from crsbench.benchmark_ci.jobs.flat import VerifyCpvPovJob

        return VerifyCpvPovJob(
            benchmark_name="test-bench",
            cpv_id="cpv_0",
            harness="fuzz_target",
            benchmark_path=Path("/benchmarks/test-bench"),
            pov_path=Path(
                "/benchmarks/test-bench/.aixcc/fuzz_target/cpv_0/blobs/pov_0.blob"
            ),
            build_job_ids=["build-single:test-bench:test-bench-asan-deltabase"],
            source_mode="pkgs",
        )

    def _make_patch_pov_test_job(self):
        from crsbench.benchmark_ci.jobs.flat import PatchPovTestJob

        return PatchPovTestJob(
            benchmark_path=Path("/benchmarks/test-bench"),
            benchmark_name="test-bench",
            cpv_id="cpv_0",
            patch_id="patch_0",
            harness="fuzz_target",
            pov_path=Path(
                "/benchmarks/test-bench/.aixcc/fuzz_target/cpv_0/blobs/pov_0.blob"
            ),
            build_patch_job_id="build-patch:test-bench:cpv_0:patch_0",
            source_mode="pkgs",
        )

    def test_serialize_verify_cpv_pov(self) -> None:
        """VerifyCpvPovJob serializes all fields."""
        from crsbench.distributed.ci_jobs import serialize_ci_job

        job = self._make_verify_cpv_pov_job()
        params = serialize_ci_job(job)

        assert params["_job_class"] == "VerifyCpvPovJob"
        assert params["benchmark_name"] == "test-bench"
        assert params["cpv_id"] == "cpv_0"
        assert params["harness"] == "fuzz_target"
        assert params["benchmark_path"] == "/benchmarks/test-bench"
        assert params["build_job_ids"] == [
            "build-single:test-bench:test-bench-asan-deltabase"
        ]
        assert params["source_mode"] == "pkgs"

    def test_serialize_patch_pov_test(self) -> None:
        """PatchPovTestJob serializes all fields."""
        from crsbench.distributed.ci_jobs import serialize_ci_job

        job = self._make_patch_pov_test_job()
        params = serialize_ci_job(job)

        assert params["_job_class"] == "PatchPovTestJob"
        assert params["patch_id"] == "patch_0"
        assert params["build_patch_job_id"] == "build-patch:test-bench:cpv_0:patch_0"

    def test_serialize_unsupported_type_raises(self) -> None:
        """Unsupported job type raises ValueError."""
        from crsbench.distributed.ci_jobs import serialize_ci_job

        job = MagicMock()
        type(job).__name__ = "UnknownJob"

        with pytest.raises(ValueError, match="Unsupported job type"):
            serialize_ci_job(job)

    def test_roundtrip_verify_cpv_pov(self) -> None:
        """VerifyCpvPovJob can be serialized and reconstructed."""
        from crsbench.distributed.ci_jobs import _reconstruct_job, serialize_ci_job

        original = self._make_verify_cpv_pov_job()
        params = serialize_ci_job(original)
        restored = _reconstruct_job(params)

        assert type(restored).__name__ == "VerifyCpvPovJob"
        assert restored.benchmark_name == "test-bench"
        assert restored.cpv_id == "cpv_0"
        assert restored.benchmark_path == Path("/benchmarks/test-bench")
        assert restored.build_job_ids == original.build_job_ids

    def test_roundtrip_patch_pov_test(self) -> None:
        """PatchPovTestJob can be serialized and reconstructed."""
        from crsbench.distributed.ci_jobs import _reconstruct_job, serialize_ci_job

        original = self._make_patch_pov_test_job()
        params = serialize_ci_job(original)
        restored = _reconstruct_job(params)

        assert type(restored).__name__ == "PatchPovTestJob"
        assert restored.patch_id == "patch_0"
        assert restored.build_patch_job_id == original.build_patch_job_id


class TestSerializeAllJobTypes:
    """Test serialization for every supported job type."""

    def test_verify_cpv_var_roundtrip(self) -> None:
        from crsbench.benchmark_ci.jobs.flat import VerifyCpvVarJob
        from crsbench.distributed.ci_jobs import _reconstruct_job, serialize_ci_job

        job = VerifyCpvVarJob(
            benchmark_name="bench",
            cpv_id="cpv_0",
            harness="h",
            benchmark_path=Path("/b"),
            pov_paths=[Path("/p1"), Path("/p2")],
            build_job_ids=["b1"],
        )
        params = serialize_ci_job(job)
        restored = _reconstruct_job(params)
        assert type(restored).__name__ == "VerifyCpvVarJob"
        assert len(restored.pov_paths) == 2

    def test_patch_var_test_roundtrip(self) -> None:
        from crsbench.benchmark_ci.jobs.flat import PatchVarTestJob
        from crsbench.distributed.ci_jobs import _reconstruct_job, serialize_ci_job

        job = PatchVarTestJob(
            benchmark_path=Path("/b"),
            benchmark_name="bench",
            cpv_id="cpv_0",
            patch_id="p0",
            harness="h",
            pov_paths=[Path("/pov1")],
            build_patch_job_id="bp1",
        )
        params = serialize_ci_job(job)
        restored = _reconstruct_job(params)
        assert type(restored).__name__ == "PatchVarTestJob"
        assert restored.patch_id == "p0"

    def test_patch_unit_test_roundtrip(self) -> None:
        from crsbench.benchmark_ci.jobs.flat import PatchUnitTestJob
        from crsbench.distributed.ci_jobs import _reconstruct_job, serialize_ci_job

        job = PatchUnitTestJob(
            benchmark_path=Path("/b"),
            benchmark_name="bench",
            cpv_id="cpv_0",
            patch_id="p0",
            harness="h",
            test_mode="RTS",
            build_patch_job_id="bp1",
        )
        params = serialize_ci_job(job)
        restored = _reconstruct_job(params)
        assert type(restored).__name__ == "PatchUnitTestJob"
        assert restored.test_mode == "RTS"

    def test_coverage_job_roundtrip(self) -> None:
        from crsbench.benchmark_ci.jobs.flat import FlatCollectCoverageJob
        from crsbench.distributed.ci_jobs import _reconstruct_job, serialize_ci_job

        job = FlatCollectCoverageJob(
            benchmark_path=Path("/b"),
            benchmark_name="bench",
            harness="h",
            build_job_ids=["b1", "b2"],
        )
        params = serialize_ci_job(job)
        restored = _reconstruct_job(params)
        assert type(restored).__name__ == "FlatCollectCoverageJob"
        assert len(restored.build_job_ids) == 2

    def test_build_patch_variant_roundtrip(self) -> None:
        from crsbench.benchmark_ci.jobs.flat import BuildPatchVariantJob
        from crsbench.distributed.ci_jobs import _reconstruct_job, serialize_ci_job

        job = BuildPatchVariantJob(
            benchmark_path=Path("/b"),
            benchmark_name="bench",
            cpv_id="cpv_0",
            patch_id="p0",
            patch_path=Path("/patch.diff"),
            harness="h",
        )
        params = serialize_ci_job(job)
        restored = _reconstruct_job(params)
        assert type(restored).__name__ == "BuildPatchVariantJob"
        assert restored.patch_path == Path("/patch.diff")


class TestCiResultsToExecutorResults:
    """Test ci_results_to_executor_results() conversion."""

    def test_success_result_converted(self) -> None:
        """Successful job result maps to ExecutorResult with SUCCESS status."""
        from crsbench.distributed.ci_jobs import ci_results_to_executor_results

        raw = {
            "verify-cpv:bench:cpv_0": {
                "job_id": "verify-cpv:bench:cpv_0",
                "job_type": "verify",
                "success": True,
                "started_at": "2025-01-01T00:00:00",
                "finished_at": "2025-01-01T00:01:00",
                "elapsed_seconds": 60.0,
                "error": None,
                "details": {"cpv_matched": ["cpv_0"]},
            }
        }
        results = ci_results_to_executor_results(raw)
        assert len(results) == 1
        r = results["verify-cpv:bench:cpv_0"]
        assert r.status.value == "success"
        assert r.job_result is not None
        assert r.job_result.success is True

    def test_failed_result_converted(self) -> None:
        """Failed job result maps to ExecutorResult with FAILED status."""
        from crsbench.distributed.ci_jobs import ci_results_to_executor_results

        raw = {
            "verify-cpv:bench:cpv_0": {
                "job_id": "verify-cpv:bench:cpv_0",
                "success": False,
                "error": "Docker image not found",
            }
        }
        results = ci_results_to_executor_results(raw)
        r = results["verify-cpv:bench:cpv_0"]
        assert r.status.value == "failed"
        assert "Docker image not found" in r.error

    def test_empty_input_returns_empty(self) -> None:
        """Empty raw results produce empty output."""
        from crsbench.distributed.ci_jobs import ci_results_to_executor_results

        assert ci_results_to_executor_results({}) == {}


class TestReconstructUnknown:
    """Test _reconstruct_job with unknown class."""

    def test_unknown_class_raises(self) -> None:
        from crsbench.distributed.ci_jobs import _reconstruct_job

        with pytest.raises(ValueError, match="Unknown job class"):
            _reconstruct_job({"_job_class": "NonExistentJob"})
