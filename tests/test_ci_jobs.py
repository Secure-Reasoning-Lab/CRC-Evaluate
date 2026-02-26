"""Tests for CI verify/test job serialization and execution."""

from pathlib import Path
from unittest.mock import MagicMock, patch

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
            patch_path_override=Path("/tmp/embedded.diff"),
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
        assert params["patch_path_override"] == "/tmp/embedded.diff"

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
        assert restored.patch_path_override == Path("/tmp/embedded.diff")


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

    def test_patch_variant_test_roundtrip(self) -> None:
        from crsbench.benchmark_ci.jobs.flat import PatchVariantTestJob
        from crsbench.distributed.ci_jobs import _reconstruct_job, serialize_ci_job

        job = PatchVariantTestJob(
            benchmark_path=Path("/b"),
            benchmark_name="bench",
            cpv_id="cpv_0",
            patch_id="p0",
            harness="h",
            test_mode="RTS",
            pov_paths=[Path("/pov1"), Path("/pov2")],
            patch_path_override=Path("/tmp/embedded-variant.diff"),
            build_patch_job_id="bp1",
        )
        params = serialize_ci_job(job)
        restored = _reconstruct_job(params)
        assert type(restored).__name__ == "PatchVariantTestJob"
        assert len(restored.pov_paths) == 2
        assert restored.test_mode == "RTS"
        assert restored.patch_path_override == Path("/tmp/embedded-variant.diff")

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
            sanitizer="undefined",
        )
        params = serialize_ci_job(job)
        assert params["sanitizer"] == "undefined"
        restored = _reconstruct_job(params)
        assert type(restored).__name__ == "BuildPatchVariantJob"
        assert restored.patch_path == Path("/patch.diff")
        assert restored.sanitizer == "undefined"

    def test_build_patch_variant_default_sanitizer(self) -> None:
        """BuildPatchVariantJob defaults sanitizer and use_inc_build on deserialize."""
        from crsbench.distributed.ci_jobs import _reconstruct_job

        params = {
            "_job_class": "BuildPatchVariantJob",
            "benchmark_path": "/b",
            "benchmark_name": "bench",
            "cpv_id": "cpv_0",
            "patch_id": "p0",
            "patch_path": "/patch.diff",
            "harness": "h",
        }
        restored = _reconstruct_job(params)
        assert restored.sanitizer == "address"
        assert restored.use_inc_build is False

    def test_build_single_variant_roundtrip(self) -> None:
        """BuildSingleVariantJob serializes and reconstructs correctly."""
        from crsbench.benchmark_ci.jobs.flat import BuildSingleVariantJob
        from crsbench.builder.types import BenchmarkMode, VariantType
        from crsbench.distributed.ci_jobs import _reconstruct_job, serialize_ci_job

        job = BuildSingleVariantJob(
            benchmark_path=Path("/benchmarks/test-bench"),
            benchmark_name="test-bench",
            variant_type=VariantType.DELTA_REF,
            commit="abc123",
            main_repo="https://github.com/test/repo",
            mode=BenchmarkMode.DELTA,
            language="c",
            cpv_num=0,
            patch_id="patch_0",
            pov_id="cpv_0",
            patches=[Path("/patches/p1.diff")],
            use_inc_build=True,
            force_rebuild=False,
            skip_if_cached=True,
            source_mode="pkgs",
            sanitizer="address",
            repo_name="test-repo",
            project_image_prefix="aixcc-afc",
        )
        params = serialize_ci_job(job)

        assert params["_job_class"] == "BuildSingleVariantJob"
        assert params["variant_type"] == "deltaref"
        assert params["mode"] == "delta"
        assert params["benchmark_path"] == "/benchmarks/test-bench"
        assert params["patches"] == ["/patches/p1.diff"]
        assert params["patch_id"] == "patch_0"
        assert params["pov_id"] == "cpv_0"

        restored = _reconstruct_job(params)
        assert type(restored).__name__ == "BuildSingleVariantJob"
        assert restored.benchmark_name == "test-bench"
        assert restored.variant_type == VariantType.DELTA_REF
        assert restored.mode == BenchmarkMode.DELTA
        assert restored.commit == "abc123"
        assert restored.patches == [Path("/patches/p1.diff")]
        assert restored.sanitizer == "address"
        assert restored.repo_name == "test-repo"

    def test_build_single_variant_default_use_inc_build_false(self) -> None:
        """BuildSingleVariantJob defaults use_inc_build to False on deserialize."""
        from crsbench.distributed.ci_jobs import _reconstruct_job

        params = {
            "_job_class": "BuildSingleVariantJob",
            "benchmark_path": "/benchmarks/test-bench",
            "benchmark_name": "test-bench",
            "variant_type": "deltaref",
            "commit": "abc123",
            "main_repo": "https://github.com/test/repo",
            "mode": "delta",
        }
        restored = _reconstruct_job(params)
        assert restored.use_inc_build is False

    def test_patch_variant_test_default_test_mode_full(self) -> None:
        """PatchVariantTestJob defaults test_mode to FULL for legacy payloads."""
        from crsbench.distributed.ci_jobs import _reconstruct_job

        params = {
            "_job_class": "PatchVariantTestJob",
            "benchmark_path": "/b",
            "benchmark_name": "bench",
            "cpv_id": "cpv_0",
            "patch_id": "p0",
            "harness": "h",
            "pov_paths": ["/pov1"],
            "build_patch_job_id": "bp1",
            "source_mode": "pkgs",
        }
        restored = _reconstruct_job(params)
        assert restored.test_mode == "FULL"


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
        # job_result should be preserved even for failed results
        assert r.job_result is not None
        assert r.job_result.success is False

    def test_failed_result_preserves_details(self) -> None:
        """Failed job result preserves job_result details for aggregation."""
        from crsbench.distributed.ci_jobs import ci_results_to_executor_results

        raw = {
            "verify-cpv-var/bench/cpv_0": {
                "job_id": "verify-cpv-var/bench/cpv_0",
                "job_type": "verify",
                "success": False,
                "error": "26/29 variants passed",
                "elapsed_seconds": 2026.4,
                "started_at": "2026-02-13T14:26:10.414759",
                "finished_at": "2026-02-13T15:00:00.000000",
                "details": {
                    "var_passed": 26,
                    "var_total": 29,
                },
            }
        }
        results = ci_results_to_executor_results(raw)
        r = results["verify-cpv-var/bench/cpv_0"]
        assert r.status.value == "failed"
        assert r.elapsed_seconds == 2026.4
        assert r.job_result is not None
        assert r.job_result.details["var_passed"] == 26
        assert r.job_result.details["var_total"] == 29
        assert r.job_result.elapsed_seconds == 2026.4

    def test_empty_input_returns_empty(self) -> None:
        """Empty raw results produce empty output."""
        from crsbench.distributed.ci_jobs import ci_results_to_executor_results

        assert ci_results_to_executor_results({}) == {}

    def test_invalid_timestamp_falls_back_to_now(self) -> None:
        """Malformed timestamps should not crash conversion."""
        from crsbench.distributed.ci_jobs import ci_results_to_executor_results

        raw = {
            "verify-cpv:bench:cpv_0": {
                "job_id": "verify-cpv:bench:cpv_0",
                "job_type": "verify",
                "success": True,
                "started_at": "not-an-iso-time",
                "finished_at": "still-not-iso",
                "elapsed_seconds": 1.0,
            }
        }
        results = ci_results_to_executor_results(raw)
        r = results["verify-cpv:bench:cpv_0"]
        assert r.status.value == "success"
        assert r.job_result is not None


class TestReconstructUnknown:
    """Test _reconstruct_job with unknown class."""

    def test_unknown_class_raises(self) -> None:
        from crsbench.distributed.ci_jobs import _reconstruct_job

        with pytest.raises(ValueError, match="Unknown job class"):
            _reconstruct_job({"_job_class": "NonExistentJob"})


class TestRecoverOrphanedDeferredJobs:
    """Test _recover_orphaned_deferred_jobs() deferred-job recovery."""

    def _make_mock_rq_job(self, job_id, status, dependency_ids=None):
        """Create a mock RQ job with the given status and dependency_ids."""
        job = MagicMock()
        job.id = job_id
        job.get_status.return_value = status
        job.dependency_ids = dependency_ids or []
        job._dependency_ids = list(dependency_ids or [])
        job.dependencies_key = f"rq:job:{job_id}:dependencies"
        return job

    @staticmethod
    def _mock_registry_factory(mock_registry):
        """Return a callable that ignores args and returns mock_registry."""

        def factory(**_kwargs):
            return mock_registry

        return factory

    @staticmethod
    def _mock_fetch_returning(dep_job):
        """Return a Job.fetch mock that always returns dep_job."""

        def fetch(_dep_id, **_kwargs):
            return dep_job

        return fetch

    def test_no_deferred_jobs_returns_zero(self) -> None:
        """When no jobs are in the deferred registry, returns 0."""
        from crsbench.distributed.ci_jobs import _recover_orphaned_deferred_jobs

        queue = MagicMock()
        queue.connection = MagicMock()

        with pytest.MonkeyPatch.context() as mp:
            mock_registry = MagicMock()
            mock_registry.get_job_ids.return_value = []
            mp.setattr(
                "rq.registry.DeferredJobRegistry",
                self._mock_registry_factory(mock_registry),
            )
            result = _recover_orphaned_deferred_jobs(
                queue,
                {"job-1": self._make_mock_rq_job("job-1", "queued")},
                {"job-1"},
            )
        assert result == 0

    def test_deferred_job_not_in_pending_is_ignored(self) -> None:
        """Deferred jobs not in the pending set are skipped."""
        from crsbench.distributed.ci_jobs import _recover_orphaned_deferred_jobs

        queue = MagicMock()
        queue.connection = MagicMock()

        with pytest.MonkeyPatch.context() as mp:
            mock_registry = MagicMock()
            mock_registry.get_job_ids.return_value = ["other-job"]
            mp.setattr(
                "rq.registry.DeferredJobRegistry",
                self._mock_registry_factory(mock_registry),
            )
            result = _recover_orphaned_deferred_jobs(
                queue,
                {"job-1": self._make_mock_rq_job("job-1", "queued")},
                {"job-1"},
            )
        assert result == 0

    def test_recovers_orphaned_deferred_job(self) -> None:
        """A deferred job with all deps finished gets force-enqueued via pipeline."""
        from crsbench.distributed.ci_jobs import _recover_orphaned_deferred_jobs
        from rq.job import JobStatus

        queue = MagicMock()
        mock_pipe = MagicMock()
        queue.connection.pipeline.return_value = mock_pipe

        dep_job = self._make_mock_rq_job("build-1", JobStatus.FINISHED)
        orphan = self._make_mock_rq_job(
            "verify-1", JobStatus.DEFERRED, dependency_ids=["build-1"]
        )

        rq_jobs = {"verify-1": orphan}
        pending = {"verify-1"}

        with pytest.MonkeyPatch.context() as mp:
            mock_registry = MagicMock()
            mock_registry.get_job_ids.return_value = ["verify-1"]
            mp.setattr(
                "rq.registry.DeferredJobRegistry",
                self._mock_registry_factory(mock_registry),
            )
            mp.setattr(
                "rq.job.Job.fetch",
                self._mock_fetch_returning(dep_job),
            )

            result = _recover_orphaned_deferred_jobs(queue, rq_jobs, pending)

        assert result == 1
        # Verify pipeline-based atomic operations
        queue.connection.pipeline.assert_called_once()
        mock_registry.remove.assert_called_once_with(orphan, pipeline=mock_pipe)
        orphan.set_status.assert_called_once_with(JobStatus.QUEUED, pipeline=mock_pipe)
        orphan.save.assert_called_once_with(pipeline=mock_pipe)
        mock_pipe.delete.assert_called_once_with(orphan.dependencies_key)
        queue.push_job_id.assert_called_once_with("verify-1", pipeline=mock_pipe)
        mock_pipe.execute.assert_called_once()
        assert orphan._dependency_ids == []

    def test_skips_deferred_job_with_unfinished_deps(self) -> None:
        """A deferred job whose deps are not all finished is NOT recovered."""
        from crsbench.distributed.ci_jobs import _recover_orphaned_deferred_jobs
        from rq.job import JobStatus

        queue = MagicMock()
        queue.connection = MagicMock()

        dep_job = self._make_mock_rq_job("build-1", JobStatus.STARTED)
        orphan = self._make_mock_rq_job(
            "verify-1", JobStatus.DEFERRED, dependency_ids=["build-1"]
        )

        rq_jobs = {"verify-1": orphan}
        pending = {"verify-1"}

        with pytest.MonkeyPatch.context() as mp:
            mock_registry = MagicMock()
            mock_registry.get_job_ids.return_value = ["verify-1"]
            mp.setattr(
                "rq.registry.DeferredJobRegistry",
                self._mock_registry_factory(mock_registry),
            )
            mp.setattr(
                "rq.job.Job.fetch",
                self._mock_fetch_returning(dep_job),
            )

            result = _recover_orphaned_deferred_jobs(queue, rq_jobs, pending)

        assert result == 0
        mock_registry.remove.assert_not_called()
        queue.connection.pipeline.assert_not_called()

    def test_skips_job_no_longer_deferred(self) -> None:
        """A job in the deferred registry but already queued is skipped."""
        from crsbench.distributed.ci_jobs import _recover_orphaned_deferred_jobs
        from rq.job import JobStatus

        queue = MagicMock()
        queue.connection = MagicMock()

        # Job is in deferred registry but refresh shows it's already queued
        job = self._make_mock_rq_job(
            "verify-1", JobStatus.QUEUED, dependency_ids=["build-1"]
        )

        with pytest.MonkeyPatch.context() as mp:
            mock_registry = MagicMock()
            mock_registry.get_job_ids.return_value = ["verify-1"]
            mp.setattr(
                "rq.registry.DeferredJobRegistry",
                self._mock_registry_factory(mock_registry),
            )

            result = _recover_orphaned_deferred_jobs(
                queue, {"verify-1": job}, {"verify-1"}
            )

        assert result == 0
        mock_registry.remove.assert_not_called()

    def test_dep_fetch_nosuchjob_skips_recovery(self) -> None:
        """If a dependency is missing from Redis, the job is NOT recovered."""
        from crsbench.distributed.ci_jobs import _recover_orphaned_deferred_jobs
        from rq.exceptions import NoSuchJobError
        from rq.job import JobStatus

        queue = MagicMock()
        queue.connection = MagicMock()

        orphan = self._make_mock_rq_job(
            "verify-1", JobStatus.DEFERRED, dependency_ids=["build-gone"]
        )

        def fetch_raises(_dep_id, **_kwargs):
            raise NoSuchJobError("No such job")

        with pytest.MonkeyPatch.context() as mp:
            mock_registry = MagicMock()
            mock_registry.get_job_ids.return_value = ["verify-1"]
            mp.setattr(
                "rq.registry.DeferredJobRegistry",
                self._mock_registry_factory(mock_registry),
            )
            mp.setattr("rq.job.Job.fetch", fetch_raises)

            result = _recover_orphaned_deferred_jobs(
                queue, {"verify-1": orphan}, {"verify-1"}
            )

        assert result == 0
        queue.connection.pipeline.assert_not_called()

    def test_multiple_jobs_mixed_recovery(self) -> None:
        """Only the orphaned job with all deps finished is recovered."""
        from crsbench.distributed.ci_jobs import _recover_orphaned_deferred_jobs
        from rq.job import JobStatus

        queue = MagicMock()
        mock_pipe = MagicMock()
        queue.connection.pipeline.return_value = mock_pipe

        finished_dep = self._make_mock_rq_job("build-1", JobStatus.FINISHED)
        started_dep = self._make_mock_rq_job("build-2", JobStatus.STARTED)

        orphan_ready = self._make_mock_rq_job(
            "verify-1", JobStatus.DEFERRED, dependency_ids=["build-1"]
        )
        orphan_blocked = self._make_mock_rq_job(
            "verify-2", JobStatus.DEFERRED, dependency_ids=["build-2"]
        )

        rq_jobs = {"verify-1": orphan_ready, "verify-2": orphan_blocked}
        pending = {"verify-1", "verify-2"}

        def fetch_dep(dep_id, **_kwargs):
            return {"build-1": finished_dep, "build-2": started_dep}[dep_id]

        with pytest.MonkeyPatch.context() as mp:
            mock_registry = MagicMock()
            mock_registry.get_job_ids.return_value = ["verify-1", "verify-2"]
            mp.setattr(
                "rq.registry.DeferredJobRegistry",
                self._mock_registry_factory(mock_registry),
            )
            mp.setattr("rq.job.Job.fetch", fetch_dep)

            result = _recover_orphaned_deferred_jobs(queue, rq_jobs, pending)

        assert result == 1
        queue.push_job_id.assert_called_once_with("verify-1", pipeline=mock_pipe)


class TestLoadBuildContextMultiSanitizer:
    """Test _load_build_context_from_disk with multiple sanitizers."""

    @patch("crsbench.utils.run_helper.get_oss_fuzz_root", return_value="/oss-fuzz")
    @patch("crsbench.evaluation.verification.pov.VerificationEngine")
    def test_loads_all_sanitizer_variants(
        self, mock_engine_cls: MagicMock, mock_root: MagicMock
    ) -> None:
        """Multi-sanitizer benchmark loads build results for each sanitizer."""
        from crsbench.distributed.ci_jobs import _load_build_context_from_disk

        assert mock_root.return_value == "/oss-fuzz"

        # Set up mock adapter with two sanitizers (asan + ubsan)
        mock_adapter = MagicMock()
        mock_adapter.get_all_cpv_sanitizers.return_value = ["address", "undefined"]

        mock_engine = MagicMock()
        mock_engine_cls.return_value = mock_engine
        mock_engine.load_adapter.return_value = mock_adapter

        # Simulate per-sanitizer build results
        asan_results = {
            "bench-asan-deltabase": MagicMock(name="asan-deltabase"),
            "bench-asan-deltaref": MagicMock(name="asan-deltaref"),
        }
        ubsan_results = {
            "bench-ubsan-deltabase": MagicMock(name="ubsan-deltabase"),
            "bench-ubsan-deltaref": MagicMock(name="ubsan-deltaref"),
            "bench-ubsan-deltaref2": MagicMock(name="ubsan-deltaref2"),
        }

        def fake_get_or_build(_adapter, *, sanitizer=None):
            if sanitizer == "address":
                return asan_results
            if sanitizer == "undefined":
                return ubsan_results
            return {}

        mock_engine.get_or_build_results.side_effect = fake_get_or_build

        context = MagicMock()
        context.shared = {}

        build_job_ids = [
            "build-single/bench/bench-asan-deltabase",
            "build-single/bench/bench-asan-deltaref",
            "build-single/bench/bench-ubsan-deltabase",
            "build-single/bench/bench-ubsan-deltaref",
            "build-single/bench/bench-ubsan-deltaref2",
        ]

        _load_build_context_from_disk(
            context, build_job_ids, Path("/benchmarks/bench"), "pkgs"
        )

        # All 5 variants should be in context.shared
        assert len(context.shared) == 5

        # Verify asan variants got asan results
        assert (
            context.shared["build-single/bench/bench-asan-deltabase"]["build_result"]
            is asan_results["bench-asan-deltabase"]
        )
        # Verify ubsan variants got ubsan results
        assert (
            context.shared["build-single/bench/bench-ubsan-deltaref2"]["build_result"]
            is ubsan_results["bench-ubsan-deltaref2"]
        )

        # get_or_build_results called once per sanitizer
        assert mock_engine.get_or_build_results.call_count == 2
        mock_engine.get_or_build_results.assert_any_call(
            mock_adapter, sanitizer="address"
        )
        mock_engine.get_or_build_results.assert_any_call(
            mock_adapter, sanitizer="undefined"
        )

    @patch("crsbench.utils.run_helper.get_oss_fuzz_root", return_value="/oss-fuzz")
    @patch("crsbench.evaluation.verification.pov.VerificationEngine")
    def test_missing_variant_logs_warning_not_fallback(
        self, mock_engine_cls: MagicMock, mock_root: MagicMock
    ) -> None:
        """Missing variant logs warning instead of silently using wrong sanitizer."""
        from crsbench.distributed.ci_jobs import _load_build_context_from_disk

        assert mock_root.return_value == "/oss-fuzz"

        mock_adapter = MagicMock()
        mock_adapter.get_all_cpv_sanitizers.return_value = ["address"]

        mock_engine = MagicMock()
        mock_engine_cls.return_value = mock_engine
        mock_engine.load_adapter.return_value = mock_adapter
        mock_engine.get_or_build_results.return_value = {
            "bench-asan-deltabase": MagicMock(),
        }

        context = MagicMock()
        context.shared = {}

        # Request a variant that doesn't exist in build results
        build_job_ids = [
            "build-single/bench/bench-ubsan-deltaref",
        ]

        _load_build_context_from_disk(
            context, build_job_ids, Path("/benchmarks/bench"), "pkgs"
        )

        # Should NOT populate context.shared with wrong-sanitizer fallback
        assert len(context.shared) == 0
