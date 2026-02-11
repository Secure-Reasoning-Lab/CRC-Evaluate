"""Tests for build_jobs.py serialization and enqueue_and_poll_builds()."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from crsbench.distributed.build_jobs import (
    enqueue_and_poll_builds,
    execute_ci_build,
    raw_results_to_executor_results,
    serialize_build_job,
)


def _make_stub_job(
    *,
    benchmark_name: str = "test-bench",
    patch_id: str | None = None,
    pov_id: str | None = None,
) -> MagicMock:
    """Create a stub BuildSingleVariantJob with required attributes."""
    from crsbench.builder.types import BenchmarkMode, VariantType

    job = MagicMock()
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
    """serialize_build_job() tests."""

    def test_includes_patch_id_and_pov_id(self) -> None:
        """Serialized dict includes patch_id and pov_id fields."""
        job = _make_stub_job(patch_id="patch_0", pov_id="cpv_0")
        result = serialize_build_job(job)

        assert result["patch_id"] == "patch_0"
        assert result["pov_id"] == "cpv_0"

    def test_patch_id_none_when_not_set(self) -> None:
        """patch_id and pov_id are None when not set."""
        job = _make_stub_job()
        result = serialize_build_job(job)

        assert result["patch_id"] is None
        assert result["pov_id"] is None

    def test_paths_serialized_as_strings(self) -> None:
        """Path objects are serialized as strings."""
        job = _make_stub_job()
        result = serialize_build_job(job)

        assert isinstance(result["benchmark_path"], str)
        assert all(isinstance(p, str) for p in result["patches"])

    def test_enums_serialized_as_values(self) -> None:
        """Enums are serialized as their string values."""
        job = _make_stub_job()
        result = serialize_build_job(job)

        assert isinstance(result["variant_type"], str)
        assert isinstance(result["mode"], str)


class TestExecuteCiBuild:
    """execute_ci_build() deserialization tests."""

    def test_roundtrip_patch_id_pov_id(self) -> None:
        """patch_id and pov_id survive serialize -> deserialize roundtrip."""
        job = _make_stub_job(patch_id="patch_1", pov_id="cpv_2")
        params = serialize_build_job(job)

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
            mock_job_instance.execute.return_value = mock_result
            mock_cls.return_value = mock_job_instance
            mock_ctx_cls.return_value = MagicMock()

            execute_ci_build(params)

            # Verify patch_id and pov_id were passed to constructor
            call_kwargs = mock_cls.call_args
            assert call_kwargs.kwargs.get("patch_id") == "patch_1"
            assert call_kwargs.kwargs.get("pov_id") == "cpv_2"


class TestRawResultsToExecutorResults:
    """raw_results_to_executor_results() tests."""

    def test_successful_result(self) -> None:
        """Successful raw result converts to SUCCESS ExecutorResult."""
        from crsbench.executor.types import JobStatus

        raw = {
            "job-1": {
                "job_id": "job-1",
                "job_type": "build",
                "success": True,
                "error": None,
                "elapsed_seconds": 5.0,
                "details": {"storage_bytes": 1000},
                "started_at": "2024-01-01T00:00:00",
                "finished_at": "2024-01-01T00:00:05",
            }
        }
        results = raw_results_to_executor_results(raw)

        assert "job-1" in results
        assert results["job-1"].status == JobStatus.SUCCESS
        assert results["job-1"].success is True
        assert results["job-1"].elapsed_seconds == 5.0

    def test_failed_result(self) -> None:
        """Failed raw result converts to FAILED ExecutorResult."""
        from crsbench.executor.types import JobStatus

        raw = {
            "job-2": {
                "job_id": "job-2",
                "job_type": "build",
                "success": False,
                "error": "Build timeout",
                "elapsed_seconds": 0.0,
                "details": {},
                "started_at": None,
                "finished_at": None,
            }
        }
        results = raw_results_to_executor_results(raw)

        assert "job-2" in results
        assert results["job-2"].status == JobStatus.FAILED
        assert results["job-2"].success is False
        assert results["job-2"].error == "Build timeout"


class TestEnqueueAndPollBuilds:
    """enqueue_and_poll_builds() with mocked Redis."""

    def _setup_redis_mock(self) -> tuple[MagicMock, MagicMock, MagicMock]:
        """Create mock redis, rq, and queue objects."""
        mock_redis_mod = MagicMock()
        mock_rq_mod = MagicMock()
        mock_conn = MagicMock()
        mock_redis_mod.Redis.return_value = mock_conn
        mock_queue = MagicMock()
        mock_rq_mod.Queue.return_value = mock_queue
        return mock_redis_mod, mock_rq_mod, mock_queue

    def test_enqueue_and_poll_success(self) -> None:
        """Enqueues jobs and polls until finished."""
        mock_redis_mod, mock_rq_mod, mock_queue = self._setup_redis_mock()

        mock_rq_job = MagicMock()
        mock_rq_job.id = "rq-123"
        mock_rq_job.get_status.return_value = "finished"
        mock_rq_job.result = {
            "job_id": "build-single:test:deltaref:address",
            "job_type": "build",
            "success": True,
            "error": None,
            "elapsed_seconds": 2.0,
            "details": {},
            "started_at": "2024-01-01T00:00:00",
            "finished_at": "2024-01-01T00:00:02",
        }
        mock_queue.enqueue.return_value = mock_rq_job

        with (
            patch.dict(
                "sys.modules",
                {"redis": mock_redis_mod, "rq": mock_rq_mod, "rq.job": mock_rq_mod.job},
            ),
        ):
            job = _make_stub_job()
            results = enqueue_and_poll_builds([job], "localhost")

        assert job.job_id in results
        assert results[job.job_id]["success"] is True

    def test_dedup_existing_job(self) -> None:
        """Handles deduplication when job ID already exists."""
        mock_redis_mod, mock_rq_mod, mock_queue = self._setup_redis_mock()

        mock_queue.enqueue.side_effect = Exception("job already exists")

        mock_existing = MagicMock()
        mock_existing.id = "existing-123"
        mock_existing.get_status.return_value = "finished"
        mock_existing.result = {
            "job_id": "build-single:test:deltaref:address",
            "success": True,
            "elapsed_seconds": 1.0,
            "details": {},
            "started_at": None,
            "finished_at": None,
        }
        mock_rq_mod.job.Job.fetch.return_value = mock_existing

        with (
            patch.dict(
                "sys.modules",
                {"redis": mock_redis_mod, "rq": mock_rq_mod, "rq.job": mock_rq_mod.job},
            ),
        ):
            job = _make_stub_job()
            results = enqueue_and_poll_builds([job], "localhost")

        assert job.job_id in results
        assert results[job.job_id]["success"] is True

    def test_failed_job_returns_error(self) -> None:
        """Failed RQ job returns error dict."""
        mock_redis_mod, mock_rq_mod, mock_queue = self._setup_redis_mock()

        mock_rq_job = MagicMock()
        mock_rq_job.id = "rq-456"
        mock_rq_job.get_status.return_value = "failed"
        mock_rq_job.result = None
        mock_rq_job.exc_info = "RuntimeError: Docker build failed"
        mock_queue.enqueue.return_value = mock_rq_job

        with (
            patch.dict(
                "sys.modules",
                {"redis": mock_redis_mod, "rq": mock_rq_mod, "rq.job": mock_rq_mod.job},
            ),
        ):
            job = _make_stub_job()
            results = enqueue_and_poll_builds([job], "localhost")

        assert job.job_id in results
        assert results[job.job_id]["success"] is False
        assert "Docker build failed" in results[job.job_id]["error"]
