"""Tests for patch_queue and patch_evaluator_jobs modules.

Tests verify EmbeddedPatch serialization, payload roundtrips, queue
initialization, enqueue logic, and poll behavior. All tests run without
requiring a Redis connection.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

from crsbench.distributed.patch_evaluator_jobs import (
    EmbeddedPatch,
    PatchBuildResult,
    PatchJobPayload,
    PatchVerifyResult,
)


class TestEmbeddedPatch:
    """Tests for EmbeddedPatch serialization and roundtrip."""

    def test_embedded_patch_roundtrip(self, tmp_path: Path) -> None:
        """Create EmbeddedPatch from file, serialize, deserialize, write, verify."""
        # Create a patch file with known content
        original_content = b"--- a/foo.c\n+++ b/foo.c\n@@ -1 +1 @@\n-bug\n+fix\n"
        original_file = tmp_path / "original.diff"
        original_file.write_bytes(original_content)

        # Create EmbeddedPatch from file
        embedded = EmbeddedPatch.from_file("patch_0", "cpv_1", original_file)

        # Serialize to dict
        d = embedded.to_dict()
        assert "patch_id" in d
        assert "pov_id" in d
        assert "patch_content_b64" in d

        # Deserialize from dict
        restored = EmbeddedPatch.from_dict(d)
        assert restored.patch_id == "patch_0"
        assert restored.pov_id == "cpv_1"

        # Write to new file and verify content matches
        output_file = tmp_path / "restored.diff"
        restored.write_to(output_file)
        assert output_file.read_bytes() == original_content

    def test_embedded_patch_from_file(self, tmp_path: Path) -> None:
        """Create EmbeddedPatch from file, verify fields and decoded content."""
        content = b"diff --git a/src/lib.c b/src/lib.c\nsome patch content\n"
        patch_file = tmp_path / "patch.diff"
        patch_file.write_bytes(content)

        embedded = EmbeddedPatch.from_file("p1", "cpv_2", patch_file)

        assert embedded.patch_id == "p1"
        assert embedded.pov_id == "cpv_2"

        # Verify decoded content matches
        import base64

        decoded = base64.b64decode(embedded.patch_content_b64)
        assert decoded == content


class TestPatchJobPayload:
    """Tests for PatchJobPayload serialization roundtrip."""

    def test_patch_job_payload_roundtrip(self) -> None:
        """Create PatchJobPayload with all fields, serialize/deserialize, verify."""
        embedded = EmbeddedPatch(
            patch_id="patch_0",
            pov_id="cpv_1",
            patch_content_b64="dGVzdCBjb250ZW50",  # "test content" in base64
        )

        payload = PatchJobPayload(
            experiment_name="exp-42",
            trial_id="trial-1",
            benchmark="sanity-mock-c-delta-01",
            harness="harness_0",
            cpv_id="cpv_1",
            patch=embedded,
            sanitizer="address",
            source_mode="pkgs",
            use_inc_build=True,
            enqueued_at=1700000000.0,
        )

        d = payload.to_dict()
        restored = PatchJobPayload.from_dict(d)

        assert restored.experiment_name == "exp-42"
        assert restored.trial_id == "trial-1"
        assert restored.benchmark == "sanity-mock-c-delta-01"
        assert restored.harness == "harness_0"
        assert restored.cpv_id == "cpv_1"
        assert restored.patch.patch_id == "patch_0"
        assert restored.patch.pov_id == "cpv_1"
        assert restored.sanitizer == "address"
        assert restored.source_mode == "pkgs"
        assert restored.use_inc_build is True
        assert restored.enqueued_at == 1700000000.0


class TestPatchBuildResult:
    """Tests for PatchBuildResult serialization roundtrip."""

    def test_patch_build_result_roundtrip(self) -> None:
        """Create PatchBuildResult, serialize/deserialize, verify fields."""
        result = PatchBuildResult(
            trial_id="trial-1",
            benchmark="mock-bench",
            harness="harness_0",
            cpv_id="cpv_1",
            patch_id="patch_0",
            success=True,
            variant_name="mock-bench-cpv_1-patch_0-address",
            error=None,
            completed_at=1700000100.0,
        )

        d = result.to_dict()
        restored = PatchBuildResult.from_dict(d)

        assert restored.trial_id == "trial-1"
        assert restored.benchmark == "mock-bench"
        assert restored.harness == "harness_0"
        assert restored.cpv_id == "cpv_1"
        assert restored.patch_id == "patch_0"
        assert restored.success is True
        assert restored.variant_name == "mock-bench-cpv_1-patch_0-address"
        assert restored.error is None
        assert restored.completed_at == 1700000100.0


class TestPatchVerifyResult:
    """Tests for PatchVerifyResult serialization roundtrip."""

    def test_patch_verify_result_roundtrip(self) -> None:
        """Create PatchVerifyResult, serialize/deserialize, verify fields."""
        result = PatchVerifyResult(
            trial_id="trial-1",
            benchmark="mock-bench",
            harness="harness_0",
            cpv_id="cpv_1",
            patch_id="patch_0",
            pov_test_passed=True,
            unit_test_passed=True,
            status="valid",
            details="Patch passes both POV and unit tests",
            error=None,
            completed_at=1700000200.0,
        )

        d = result.to_dict()
        restored = PatchVerifyResult.from_dict(d)

        assert restored.trial_id == "trial-1"
        assert restored.benchmark == "mock-bench"
        assert restored.harness == "harness_0"
        assert restored.cpv_id == "cpv_1"
        assert restored.patch_id == "patch_0"
        assert restored.pov_test_passed is True
        assert restored.unit_test_passed is True
        assert restored.status == "valid"
        assert restored.details == "Patch passes both POV and unit tests"
        assert restored.error is None
        assert restored.completed_at == 1700000200.0


class TestInitializePatchQueues:
    """Tests for initialize_patch_queues function."""

    @patch("crsbench.distributed.patch_queue.REDIS_AVAILABLE", new=False)
    def test_initialize_patch_queues_no_redis(self) -> None:
        """When REDIS_AVAILABLE is False, returns (None, None)."""
        from crsbench.distributed.patch_queue import initialize_patch_queues

        build_q, verify_q = initialize_patch_queues("localhost", "test-exp")
        assert build_q is None
        assert verify_q is None


class TestEnqueuePatchJobs:
    """Tests for enqueue_patch_jobs function."""

    def test_enqueue_patch_jobs_creates_build_and_verify(self, tmp_path: Path) -> None:
        """Enqueue a single patch: verify build and verify jobs created."""
        from crsbench.distributed.patch_queue import enqueue_patch_jobs

        # Create a mock patch file
        patch_file = tmp_path / "patch.diff"
        patch_file.write_text("--- a/x.c\n+++ b/x.c\n")

        # Mock build queue
        mock_build_job = MagicMock()
        mock_build_job.id = "build-job-001"
        build_queue = MagicMock()
        build_queue.enqueue.return_value = mock_build_job

        # Mock verify queue
        mock_verify_job = MagicMock()
        mock_verify_job.id = "verify-job-001"
        verify_queue = MagicMock()
        verify_queue.enqueue.return_value = mock_verify_job

        patches = [("cpv_1", "patch_0", patch_file)]

        job_ids = enqueue_patch_jobs(
            build_queue,
            verify_queue,
            "test-exp",
            "trial-1",
            "mock-bench",
            "harness_0",
            patches,
        )

        # Verify build queue called with execute_patch_build
        assert build_queue.enqueue.call_count == 1
        build_call_args = build_queue.enqueue.call_args
        assert (
            build_call_args[0][0]
            == "crsbench.distributed.patch_evaluator_jobs.execute_patch_build"
        )

        # Verify verify queue called with execute_patch_verify and depends_on
        assert verify_queue.enqueue.call_count == 1
        verify_call_args = verify_queue.enqueue.call_args
        assert (
            verify_call_args[0][0]
            == "crsbench.distributed.patch_evaluator_jobs.execute_patch_verify"
        )
        assert verify_call_args[1]["depends_on"] == [mock_build_job]

        # Returns list with one verify job ID
        assert job_ids == ["verify-job-001"]


class TestPollPatchVerdicts:
    """Tests for poll_patch_verdicts function."""

    @patch("crsbench.distributed.patch_queue.REDIS_AVAILABLE", new=True)
    @patch("crsbench.distributed.patch_queue.rq")
    @patch("crsbench.distributed.queue.create_redis_connection")
    def test_poll_patch_verdicts_completed(
        self, mock_create_conn: MagicMock, mock_rq: MagicMock
    ) -> None:
        """Completed job returns result in completed list."""
        from crsbench.distributed.patch_queue import poll_patch_verdicts

        mock_conn = MagicMock()
        mock_create_conn.return_value = mock_conn

        # Mock a finished job with result
        result_dict = {
            "trial_id": "trial-1",
            "benchmark": "mock-bench",
            "harness": "harness_0",
            "cpv_id": "cpv_1",
            "patch_id": "patch_0",
            "status": "valid",
            "pov_test_passed": True,
            "unit_test_passed": True,
        }
        mock_job = MagicMock()
        mock_job.get_status.return_value = "finished"
        mock_job.result = result_dict
        mock_rq.job.Job.fetch.return_value = mock_job

        completed, remaining = poll_patch_verdicts("localhost", ["job-001"])

        assert len(completed) == 1
        assert completed[0] == result_dict
        assert remaining == []

    @patch("crsbench.distributed.patch_queue.REDIS_AVAILABLE", new=True)
    @patch("crsbench.distributed.patch_queue.rq")
    @patch("crsbench.distributed.queue.create_redis_connection")
    def test_poll_patch_verdicts_pending(
        self, mock_create_conn: MagicMock, mock_rq: MagicMock
    ) -> None:
        """Pending job returns empty completed, job ID in remaining."""
        from crsbench.distributed.patch_queue import poll_patch_verdicts

        mock_conn = MagicMock()
        mock_create_conn.return_value = mock_conn

        mock_job = MagicMock()
        mock_job.get_status.return_value = "started"
        mock_rq.job.Job.fetch.return_value = mock_job

        completed, remaining = poll_patch_verdicts("localhost", ["job-002"])

        assert completed == []
        assert remaining == ["job-002"]

    @patch("crsbench.distributed.patch_queue.REDIS_AVAILABLE", new=True)
    @patch("crsbench.distributed.patch_queue.rq")
    @patch("crsbench.distributed.queue.create_redis_connection")
    def test_poll_patch_verdicts_failed(
        self, mock_create_conn: MagicMock, mock_rq: MagicMock
    ) -> None:
        """Failed job returns error result in completed list."""
        from crsbench.distributed.patch_queue import poll_patch_verdicts

        mock_conn = MagicMock()
        mock_create_conn.return_value = mock_conn

        mock_job = MagicMock()
        mock_job.get_status.return_value = "failed"
        mock_job.exc_info = "RuntimeError: build crashed"
        mock_rq.job.Job.fetch.return_value = mock_job

        completed, remaining = poll_patch_verdicts("localhost", ["job-003"])

        assert len(completed) == 1
        assert completed[0]["status"] == "error"
        assert "RuntimeError: build crashed" in completed[0]["error"]
        assert remaining == []
