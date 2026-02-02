"""Evaluator job execution for distributed POV verification.

This module contains the RQ job functions invoked by evaluator workers,
plus data structures for job payloads and results.

verify_single_pov() is the per-POV verification function — one POV per job
for fine-grained parallelism across evaluator workers.
"""

import base64
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


# =============================================================================
# Job Payload / Result Structures
# =============================================================================


@dataclass
class EmbeddedPov:
    """A POV with its content embedded for cross-machine transport.

    Attributes:
        pov_id: Filename or identifier for this POV
        pov_data_b64: Base64-encoded raw POV file content
    """

    pov_id: str
    pov_data_b64: str

    @classmethod
    def from_bytes(cls, pov_id: str, pov_data: bytes) -> "EmbeddedPov":
        """Create from raw bytes."""
        return cls(pov_id=pov_id, pov_data_b64=base64.b64encode(pov_data).decode())

    def to_bytes(self) -> bytes:
        """Decode POV data back to raw bytes."""
        return base64.b64decode(self.pov_data_b64)

    def to_dict(self) -> dict[str, str]:
        return {"pov_id": self.pov_id, "pov_data_b64": self.pov_data_b64}

    @classmethod
    def from_dict(cls, d: dict[str, str]) -> "EmbeddedPov":
        return cls(pov_id=d["pov_id"], pov_data_b64=d["pov_data_b64"])


@dataclass
class PovVerdict:
    """Verdict for a single POV.

    Attributes:
        pov_id: POV identifier
        triggered_bug: Whether the POV triggered any bug
        status: Verification status string (PovVerificationStatus value)
        cpv_matches: Which CPVs this POV matches (if any)
        variant_results: Per-variant crash/no-crash results
        crash_logs: Per-variant crash log content
        error: Error message if verification failed
    """

    pov_id: str
    triggered_bug: bool
    status: str = "not_vulnerable"
    cpv_matches: list[str] = field(default_factory=list)
    variant_results: dict[str, bool] = field(default_factory=dict)
    crash_logs: dict[str, str] = field(default_factory=dict)
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "pov_id": self.pov_id,
            "triggered_bug": self.triggered_bug,
            "status": self.status,
            "cpv_matches": self.cpv_matches,
            "variant_results": self.variant_results,
            "crash_logs": self.crash_logs,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PovVerdict":
        return cls(
            pov_id=d["pov_id"],
            triggered_bug=d["triggered_bug"],
            status=d.get("status", "not_vulnerable"),
            cpv_matches=d.get("cpv_matches", []),
            variant_results=d.get("variant_results", {}),
            crash_logs=d.get("crash_logs", {}),
            error=d.get("error"),
        )


# =============================================================================
# Module-level engine (set by evaluator at startup)
# =============================================================================

_built_results: dict[str, dict] = {}
_verification_engine: Optional[Any] = None


def set_engine(engine: Any) -> None:
    """Set the verification engine (called by evaluator at startup).

    Build results are populated lazily via _try_lazy_load_builds() when
    verify jobs arrive for a benchmark.

    Args:
        engine: VerificationEngine instance with oss_fuzz_path set
    """
    global _verification_engine  # noqa: PLW0603
    _verification_engine = engine


def _try_lazy_load_builds(benchmark_name: str) -> None:
    """Attempt to lazily load build results for a benchmark.

    When builds execute on the same evaluator machine before verify jobs
    (build queue has priority), Docker images are already on disk.
    ``engine.get_or_build_results()`` finds them without rebuilding.

    Args:
        benchmark_name: Name of the benchmark to load builds for
    """
    engine = _verification_engine
    if engine is None:
        return

    benchmarks_root = Path(
        os.environ.get("CRSBENCH_EVALUATOR_BENCHMARKS_ROOT", "benchmarks")
    )
    benchmark_path = benchmarks_root / benchmark_name

    if not benchmark_path.exists():
        logger.debug(f"Lazy load skipped: {benchmark_path} does not exist")
        return

    adapter = engine.load_adapter(benchmark_path)
    if adapter is None:
        logger.debug(f"Lazy load skipped: adapter not found for {benchmark_name}")
        return

    try:
        results = engine.get_or_build_results(adapter)
        _built_results[benchmark_name] = results
        logger.info(
            f"Lazy loaded build results for {benchmark_name} ({len(results)} variants)"
        )
    except Exception as e:
        logger.warning(f"Lazy load failed for {benchmark_name}: {e}")


# =============================================================================
# Per-POV Verification (RQ job function)
# =============================================================================


@dataclass
class SinglePovPayload:
    """Payload for verifying a single POV via Redis queue.

    Lighter-weight than batch payloads: one POV per job for finer
    granularity, better parallelism, and individual retry.

    Attributes:
        experiment_name: Experiment identifier for queue routing
        trial_id: Trial identifier to correlate results
        benchmark: Benchmark name (evaluator resolves path locally)
        harness: Fuzz harness name
        pov: Single embedded POV with content
        enqueued_at: Timestamp when job was enqueued
    """

    experiment_name: str
    trial_id: str
    benchmark: str
    harness: str
    pov: EmbeddedPov
    enqueued_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "experiment_name": self.experiment_name,
            "trial_id": self.trial_id,
            "benchmark": self.benchmark,
            "harness": self.harness,
            "pov": self.pov.to_dict(),
            "enqueued_at": self.enqueued_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SinglePovPayload":
        return cls(
            experiment_name=d["experiment_name"],
            trial_id=d["trial_id"],
            benchmark=d["benchmark"],
            harness=d["harness"],
            pov=EmbeddedPov.from_dict(d["pov"]),
            enqueued_at=d["enqueued_at"],
        )


@dataclass
class SinglePovResult:
    """Result of verifying a single POV.

    Attributes:
        trial_id: Trial identifier
        benchmark: Benchmark name
        harness: Harness name
        verdict: Single POV verdict
        completed_at: Timestamp when verification completed
    """

    trial_id: str
    benchmark: str
    harness: str
    verdict: PovVerdict
    completed_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "trial_id": self.trial_id,
            "benchmark": self.benchmark,
            "harness": self.harness,
            "verdict": self.verdict.to_dict(),
            "completed_at": self.completed_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SinglePovResult":
        return cls(
            trial_id=d["trial_id"],
            benchmark=d["benchmark"],
            harness=d["harness"],
            verdict=PovVerdict.from_dict(d["verdict"]),
            completed_at=d["completed_at"],
        )


def verify_single_pov(payload_dict: dict[str, Any]) -> dict[str, Any]:
    """Verify a single POV. RQ job function.

    Processes one POV at a time for finer granularity, better parallelism
    across evaluator workers, and individual retry without re-verifying
    an entire batch.

    Uses module-level engine and lazy-loaded build cache.

    Args:
        payload_dict: Serialized SinglePovPayload dict

    Returns:
        Serialized SinglePovResult dict
    """
    from crsbench.evaluation.verification.models import (
        PovVerificationRequest,
        PovVerificationStatus,
    )

    payload = SinglePovPayload.from_dict(payload_dict)
    pov = payload.pov

    logger.info(
        f"Verifying POV {pov.pov_id} for trial {payload.trial_id} "
        f"benchmark {payload.benchmark}"
    )

    # Check that we have builds for this benchmark; try lazy load if missing
    if payload.benchmark not in _built_results:
        _try_lazy_load_builds(payload.benchmark)

    if payload.benchmark not in _built_results:
        error_msg = (
            f"No built variants for benchmark '{payload.benchmark}'. "
            "Evaluator was not configured with this benchmark."
        )
        logger.error(error_msg)
        return SinglePovResult(
            trial_id=payload.trial_id,
            benchmark=payload.benchmark,
            harness=payload.harness,
            verdict=PovVerdict(pov_id=pov.pov_id, triggered_bug=False, status="error", error=error_msg),
            completed_at=time.time(),
        ).to_dict()

    build_results = _built_results[payload.benchmark]

    engine = _verification_engine
    if engine is None:
        error_msg = "VerificationEngine not initialized"
        logger.error(error_msg)
        return SinglePovResult(
            trial_id=payload.trial_id,
            benchmark=payload.benchmark,
            harness=payload.harness,
            verdict=PovVerdict(pov_id=pov.pov_id, triggered_bug=False, status="error", error=error_msg),
            completed_at=time.time(),
        ).to_dict()

    # Resolve benchmark path
    benchmarks_root = Path(
        os.environ.get("CRSBENCH_EVALUATOR_BENCHMARKS_ROOT", "benchmarks")
    )
    benchmark_path = benchmarks_root / payload.benchmark
    adapter = engine.load_adapter(benchmark_path)

    if adapter is None:
        error_msg = f"Failed to load adapter for benchmark '{payload.benchmark}'"
        logger.error(error_msg)
        return SinglePovResult(
            trial_id=payload.trial_id,
            benchmark=payload.benchmark,
            harness=payload.harness,
            verdict=PovVerdict(pov_id=pov.pov_id, triggered_bug=False, status="error", error=error_msg),
            completed_at=time.time(),
        ).to_dict()

    # Verify the single POV
    try:
        pov_data = pov.to_bytes()
        request = PovVerificationRequest(
            pov_data=pov_data,
            harness=payload.harness,
            benchmark=payload.benchmark,
            pov_id=pov.pov_id,
        )

        result = engine.verify_pov(
            request=request,
            adapter=adapter,
            build_results=build_results,
        )

        crash_logs: dict[str, str] = {}
        if result.crash_info and "logs" in result.crash_info:
            crash_logs = result.crash_info["logs"]

        verdict = PovVerdict(
            pov_id=pov.pov_id,
            triggered_bug=result.status == PovVerificationStatus.CPV,
            status=result.status.value,
            cpv_matches=result.cpv_matched,
            variant_results={},
            crash_logs=crash_logs,
            error=result.details
            if result.status == PovVerificationStatus.ERROR
            else None,
        )

        logger.info(
            f"  POV {pov.pov_id}: {result.status.value} (CPVs: {result.cpv_matched})"
        )

    except Exception as e:
        logger.error(f"  POV {pov.pov_id}: verification failed: {e}")
        verdict = PovVerdict(
            pov_id=pov.pov_id,
            triggered_bug=False,
            status="error",
            error=str(e),
        )

    return SinglePovResult(
        trial_id=payload.trial_id,
        benchmark=payload.benchmark,
        harness=payload.harness,
        verdict=verdict,
        completed_at=time.time(),
    ).to_dict()
