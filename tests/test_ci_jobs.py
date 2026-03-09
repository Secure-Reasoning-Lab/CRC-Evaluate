"""Tests for CI verify/test job serialization and execution."""

from datetime import datetime, timedelta, timezone
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
        assert restored.use_inc_build is True

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
            inc_image_policy="pull_only",
            inc_image_registry="ghcr.io/example/custom",
            inc_image_max_pull_bytes=123456,
            inc_image_pull_timeout=77,
            local_image_prefix="custom-prefix",
        )
        params = serialize_ci_job(job)

        assert params["_job_class"] == "BuildSingleVariantJob"
        assert params["variant_type"] == "deltaref"
        assert params["mode"] == "delta"
        assert params["benchmark_path"] == "/benchmarks/test-bench"
        assert params["patches"] == ["/patches/p1.diff"]
        assert params["patch_id"] == "patch_0"
        assert params["pov_id"] == "cpv_0"
        assert params["inc_image_policy"] == "pull_only"
        assert params["inc_image_registry"] == "ghcr.io/example/custom"
        assert params["inc_image_max_pull_bytes"] == 123456
        assert params["inc_image_pull_timeout"] == 77
        assert params["local_image_prefix"] == "custom-prefix"

        restored = _reconstruct_job(params)
        assert type(restored).__name__ == "BuildSingleVariantJob"
        assert restored.benchmark_name == "test-bench"
        assert restored.variant_type == VariantType.DELTA_REF
        assert restored.mode == BenchmarkMode.DELTA
        assert restored.commit == "abc123"
        assert restored.patches == [Path("/patches/p1.diff")]
        assert restored.sanitizer == "address"
        assert restored.repo_name == "test-repo"
        assert restored.inc_image_policy == "pull_only"
        assert restored.inc_image_registry == "ghcr.io/example/custom"
        assert restored.inc_image_max_pull_bytes == 123456
        assert restored.inc_image_pull_timeout == 77
        assert restored.local_image_prefix == "custom-prefix"

    def test_build_single_variant_default_use_inc_build_false(self) -> None:
        """BuildSingleVariantJob defaults use_inc_build to True on deserialize."""
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
        assert restored.use_inc_build is True

    def test_prepare_inc_image_job_roundtrip(self) -> None:
        """PrepareIncImageJob serializes and reconstructs correctly."""
        from crsbench.benchmark_ci.jobs.flat import PrepareIncImageJob
        from crsbench.distributed.ci_jobs import _reconstruct_job, serialize_ci_job

        job = PrepareIncImageJob(
            benchmark_path=Path("/benchmarks/test-bench"),
            benchmark_name="test-bench",
            sanitizer="address",
            use_inc_build=True,
            source_mode="pkgs",
            inc_image_policy="pull_only",
            inc_image_registry="ghcr.io/example/custom",
            inc_image_max_pull_bytes=123456,
            inc_image_pull_timeout=77,
            local_image_prefix="custom-prefix",
        )
        params = serialize_ci_job(job)
        assert params["_job_class"] == "PrepareIncImageJob"
        assert params["benchmark_path"] == "/benchmarks/test-bench"
        assert params["benchmark_name"] == "test-bench"
        assert params["sanitizer"] == "address"
        assert params["use_inc_build"] is True
        assert params["force_rebuild"] is False
        assert params["inc_image_policy"] == "pull_only"
        assert params["inc_image_registry"] == "ghcr.io/example/custom"
        assert params["inc_image_max_pull_bytes"] == 123456
        assert params["inc_image_pull_timeout"] == 77
        assert params["local_image_prefix"] == "custom-prefix"

        restored = _reconstruct_job(params)
        assert type(restored).__name__ == "PrepareIncImageJob"
        assert restored.benchmark_name == "test-bench"
        assert restored.sanitizer == "address"
        assert restored.force_rebuild is False
        assert restored.inc_image_policy == "pull_only"
        assert restored.inc_image_registry == "ghcr.io/example/custom"
        assert restored.inc_image_max_pull_bytes == 123456
        assert restored.inc_image_pull_timeout == 77
        assert restored.local_image_prefix == "custom-prefix"

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


class TestExecuteCiJobContextPreload:
    """Test execute_ci_job context preloading behavior."""

    def test_skips_patch_context_when_already_loaded_from_build_ids(self) -> None:
        """Avoid duplicate patch context loads for the same build job id."""
        from crsbench.distributed.ci_jobs import execute_ci_job

        fake_result = MagicMock()
        fake_result.to_dict.return_value = {"success": True}

        fake_job = MagicMock()
        fake_job.build_job_ids = ["build-patch/bench/cpv_0/patch_0"]
        fake_job.build_patch_job_id = "build-patch/bench/cpv_0/patch_0"
        fake_job.benchmark_path = Path("/benchmarks/bench")
        fake_job.execute.return_value = fake_result

        def preload_shared(context, *_args, **_kwargs):
            context.shared["build-patch/bench/cpv_0/patch_0"] = {"variant_name": "v"}

        with (
            patch(
                "crsbench.distributed.ci_jobs._reconstruct_job", return_value=fake_job
            ),
            patch(
                "crsbench.distributed.ci_jobs._load_build_context_from_disk",
                side_effect=preload_shared,
            ),
            patch(
                "crsbench.distributed.ci_jobs._load_patch_build_context"
            ) as mock_patch_load,
        ):
            result = execute_ci_job({"_job_class": "PatchPovTestJob"})

        assert result == {"success": True}
        mock_patch_load.assert_not_called()
        fake_job.execute.assert_called_once()

    def test_loads_patch_context_when_missing_from_shared(self) -> None:
        """Patch context should still load when not preloaded by build ids."""
        from crsbench.distributed.ci_jobs import execute_ci_job

        fake_result = MagicMock()
        fake_result.to_dict.return_value = {"success": True}

        fake_job = MagicMock()
        fake_job.build_job_ids = []
        fake_job.build_patch_job_id = "build-patch/bench/cpv_0/patch_0"
        fake_job.execute.return_value = fake_result

        with (
            patch(
                "crsbench.distributed.ci_jobs._reconstruct_job", return_value=fake_job
            ),
            patch(
                "crsbench.distributed.ci_jobs._load_patch_build_context"
            ) as mock_patch_load,
        ):
            result = execute_ci_job({"_job_class": "PatchPovTestJob"})

        assert result == {"success": True}
        mock_patch_load.assert_called_once()
        fake_job.execute.assert_called_once()

    def test_returns_structured_error_on_missing_build_context(self) -> None:
        """execute_ci_job should return typed infra failure for missing context."""
        from crsbench.distributed.ci_jobs import (
            InfraMissingBuildContextError,
            execute_ci_job,
        )

        fake_job = MagicMock()
        fake_job.job_id = "verify-cpv-pov/bench/cpv_0"
        fake_job.build_job_ids = ["build-single/bench/bench-asan-deltaref"]
        fake_job.benchmark_path = Path("/benchmarks/bench")
        fake_job.use_inc_build = True
        fake_job.execute = MagicMock()

        with (
            patch(
                "crsbench.distributed.ci_jobs._reconstruct_job", return_value=fake_job
            ),
            patch(
                "crsbench.distributed.ci_jobs._load_build_context_from_disk",
                side_effect=InfraMissingBuildContextError(
                    benchmark="bench",
                    build_job_ids_requested=fake_job.build_job_ids,
                    missing_build_job_ids=fake_job.build_job_ids,
                    available_variants=[],
                    source_mode="pkgs",
                    use_inc_build=True,
                ),
            ),
        ):
            result = execute_ci_job(
                {"_job_class": "VerifyCpvPovJob", "source_mode": "pkgs"}
            )

        assert result["success"] is False
        assert result["job_id"] == "verify-cpv-pov/bench/cpv_0"
        assert "infra_missing_build_context" in result["error"]
        assert result["details"]["error_code"] == "infra_missing_build_context"
        fake_job.execute.assert_not_called()

    def test_patch_context_prefers_upstream_build_result_metadata(self) -> None:
        """Patch context should use actual upstream build outcome when available."""
        from crsbench.builder.types import BenchmarkMode
        from crsbench.distributed.ci_jobs import _load_patch_build_context

        context = MagicMock()
        context.shared = {}

        job = MagicMock()
        job.build_patch_job_id = "build-patch/bench/cpv_0/patch_0"
        job.benchmark_name = "bench"
        job.benchmark_path = Path("/bench")
        job.harness = "h0"
        job.cpv_id = "cpv_0"
        job.patch_id = "patch_0"
        job.use_inc_build = True

        adapter = MagicMock()
        adapter.get_cpv_sanitizer.return_value = "address"
        adapter.get_mode.return_value = BenchmarkMode.DELTA
        adapter.lang = "c"
        adapter.get_ref_commit.return_value = "abc123"
        adapter.get_base_commit.return_value = "abc123"
        adapter.main_repo = "https://example.com/repo"

        current_rq_job = MagicMock()
        current_rq_job.connection = MagicMock()
        upstream_rq_job = MagicMock()
        upstream_rq_job.result = {
            "details": {
                "variant_name": "bench-delta-cpv_0-patch_0",
                "sanitizer": "memory",
                "inc_build_available": False,
            }
        }

        with (
            patch(
                "crsbench.utils.run_helper.ensure_oss_fuzz_root", return_value="/tmp/of"
            ),
            patch(
                "crsbench.evaluation.verification.pov.VerificationEngine"
            ) as mock_engine_cls,
            patch(
                "crsbench.builder.infrastructure.OSSFuzzInfrastructure"
            ) as mock_infra_cls,
            patch("rq.get_current_job", return_value=current_rq_job),
            patch("rq.job.Job.fetch", return_value=upstream_rq_job),
        ):
            mock_engine = MagicMock()
            mock_engine.load_adapter.return_value = adapter
            mock_engine_cls.return_value = mock_engine
            mock_infra = MagicMock()
            mock_infra.is_variant_built.return_value = True
            mock_infra_cls.return_value = mock_infra
            _load_patch_build_context(context, job, source_mode="pkgs")

        stored = context.shared[job.build_patch_job_id]
        assert stored["variant_name"] == "bench-delta-cpv_0-patch_0"
        assert stored["sanitizer"] == "memory"
        assert stored["inc_build_available"] is False

    def test_patch_context_defaults_to_job_inc_build_without_upstream_result(
        self,
    ) -> None:
        """Missing upstream build result should fall back to job.use_inc_build."""
        from crsbench.builder.types import BenchmarkMode
        from crsbench.distributed.ci_jobs import _load_patch_build_context

        context = MagicMock()
        context.shared = {}

        job = MagicMock()
        job.build_patch_job_id = "build-patch/bench/cpv_0/patch_0"
        job.benchmark_name = "bench"
        job.benchmark_path = Path("/bench")
        job.harness = "h0"
        job.cpv_id = "cpv_0"
        job.patch_id = "patch_0"
        job.use_inc_build = True

        adapter = MagicMock()
        adapter.get_cpv_sanitizer.return_value = "address"
        adapter.get_mode.return_value = BenchmarkMode.DELTA
        adapter.lang = "c"
        adapter.get_ref_commit.return_value = "abc123"
        adapter.get_base_commit.return_value = "abc123"
        adapter.main_repo = "https://example.com/repo"

        with (
            patch(
                "crsbench.utils.run_helper.ensure_oss_fuzz_root", return_value="/tmp/of"
            ),
            patch(
                "crsbench.evaluation.verification.pov.VerificationEngine"
            ) as mock_engine_cls,
            patch(
                "crsbench.builder.infrastructure.OSSFuzzInfrastructure"
            ) as mock_infra_cls,
            patch("rq.get_current_job", return_value=None),
        ):
            mock_engine = MagicMock()
            mock_engine.load_adapter.return_value = adapter
            mock_engine_cls.return_value = mock_engine
            mock_infra = MagicMock()
            mock_infra.is_variant_built.return_value = True
            mock_infra_cls.return_value = mock_infra
            _load_patch_build_context(context, job, source_mode="pkgs")

        stored = context.shared[job.build_patch_job_id]
        assert stored["inc_build_available"] is True

    def test_patch_context_missing_artifact_fails_fast(self) -> None:
        """Patch context should fail when build metadata exists but artifact is absent."""
        from crsbench.builder.types import BenchmarkMode
        from crsbench.distributed.ci_jobs import (
            InfraMissingBuildContextError,
            _load_patch_build_context,
        )

        context = MagicMock()
        context.shared = {}

        job = MagicMock()
        job.build_patch_job_id = "build-patch/bench/cpv_0/patch_0"
        job.benchmark_name = "bench"
        job.benchmark_path = Path("/bench")
        job.harness = "h0"
        job.cpv_id = "cpv_0"
        job.patch_id = "patch_0"
        job.use_inc_build = True

        adapter = MagicMock()
        adapter.get_cpv_sanitizer.return_value = "address"
        adapter.get_mode.return_value = BenchmarkMode.DELTA
        adapter.lang = "c"
        adapter.get_ref_commit.return_value = "abc123"
        adapter.get_base_commit.return_value = "abc123"
        adapter.main_repo = "https://example.com/repo"

        with (
            patch(
                "crsbench.utils.run_helper.ensure_oss_fuzz_root", return_value="/tmp/of"
            ),
            patch(
                "crsbench.evaluation.verification.pov.VerificationEngine"
            ) as mock_engine_cls,
            patch(
                "crsbench.builder.infrastructure.OSSFuzzInfrastructure"
            ) as mock_infra_cls,
            patch("rq.get_current_job", return_value=None),
        ):
            mock_engine = MagicMock()
            mock_engine.load_adapter.return_value = adapter
            mock_engine_cls.return_value = mock_engine
            mock_infra = MagicMock()
            mock_infra.is_variant_built.return_value = False
            mock_infra_cls.return_value = mock_infra

            with pytest.raises(InfraMissingBuildContextError) as exc_info:
                _load_patch_build_context(context, job, source_mode="pkgs")

        err = exc_info.value
        assert err.error_code == "infra_missing_patch_build_context"


class TestBuildPatchVariantJobSanitizer:
    """Test sanitizer resolution in BuildPatchVariantJob."""

    def test_uses_adapter_cpv_sanitizer_over_payload(self, tmp_path: Path) -> None:
        """BuildPatchVariantJob should prefer adapter CPV sanitizer."""
        from crsbench.benchmark_ci.jobs.base import JobContext
        from crsbench.benchmark_ci.jobs.flat import BuildPatchVariantJob
        from crsbench.builder.types import BenchmarkMode
        from crsbench.evaluation.verification.models import PatchVerificationStatus

        patch_path = tmp_path / "patch.diff"
        patch_path.write_text("dummy")

        job = BuildPatchVariantJob(
            benchmark_path=Path("/bench"),
            benchmark_name="bench",
            cpv_id="cpv_0",
            patch_id="patch_0",
            patch_path=patch_path,
            harness="h0",
            sanitizer="address",
            use_inc_build=True,
            source_mode="pkgs",
        )

        context = JobContext(output_dir=tmp_path)

        adapter = MagicMock()
        adapter.get_cpv_sanitizer.return_value = "undefined"
        adapter.get_mode.return_value = BenchmarkMode.DELTA
        adapter.lang = "c"
        adapter.get_ref_commit.return_value = "abc123"
        adapter.get_base_commit.return_value = "abc123"
        adapter.main_repo = "https://example.com/repo"

        verify_result = MagicMock()
        verify_result.status = PatchVerificationStatus.VALID
        verify_result.fallback_used = False
        verify_result.inc_build_available = True
        verify_result.build_time = 0.1
        verify_result.build_stdout = ""
        verify_result.build_stderr = ""
        verify_result.details = ""

        with (
            patch(
                "crsbench.utils.run_helper.ensure_oss_fuzz_root",
                return_value="/tmp/of",
            ),
            patch(
                "crsbench.evaluation.verification.pov.VerificationEngine"
            ) as mock_pov_engine_cls,
            patch(
                "crsbench.evaluation.verification.patch.PatchVerificationEngine"
            ) as mock_patch_engine_cls,
        ):
            mock_pov_engine = MagicMock()
            mock_pov_engine.load_adapter.return_value = adapter
            mock_pov_engine_cls.return_value = mock_pov_engine

            mock_patch_engine = MagicMock()
            mock_patch_engine.verify_patch.return_value = verify_result
            mock_patch_engine_cls.return_value = mock_patch_engine

            result = job.execute(context)

        assert result.success is True
        assert mock_patch_engine_cls.call_args.kwargs["sanitizer"] == "undefined"
        assert (
            mock_patch_engine.verify_patch.call_args.kwargs.get("allow_build") is True
        )
        stored = context.shared[job.job_id]
        assert stored["sanitizer"] == "undefined"
        assert "-ubsan-" in stored["variant_name"]


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

        def fake_get_or_build(
            _adapter, *, sanitizer=None, use_inc_build=True, allow_build=True
        ):
            _ = use_inc_build
            _ = allow_build
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
            context,
            build_job_ids,
            Path("/benchmarks/bench"),
            "pkgs",
            use_inc_build=True,
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
            mock_adapter, sanitizer="address", use_inc_build=True, allow_build=False
        )
        mock_engine.get_or_build_results.assert_any_call(
            mock_adapter, sanitizer="undefined", use_inc_build=True, allow_build=False
        )

    @patch("crsbench.utils.run_helper.get_oss_fuzz_root", return_value="/oss-fuzz")
    @patch("crsbench.evaluation.verification.pov.VerificationEngine")
    def test_missing_variant_logs_warning_not_fallback(
        self, mock_engine_cls: MagicMock, mock_root: MagicMock
    ) -> None:
        """Missing variant should fail fast with infra_missing_build_context."""
        from crsbench.distributed.ci_jobs import (
            InfraMissingBuildContextError,
            _load_build_context_from_disk,
        )

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

        with pytest.raises(InfraMissingBuildContextError) as exc_info:
            _load_build_context_from_disk(
                context,
                build_job_ids,
                Path("/benchmarks/bench"),
                "pkgs",
                use_inc_build=True,
            )

        err = exc_info.value
        assert err.error_code == "infra_missing_build_context"
        assert err.missing_build_job_ids == build_job_ids
        # Should NOT populate context.shared with wrong-sanitizer fallback
        assert len(context.shared) == 0

    @patch("crsbench.utils.run_helper.get_oss_fuzz_root", return_value="/oss-fuzz")
    @patch("crsbench.evaluation.verification.pov.VerificationEngine")
    def test_fallback_load_accepts_non_inc_variants(
        self, mock_engine_cls: MagicMock, mock_root: MagicMock
    ) -> None:
        """When strict inc load misses variants, retry with non-inc contexts."""
        from crsbench.distributed.ci_jobs import _load_build_context_from_disk

        assert mock_root.return_value == "/oss-fuzz"

        mock_adapter = MagicMock()
        mock_adapter.get_all_cpv_sanitizers.return_value = ["address"]

        mock_engine = MagicMock()
        mock_engine_cls.return_value = mock_engine
        mock_engine.load_adapter.return_value = mock_adapter

        # Strict inc load misses required variant; fallback load finds it.
        def fake_get_or_build(
            _adapter, *, sanitizer=None, use_inc_build=True, allow_build=True
        ):
            _ = sanitizer
            _ = allow_build
            if use_inc_build:
                return {}
            return {"bench-asan-deltaref": MagicMock(name="fallback-deltaref")}

        mock_engine.get_or_build_results.side_effect = fake_get_or_build

        context = MagicMock()
        context.shared = {}

        build_job_ids = ["build-single/bench/bench-asan-deltaref"]
        _load_build_context_from_disk(
            context,
            build_job_ids,
            Path("/benchmarks/bench"),
            "pkgs",
            use_inc_build=True,
        )

        assert "build-single/bench/bench-asan-deltaref" in context.shared
        assert mock_engine.get_or_build_results.call_count == 2
        mock_engine.get_or_build_results.assert_any_call(
            mock_adapter, sanitizer="address", use_inc_build=True, allow_build=False
        )
        mock_engine.get_or_build_results.assert_any_call(
            mock_adapter, sanitizer="address", use_inc_build=False, allow_build=False
        )


class TestEnqueueAndPollCiJobs:
    """Test enqueue_and_poll_ci_jobs stale/duplicate behavior."""

    @staticmethod
    def _make_build_job():
        from crsbench.benchmark_ci.jobs.flat import BuildSingleVariantJob
        from crsbench.builder.types import BenchmarkMode, VariantType

        return BuildSingleVariantJob(
            benchmark_path=Path("/bench"),
            benchmark_name="bench",
            variant_type=VariantType.DELTA_REF,
            commit="abc123",
            main_repo="https://example.com/repo.git",
            mode=BenchmarkMode.DELTA,
        )

    @staticmethod
    def _make_mock_rq_job(job_id: str, status: str, result: dict | None = None):
        job = MagicMock()
        job.id = job_id
        job._status = status
        job.result = result
        job.exc_info = None
        job.started_at = None
        job.timeout = 3600
        job.delete_called = False
        job.get_status.side_effect = lambda: job._status
        job.refresh.return_value = None

        def _delete():
            job.delete_called = True

        job.delete.side_effect = _delete
        return job

    def test_rebuilds_finished_build_job(self, monkeypatch: pytest.MonkeyPatch):
        """Finished build IDs are refreshed to guarantee fresh artifacts."""
        from crsbench.distributed.ci_jobs import enqueue_and_poll_ci_jobs

        existing = self._make_mock_rq_job(
            "build-single/bench/bench-asan-deltaref", "finished"
        )
        reenqueued = self._make_mock_rq_job(
            "build-single/bench/bench-asan-deltaref",
            "finished",
            {
                "job_id": "build-single/bench/bench-asan-deltaref",
                "job_type": "build",
                "success": True,
                "elapsed_seconds": 1.0,
                "details": {},
                "started_at": None,
                "finished_at": None,
                "error": None,
            },
        )

        queue = MagicMock()
        queue.name = "crsbench_ci_build"
        queue.connection = MagicMock()
        queue.enqueue.side_effect = [RuntimeError("duplicate"), reenqueued]

        monkeypatch.setattr(
            "crsbench.distributed.queue.create_redis_connection",
            lambda _host: MagicMock(),
        )
        monkeypatch.setattr("rq.Queue", lambda *_args, **_kwargs: queue)
        monkeypatch.setattr(
            "rq.job.Job.fetch",
            lambda _job_id, **_kwargs: existing,
        )
        monkeypatch.setattr(
            "rq.job.Job.fetch_many",
            lambda pending_ids, **_kwargs: [reenqueued for _ in pending_ids],
        )

        job = self._make_build_job()

        results = enqueue_and_poll_ci_jobs([job], redis_host="localhost")

        assert queue.enqueue.call_count == 2
        assert existing.delete_called is True
        assert job.job_id in results
        assert results[job.job_id]["success"] is True

    def test_refresh_all_deletes_failed_terminal_before_enqueue(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """refresh_all policy should delete failed stale records before enqueue."""
        from crsbench.distributed.ci_jobs import enqueue_and_poll_ci_jobs

        existing = self._make_mock_rq_job(
            "build-single/bench/bench-asan-deltaref", "failed"
        )
        enqueued = self._make_mock_rq_job(
            "build-single/bench/bench-asan-deltaref",
            "finished",
            {
                "job_id": "build-single/bench/bench-asan-deltaref",
                "job_type": "build",
                "success": True,
                "elapsed_seconds": 1.0,
                "details": {},
                "started_at": None,
                "finished_at": None,
                "error": None,
            },
        )

        queue = MagicMock()
        queue.name = "crsbench_ci_build"
        queue.connection = MagicMock()
        queue.enqueue.return_value = enqueued

        monkeypatch.setattr(
            "crsbench.distributed.queue.create_redis_connection",
            lambda _host: MagicMock(),
        )
        monkeypatch.setattr("rq.Queue", lambda *_args, **_kwargs: queue)
        monkeypatch.setattr(
            "rq.job.Job.fetch",
            lambda _job_id, **_kwargs: existing,
        )
        monkeypatch.setattr(
            "rq.job.Job.fetch_many",
            lambda pending_ids, **_kwargs: [enqueued for _ in pending_ids],
        )

        job = self._make_build_job()
        results = enqueue_and_poll_ci_jobs(
            [job],
            redis_host="localhost",
            stale_terminal_policy="refresh_all",
        )

        assert existing.delete_called is True
        assert queue.enqueue.call_count == 1
        assert job.job_id in results
        assert results[job.job_id]["success"] is True

    def test_refresh_all_deletes_finished_terminal_before_enqueue(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """refresh_all should also refresh finished jobs."""
        from crsbench.distributed.ci_jobs import enqueue_and_poll_ci_jobs

        existing = self._make_mock_rq_job(
            "build-single/bench/bench-asan-deltaref", "finished"
        )
        enqueued = self._make_mock_rq_job(
            "build-single/bench/bench-asan-deltaref",
            "finished",
            {
                "job_id": "build-single/bench/bench-asan-deltaref",
                "job_type": "build",
                "success": True,
                "elapsed_seconds": 1.0,
                "details": {},
                "started_at": None,
                "finished_at": None,
                "error": None,
            },
        )

        queue = MagicMock()
        queue.name = "crsbench_ci_build"
        queue.connection = MagicMock()
        queue.enqueue.return_value = enqueued

        monkeypatch.setattr(
            "crsbench.distributed.queue.create_redis_connection",
            lambda _host: MagicMock(),
        )
        monkeypatch.setattr("rq.Queue", lambda *_args, **_kwargs: queue)
        monkeypatch.setattr(
            "rq.job.Job.fetch",
            lambda _job_id, **_kwargs: existing,
        )
        monkeypatch.setattr(
            "rq.job.Job.fetch_many",
            lambda pending_ids, **_kwargs: [enqueued for _ in pending_ids],
        )

        job = self._make_build_job()
        results = enqueue_and_poll_ci_jobs(
            [job],
            redis_host="localhost",
            stale_terminal_policy="refresh_all",
        )

        assert existing.delete_called is True
        assert queue.enqueue.call_count == 1
        assert job.job_id in results
        assert results[job.job_id]["success"] is True

    def test_default_policy_refreshes_finished_verify_job(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Default stale policy should refresh finished non-build jobs."""
        from crsbench.distributed.ci_jobs import enqueue_and_poll_ci_jobs

        existing = self._make_mock_rq_job("verify-cpv-pov/bench/cpv_0", "finished")
        existing.result = {
            "job_id": "verify-cpv-pov/bench/cpv_0",
            "job_type": "verify",
            "success": True,
            "elapsed_seconds": 1.0,
            "details": {},
            "started_at": None,
            "finished_at": None,
            "error": None,
        }
        enqueued = self._make_mock_rq_job("verify-cpv-pov/bench/cpv_0", "finished")
        enqueued.result = {
            "job_id": "verify-cpv-pov/bench/cpv_0",
            "job_type": "verify",
            "success": True,
            "elapsed_seconds": 2.0,
            "details": {"rerun": True},
            "started_at": None,
            "finished_at": None,
            "error": None,
        }

        queue = MagicMock()
        queue.name = "crsbench_ci_verify"
        queue.connection = MagicMock()
        queue.enqueue.return_value = enqueued

        monkeypatch.setattr(
            "crsbench.distributed.queue.create_redis_connection",
            lambda _host: MagicMock(),
        )
        monkeypatch.setattr("rq.Queue", lambda *_args, **_kwargs: queue)
        monkeypatch.setattr(
            "rq.job.Job.fetch",
            lambda _job_id, **_kwargs: existing,
        )
        monkeypatch.setattr(
            "rq.job.Job.fetch_many",
            lambda pending_ids, **_kwargs: [enqueued for _ in pending_ids],
        )

        job = MagicMock()
        job.job_type = "verify"
        job.job_id = "verify-cpv-pov/bench/cpv_0"
        job.depends_on = []
        monkeypatch.setattr(
            "crsbench.distributed.ci_jobs.serialize_ci_job", lambda _j: {}
        )

        results = enqueue_and_poll_ci_jobs([job], redis_host="localhost")

        assert existing.delete_called is True
        assert queue.enqueue.call_count == 1
        assert results[job.job_id]["details"] == {"rerun": True}

    def test_explicit_policy_can_reuse_finished_verify_job(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Explicit refresh_stopped_canceled_failed should still reuse finished."""
        from crsbench.distributed.ci_jobs import enqueue_and_poll_ci_jobs

        existing = self._make_mock_rq_job("verify-cpv-pov/bench/cpv_0", "finished")
        existing.result = {
            "job_id": "verify-cpv-pov/bench/cpv_0",
            "job_type": "verify",
            "success": True,
            "elapsed_seconds": 1.0,
            "details": {"reused": True},
            "started_at": None,
            "finished_at": None,
            "error": None,
        }

        queue = MagicMock()
        queue.name = "crsbench_ci_verify"
        queue.connection = MagicMock()

        monkeypatch.setattr(
            "crsbench.distributed.queue.create_redis_connection",
            lambda _host: MagicMock(),
        )
        monkeypatch.setattr("rq.Queue", lambda *_args, **_kwargs: queue)
        monkeypatch.setattr(
            "rq.job.Job.fetch",
            lambda _job_id, **_kwargs: existing,
        )
        monkeypatch.setattr(
            "rq.job.Job.fetch_many",
            lambda pending_ids, **_kwargs: [existing for _ in pending_ids],
        )

        job = MagicMock()
        job.job_type = "verify"
        job.job_id = "verify-cpv-pov/bench/cpv_0"
        job.depends_on = []
        monkeypatch.setattr(
            "crsbench.distributed.ci_jobs.serialize_ci_job", lambda _j: {}
        )

        results = enqueue_and_poll_ci_jobs(
            [job],
            redis_host="localhost",
            stale_terminal_policy="refresh_stopped_canceled_failed",
        )

        assert existing.delete_called is False
        assert queue.enqueue.call_count == 0
        assert results[job.job_id]["details"] == {"reused": True}

    def test_raises_on_unknown_dependency(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Dependencies must be strict; unknown IDs cannot be silently ignored."""
        from crsbench.distributed.ci_jobs import enqueue_and_poll_ci_jobs

        queue = MagicMock()
        queue.name = "crsbench_ci_build"
        queue.connection = MagicMock()

        monkeypatch.setattr(
            "crsbench.distributed.queue.create_redis_connection",
            lambda _host: MagicMock(),
        )
        monkeypatch.setattr("rq.Queue", lambda *_args, **_kwargs: queue)

        job = MagicMock()
        job.job_type = "verify"
        job.job_id = "verify-cpv-pov/bench/cpv_0"
        job.depends_on = ["non-existent-build-job-id"]
        monkeypatch.setattr(
            "crsbench.distributed.ci_jobs.serialize_ci_job", lambda _j: {}
        )

        with pytest.raises(ValueError, match="unknown dependencies"):
            enqueue_and_poll_ci_jobs([job], redis_host="localhost")

        queue.enqueue.assert_not_called()

    def test_raises_on_dependency_ordering_violation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Known dependency IDs declared later should fail as ordering errors."""
        from crsbench.distributed.ci_jobs import enqueue_and_poll_ci_jobs

        queue = MagicMock()
        queue.name = "crsbench_ci_build"
        queue.connection = MagicMock()

        monkeypatch.setattr(
            "crsbench.distributed.queue.create_redis_connection",
            lambda _host: MagicMock(),
        )
        monkeypatch.setattr("rq.Queue", lambda *_args, **_kwargs: queue)
        monkeypatch.setattr(
            "crsbench.distributed.ci_jobs.serialize_ci_job", lambda _j: {}
        )

        verify_job = MagicMock()
        verify_job.job_type = "verify"
        verify_job.job_id = "a-verify-cpv-pov/bench/cpv_0"
        verify_job.depends_on = ["z-verify-cpv-pov/bench/cpv_1"]

        later_verify_job = MagicMock()
        later_verify_job.job_type = "verify"
        later_verify_job.job_id = "z-verify-cpv-pov/bench/cpv_1"
        later_verify_job.depends_on = []

        with pytest.raises(ValueError, match="invalid DAG ordering"):
            enqueue_and_poll_ci_jobs(
                [verify_job, later_verify_job], redis_host="localhost"
            )

        queue.enqueue.assert_not_called()

    def test_reuses_active_duplicate_job(self, monkeypatch: pytest.MonkeyPatch):
        """Active started duplicate IDs should be reused instead of failing."""
        from crsbench.distributed.ci_jobs import enqueue_and_poll_ci_jobs

        existing = self._make_mock_rq_job(
            "build-single/bench/bench-asan-deltaref", "started"
        )
        existing.result = {
            "job_id": "build-single/bench/bench-asan-deltaref",
            "job_type": "build",
            "success": True,
            "elapsed_seconds": 1.0,
            "details": {},
            "started_at": None,
            "finished_at": None,
            "error": None,
        }

        queue = MagicMock()
        queue.name = "crsbench_ci_build"
        queue.connection = MagicMock()
        queue.enqueue.return_value = existing

        monkeypatch.setattr(
            "crsbench.distributed.queue.create_redis_connection",
            lambda _host: MagicMock(),
        )
        monkeypatch.setattr("rq.Queue", lambda *_args, **_kwargs: queue)
        monkeypatch.setattr(
            "rq.job.Job.fetch",
            lambda _job_id, **_kwargs: existing,
        )
        poll_calls = {"count": 0}

        def _fetch_many(pending_ids, **_kwargs):
            poll_calls["count"] += 1
            if poll_calls["count"] >= 1:
                existing._status = "finished"
            return [existing for _ in pending_ids]

        monkeypatch.setattr("rq.job.Job.fetch_many", _fetch_many)

        job = self._make_build_job()
        results = enqueue_and_poll_ci_jobs([job], redis_host="localhost")

        assert queue.enqueue.call_count == 0
        assert job.job_id in results
        assert results[job.job_id]["success"] is True

    def test_refreshes_stale_started_duplicate_job(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Stale STARTED duplicate should be deleted/re-enqueued, not reused."""
        from crsbench.distributed.ci_jobs import enqueue_and_poll_ci_jobs

        existing = self._make_mock_rq_job(
            "build-single/bench/bench-asan-deltaref", "started"
        )
        existing.started_at = datetime.now(timezone.utc) - timedelta(seconds=5000)
        existing.timeout = 300
        fresh = self._make_mock_rq_job(
            "build-single/bench/bench-asan-deltaref",
            "finished",
            {
                "job_id": "build-single/bench/bench-asan-deltaref",
                "job_type": "build",
                "success": True,
                "elapsed_seconds": 1.0,
                "details": {},
                "started_at": None,
                "finished_at": None,
                "error": None,
            },
        )

        queue = MagicMock()
        queue.name = "crsbench_ci_build"
        queue.connection = MagicMock()
        queue.enqueue.return_value = fresh

        monkeypatch.setattr(
            "crsbench.distributed.queue.create_redis_connection",
            lambda _host: MagicMock(),
        )
        monkeypatch.setattr("rq.Queue", lambda *_args, **_kwargs: queue)
        monkeypatch.setattr("rq.job.Job.fetch", lambda _job_id, **_kwargs: existing)
        monkeypatch.setattr(
            "rq.job.Job.fetch_many",
            lambda pending_ids, **_kwargs: [fresh for _ in pending_ids],
        )

        job = self._make_build_job()
        results = enqueue_and_poll_ci_jobs([job], redis_host="localhost")

        assert existing.delete_called is True
        assert queue.enqueue.call_count == 1
        assert results[job.job_id]["success"] is True

    def test_duplicate_fallback_reuses_finished_non_build_for_refresh_stopped_failed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Duplicate fallback should reuse finished verify job for non-refresh policy."""
        from crsbench.distributed.ci_jobs import enqueue_and_poll_ci_jobs

        existing = self._make_mock_rq_job("verify-cpv-pov/bench/cpv_0", "finished")
        existing.result = {
            "job_id": "verify-cpv-pov/bench/cpv_0",
            "job_type": "verify",
            "success": True,
            "elapsed_seconds": 1.0,
            "details": {"reused": True},
            "started_at": None,
            "finished_at": None,
            "error": None,
        }

        queue = MagicMock()
        queue.name = "crsbench_ci_verify"
        queue.connection = MagicMock()
        queue.enqueue.side_effect = RuntimeError("duplicate")

        fetch_calls = {"count": 0}

        def _fetch(_job_id, **_kwargs):
            fetch_calls["count"] += 1
            if fetch_calls["count"] == 1:
                raise RuntimeError("not found during prescan")
            return existing

        monkeypatch.setattr(
            "crsbench.distributed.queue.create_redis_connection",
            lambda _host: MagicMock(),
        )
        monkeypatch.setattr("rq.Queue", lambda *_args, **_kwargs: queue)
        monkeypatch.setattr("rq.job.Job.fetch", _fetch)
        monkeypatch.setattr(
            "rq.job.Job.fetch_many",
            lambda pending_ids, **_kwargs: [existing for _ in pending_ids],
        )
        monkeypatch.setattr(
            "crsbench.distributed.ci_jobs.serialize_ci_job", lambda _j: {}
        )

        job = MagicMock()
        job.job_type = "verify"
        job.job_id = "verify-cpv-pov/bench/cpv_0"
        job.depends_on = []

        results = enqueue_and_poll_ci_jobs(
            [job],
            redis_host="localhost",
            stale_terminal_policy="refresh_stopped_canceled_failed",
        )

        assert queue.enqueue.call_count == 1
        assert existing.delete_called is False
        assert results[job.job_id]["details"] == {"reused": True}

    def test_duplicate_fallback_refresh_failed_deletes_failed_and_reenqueues(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Duplicate fallback should refresh failed terminal jobs for refresh_failed."""
        from crsbench.distributed.ci_jobs import enqueue_and_poll_ci_jobs

        existing = self._make_mock_rq_job("verify-cpv-pov/bench/cpv_0", "failed")
        existing.result = {
            "job_id": "verify-cpv-pov/bench/cpv_0",
            "job_type": "verify",
            "success": False,
            "elapsed_seconds": 1.0,
            "details": {"stale": True},
            "started_at": None,
            "finished_at": None,
            "error": "old-error",
        }
        reenqueued = self._make_mock_rq_job("verify-cpv-pov/bench/cpv_0", "finished")
        reenqueued.result = {
            "job_id": "verify-cpv-pov/bench/cpv_0",
            "job_type": "verify",
            "success": True,
            "elapsed_seconds": 2.0,
            "details": {"rerun": True},
            "started_at": None,
            "finished_at": None,
            "error": None,
        }

        queue = MagicMock()
        queue.name = "crsbench_ci_verify"
        queue.connection = MagicMock()
        queue.enqueue.side_effect = [RuntimeError("duplicate"), reenqueued]

        fetch_calls = {"count": 0}

        def _fetch(_job_id, **_kwargs):
            fetch_calls["count"] += 1
            if fetch_calls["count"] == 1:
                raise RuntimeError("not found during prescan")
            return existing

        monkeypatch.setattr(
            "crsbench.distributed.queue.create_redis_connection",
            lambda _host: MagicMock(),
        )
        monkeypatch.setattr("rq.Queue", lambda *_args, **_kwargs: queue)
        monkeypatch.setattr("rq.job.Job.fetch", _fetch)
        monkeypatch.setattr(
            "rq.job.Job.fetch_many",
            lambda pending_ids, **_kwargs: [reenqueued for _ in pending_ids],
        )
        monkeypatch.setattr(
            "crsbench.distributed.ci_jobs.serialize_ci_job", lambda _j: {}
        )

        job = MagicMock()
        job.job_type = "verify"
        job.job_id = "verify-cpv-pov/bench/cpv_0"
        job.depends_on = []

        results = enqueue_and_poll_ci_jobs(
            [job],
            redis_host="localhost",
            stale_terminal_policy="refresh_failed",
        )

        assert queue.enqueue.call_count == 2
        assert existing.delete_called is True
        assert results[job.job_id]["details"] == {"rerun": True}

    def test_duplicate_fallback_quit_policy_raises_for_terminal_duplicate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Duplicate fallback should honor quit policy for terminal duplicates."""
        from crsbench.distributed.ci_jobs import enqueue_and_poll_ci_jobs

        existing = self._make_mock_rq_job("verify-cpv-pov/bench/cpv_0", "failed")

        queue = MagicMock()
        queue.name = "crsbench_ci_verify"
        queue.connection = MagicMock()
        queue.enqueue.side_effect = RuntimeError("duplicate")

        fetch_calls = {"count": 0}

        def _fetch(_job_id, **_kwargs):
            fetch_calls["count"] += 1
            if fetch_calls["count"] == 1:
                raise RuntimeError("not found during prescan")
            return existing

        monkeypatch.setattr(
            "crsbench.distributed.queue.create_redis_connection",
            lambda _host: MagicMock(),
        )
        monkeypatch.setattr("rq.Queue", lambda *_args, **_kwargs: queue)
        monkeypatch.setattr("rq.job.Job.fetch", _fetch)
        monkeypatch.setattr(
            "crsbench.distributed.ci_jobs.serialize_ci_job", lambda _j: {}
        )

        job = MagicMock()
        job.job_type = "verify"
        job.job_id = "verify-cpv-pov/bench/cpv_0"
        job.depends_on = []

        with pytest.raises(RuntimeError, match="quitting per selected policy"):
            enqueue_and_poll_ci_jobs(
                [job],
                redis_host="localhost",
                stale_terminal_policy="quit",
            )

        assert existing.delete_called is False
        assert queue.enqueue.call_count == 1

    @pytest.mark.parametrize("terminal_status", ["stopped", "canceled"])
    def test_poll_drains_terminal_statuses(
        self, monkeypatch: pytest.MonkeyPatch, terminal_status: str
    ) -> None:
        """Polling loop should treat stopped/canceled as terminal states."""
        from crsbench.distributed.ci_jobs import enqueue_and_poll_ci_jobs

        queue = MagicMock()
        queue.name = "crsbench_ci_build"
        queue.connection = MagicMock()

        enqueued = self._make_mock_rq_job(
            "build-single/bench/bench-asan-deltaref", "queued"
        )
        enqueued._status = terminal_status
        queue.enqueue.return_value = enqueued

        monkeypatch.setattr(
            "crsbench.distributed.queue.create_redis_connection",
            lambda _host: MagicMock(),
        )
        monkeypatch.setattr("rq.Queue", lambda *_args, **_kwargs: queue)
        monkeypatch.setattr(
            "rq.job.Job.fetch_many",
            lambda pending_ids, **_kwargs: [enqueued for _ in pending_ids],
        )

        job = self._make_build_job()
        results = enqueue_and_poll_ci_jobs([job], redis_host="localhost")

        assert results[job.job_id]["success"] is False
        assert "Unknown error" in results[job.job_id]["error"]

    def test_poll_handles_enum_like_finished_status(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Polling should treat enum-like FINISHED statuses as terminal."""
        from crsbench.distributed.ci_jobs import enqueue_and_poll_ci_jobs

        class _Status:
            def __init__(self, value: str) -> None:
                self.value = value

            def __str__(self) -> str:
                return f"JobStatus.{self.value.upper()}"

        queue = MagicMock()
        queue.name = "crsbench_ci_build"
        queue.connection = MagicMock()

        enqueued = self._make_mock_rq_job(
            "build-single/bench/bench-asan-deltaref", "queued"
        )
        enqueued.get_status.side_effect = lambda: _Status("finished")
        enqueued.result = {
            "job_id": enqueued.id,
            "job_type": "build",
            "success": True,
            "elapsed_seconds": 1.0,
            "details": {},
            "started_at": None,
            "finished_at": None,
            "error": None,
        }
        queue.enqueue.return_value = enqueued

        monkeypatch.setattr(
            "crsbench.distributed.queue.create_redis_connection",
            lambda _host: MagicMock(),
        )
        monkeypatch.setattr("rq.Queue", lambda *_args, **_kwargs: queue)
        monkeypatch.setattr(
            "rq.job.Job.fetch_many",
            lambda pending_ids, **_kwargs: [enqueued for _ in pending_ids],
        )

        job = self._make_build_job()
        results = enqueue_and_poll_ci_jobs([job], redis_host="localhost")

        assert results[job.job_id]["success"] is True

    def test_poll_missing_job_is_terminal_infra_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """fetch_many None should be treated as terminal infra failure."""
        from crsbench.distributed.ci_jobs import enqueue_and_poll_ci_jobs

        queue = MagicMock()
        queue.name = "crsbench_ci_build"
        queue.connection = MagicMock()

        enqueued = self._make_mock_rq_job(
            "build-single/bench/bench-asan-deltaref", "queued"
        )
        queue.enqueue.return_value = enqueued

        monkeypatch.setattr(
            "crsbench.distributed.queue.create_redis_connection",
            lambda _host: MagicMock(),
        )
        monkeypatch.setattr("rq.Queue", lambda *_args, **_kwargs: queue)
        monkeypatch.setattr(
            "rq.job.Job.fetch_many",
            lambda pending_ids, **_kwargs: [None for _ in pending_ids],
        )

        job = self._make_build_job()
        results = enqueue_and_poll_ci_jobs([job], redis_host="localhost")

        assert results[job.job_id]["success"] is False
        assert "infra_missing_rq_job" in results[job.job_id]["error"]
        assert results[job.job_id]["details"]["error_code"] == "infra_missing_rq_job"

    def test_poll_stale_started_job_is_terminal_infra_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Stale STARTED jobs should not block polling forever."""
        from crsbench.distributed.ci_jobs import enqueue_and_poll_ci_jobs

        queue = MagicMock()
        queue.name = "crsbench_ci_build"
        queue.connection = MagicMock()

        enqueued = self._make_mock_rq_job(
            "build-single/bench/bench-asan-deltaref", "started"
        )
        enqueued.started_at = datetime.now(timezone.utc) - timedelta(seconds=4000)
        enqueued.timeout = 300
        queue.enqueue.return_value = enqueued

        monkeypatch.setattr(
            "crsbench.distributed.queue.create_redis_connection",
            lambda _host: MagicMock(),
        )
        monkeypatch.setattr("rq.Queue", lambda *_args, **_kwargs: queue)
        monkeypatch.setattr(
            "rq.job.Job.fetch_many",
            lambda pending_ids, **_kwargs: [enqueued for _ in pending_ids],
        )

        job = self._make_build_job()
        results = enqueue_and_poll_ci_jobs([job], redis_host="localhost")

        assert results[job.job_id]["success"] is False
        assert "infra_stale_started_job" in results[job.job_id]["error"]
        assert results[job.job_id]["details"]["error_code"] == "infra_stale_started_job"


class TestBlockedDeferredRecovery:
    """Test blocked deferred dependency detection."""

    @staticmethod
    def _make_mock_rq_job(
        job_id: str,
        status: str,
        dependency_ids: list[str] | None = None,
        *,
        created_at: datetime | None = None,
    ):
        job = MagicMock()
        job.id = job_id
        job._status = status
        job.dependency_ids = dependency_ids or []
        job.created_at = created_at or (
            datetime.now(timezone.utc) - timedelta(seconds=300)
        )
        job.get_status.side_effect = lambda: job._status
        job.refresh.return_value = None
        return job

    def test_marks_deferred_job_when_dependency_failed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from crsbench.distributed.ci_jobs import _mark_blocked_deferred_jobs

        queue = MagicMock()
        queue.name = "crsbench_ci_verify"
        queue.connection = MagicMock()

        deferred = self._make_mock_rq_job("verify-1", "deferred", ["build-1"])
        failed_dep = self._make_mock_rq_job("build-1", "failed")

        rq_jobs = {"verify-1": deferred}
        pending = {"verify-1"}
        missing: dict[str, dict] = {}

        mock_registry = MagicMock()
        mock_registry.get_job_ids.return_value = ["verify-1"]

        monkeypatch.setattr(
            "rq.registry.DeferredJobRegistry", lambda **_kwargs: mock_registry
        )
        monkeypatch.setattr("rq.job.Job.fetch", lambda _dep_id, **_kwargs: failed_dep)

        marked = _mark_blocked_deferred_jobs(queue, rq_jobs, pending, missing)

        assert marked == 1
        assert "verify-1" not in pending
        assert missing["verify-1"]["details"]["error_code"] == "infra_dependency_failed"

    def test_marks_deferred_job_immediately_when_dependency_failed_even_if_new(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from crsbench.distributed.ci_jobs import _mark_blocked_deferred_jobs

        queue = MagicMock()
        queue.name = "crsbench_ci_verify"
        queue.connection = MagicMock()

        deferred = self._make_mock_rq_job(
            "verify-1",
            "deferred",
            ["build-1"],
            created_at=datetime.now(timezone.utc),
        )
        failed_dep = self._make_mock_rq_job("build-1", "failed")

        rq_jobs = {"verify-1": deferred}
        pending = {"verify-1"}
        missing: dict[str, dict] = {}

        mock_registry = MagicMock()
        mock_registry.get_job_ids.return_value = ["verify-1"]

        monkeypatch.setattr(
            "rq.registry.DeferredJobRegistry", lambda **_kwargs: mock_registry
        )
        monkeypatch.setattr("rq.job.Job.fetch", lambda _dep_id, **_kwargs: failed_dep)

        marked = _mark_blocked_deferred_jobs(queue, rq_jobs, pending, missing)

        assert marked == 1
        assert "verify-1" not in pending
        assert missing["verify-1"]["details"]["error_code"] == "infra_dependency_failed"
