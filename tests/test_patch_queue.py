"""Tests for patch_queue and patch_evaluator_jobs modules.

Tests verify EmbeddedPatch serialization, payload roundtrips, queue
initialization, enqueue logic, and poll behavior. All tests run without
requiring a Redis connection.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

from crsbench.builder.types import BenchmarkMode
from crsbench.distributed.patch_evaluator_jobs import (
    EmbeddedPatch,
    PatchBuildResult,
    PatchJobPayload,
    PatchVerifyResult,
    _cleanup_patch_variant_artifacts,
    _collect_patch_verify_logs,
    _resolve_patch_job_output_dir,
    _resolve_patch_variant_name,
    execute_patch_build,
    execute_patch_verify,
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
            verify_variants=True,
            test_mode="RTS",
            use_inc_build=True,
            build_patch_job_id="rq-build-123",
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
        assert restored.verify_variants is True
        assert restored.test_mode == "RTS"
        assert restored.use_inc_build is True
        assert restored.build_patch_job_id == "rq-build-123"
        assert restored.enqueued_at == 1700000000.0

    def test_patch_job_payload_use_inc_build_defaults_true(self) -> None:
        """Missing use_inc_build in payload should default to True."""
        payload = {
            "experiment_name": "exp",
            "trial_id": "trial-1",
            "benchmark": "bench",
            "harness": "h0",
            "cpv_id": "cpv_0",
            "patch": {
                "patch_id": "patch_0",
                "pov_id": "cpv_0",
                "patch_content_b64": "ZHVtbXk=",
            },
        }

        restored = PatchJobPayload.from_dict(payload)
        assert restored.test_mode == "FULL"
        assert restored.use_inc_build is True


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
            logs={"build.stderr": "compile failed"},
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
        assert restored.logs == {"build.stderr": "compile failed"}


class TestExecutePatchBuild:
    """Tests for execute_patch_build log propagation on build failures."""

    @patch("crsbench.distributed.patch_evaluator_jobs.resolve_benchmark_path")
    @patch("crsbench.distributed.patch_evaluator_jobs.get_evaluator_benchmarks_root")
    @patch("crsbench.distributed.ci_jobs.serialize_ci_job")
    @patch("crsbench.distributed.ci_jobs.execute_ci_job")
    @patch("crsbench.distributed.patch_evaluator_jobs.tempfile.mkdtemp")
    def test_execute_patch_build_includes_stream_logs_on_failure(
        self,
        mock_mkdtemp: MagicMock,
        mock_execute_ci_job: MagicMock,
        mock_serialize_ci_job: MagicMock,
        mock_get_benchmarks_root: MagicMock,
        mock_resolve_benchmark_path: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Failed patch builds should return collected build stdout/stderr snippets."""
        benchmark_root = tmp_path / "benchmarks"
        benchmark_path = benchmark_root / "bench"
        benchmark_path.mkdir(parents=True)
        mock_get_benchmarks_root.return_value = benchmark_root
        mock_resolve_benchmark_path.return_value = benchmark_path

        build_output_dir = tmp_path / "build-output"
        build_output_dir.mkdir(parents=True)
        mock_mkdtemp.return_value = str(build_output_dir)

        def _fake_execute_ci_job(params: dict) -> dict:
            logs_dir = Path(params["output_dir"]) / "bench" / "build"
            logs_dir.mkdir(parents=True, exist_ok=True)
            (logs_dir / "build.stderr").write_text("compile error detail")
            (logs_dir / "build.stdout").write_text("build stdout")
            return {
                "success": False,
                "error": "Build failed",
                "details": {"variant_name": "bench-asan-patched"},
            }

        mock_execute_ci_job.side_effect = _fake_execute_ci_job
        mock_serialize_ci_job.return_value = {"_job_class": "BuildPatchVariantJob"}

        payload = PatchJobPayload(
            experiment_name="exp",
            trial_id="trial-1",
            benchmark="bench",
            harness="h0",
            cpv_id="cpv_0",
            patch=EmbeddedPatch(
                patch_id="patch_0",
                pov_id="cpv_0",
                patch_content_b64="ZHVtbXk=",  # dummy
            ),
        )

        result = execute_patch_build(payload.to_dict())

        assert result["success"] is False
        assert result["error"] == "Build failed"
        assert isinstance(result.get("logs"), dict)
        assert result["logs"].get("bench/build/build.stderr") == "compile error detail"
        assert result["logs"].get("bench/build/build.stdout") == "build stdout"


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
            security_verdict="PASS",
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
        assert restored.security_verdict == "PASS"
        assert restored.details == "Patch passes both POV and unit tests"
        assert restored.error is None


class TestPatchVariantResolution:
    """Tests for patched variant name resolution in distributed cleanup."""

    @patch("crsbench.evaluation.verification.pov.VerificationEngine")
    def test_resolve_variant_prefers_cpv_sanitizer(
        self,
        mock_engine_cls: MagicMock,
    ) -> None:
        """Cleanup variant resolution should prefer CPV sanitizer metadata."""
        adapter = MagicMock()
        adapter.get_mode.return_value = BenchmarkMode.DELTA
        adapter.lang = "c"
        adapter.get_ref_commit.return_value = "a" * 40
        adapter.get_base_commit.return_value = "b" * 40
        adapter.main_repo = "https://example.com/repo.git"
        adapter.get_cpv_sanitizer.return_value = "address"

        mock_engine = MagicMock()
        mock_engine.load_adapter.return_value = adapter
        mock_engine_cls.return_value = mock_engine

        payload = PatchJobPayload(
            experiment_name="exp",
            trial_id="trial-1",
            benchmark="afc-dav1d-full-01",
            harness="h0",
            cpv_id="cpv_0",
            patch=EmbeddedPatch(
                patch_id="patch-1",
                pov_id="cpv_0",
                patch_content_b64="ZHVtbXk=",
            ),
            sanitizer="undefined",
            source_mode="pkgs",
        )
        with patch(
            "crsbench.utils.run_helper.ensure_oss_fuzz_root",
            return_value="/tmp/oss-fuzz",
        ):
            variant = _resolve_patch_variant_name(payload, Path("/tmp/bench"))
        assert variant is not None
        assert "-asan-" in variant


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
        from crsbench.distributed.patch_queue import (
            _make_patch_build_rq_job_id,
            _make_patch_verify_rq_job_id,
            _patch_content_hash,
            enqueue_patch_jobs,
        )

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
            sanitizer="undefined",
            verify_variants=True,
        )

        # Verify build queue called with execute_patch_build
        assert build_queue.enqueue.call_count == 1
        build_call_args = build_queue.enqueue.call_args
        assert (
            build_call_args[0][0]
            == "crsbench.distributed.patch_evaluator_jobs.execute_patch_build"
        )
        build_payload = build_call_args[0][1]
        patch_hash = _patch_content_hash(build_payload["patch"]["patch_content_b64"])
        assert build_payload["use_inc_build"] is True
        assert build_payload["test_mode"] == "FULL"
        assert build_payload["sanitizer"] == "undefined"
        assert build_call_args[1]["job_id"] == _make_patch_build_rq_job_id(
            experiment_name="test-exp",
            trial_id="trial-1",
            benchmark="mock-bench",
            harness="harness_0",
            cpv_id="cpv_1",
            patch_id="patch_0",
            sanitizer="undefined",
            source_mode="pkgs",
            use_inc_build=True,
            patch_content_hash=patch_hash,
        )
        assert (
            build_call_args[1]["meta"]["scheduler_owner_key"]
            == "trial::test-exp::trial-1"
        )

        # Verify verify queue called with execute_patch_verify and depends_on
        assert verify_queue.enqueue.call_count == 1
        verify_call_args = verify_queue.enqueue.call_args
        assert (
            verify_call_args[0][0]
            == "crsbench.distributed.patch_evaluator_jobs.execute_patch_verify"
        )
        assert verify_call_args[1]["depends_on"] == [mock_build_job]
        verify_payload = verify_call_args[0][1]
        assert verify_payload["verify_variants"] is True
        assert verify_payload["test_mode"] == "FULL"
        assert verify_payload["use_inc_build"] is True
        assert verify_payload["sanitizer"] == "undefined"
        assert verify_payload["build_patch_job_id"] == "build-job-001"
        assert verify_call_args[1]["job_id"] == _make_patch_verify_rq_job_id(
            experiment_name="test-exp",
            trial_id="trial-1",
            benchmark="mock-bench",
            harness="harness_0",
            cpv_id="cpv_1",
            patch_id="patch_0",
            verify_variants=True,
            test_mode="FULL",
            source_mode="pkgs",
            use_inc_build=True,
            sanitizer="undefined",
            patch_content_hash=patch_hash,
        )
        assert (
            verify_call_args[1]["meta"]["scheduler_owner_key"]
            == "trial::test-exp::trial-1"
        )

        # Returns list with one verify job ID
        assert job_ids == ["verify-job-001"]

    @patch("crsbench.distributed.patch_queue.rq")
    def test_enqueue_patch_jobs_reuses_existing_jobs_on_duplicate(
        self, mock_rq: MagicMock, tmp_path: Path
    ) -> None:
        """Duplicate deterministic enqueue should reuse existing RQ jobs."""
        from crsbench.distributed.patch_queue import enqueue_patch_jobs

        patch_file = tmp_path / "patch.diff"
        patch_file.write_text("--- a/x.c\n+++ b/x.c\n")

        build_queue = MagicMock()
        build_queue.connection = MagicMock()
        build_queue.enqueue.side_effect = RuntimeError("job id already exists")
        verify_queue = MagicMock()
        verify_queue.connection = MagicMock()
        verify_queue.enqueue.side_effect = RuntimeError("job id already exists")

        existing_build = MagicMock()
        existing_build.id = "build-existing"
        existing_verify = MagicMock()
        existing_verify.id = "verify-existing"
        mock_rq.job.Job.fetch.side_effect = [existing_build, existing_verify]

        job_ids = enqueue_patch_jobs(
            build_queue,
            verify_queue,
            "test-exp",
            "trial-1",
            "mock-bench",
            "harness_0",
            [("cpv_1", "patch_0", patch_file)],
        )

        assert job_ids == ["verify-existing"]
        assert mock_rq.job.Job.fetch.call_count == 2

    def test_patch_job_ids_change_when_patch_content_changes(self) -> None:
        """Deterministic IDs should change if embedded patch bytes change."""
        from crsbench.distributed.patch_queue import (
            _make_patch_build_rq_job_id,
            _make_patch_verify_rq_job_id,
            _patch_content_hash,
        )

        patch_hash_a = _patch_content_hash("Zm9v")  # "foo"
        patch_hash_b = _patch_content_hash("YmFy")  # "bar"

        build_a = _make_patch_build_rq_job_id(
            experiment_name="exp",
            trial_id="trial-1",
            benchmark="bench",
            harness="h",
            cpv_id="cpv_0",
            patch_id="patch_0",
            sanitizer="address",
            source_mode="pkgs",
            use_inc_build=True,
            patch_content_hash=patch_hash_a,
        )
        build_b = _make_patch_build_rq_job_id(
            experiment_name="exp",
            trial_id="trial-1",
            benchmark="bench",
            harness="h",
            cpv_id="cpv_0",
            patch_id="patch_0",
            sanitizer="address",
            source_mode="pkgs",
            use_inc_build=True,
            patch_content_hash=patch_hash_b,
        )
        verify_a = _make_patch_verify_rq_job_id(
            experiment_name="exp",
            trial_id="trial-1",
            benchmark="bench",
            harness="h",
            cpv_id="cpv_0",
            patch_id="patch_0",
            verify_variants=True,
            test_mode="FULL",
            source_mode="pkgs",
            use_inc_build=True,
            sanitizer="address",
            patch_content_hash=patch_hash_a,
        )
        verify_b = _make_patch_verify_rq_job_id(
            experiment_name="exp",
            trial_id="trial-1",
            benchmark="bench",
            harness="h",
            cpv_id="cpv_0",
            patch_id="patch_0",
            verify_variants=True,
            test_mode="FULL",
            source_mode="pkgs",
            use_inc_build=True,
            sanitizer="address",
            patch_content_hash=patch_hash_b,
        )

        assert build_a != build_b
        assert verify_a != verify_b


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
    def test_poll_patch_verdicts_finished_without_result(
        self, mock_create_conn: MagicMock, mock_rq: MagicMock
    ) -> None:
        """Finished job with missing result should be terminal error."""
        from crsbench.distributed.patch_queue import poll_patch_verdicts

        mock_conn = MagicMock()
        mock_create_conn.return_value = mock_conn

        mock_job = MagicMock()
        mock_job.get_status.return_value = "finished"
        mock_job.result = None
        mock_job.args = (
            {
                "trial_id": "trial-2",
                "benchmark": "bench",
                "harness": "h0",
                "cpv_id": "cpv_0",
                "patch": {"patch_id": "patch_2"},
            },
        )
        mock_rq.job.Job.fetch.return_value = mock_job

        completed, remaining = poll_patch_verdicts("localhost", ["job-002"])

        assert len(completed) == 1
        assert completed[0]["status"] == "error"
        assert completed[0]["trial_id"] == "trial-2"
        assert completed[0]["patch_id"] == "patch_2"
        assert "without a result payload" in completed[0]["error"]
        assert remaining == []

    @patch("crsbench.distributed.patch_queue.REDIS_AVAILABLE", new=True)
    @patch("crsbench.distributed.patch_queue.rq")
    @patch("crsbench.distributed.queue.create_redis_connection")
    def test_poll_patch_verdicts_finished_with_invalid_result_payload(
        self, mock_create_conn: MagicMock, mock_rq: MagicMock
    ) -> None:
        """Finished job with non-dict result should become terminal error."""
        from crsbench.distributed.patch_queue import poll_patch_verdicts

        mock_conn = MagicMock()
        mock_create_conn.return_value = mock_conn

        mock_job = MagicMock()
        mock_job.get_status.return_value = "finished"
        mock_job.result = "not-a-dict"
        mock_job.args = ({"trial_id": "trial-2", "patch": {"patch_id": "patch_2"}},)
        mock_rq.job.Job.fetch.return_value = mock_job

        completed, remaining = poll_patch_verdicts("localhost", ["job-002"])

        assert len(completed) == 1
        assert completed[0]["status"] == "error"
        assert "invalid result payload type" in completed[0]["error"]
        assert remaining == []

    @patch("crsbench.distributed.patch_queue.REDIS_AVAILABLE", new=True)
    @patch("crsbench.distributed.patch_queue.rq")
    @patch("crsbench.distributed.queue.create_redis_connection")
    def test_poll_patch_verdicts_cancelled_treated_as_terminal_error(
        self, mock_create_conn: MagicMock, mock_rq: MagicMock
    ) -> None:
        """Cancelled jobs should not remain pending forever."""
        from crsbench.distributed.patch_queue import poll_patch_verdicts

        mock_conn = MagicMock()
        mock_create_conn.return_value = mock_conn

        mock_job = MagicMock()
        mock_job.get_status.return_value = "cancelled"
        mock_job.args = ({"trial_id": "trial-2", "patch": {"patch_id": "patch_2"}},)
        mock_rq.job.Job.fetch.return_value = mock_job

        completed, remaining = poll_patch_verdicts("localhost", ["job-002"])

        assert len(completed) == 1
        assert completed[0]["status"] == "error"
        assert "non-success job status: cancelled" in completed[0]["error"]
        assert remaining == []

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
        mock_job.args = (
            {
                "trial_id": "trial-3",
                "benchmark": "bench",
                "harness": "h0",
                "cpv_id": "cpv_0",
                "patch": {"patch_id": "patch_0"},
            },
        )
        mock_rq.job.Job.fetch.return_value = mock_job

        completed, remaining = poll_patch_verdicts("localhost", ["job-003"])

        assert len(completed) == 1
        assert completed[0]["trial_id"] == "trial-3"
        assert completed[0]["benchmark"] == "bench"
        assert completed[0]["patch_id"] == "patch_0"
        assert completed[0]["status"] == "error"
        assert "RuntimeError: build crashed" in completed[0]["error"]
        assert remaining == []

    @patch("crsbench.distributed.patch_queue.REDIS_AVAILABLE", new=True)
    @patch("crsbench.distributed.patch_queue.rq")
    @patch("crsbench.distributed.queue.create_redis_connection")
    def test_poll_patch_verdicts_failed_with_malformed_patch_payload(
        self, mock_create_conn: MagicMock, mock_rq: MagicMock
    ) -> None:
        """Malformed payload should still return completed error, not remaining."""
        from crsbench.distributed.patch_queue import poll_patch_verdicts

        mock_conn = MagicMock()
        mock_create_conn.return_value = mock_conn

        mock_job = MagicMock()
        mock_job.get_status.return_value = "failed"
        mock_job.exc_info = "RuntimeError: verify crashed"
        mock_job.args = ({"trial_id": "trial-4", "patch": None},)
        mock_rq.job.Job.fetch.return_value = mock_job

        completed, remaining = poll_patch_verdicts("localhost", ["job-004"])

        assert len(completed) == 1
        assert completed[0]["trial_id"] == "trial-4"
        assert completed[0]["patch_id"] == ""
        assert remaining == []

    @patch("crsbench.distributed.patch_queue.REDIS_AVAILABLE", new=True)
    @patch("crsbench.distributed.patch_queue.rq")
    @patch("crsbench.distributed.queue.create_redis_connection")
    def test_poll_patch_verdicts_failed_with_none_args(
        self, mock_create_conn: MagicMock, mock_rq: MagicMock
    ) -> None:
        """None args should still yield a completed error verdict."""
        from crsbench.distributed.patch_queue import poll_patch_verdicts

        mock_conn = MagicMock()
        mock_create_conn.return_value = mock_conn

        mock_job = MagicMock()
        mock_job.get_status.return_value = "failed"
        mock_job.exc_info = "RuntimeError: verify crashed"
        mock_job.args = None
        mock_rq.job.Job.fetch.return_value = mock_job

        completed, remaining = poll_patch_verdicts("localhost", ["job-005"])

        assert len(completed) == 1
        assert completed[0]["status"] == "error"
        assert remaining == []

    @patch("crsbench.distributed.patch_queue.REDIS_AVAILABLE", new=True)
    @patch("crsbench.distributed.patch_queue.rq")
    @patch("crsbench.distributed.queue.create_redis_connection")
    def test_poll_patch_verdicts_failed_with_kwargs_payload(
        self, mock_create_conn: MagicMock, mock_rq: MagicMock
    ) -> None:
        """Failed jobs should recover routing fields from kwargs payload."""
        from crsbench.distributed.patch_queue import poll_patch_verdicts

        mock_conn = MagicMock()
        mock_create_conn.return_value = mock_conn

        mock_job = MagicMock()
        mock_job.get_status.return_value = "failed"
        mock_job.exc_info = "RuntimeError: verify crashed"
        mock_job.args = ()
        mock_job.kwargs = {
            "trial_id": "trial-6",
            "benchmark": "bench",
            "harness": "h0",
            "cpv_id": "cpv_0",
            "patch": {"patch_id": "patch_6"},
        }
        mock_rq.job.Job.fetch.return_value = mock_job

        completed, remaining = poll_patch_verdicts("localhost", ["job-006"])

        assert len(completed) == 1
        assert completed[0]["trial_id"] == "trial-6"
        assert completed[0]["patch_id"] == "patch_6"
        assert remaining == []


class TestPatchVerifyCleanup:
    """Tests for patched image cleanup in execute_patch_verify."""

    @patch("crsbench.builder.infrastructure.OSSFuzzInfrastructure")
    @patch("crsbench.utils.run_helper.get_oss_fuzz_root", return_value="/tmp/oss-fuzz")
    def test_cleanup_removes_patch_and_unittest_variants(
        self, mock_root: MagicMock, mock_infra_cls: MagicMock
    ) -> None:
        """Cleanup helper removes both patch and patch-unittest artifacts."""
        assert mock_root is not None
        mock_infra = MagicMock()
        mock_infra_cls.return_value = mock_infra

        _cleanup_patch_variant_artifacts("bench-asan-delta-patched-cpv_0-patch_0")

        # main patch variant
        mock_infra.cleanup_docker_images.assert_any_call(
            "bench-asan-delta-patched-cpv_0-patch_0"
        )
        mock_infra.cleanup_variant.assert_any_call(
            "bench-asan-delta-patched-cpv_0-patch_0"
        )
        # unittest variant
        mock_infra.cleanup_docker_images.assert_any_call(
            "bench-asan-delta-patched-cpv_0-patch_0-unittest"
        )
        mock_infra.cleanup_variant.assert_any_call(
            "bench-asan-delta-patched-cpv_0-patch_0-unittest"
        )

    @patch("crsbench.distributed.patch_evaluator_jobs._cleanup_patch_variant_artifacts")
    @patch(
        "crsbench.distributed.patch_evaluator_jobs._resolve_patch_variant_name",
        return_value="mock-bench-asan-delta-patched-cpv_0-patch_0",
    )
    @patch(
        "crsbench.distributed.patch_evaluator_jobs.resolve_benchmark_path",
        return_value=Path("/tmp/nonexistent-benchmark"),
    )
    @patch(
        "crsbench.distributed.patch_evaluator_jobs.get_evaluator_benchmarks_root",
        return_value=Path("/tmp"),
    )
    def test_cleanup_runs_on_early_return_when_pov_missing(
        self,
        mock_root: MagicMock,
        mock_resolve: MagicMock,
        mock_variant: MagicMock,
        mock_cleanup: MagicMock,
    ) -> None:
        """Cleanup runs even when verify exits early due to missing POV file."""
        assert mock_root is not None
        assert mock_resolve is not None
        assert mock_variant is not None
        payload = PatchJobPayload(
            experiment_name="exp",
            trial_id="trial-1",
            benchmark="mock-bench",
            harness="h0",
            cpv_id="cpv_0",
            patch=EmbeddedPatch(
                patch_id="patch_0",
                pov_id="cpv_0",
                patch_content_b64="ZHVtbXk=",  # "dummy"
            ),
        )

        result = execute_patch_verify(payload.to_dict())
        assert result["status"] == "error"
        assert "No POV files found" in result["details"]
        mock_cleanup.assert_called_once_with(
            "mock-bench-asan-delta-patched-cpv_0-patch_0"
        )

    @patch("crsbench.distributed.ci_jobs.execute_ci_job")
    @patch(
        "crsbench.distributed.patch_evaluator_jobs.resolve_benchmark_path",
    )
    @patch(
        "crsbench.distributed.patch_evaluator_jobs.get_evaluator_benchmarks_root",
    )
    def test_verify_uses_embedded_patch_path_override(
        self,
        mock_root: MagicMock,
        mock_resolve: MagicMock,
        mock_execute_ci: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Distributed verify should pass embedded patch path override to CI job."""
        mock_root.return_value = tmp_path
        bench = tmp_path / "mock-bench"
        (bench / ".aixcc" / "h0" / "cpv_0" / "blobs").mkdir(parents=True, exist_ok=True)
        (bench / ".aixcc" / "h0" / "cpv_0" / "blobs" / "pov_0.blob").write_bytes(b"x")
        mock_resolve.return_value = bench
        mock_execute_ci.return_value = {
            "success": False,
            "error": "verification failed",
            "details": {},
        }

        payload = PatchJobPayload(
            experiment_name="exp",
            trial_id="trial-2",
            benchmark="mock-bench",
            harness="h0",
            cpv_id="cpv_0",
            patch=EmbeddedPatch(
                patch_id="patch_1",
                pov_id="cpv_0",
                patch_content_b64="ZHVtbXk=",  # dummy
            ),
            verify_variants=True,
            build_patch_job_id="rq-build-777",
        )

        result = execute_patch_verify(payload.to_dict())
        assert result["status"] == "error"
        called_params = mock_execute_ci.call_args[0][0]
        patch_override = called_params.get("patch_path_override")
        assert isinstance(patch_override, str)
        assert patch_override.endswith("/patches/patch_1.diff")
        assert called_params["build_patch_job_id"] == "rq-build-777"


class TestPatchJobOutputDir:
    """Tests for distributed patch job output dir resolution."""

    def test_resolve_patch_job_output_dir_is_deterministic(self) -> None:
        """Path should be stable and namespaced by experiment/trial/patch identity."""
        payload = PatchJobPayload(
            experiment_name="exp/main",
            trial_id="trial:1",
            benchmark="mock-bench",
            harness="harness_0",
            cpv_id="cpv_0",
            patch=EmbeddedPatch(
                patch_id="patch#0",
                pov_id="cpv_0",
                patch_content_b64="ZHVtbXk=",  # "dummy"
            ),
        )

        path = _resolve_patch_job_output_dir(payload)
        path_str = str(path)

        assert "distributed-patch-jobs" in path_str
        assert "exp_main" in path_str
        assert "trial_1" in path_str
        assert "mock-bench" in path_str
        assert "harness_0" in path_str
        assert "cpv_0" in path_str
        assert "patch_0" in path_str


class TestPatchLogCollection:
    """Tests for distributed patch log collection safeguards."""

    def test_collect_patch_verify_logs_truncates_large_logs(
        self, tmp_path: Path
    ) -> None:
        """Collected logs should be bounded and marked truncated when oversized."""
        payload = PatchJobPayload(
            experiment_name="exp",
            trial_id="trial-1",
            benchmark="mock-bench",
            harness="harness_0",
            cpv_id="cpv_0",
            patch=EmbeddedPatch(
                patch_id="patch_0",
                pov_id="cpv_0",
                patch_content_b64="ZHVtbXk=",
            ),
        )
        out_dir = _resolve_patch_job_output_dir(payload)
        logs_dir = out_dir / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        (logs_dir / "huge.stdout").write_bytes(b"a" * (300 * 1024))

        logs = _collect_patch_verify_logs(payload)
        assert "huge.stdout" in logs
        assert "[truncated additional bytes]" in logs["huge.stdout"]

    def test_collect_patch_verify_logs_skips_unreadable(self, tmp_path: Path) -> None:
        """Unreadable log files should not crash collection."""
        payload = PatchJobPayload(
            experiment_name="exp",
            trial_id="trial-2",
            benchmark="mock-bench",
            harness="harness_0",
            cpv_id="cpv_0",
            patch=EmbeddedPatch(
                patch_id="patch_1",
                pov_id="cpv_0",
                patch_content_b64="ZHVtbXk=",
            ),
        )
        out_dir = _resolve_patch_job_output_dir(payload)
        logs_dir = out_dir / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        path = logs_dir / "ok.stdout"
        path.write_text("ok")

        with patch.object(Path, "open", side_effect=OSError("boom")):
            logs = _collect_patch_verify_logs(payload)

        assert logs == {}

    def test_collect_patch_verify_logs_count_only_eligible_logs(
        self, tmp_path: Path
    ) -> None:
        """Non-log files should not consume file-count budget."""
        payload = PatchJobPayload(
            experiment_name="exp",
            trial_id="trial-3",
            benchmark="mock-bench",
            harness="harness_0",
            cpv_id="cpv_0",
            patch=EmbeddedPatch(
                patch_id="patch_2",
                pov_id="cpv_0",
                patch_content_b64="ZHVtbXk=",
            ),
        )
        out_dir = _resolve_patch_job_output_dir(payload)
        logs_dir = out_dir / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)

        # Irrelevant entries
        for i in range(100):
            (logs_dir / f"junk-{i}.txt").write_text("x")

        # Eligible entries should still be collected
        (logs_dir / "a.stdout").write_text("ok")
        (logs_dir / "b.stderr").write_text("err")

        logs = _collect_patch_verify_logs(payload)
        assert logs["a.stdout"] == "ok"
        assert logs["b.stderr"] == "err"

    @patch("crsbench.distributed.patch_evaluator_jobs._MAX_LOG_FILE_COUNT", new=1)
    def test_collect_patch_verify_logs_order_is_deterministic(
        self, tmp_path: Path
    ) -> None:
        """When capped, lexicographically first eligible log is selected."""
        payload = PatchJobPayload(
            experiment_name="exp",
            trial_id="trial-4",
            benchmark="mock-bench",
            harness="harness_0",
            cpv_id="cpv_0",
            patch=EmbeddedPatch(
                patch_id="patch_3",
                pov_id="cpv_0",
                patch_content_b64="ZHVtbXk=",
            ),
        )
        out_dir = _resolve_patch_job_output_dir(payload)
        logs_dir = out_dir / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)

        (logs_dir / "z.stderr").write_text("last")
        (logs_dir / "a.stdout").write_text("first")

        logs = _collect_patch_verify_logs(payload)
        assert list(logs.keys()) == ["a.stdout"]

    @patch("crsbench.distributed.patch_evaluator_jobs._MAX_LOG_FILE_COUNT", new=2)
    def test_collect_patch_verify_logs_large_set_respects_cap_and_order(
        self, tmp_path: Path
    ) -> None:
        """Large eligible sets should still select deterministic lowest names."""
        payload = PatchJobPayload(
            experiment_name="exp",
            trial_id="trial-5",
            benchmark="mock-bench",
            harness="harness_0",
            cpv_id="cpv_0",
            patch=EmbeddedPatch(
                patch_id="patch_4",
                pov_id="cpv_0",
                patch_content_b64="ZHVtbXk=",
            ),
        )
        out_dir = _resolve_patch_job_output_dir(payload)
        logs_dir = out_dir / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)

        for i in range(200):
            (logs_dir / f"log-{i:04d}.stdout").write_text(str(i))
        (logs_dir / "aaa.stderr").write_text("a")
        (logs_dir / "aab.stdout").write_text("b")

        logs = _collect_patch_verify_logs(payload)
        assert list(logs.keys()) == ["aaa.stderr", "aab.stdout"]
