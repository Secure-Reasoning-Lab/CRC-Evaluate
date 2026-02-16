"""Patch evaluator job execution for distributed patch verification.

This module contains the RQ job functions invoked by evaluator workers
for patch build and verification, plus data structures for job payloads
and results.

Patch builds go to the BUILD queue (multi-CPU), and patch verify jobs
(POV test, unit test) go to the VERIFY queue (1 CPU) with RQ dependency
on the build job. This follows the same dual-queue pattern as POV
verification via evaluator_jobs.py / verify_queue.py.
"""

import base64
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from crsbench.distributed.evaluator_jobs import (
    get_evaluator_benchmarks_root,
    resolve_benchmark_path,
)
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


# =============================================================================
# Embedded Patch (cross-machine transport)
# =============================================================================


@dataclass
class EmbeddedPatch:
    """A patch with its content embedded for cross-machine transport.

    Similar to EmbeddedPov in evaluator_jobs.py, but for diff/patch files.
    Patch content is base64-encoded so it can be serialized to JSON for
    Redis transport.

    Attributes:
        patch_id: Patch identifier
        pov_id: CPV/POV this patch targets
        patch_content_b64: Base64-encoded patch file content
    """

    patch_id: str
    pov_id: str
    patch_content_b64: str

    @classmethod
    def from_file(cls, patch_id: str, pov_id: str, patch_path: Path) -> "EmbeddedPatch":
        """Create from a patch file on disk.

        Args:
            patch_id: Patch identifier
            pov_id: CPV/POV this patch targets
            patch_path: Path to the patch file

        Returns:
            EmbeddedPatch with base64-encoded content
        """
        content = patch_path.read_bytes()
        return cls(
            patch_id=patch_id,
            pov_id=pov_id,
            patch_content_b64=base64.b64encode(content).decode(),
        )

    def write_to(self, dest_path: Path) -> None:
        """Decode base64 content and write patch to disk.

        Args:
            dest_path: Destination file path to write decoded patch content
        """
        content = base64.b64decode(self.patch_content_b64)
        dest_path.write_bytes(content)

    def to_dict(self) -> dict[str, str]:
        return {
            "patch_id": self.patch_id,
            "pov_id": self.pov_id,
            "patch_content_b64": self.patch_content_b64,
        }

    @classmethod
    def from_dict(cls, d: dict[str, str]) -> "EmbeddedPatch":
        return cls(
            patch_id=d["patch_id"],
            pov_id=d["pov_id"],
            patch_content_b64=d["patch_content_b64"],
        )


# =============================================================================
# Job Payload / Result Structures
# =============================================================================


@dataclass
class PatchJobPayload:
    """Payload for patch build and verify jobs via Redis queue.

    Attributes:
        experiment_name: Experiment identifier for queue routing
        trial_id: Trial identifier to correlate results
        benchmark: Benchmark name (evaluator resolves path locally)
        harness: Fuzz harness name
        cpv_id: CPV identifier this patch targets
        patch: Embedded patch with content
        sanitizer: Sanitizer to use for build
        source_mode: Source mode for builds
        use_inc_build: Whether to use incremental build
        enqueued_at: Timestamp when job was enqueued
    """

    experiment_name: str
    trial_id: str
    benchmark: str
    harness: str
    cpv_id: str
    patch: EmbeddedPatch
    sanitizer: str = "address"
    source_mode: str = "pkgs"
    use_inc_build: bool = True
    enqueued_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "experiment_name": self.experiment_name,
            "trial_id": self.trial_id,
            "benchmark": self.benchmark,
            "harness": self.harness,
            "cpv_id": self.cpv_id,
            "patch": self.patch.to_dict(),
            "sanitizer": self.sanitizer,
            "source_mode": self.source_mode,
            "use_inc_build": self.use_inc_build,
            "enqueued_at": self.enqueued_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PatchJobPayload":
        return cls(
            experiment_name=d["experiment_name"],
            trial_id=d["trial_id"],
            benchmark=d["benchmark"],
            harness=d["harness"],
            cpv_id=d["cpv_id"],
            patch=EmbeddedPatch.from_dict(d["patch"]),
            sanitizer=d.get("sanitizer", "address"),
            source_mode=d.get("source_mode", "pkgs"),
            use_inc_build=d.get("use_inc_build", True),
            enqueued_at=d.get("enqueued_at", 0.0),
        )


@dataclass
class PatchBuildResult:
    """Result of a patch build job.

    Attributes:
        trial_id: Trial identifier
        benchmark: Benchmark name
        harness: Harness name
        cpv_id: CPV identifier
        patch_id: Patch identifier
        success: Whether the build succeeded
        variant_name: Built variant name (empty string if build failed)
        error: Error message if build failed
        completed_at: Timestamp when build completed
    """

    trial_id: str
    benchmark: str
    harness: str
    cpv_id: str
    patch_id: str
    success: bool
    variant_name: str = ""
    error: Optional[str] = None
    completed_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "trial_id": self.trial_id,
            "benchmark": self.benchmark,
            "harness": self.harness,
            "cpv_id": self.cpv_id,
            "patch_id": self.patch_id,
            "success": self.success,
            "variant_name": self.variant_name,
            "error": self.error,
            "completed_at": self.completed_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PatchBuildResult":
        return cls(
            trial_id=d["trial_id"],
            benchmark=d["benchmark"],
            harness=d["harness"],
            cpv_id=d["cpv_id"],
            patch_id=d["patch_id"],
            success=d["success"],
            variant_name=d.get("variant_name", ""),
            error=d.get("error"),
            completed_at=d.get("completed_at", 0.0),
        )


@dataclass
class PatchVerifyResult:
    """Result of a patch verify job (POV test + unit test).

    Attributes:
        trial_id: Trial identifier
        benchmark: Benchmark name
        harness: Harness name
        cpv_id: CPV identifier
        patch_id: Patch identifier
        pov_test_passed: Whether POV test passed (None if not run)
        unit_test_passed: Whether unit test passed (None if not run)
        status: Verification status string
        details: Additional details about the verification
        error: Error message if verification failed
        completed_at: Timestamp when verification completed
    """

    trial_id: str
    benchmark: str
    harness: str
    cpv_id: str
    patch_id: str
    pov_test_passed: Optional[bool] = None
    unit_test_passed: Optional[bool] = None
    status: str = ""
    details: str = ""
    error: Optional[str] = None
    completed_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "trial_id": self.trial_id,
            "benchmark": self.benchmark,
            "harness": self.harness,
            "cpv_id": self.cpv_id,
            "patch_id": self.patch_id,
            "pov_test_passed": self.pov_test_passed,
            "unit_test_passed": self.unit_test_passed,
            "status": self.status,
            "details": self.details,
            "error": self.error,
            "completed_at": self.completed_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PatchVerifyResult":
        return cls(
            trial_id=d["trial_id"],
            benchmark=d["benchmark"],
            harness=d["harness"],
            cpv_id=d["cpv_id"],
            patch_id=d["patch_id"],
            pov_test_passed=d.get("pov_test_passed"),
            unit_test_passed=d.get("unit_test_passed"),
            status=d.get("status", ""),
            details=d.get("details", ""),
            error=d.get("error"),
            completed_at=d.get("completed_at", 0.0),
        )


# =============================================================================
# RQ Job Functions
# =============================================================================


def execute_patch_build(payload_dict: dict[str, Any]) -> dict[str, Any]:
    """Execute a patch build job. RQ job function.

    Deserializes the payload, writes the embedded patch content to a temp
    file, creates a BuildPatchVariantJob, and delegates execution to
    ci_jobs.execute_ci_job().

    Args:
        payload_dict: Serialized PatchJobPayload dict

    Returns:
        Serialized PatchBuildResult dict
    """
    from crsbench.distributed import ci_jobs

    payload = PatchJobPayload.from_dict(payload_dict)
    patch = payload.patch

    logger.info(
        f"Building patch {patch.patch_id} for {payload.benchmark}/{payload.cpv_id} "
        f"trial={payload.trial_id}"
    )

    benchmarks_root = get_evaluator_benchmarks_root()
    benchmark_path = resolve_benchmark_path(benchmarks_root, payload.benchmark)

    temp_patch_path: Optional[Path] = None
    try:
        # Write embedded patch content to temp file
        temp_fd, temp_path_str = tempfile.mkstemp(suffix=".diff", prefix="patch_")
        os.close(temp_fd)
        temp_patch_path = Path(temp_path_str)
        patch.write_to(temp_patch_path)

        # Create BuildPatchVariantJob using temp patch file
        from crsbench.benchmark_ci.jobs.flat import BuildPatchVariantJob

        build_job = BuildPatchVariantJob(
            benchmark_path=benchmark_path,
            benchmark_name=payload.benchmark,
            cpv_id=payload.cpv_id,
            patch_id=patch.patch_id,
            patch_path=temp_patch_path,
            harness=payload.harness,
            sanitizer=payload.sanitizer,
            use_inc_build=payload.use_inc_build,
            source_mode=payload.source_mode,
        )

        # Serialize and execute via ci_jobs
        params = ci_jobs.serialize_ci_job(build_job)
        result_dict = ci_jobs.execute_ci_job(params)

        success = result_dict.get("success", False)
        variant_name = result_dict.get("details", {}).get("variant_name", "")
        error = result_dict.get("error")

        return PatchBuildResult(
            trial_id=payload.trial_id,
            benchmark=payload.benchmark,
            harness=payload.harness,
            cpv_id=payload.cpv_id,
            patch_id=patch.patch_id,
            success=success,
            variant_name=variant_name,
            error=error,
            completed_at=time.time(),
        ).to_dict()

    except Exception as e:
        logger.error(f"Patch build failed for {patch.patch_id}: {e}")
        return PatchBuildResult(
            trial_id=payload.trial_id,
            benchmark=payload.benchmark,
            harness=payload.harness,
            cpv_id=payload.cpv_id,
            patch_id=patch.patch_id,
            success=False,
            error=str(e),
            completed_at=time.time(),
        ).to_dict()

    finally:
        if temp_patch_path and temp_patch_path.exists():
            temp_patch_path.unlink()


def execute_patch_verify(payload_dict: dict[str, Any]) -> dict[str, Any]:
    """Execute a patch verify job (POV test + unit test). RQ job function.

    Deserializes the payload, discovers the POV file from the benchmark
    directory, creates PatchPovTestJob and/or PatchUnitTestJob, and
    delegates execution to ci_jobs.execute_ci_job().

    Args:
        payload_dict: Serialized PatchJobPayload dict

    Returns:
        Serialized PatchVerifyResult dict
    """
    from crsbench.distributed import ci_jobs

    payload = PatchJobPayload.from_dict(payload_dict)
    patch = payload.patch

    logger.info(
        f"Verifying patch {patch.patch_id} for {payload.benchmark}/{payload.cpv_id} "
        f"trial={payload.trial_id}"
    )

    benchmarks_root = get_evaluator_benchmarks_root()
    benchmark_path = resolve_benchmark_path(benchmarks_root, payload.benchmark)

    # Discover POV path from benchmark directory
    pov_path = (
        benchmark_path
        / ".aixcc"
        / payload.harness
        / payload.cpv_id
        / "blobs"
        / "pov_0.blob"
    )
    if not pov_path.exists():
        error_msg = f"POV file not found at {pov_path}"
        logger.error(error_msg)
        return PatchVerifyResult(
            trial_id=payload.trial_id,
            benchmark=payload.benchmark,
            harness=payload.harness,
            cpv_id=payload.cpv_id,
            patch_id=patch.patch_id,
            status="error",
            details=error_msg,
            error=error_msg,
            completed_at=time.time(),
        ).to_dict()

    # Build the patch build job ID (format matches BuildPatchVariantJob.job_id)
    build_patch_job_id = (
        f"build-patch/{payload.benchmark}/{payload.cpv_id}/{patch.patch_id}"
    )

    pov_test_passed: Optional[bool] = None
    unit_test_passed: Optional[bool] = None
    status = "error"
    details = ""
    error: Optional[str] = None

    try:
        # Run POV test
        from crsbench.benchmark_ci.jobs.flat import PatchPovTestJob

        pov_test_job = PatchPovTestJob(
            benchmark_path=benchmark_path,
            benchmark_name=payload.benchmark,
            cpv_id=payload.cpv_id,
            patch_id=patch.patch_id,
            harness=payload.harness,
            pov_path=pov_path,
            build_patch_job_id=build_patch_job_id,
            source_mode=payload.source_mode,
        )
        pov_params = ci_jobs.serialize_ci_job(pov_test_job)
        pov_result = ci_jobs.execute_ci_job(pov_params)
        pov_test_passed = pov_result.get("success", False)

        # Run unit test
        from crsbench.benchmark_ci.jobs.flat import PatchUnitTestJob

        unit_test_job = PatchUnitTestJob(
            benchmark_path=benchmark_path,
            benchmark_name=payload.benchmark,
            cpv_id=payload.cpv_id,
            patch_id=patch.patch_id,
            harness=payload.harness,
            build_patch_job_id=build_patch_job_id,
            source_mode=payload.source_mode,
        )
        unit_params = ci_jobs.serialize_ci_job(unit_test_job)
        unit_result = ci_jobs.execute_ci_job(unit_params)
        unit_test_passed = unit_result.get("success", False)

        # Determine overall status
        if pov_test_passed and unit_test_passed:
            status = "valid"
            details = "Patch passes both POV and unit tests"
        elif not pov_test_passed:
            status = "pov_still_triggers"
            details = pov_result.get("error", "POV still crashes with patch")
        else:
            status = "test_failed"
            details = unit_result.get("error", "Unit tests failed with patch")

    except Exception as e:
        logger.error(f"Patch verify failed for {patch.patch_id}: {e}")
        error = str(e)
        status = "error"
        details = str(e)

    return PatchVerifyResult(
        trial_id=payload.trial_id,
        benchmark=payload.benchmark,
        harness=payload.harness,
        cpv_id=payload.cpv_id,
        patch_id=patch.patch_id,
        pov_test_passed=pov_test_passed,
        unit_test_passed=unit_test_passed,
        status=status,
        details=details,
        error=error,
        completed_at=time.time(),
    ).to_dict()
