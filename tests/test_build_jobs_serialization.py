"""Tests for build_jobs.py serialization and execute_ci_build()."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from crsbench.distributed.build_jobs import execute_ci_build
from crsbench.distributed.ci_jobs import serialize_ci_job


class _StubBuildSingleVariantJob:
    """Minimal stub that satisfies serialize_ci_job type name check."""


# Rename so type().__name__ returns "BuildSingleVariantJob"
_StubBuildSingleVariantJob.__name__ = "BuildSingleVariantJob"
_StubBuildSingleVariantJob.__qualname__ = "BuildSingleVariantJob"


def _make_stub_job(
    *,
    benchmark_name: str = "test-bench",
    patch_id: str | None = None,
    pov_id: str | None = None,
) -> _StubBuildSingleVariantJob:
    """Create a stub BuildSingleVariantJob with required attributes."""
    from crsbench.builder.types import BenchmarkMode, VariantType

    job = _StubBuildSingleVariantJob()
    job.benchmark_path = Path("/benchmarks/test-bench")
    job.benchmark_name = benchmark_name
    job.variant_type = VariantType.DELTA_REF
    job.commit = "abc123"
    job.main_repo = "https://github.com/test/repo"
    job.mode = BenchmarkMode.DELTA
    job.language = "c"
    job.cpv_num = 0
    job.patch_id = patch_id
    job.pov_id = pov_id
    job.patches = [Path("/patches/p1.diff")]
    job.use_inc_build = True
    job.force_rebuild = False
    job.skip_if_cached = False
    job.source_mode = "pkgs"
    job.sanitizer = "address"
    job.repo_name = "test-repo"
    job.project_image_prefix = "aixcc-afc"
    job.job_id = f"build-single:{benchmark_name}:deltaref:address"
    return job


class TestSerializeBuildJob:
    """serialize_ci_job() tests."""

    def test_includes_patch_id_and_pov_id(self) -> None:
        """Serialized dict includes patch_id and pov_id fields."""
        job = _make_stub_job(patch_id="patch_0", pov_id="cpv_0")
        result = serialize_ci_job(job)

        assert result["patch_id"] == "patch_0"
        assert result["pov_id"] == "cpv_0"

    def test_patch_id_none_when_not_set(self) -> None:
        """patch_id and pov_id are None when not set."""
        job = _make_stub_job()
        result = serialize_ci_job(job)

        assert result["patch_id"] is None
        assert result["pov_id"] is None

    def test_paths_serialized_as_strings(self) -> None:
        """Path objects are serialized as strings."""
        job = _make_stub_job()
        result = serialize_ci_job(job)

        assert isinstance(result["benchmark_path"], str)
        assert all(isinstance(p, str) for p in result["patches"])

    def test_enums_serialized_as_values(self) -> None:
        """Enums are serialized as their string values."""
        job = _make_stub_job()
        result = serialize_ci_job(job)

        assert isinstance(result["variant_type"], str)
        assert isinstance(result["mode"], str)


class TestExecuteCiBuild:
    """execute_ci_build() deserialization tests."""

    def test_roundtrip_patch_id_pov_id(self) -> None:
        """patch_id and pov_id survive serialize -> deserialize roundtrip."""
        job = _make_stub_job(patch_id="patch_1", pov_id="cpv_2")
        params = serialize_ci_job(job)

        # Mock out the actual build execution via the source modules
        with (
            patch("crsbench.benchmark_ci.jobs.flat.BuildSingleVariantJob") as mock_cls,
            patch("crsbench.benchmark_ci.jobs.base.JobContext") as mock_ctx_cls,
        ):
            mock_job_instance = MagicMock()
            mock_result = MagicMock()
            mock_result.job_id = "test-job"
            mock_result.job_type = "build"
            mock_result.success = True
            mock_result.error = None
            mock_result.elapsed_seconds = 1.0
            mock_result.details = {}
            mock_result.started_at = None
            mock_result.finished_at = None
            mock_result.to_dict.return_value = {
                "job_id": "test-job",
                "job_type": "build",
                "success": True,
                "error": None,
                "elapsed_seconds": 1.0,
                "details": {},
                "started_at": None,
                "finished_at": None,
            }
            mock_job_instance.execute.return_value = mock_result
            mock_cls.return_value = mock_job_instance
            mock_ctx_cls.return_value = MagicMock()

            execute_ci_build(params)

            # Verify patch_id and pov_id were passed to constructor
            call_kwargs = mock_cls.call_args
            assert call_kwargs.kwargs.get("patch_id") == "patch_1"
            assert call_kwargs.kwargs.get("pov_id") == "cpv_2"
