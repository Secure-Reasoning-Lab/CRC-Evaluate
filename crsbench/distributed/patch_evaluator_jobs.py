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
import re
import shutil
import tempfile
import time
from bisect import insort
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from crsbench.distributed.evaluator_jobs import (
    get_evaluator_benchmarks_root,
    resolve_benchmark_path,
)
from crsbench.utils.logger import get_logger
from crsbench.validation.ground_truth_paths import GroundTruthPaths

logger = get_logger(__name__)

_MAX_LOG_FILE_BYTES = 256 * 1024
_MAX_LOG_TOTAL_BYTES = 1024 * 1024
_MAX_LOG_FILE_COUNT = 64


def _truncate_text_to_utf8_bytes(text: str, max_bytes: int) -> str:
    """Truncate text to fit max UTF-8 bytes without breaking characters."""
    if max_bytes <= 0:
        return ""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _sanitize_path_token(value: str) -> str:
    """Sanitize arbitrary strings for filesystem-safe path segments."""
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return safe or "unknown"


def _resolve_patch_job_output_dir(payload: "PatchJobPayload") -> Path:
    """Resolve deterministic per-job output directory for distributed patch jobs."""
    root = Path(tempfile.gettempdir()) / "crsbench-distributed-patch-jobs"
    return (
        root
        / _sanitize_path_token(payload.experiment_name)
        / _sanitize_path_token(payload.trial_id)
        / _sanitize_path_token(payload.benchmark)
        / _sanitize_path_token(payload.harness)
        / _sanitize_path_token(payload.cpv_id)
        / _sanitize_path_token(payload.patch.patch_id)
    )


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
        verify_variants: Whether to verify against all POV variants per CPV
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
    verify_variants: bool = False
    test_mode: str = "FULL"
    use_inc_build: bool = False
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
            "verify_variants": self.verify_variants,
            "test_mode": self.test_mode,
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
            verify_variants=d.get("verify_variants", False),
            test_mode=d.get("test_mode", "FULL"),
            use_inc_build=d.get("use_inc_build", False),
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
        security_verdict: PASS/FAIL security verdict
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
    security_verdict: str = "FAIL"
    details: str = ""
    error: Optional[str] = None
    completed_at: float = 0.0
    logs: dict[str, str] = field(default_factory=dict)

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
            "security_verdict": self.security_verdict,
            "details": self.details,
            "error": self.error,
            "completed_at": self.completed_at,
            "logs": self.logs,
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
            security_verdict=d.get("security_verdict", "FAIL"),
            details=d.get("details", ""),
            error=d.get("error"),
            completed_at=d.get("completed_at", 0.0),
            logs=d.get("logs", {}),
        )


def _collect_patch_verify_logs(payload: "PatchJobPayload") -> dict[str, str]:
    """Collect bounded patch verify stream logs from worker-local output dir."""
    output_dir = _resolve_patch_job_output_dir(payload)
    logs_dir = output_dir / "logs"
    if not logs_dir.exists():
        return {}

    collected: dict[str, str] = {}
    remaining = _MAX_LOG_TOTAL_BYTES
    selected_logs: list[tuple[str, Path]] = []
    for path in logs_dir.iterdir():
        if not path.is_file() or path.suffix not in {".stdout", ".stderr"}:
            continue
        insort(selected_logs, (path.name, path))
        if len(selected_logs) > _MAX_LOG_FILE_COUNT:
            selected_logs.pop()

    for _, path in selected_logs:
        if remaining <= 0:
            break
        # Reserve room for filename overhead in serialized payload.
        name_overhead = len(path.name.encode("utf-8")) + 4
        if remaining <= name_overhead:
            break
        budget = min(_MAX_LOG_FILE_BYTES, remaining - name_overhead)
        try:
            with path.open("rb") as f:
                content = f.read(budget + 1)
        except Exception as e:
            logger.warning(f"Failed reading patch log {path}: {e}")
            continue

        truncated = len(content) > budget
        chunk = content[:budget] if truncated else content
        text = chunk.decode("utf-8", errors="replace")
        if truncated:
            text += "\n[truncated additional bytes]"
        text = _truncate_text_to_utf8_bytes(text, remaining - name_overhead)
        if not text:
            break
        collected[path.name] = text
        remaining -= len(text.encode("utf-8")) + name_overhead
    return collected


def _cleanup_patch_job_output_dir(payload: "PatchJobPayload") -> None:
    """Remove temporary worker-local patch job artifacts."""
    output_dir = _resolve_patch_job_output_dir(payload)
    if output_dir.exists():
        try:
            shutil.rmtree(output_dir)
        except Exception as e:
            logger.warning(f"Failed to cleanup patch job output dir {output_dir}: {e}")


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
        params["output_dir"] = str(_resolve_patch_job_output_dir(payload))
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


def _resolve_patch_variant_name(
    payload: PatchJobPayload, benchmark_path: Path
) -> Optional[str]:
    """Resolve patched variant name for post-verify Docker image cleanup."""
    try:
        from crsbench.builder.types import BuildConfig, VariantType
        from crsbench.evaluation.verification.pov import VerificationEngine
        from crsbench.utils.run_helper import get_oss_fuzz_root

        oss_fuzz_path = Path(get_oss_fuzz_root())
        engine = VerificationEngine(oss_fuzz_path, source_mode=payload.source_mode)
        adapter = engine.load_adapter(benchmark_path)
        if not adapter:
            return None

        sanitizer = adapter.get_cpv_sanitizer(payload.harness, payload.cpv_id)
        config = BuildConfig(
            benchmark_name=payload.benchmark,
            benchmark_path=benchmark_path,
            variant_type=VariantType.PATCHED,
            mode=adapter.get_mode(),
            sanitizer=sanitizer,
            language=adapter.lang,
            commit=adapter.get_ref_commit() or adapter.get_base_commit(),
            main_repo=adapter.main_repo,
            patch_id=payload.patch.patch_id,
            pov_id=payload.cpv_id,
        )
        return config.variant_name
    except Exception as e:
        logger.debug(
            "Failed to resolve patched variant name for cleanup "
            f"({payload.benchmark}/{payload.harness}/{payload.cpv_id}): {e}"
        )
        return None


def _cleanup_patch_variant_artifacts(variant_name: Optional[str]) -> None:
    """Remove patched variant artifacts after patch verification.

    Cleans both the patch variant and its unittest variant:
    - Docker images
    - build/out + build/work artifacts
    - isolated source dirs and project symlinks
    """
    if not variant_name:
        return
    try:
        from crsbench.builder.infrastructure import OSSFuzzInfrastructure
        from crsbench.utils.run_helper import get_oss_fuzz_root

        infra = OSSFuzzInfrastructure(Path(get_oss_fuzz_root()))
        for name in (variant_name, f"{variant_name}-unittest"):
            infra.cleanup_docker_images(name)
            infra.cleanup_variant(name)
        logger.info(f"Removed patch verification artifacts for variant={variant_name}")
    except Exception as e:
        logger.warning(
            f"Failed to remove patch verification artifacts for variant={variant_name}: {e}"
        )


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

    cleanup_variant_name = _resolve_patch_variant_name(payload, benchmark_path)
    try:
        output_dir = _resolve_patch_job_output_dir(payload)
        patch_file = output_dir / "patches" / f"{patch.patch_id}.diff"
        patch_file.parent.mkdir(parents=True, exist_ok=True)
        patch.write_to(patch_file)

        # Discover POV paths from benchmark directory
        blobs_dir = GroundTruthPaths(benchmark_path).cpv_blobs_dir(
            payload.harness, payload.cpv_id
        )
        pov_paths = list(blobs_dir.glob("pov_*.blob")) if blobs_dir.exists() else []

        def _extract_pov_num(path: Path) -> int:
            try:
                return int(path.stem.split("_")[1])
            except (IndexError, ValueError):
                return 999

        pov_paths.sort(key=_extract_pov_num)

        if not pov_paths:
            error_msg = f"No POV files found under {blobs_dir}"
            logger.error(error_msg)
            return PatchVerifyResult(
                trial_id=payload.trial_id,
                benchmark=payload.benchmark,
                harness=payload.harness,
                cpv_id=payload.cpv_id,
                patch_id=patch.patch_id,
                status="error",
                security_verdict="FAIL",
                details=error_msg,
                error=error_msg,
                completed_at=time.time(),
                logs=_collect_patch_verify_logs(payload),
            ).to_dict()

        # Build the patch build job ID (format matches BuildPatchVariantJob.job_id)
        build_patch_job_id = (
            f"build-patch/{payload.benchmark}/{payload.cpv_id}/{patch.patch_id}"
        )

        pov_test_passed: Optional[bool] = None
        unit_test_passed: Optional[bool] = None
        status = "error"
        security_verdict = "FAIL"
        details = ""
        error: Optional[str] = None

        if payload.verify_variants:
            # Variant mode: PatchVariantTestJob runs full patch verification
            # (all pov_* variants + unit tests) in one job.
            from crsbench.benchmark_ci.jobs.flat import PatchVariantTestJob

            variant_test_job = PatchVariantTestJob(
                benchmark_path=benchmark_path,
                benchmark_name=payload.benchmark,
                cpv_id=payload.cpv_id,
                patch_id=patch.patch_id,
                harness=payload.harness,
                pov_paths=pov_paths,
                test_mode=payload.test_mode,
                patch_path_override=patch_file,
                build_patch_job_id=build_patch_job_id,
                source_mode=payload.source_mode,
            )
            variant_params = ci_jobs.serialize_ci_job(variant_test_job)
            variant_params["output_dir"] = str(output_dir)
            variant_result = ci_jobs.execute_ci_job(variant_params)
            details_dict = variant_result.get("details", {}) or {}
            pov_test_passed = details_dict.get("pov_test_passed")
            unit_test_passed = details_dict.get("unit_tests_passed")

            if variant_result.get("success", False):
                status = "valid"
                security_verdict = "PASS"
                details = "Patch passes all POV variants and unit tests"
            elif pov_test_passed is False:
                status = "pov_still_triggers"
                security_verdict = "FAIL"
                details = variant_result.get(
                    "error", "One or more POV variants still crash"
                )
            elif unit_test_passed is False:
                status = "test_failed"
                security_verdict = "FAIL"
                details = variant_result.get("error", "Unit tests failed with patch")
            else:
                status = "error"
                security_verdict = "FAIL"
                details = variant_result.get(
                    "error", "Patch variant verification failed"
                )
        else:
            # Legacy mode: verify against single pov_0 only, then run unit tests.
            from crsbench.benchmark_ci.jobs.flat import PatchPovTestJob

            pov_test_job = PatchPovTestJob(
                benchmark_path=benchmark_path,
                benchmark_name=payload.benchmark,
                cpv_id=payload.cpv_id,
                patch_id=patch.patch_id,
                harness=payload.harness,
                pov_path=pov_paths[0],
                patch_path_override=patch_file,
                build_patch_job_id=build_patch_job_id,
                source_mode=payload.source_mode,
            )
            pov_params = ci_jobs.serialize_ci_job(pov_test_job)
            pov_params["output_dir"] = str(output_dir)
            pov_result = ci_jobs.execute_ci_job(pov_params)
            pov_test_passed = pov_result.get("success", False)

            from crsbench.benchmark_ci.jobs.flat import PatchUnitTestJob

            unit_test_job = PatchUnitTestJob(
                benchmark_path=benchmark_path,
                benchmark_name=payload.benchmark,
                cpv_id=payload.cpv_id,
                patch_id=patch.patch_id,
                harness=payload.harness,
                test_mode=payload.test_mode,
                patch_path_override=patch_file,
                build_patch_job_id=build_patch_job_id,
                source_mode=payload.source_mode,
            )
            unit_params = ci_jobs.serialize_ci_job(unit_test_job)
            unit_params["output_dir"] = str(output_dir)
            unit_result = ci_jobs.execute_ci_job(unit_params)
            unit_test_passed = unit_result.get("success", False)

            # Determine overall status
            if pov_test_passed and unit_test_passed:
                status = "valid"
                security_verdict = "PASS"
                details = "Patch passes both POV and unit tests"
            elif not pov_test_passed:
                status = "pov_still_triggers"
                security_verdict = "FAIL"
                details = pov_result.get("error", "POV still crashes with patch")
            else:
                status = "test_failed"
                security_verdict = "FAIL"
                details = unit_result.get("error", "Unit tests failed with patch")

        return PatchVerifyResult(
            trial_id=payload.trial_id,
            benchmark=payload.benchmark,
            harness=payload.harness,
            cpv_id=payload.cpv_id,
            patch_id=patch.patch_id,
            pov_test_passed=pov_test_passed,
            unit_test_passed=unit_test_passed,
            status=status,
            security_verdict=security_verdict,
            details=details,
            error=error,
            completed_at=time.time(),
            logs=_collect_patch_verify_logs(payload),
        ).to_dict()

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
            pov_test_passed=None,
            unit_test_passed=None,
            status=status,
            security_verdict="FAIL",
            details=details,
            error=error,
            completed_at=time.time(),
            logs=_collect_patch_verify_logs(payload),
        ).to_dict()
    finally:
        _cleanup_patch_variant_artifacts(cleanup_variant_name)
        _cleanup_patch_job_output_dir(payload)
