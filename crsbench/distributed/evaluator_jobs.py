"""Evaluator job execution for distributed POV verification.

This module contains the job function invoked by RQ when a verification job
is dequeued, plus the data structures for job payloads and results.
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
class VerificationJobPayload:
    """Payload for a verification job enqueued to Redis.

    Attributes:
        experiment_name: Experiment identifier for queue routing
        trial_id: Trial identifier to correlate results back
        benchmark: Benchmark name (evaluator resolves path locally)
        harness: Fuzz harness name
        povs: List of POVs with embedded content
        enqueued_at: Timestamp when job was enqueued
    """

    experiment_name: str
    trial_id: str
    benchmark: str
    harness: str
    povs: list[EmbeddedPov]
    enqueued_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "experiment_name": self.experiment_name,
            "trial_id": self.trial_id,
            "benchmark": self.benchmark,
            "harness": self.harness,
            "povs": [p.to_dict() for p in self.povs],
            "enqueued_at": self.enqueued_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "VerificationJobPayload":
        return cls(
            experiment_name=d["experiment_name"],
            trial_id=d["trial_id"],
            benchmark=d["benchmark"],
            harness=d["harness"],
            povs=[EmbeddedPov.from_dict(p) for p in d["povs"]],
            enqueued_at=d["enqueued_at"],
        )


@dataclass
class PovVerdict:
    """Verdict for a single POV.

    Attributes:
        pov_id: POV identifier
        triggered_bug: Whether the POV triggered any bug
        cpv_matches: Which CPVs this POV matches (if any)
        variant_results: Per-variant crash/no-crash results
        error: Error message if verification failed
    """

    pov_id: str
    triggered_bug: bool
    cpv_matches: list[str] = field(default_factory=list)
    variant_results: dict[str, bool] = field(default_factory=dict)
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "pov_id": self.pov_id,
            "triggered_bug": self.triggered_bug,
            "cpv_matches": self.cpv_matches,
            "variant_results": self.variant_results,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PovVerdict":
        return cls(
            pov_id=d["pov_id"],
            triggered_bug=d["triggered_bug"],
            cpv_matches=d.get("cpv_matches", []),
            variant_results=d.get("variant_results", {}),
            error=d.get("error"),
        )


@dataclass
class VerificationResult:
    """Result of a verification job.

    Attributes:
        trial_id: Trial identifier
        benchmark: Benchmark name
        harness: Harness name
        verdicts: Per-POV verdicts
        completed_at: Timestamp when verification completed
    """

    trial_id: str
    benchmark: str
    harness: str
    verdicts: list[PovVerdict]
    completed_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "trial_id": self.trial_id,
            "benchmark": self.benchmark,
            "harness": self.harness,
            "verdicts": [v.to_dict() for v in self.verdicts],
            "completed_at": self.completed_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "VerificationResult":
        return cls(
            trial_id=d["trial_id"],
            benchmark=d["benchmark"],
            harness=d["harness"],
            verdicts=[PovVerdict.from_dict(v) for v in d["verdicts"]],
            completed_at=d["completed_at"],
        )


# =============================================================================
# Job Function (invoked by RQ)
# =============================================================================

# Module-level cache populated by evaluator.py at startup
_built_results: dict[str, dict] = {}
_verification_engine: Optional[Any] = None


def set_build_cache(
    engine: Any,
    built_results: dict[str, dict],
) -> None:
    """Set the module-level build cache (called by evaluator at startup).

    Args:
        engine: VerificationEngine instance with oss_fuzz_path set
        built_results: Pre-built results keyed by benchmark name
    """
    global _built_results, _verification_engine  # noqa: PLW0603
    _verification_engine = engine
    _built_results = built_results


def verify_povs(payload_dict: dict[str, Any]) -> dict[str, Any]:
    """Verify POVs from a job payload.

    This is the RQ job function that evaluator workers execute.
    It uses pre-built variant images from the module-level cache.

    Args:
        payload_dict: Serialized VerificationJobPayload dict

    Returns:
        Serialized VerificationResult dict
    """
    from crsbench.evaluation.verification.models import (
        PovVerificationRequest,
        PovVerificationStatus,
    )

    payload = VerificationJobPayload.from_dict(payload_dict)
    logger.info(
        f"Verifying {len(payload.povs)} POVs for trial {payload.trial_id} "
        f"benchmark {payload.benchmark}"
    )

    # Check that we have builds for this benchmark
    if payload.benchmark not in _built_results:
        error_msg = (
            f"No built variants for benchmark '{payload.benchmark}'. "
            "Evaluator was not configured with this benchmark."
        )
        logger.error(error_msg)
        verdicts = [
            PovVerdict(
                pov_id=pov.pov_id,
                triggered_bug=False,
                error=error_msg,
            )
            for pov in payload.povs
        ]
        return VerificationResult(
            trial_id=payload.trial_id,
            benchmark=payload.benchmark,
            harness=payload.harness,
            verdicts=verdicts,
            completed_at=time.time(),
        ).to_dict()

    # Get pre-built results and adapter for this benchmark
    build_results = _built_results[payload.benchmark]

    # Load adapter for this benchmark
    engine = _verification_engine
    if engine is None:
        error_msg = "VerificationEngine not initialized"
        logger.error(error_msg)
        return VerificationResult(
            trial_id=payload.trial_id,
            benchmark=payload.benchmark,
            harness=payload.harness,
            verdicts=[
                PovVerdict(pov_id=p.pov_id, triggered_bug=False, error=error_msg)
                for p in payload.povs
            ],
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
        return VerificationResult(
            trial_id=payload.trial_id,
            benchmark=payload.benchmark,
            harness=payload.harness,
            verdicts=[
                PovVerdict(pov_id=p.pov_id, triggered_bug=False, error=error_msg)
                for p in payload.povs
            ],
            completed_at=time.time(),
        ).to_dict()

    # Verify each POV
    verdicts = []
    for pov in payload.povs:
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

            # Convert PovVerificationResult to PovVerdict
            verdict = PovVerdict(
                pov_id=pov.pov_id,
                triggered_bug=result.status == PovVerificationStatus.CPV,
                cpv_matches=result.cpv_matched,
                variant_results={},
                error=result.details
                if result.status == PovVerificationStatus.ERROR
                else None,
            )
            verdicts.append(verdict)

            logger.info(
                f"  POV {pov.pov_id}: {result.status.value} "
                f"(CPVs: {result.cpv_matched})"
            )

        except Exception as e:
            logger.error(f"  POV {pov.pov_id}: verification failed: {e}")
            verdicts.append(
                PovVerdict(
                    pov_id=pov.pov_id,
                    triggered_bug=False,
                    error=str(e),
                )
            )

    return VerificationResult(
        trial_id=payload.trial_id,
        benchmark=payload.benchmark,
        harness=payload.harness,
        verdicts=verdicts,
        completed_at=time.time(),
    ).to_dict()


# =============================================================================
# Per-POV Verification (v2.2 — replaces batch verify_povs)
# =============================================================================


@dataclass
class SinglePovPayload:
    """Payload for verifying a single POV via Redis queue.

    Lighter-weight than VerificationJobPayload: one POV per job for
    finer granularity, better parallelism, and individual retry.

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

    Per-POV counterpart to verify_povs(). Processes one POV at a time for
    finer granularity, better parallelism across evaluator workers, and
    individual retry without re-verifying an entire batch.

    Uses the same module-level cache as verify_povs() for pre-built variants.

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

    # Check that we have builds for this benchmark
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
            verdict=PovVerdict(pov_id=pov.pov_id, triggered_bug=False, error=error_msg),
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
            verdict=PovVerdict(pov_id=pov.pov_id, triggered_bug=False, error=error_msg),
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
            verdict=PovVerdict(pov_id=pov.pov_id, triggered_bug=False, error=error_msg),
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

        verdict = PovVerdict(
            pov_id=pov.pov_id,
            triggered_bug=result.status == PovVerificationStatus.CPV,
            cpv_matches=result.cpv_matched,
            variant_results={},
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
            error=str(e),
        )

    return SinglePovResult(
        trial_id=payload.trial_id,
        benchmark=payload.benchmark,
        harness=payload.harness,
        verdict=verdict,
        completed_at=time.time(),
    ).to_dict()
