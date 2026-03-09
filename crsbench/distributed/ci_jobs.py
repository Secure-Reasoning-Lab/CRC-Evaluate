"""Distributed CI verify/test job execution.

Provides RQ-compatible functions for executing CI verify and test jobs
remotely via Redis workers. Used by ``crsbench benchmark ci all`` to run verify/test
jobs on evaluator machines where Docker images exist.

Pattern: Same as build_jobs.py — serialize → enqueue → execute on evaluator → poll.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, cast

from crsbench.utils.logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    import rq
    import rq.job
    import rq.registry

    from crsbench.builder import BuildResult

logger = get_logger(__name__)

# Job class names that we support serializing/deserializing
_SUPPORTED_JOB_TYPES = frozenset(
    {
        "BuildSingleVariantJob",
        "VerifyCpvPovJob",
        "VerifyCpvVarJob",
        "PatchPovTestJob",
        "PatchVarTestJob",
        "PatchVariantTestJob",
        "PatchUnitTestJob",
        "FlatCollectCoverageJob",
        "BuildPatchVariantJob",
        "PrepareIncImageJob",
    }
)


@dataclass
class InfraMissingBuildContextError(RuntimeError):
    """Raised when verify/test jobs cannot load required build context."""

    benchmark: str
    build_job_ids_requested: list[str]
    missing_build_job_ids: list[str]
    available_variants: list[str]
    source_mode: str
    use_inc_build: bool
    error_code: str = "infra_missing_build_context"

    def __str__(self) -> str:
        missing = ", ".join(self.missing_build_job_ids) or "<none>"
        return (
            f"{self.error_code}: benchmark={self.benchmark}, "
            f"missing_build_job_ids=[{missing}]"
        )

    def to_details(self) -> dict[str, Any]:
        return {
            "error_code": self.error_code,
            "benchmark": self.benchmark,
            "build_job_ids_requested": self.build_job_ids_requested,
            "missing_build_job_ids": self.missing_build_job_ids,
            "available_variants": self.available_variants,
            "source_mode": self.source_mode,
            "use_inc_build": self.use_inc_build,
            "retryable": False,
        }


def serialize_ci_job(job: Any) -> dict[str, Any]:
    """Serialize a flat.py CI job for RQ enqueue.

    Converts Path objects to strings so the payload is JSON-safe
    for Redis transport.

    Args:
        job: Any flat.py Job instance (verify, patch, coverage)

    Returns:
        Dict suitable for passing to ``execute_ci_job()``
    """
    cls_name = type(job).__name__
    if cls_name not in _SUPPORTED_JOB_TYPES:
        raise ValueError(f"Unsupported job type for serialization: {cls_name}")

    params: dict[str, Any] = {"_job_class": cls_name}

    # Serialize dataclass fields based on job type
    if cls_name == "BuildSingleVariantJob":
        params.update(
            {
                "benchmark_path": str(job.benchmark_path),
                "benchmark_name": job.benchmark_name,
                "variant_type": job.variant_type.value,
                "commit": job.commit,
                "main_repo": job.main_repo,
                "mode": job.mode.value,
                "language": job.language,
                "cpv_num": job.cpv_num,
                "patch_id": job.patch_id,
                "pov_id": job.pov_id,
                "patches": [str(p) for p in job.patches],
                "use_inc_build": job.use_inc_build,
                "force_rebuild": job.force_rebuild,
                "skip_if_cached": job.skip_if_cached,
                "source_mode": job.source_mode,
                "sanitizer": job.sanitizer,
                "repo_name": job.repo_name,
                "project_image_prefix": job.project_image_prefix,
                "prepare_inc_job_id": getattr(job, "prepare_inc_job_id", ""),
                "inc_image_policy": getattr(job, "inc_image_policy", None),
                "inc_image_registry": getattr(job, "inc_image_registry", None),
                "inc_image_max_pull_bytes": getattr(
                    job, "inc_image_max_pull_bytes", None
                ),
                "inc_image_pull_timeout": getattr(job, "inc_image_pull_timeout", None),
                "local_image_prefix": getattr(job, "local_image_prefix", None),
            }
        )
    elif cls_name == "VerifyCpvPovJob":
        params.update(
            {
                "benchmark_name": job.benchmark_name,
                "cpv_id": job.cpv_id,
                "harness": job.harness,
                "benchmark_path": str(job.benchmark_path)
                if job.benchmark_path
                else None,
                "pov_path": str(job.pov_path) if job.pov_path else None,
                "build_job_ids": job.build_job_ids,
                "use_inc_build": job.use_inc_build,
                "source_mode": job.source_mode,
            }
        )
    elif cls_name == "VerifyCpvVarJob":
        params.update(
            {
                "benchmark_name": job.benchmark_name,
                "cpv_id": job.cpv_id,
                "harness": job.harness,
                "benchmark_path": str(job.benchmark_path)
                if job.benchmark_path
                else None,
                "pov_paths": [str(p) for p in job.pov_paths],
                "build_job_ids": job.build_job_ids,
                "use_inc_build": job.use_inc_build,
                "source_mode": job.source_mode,
            }
        )
    elif cls_name == "PatchPovTestJob":
        params.update(
            {
                "benchmark_path": str(job.benchmark_path),
                "benchmark_name": job.benchmark_name,
                "cpv_id": job.cpv_id,
                "patch_id": job.patch_id,
                "harness": job.harness,
                "pov_path": str(job.pov_path) if job.pov_path else None,
                "patch_path_override": str(job.patch_path_override)
                if job.patch_path_override
                else None,
                "build_patch_job_id": job.build_patch_job_id,
                "use_inc_build": job.use_inc_build,
                "source_mode": job.source_mode,
            }
        )
    elif cls_name == "PatchVarTestJob":
        params.update(
            {
                "benchmark_path": str(job.benchmark_path),
                "benchmark_name": job.benchmark_name,
                "cpv_id": job.cpv_id,
                "patch_id": job.patch_id,
                "harness": job.harness,
                "pov_paths": [str(p) for p in job.pov_paths],
                "patch_path_override": str(job.patch_path_override)
                if job.patch_path_override
                else None,
                "build_patch_job_id": job.build_patch_job_id,
                "use_inc_build": job.use_inc_build,
                "source_mode": job.source_mode,
            }
        )
    elif cls_name == "PatchVariantTestJob":
        params.update(
            {
                "benchmark_path": str(job.benchmark_path),
                "benchmark_name": job.benchmark_name,
                "cpv_id": job.cpv_id,
                "patch_id": job.patch_id,
                "harness": job.harness,
                "test_mode": job.test_mode,
                "pov_paths": [str(p) for p in job.pov_paths],
                "patch_path_override": str(job.patch_path_override)
                if job.patch_path_override
                else None,
                "build_patch_job_id": job.build_patch_job_id,
                "use_inc_build": job.use_inc_build,
                "source_mode": job.source_mode,
            }
        )
    elif cls_name == "PatchUnitTestJob":
        params.update(
            {
                "benchmark_path": str(job.benchmark_path),
                "benchmark_name": job.benchmark_name,
                "cpv_id": job.cpv_id,
                "patch_id": job.patch_id,
                "harness": job.harness,
                "test_mode": job.test_mode,
                "patch_path_override": str(job.patch_path_override)
                if job.patch_path_override
                else None,
                "build_patch_job_id": job.build_patch_job_id,
                "use_inc_build": job.use_inc_build,
                "source_mode": job.source_mode,
            }
        )
    elif cls_name == "FlatCollectCoverageJob":
        params.update(
            {
                "benchmark_path": str(job.benchmark_path),
                "benchmark_name": job.benchmark_name,
                "harness": job.harness,
                "build_job_id": job.build_job_id,
                "source_mode": job.source_mode,
                "build_job_ids": job.build_job_ids,
            }
        )
    elif cls_name == "BuildPatchVariantJob":
        params.update(
            {
                "benchmark_path": str(job.benchmark_path),
                "benchmark_name": job.benchmark_name,
                "cpv_id": job.cpv_id,
                "patch_id": job.patch_id,
                "patch_path": str(job.patch_path),
                "harness": job.harness,
                "sanitizer": job.sanitizer,
                "use_inc_build": job.use_inc_build,
                "force_rebuild": job.force_rebuild,
                "build_job_id": job.build_job_id,
                "prepare_inc_job_id": getattr(job, "prepare_inc_job_id", ""),
                "source_mode": job.source_mode,
                "inc_image_policy": getattr(job, "inc_image_policy", None),
                "inc_image_registry": getattr(job, "inc_image_registry", None),
                "inc_image_max_pull_bytes": getattr(
                    job, "inc_image_max_pull_bytes", None
                ),
                "inc_image_pull_timeout": getattr(job, "inc_image_pull_timeout", None),
                "local_image_prefix": getattr(job, "local_image_prefix", None),
            }
        )
    elif cls_name == "PrepareIncImageJob":
        params.update(
            {
                "benchmark_path": str(job.benchmark_path),
                "benchmark_name": job.benchmark_name,
                "sanitizer": job.sanitizer,
                "use_inc_build": job.use_inc_build,
                "force_rebuild": job.force_rebuild,
                "source_mode": job.source_mode,
                "inc_image_policy": getattr(job, "inc_image_policy", None),
                "inc_image_registry": getattr(job, "inc_image_registry", None),
                "inc_image_max_pull_bytes": getattr(
                    job, "inc_image_max_pull_bytes", None
                ),
                "inc_image_pull_timeout": getattr(job, "inc_image_pull_timeout", None),
                "local_image_prefix": getattr(job, "local_image_prefix", None),
            }
        )

    return params


def _reconstruct_job(params: dict[str, Any]) -> Any:
    """Reconstruct a flat.py job from serialized parameters.

    Args:
        params: Serialized job parameters from serialize_ci_job()

    Returns:
        Reconstructed Job instance
    """
    from crsbench.benchmark_ci.jobs.flat import (
        BuildPatchVariantJob,
        BuildSingleVariantJob,
        FlatCollectCoverageJob,
        PatchPovTestJob,
        PatchUnitTestJob,
        PatchVariantTestJob,
        PatchVarTestJob,
        PrepareIncImageJob,
        VerifyCpvPovJob,
        VerifyCpvVarJob,
    )
    from crsbench.builder.types import BenchmarkMode, VariantType

    cls_name = params["_job_class"]

    if cls_name == "BuildSingleVariantJob":
        return BuildSingleVariantJob(
            benchmark_path=Path(params["benchmark_path"]),
            benchmark_name=params["benchmark_name"],
            variant_type=VariantType(params["variant_type"]),
            commit=params["commit"],
            main_repo=params["main_repo"],
            mode=BenchmarkMode(params["mode"]),
            language=params.get("language", "c"),
            cpv_num=params.get("cpv_num"),
            patch_id=params.get("patch_id"),
            pov_id=params.get("pov_id"),
            patches=[Path(p) for p in params.get("patches", [])],
            use_inc_build=params.get("use_inc_build", True),
            force_rebuild=params.get("force_rebuild", False),
            skip_if_cached=params.get("skip_if_cached", False),
            source_mode=params.get("source_mode", "pkgs"),
            sanitizer=params.get("sanitizer", "address"),
            repo_name=params.get("repo_name"),
            project_image_prefix=params.get("project_image_prefix", "crsbench"),
            prepare_inc_job_id=params.get("prepare_inc_job_id", ""),
            inc_image_policy=params.get("inc_image_policy"),
            inc_image_registry=params.get("inc_image_registry"),
            inc_image_max_pull_bytes=params.get("inc_image_max_pull_bytes"),
            inc_image_pull_timeout=params.get("inc_image_pull_timeout"),
            local_image_prefix=params.get("local_image_prefix"),
        )
    if cls_name == "VerifyCpvPovJob":
        return VerifyCpvPovJob(
            benchmark_name=params["benchmark_name"],
            cpv_id=params["cpv_id"],
            harness=params["harness"],
            benchmark_path=Path(params["benchmark_path"])
            if params.get("benchmark_path")
            else None,
            pov_path=Path(params["pov_path"]) if params.get("pov_path") else None,
            build_job_ids=params.get("build_job_ids", []),
            use_inc_build=params.get("use_inc_build", True),
            source_mode=params.get("source_mode", "pkgs"),
        )
    if cls_name == "VerifyCpvVarJob":
        return VerifyCpvVarJob(
            benchmark_name=params["benchmark_name"],
            cpv_id=params["cpv_id"],
            harness=params["harness"],
            benchmark_path=Path(params["benchmark_path"])
            if params.get("benchmark_path")
            else None,
            pov_paths=[Path(p) for p in params.get("pov_paths", [])],
            build_job_ids=params.get("build_job_ids", []),
            use_inc_build=params.get("use_inc_build", True),
            source_mode=params.get("source_mode", "pkgs"),
        )
    if cls_name == "PatchPovTestJob":
        return PatchPovTestJob(
            benchmark_path=Path(params["benchmark_path"]),
            benchmark_name=params["benchmark_name"],
            cpv_id=params["cpv_id"],
            patch_id=params["patch_id"],
            harness=params["harness"],
            pov_path=Path(params["pov_path"]) if params.get("pov_path") else None,
            patch_path_override=Path(params["patch_path_override"])
            if params.get("patch_path_override")
            else None,
            build_patch_job_id=params.get("build_patch_job_id", ""),
            use_inc_build=params.get("use_inc_build", True),
            source_mode=params.get("source_mode", "pkgs"),
        )
    if cls_name == "PatchVarTestJob":
        return PatchVarTestJob(
            benchmark_path=Path(params["benchmark_path"]),
            benchmark_name=params["benchmark_name"],
            cpv_id=params["cpv_id"],
            patch_id=params["patch_id"],
            harness=params["harness"],
            pov_paths=[Path(p) for p in params.get("pov_paths", [])],
            patch_path_override=Path(params["patch_path_override"])
            if params.get("patch_path_override")
            else None,
            build_patch_job_id=params.get("build_patch_job_id", ""),
            use_inc_build=params.get("use_inc_build", True),
            source_mode=params.get("source_mode", "pkgs"),
        )
    if cls_name == "PatchVariantTestJob":
        return PatchVariantTestJob(
            benchmark_path=Path(params["benchmark_path"]),
            benchmark_name=params["benchmark_name"],
            cpv_id=params["cpv_id"],
            patch_id=params["patch_id"],
            harness=params["harness"],
            test_mode=params.get("test_mode", "FULL"),
            pov_paths=[Path(p) for p in params.get("pov_paths", [])],
            patch_path_override=Path(params["patch_path_override"])
            if params.get("patch_path_override")
            else None,
            build_patch_job_id=params.get("build_patch_job_id", ""),
            use_inc_build=params.get("use_inc_build", True),
            source_mode=params.get("source_mode", "pkgs"),
        )
    if cls_name == "PatchUnitTestJob":
        return PatchUnitTestJob(
            benchmark_path=Path(params["benchmark_path"]),
            benchmark_name=params["benchmark_name"],
            cpv_id=params["cpv_id"],
            patch_id=params["patch_id"],
            harness=params["harness"],
            test_mode=params.get("test_mode", "FULL"),
            patch_path_override=Path(params["patch_path_override"])
            if params.get("patch_path_override")
            else None,
            build_patch_job_id=params.get("build_patch_job_id", ""),
            use_inc_build=params.get("use_inc_build", True),
            source_mode=params.get("source_mode", "pkgs"),
        )
    if cls_name == "FlatCollectCoverageJob":
        return FlatCollectCoverageJob(
            benchmark_path=Path(params["benchmark_path"]),
            benchmark_name=params["benchmark_name"],
            harness=params["harness"],
            build_job_id=params.get("build_job_id", ""),
            source_mode=params.get("source_mode", "pkgs"),
            build_job_ids=params.get("build_job_ids", []),
        )
    if cls_name == "BuildPatchVariantJob":
        return BuildPatchVariantJob(
            benchmark_path=Path(params["benchmark_path"]),
            benchmark_name=params["benchmark_name"],
            cpv_id=params["cpv_id"],
            patch_id=params["patch_id"],
            patch_path=Path(params["patch_path"]),
            harness=params.get("harness", ""),
            sanitizer=params.get("sanitizer", "address"),
            use_inc_build=params.get("use_inc_build", True),
            force_rebuild=params.get("force_rebuild", False),
            build_job_id=params.get("build_job_id", ""),
            prepare_inc_job_id=params.get("prepare_inc_job_id", ""),
            source_mode=params.get("source_mode", "pkgs"),
            inc_image_policy=params.get("inc_image_policy"),
            inc_image_registry=params.get("inc_image_registry"),
            inc_image_max_pull_bytes=params.get("inc_image_max_pull_bytes"),
            inc_image_pull_timeout=params.get("inc_image_pull_timeout"),
            local_image_prefix=params.get("local_image_prefix"),
        )
    if cls_name == "PrepareIncImageJob":
        return PrepareIncImageJob(
            benchmark_path=Path(params["benchmark_path"]),
            benchmark_name=params["benchmark_name"],
            sanitizer=params.get("sanitizer", "address"),
            use_inc_build=params.get("use_inc_build", True),
            force_rebuild=params.get("force_rebuild", False),
            source_mode=params.get("source_mode", "pkgs"),
            inc_image_policy=params.get("inc_image_policy"),
            inc_image_registry=params.get("inc_image_registry"),
            inc_image_max_pull_bytes=params.get("inc_image_max_pull_bytes"),
            inc_image_pull_timeout=params.get("inc_image_pull_timeout"),
            local_image_prefix=params.get("local_image_prefix"),
        )

    raise ValueError(f"Unknown job class: {cls_name}")


def _load_build_context_from_disk(
    context: Any,
    build_job_ids: list[str],
    benchmark_path: Path,
    source_mode: str,
    use_inc_build: bool,
) -> None:
    """Load build results from disk into context.shared.

    On the evaluator, Docker images already exist from previous build queue
    execution. This loads the BuildResult objects and adapter into
    context.shared so verify jobs can access them.

    Args:
        context: JobContext whose shared dict will be populated
        build_job_ids: Build job IDs that verify jobs depend on
        benchmark_path: Path to benchmark directory
        source_mode: Source mode for VerificationEngine
    """
    from crsbench.evaluation.verification.pov import VerificationEngine
    from crsbench.utils.run_helper import ensure_oss_fuzz_root

    oss_fuzz_path = Path(ensure_oss_fuzz_root())
    engine = VerificationEngine(oss_fuzz_path, source_mode=source_mode)
    adapter = engine.load_adapter(benchmark_path)
    if not adapter:
        raise InfraMissingBuildContextError(
            benchmark=benchmark_path.name,
            build_job_ids_requested=build_job_ids,
            missing_build_job_ids=build_job_ids,
            available_variants=[],
            source_mode=source_mode,
            use_inc_build=use_inc_build,
        )

    # Parse required variant names from build job ids up front so we can
    # detect and recover missing contexts for fallback-built variants.
    required_variant_names: set[str] = set()
    for bid in build_job_ids:
        parts = bid.split("/")
        if len(parts) > 2 and parts[2]:
            required_variant_names.add(parts[2])

    # Get build results for ALL sanitizers this benchmark uses
    build_results: dict[str, BuildResult] = {}
    for san in adapter.get_all_cpv_sanitizers():
        results = engine.get_or_build_results(
            adapter,
            sanitizer=san,
            use_inc_build=use_inc_build,
            allow_build=False,
        )
        build_results.update(results)

    # If verify requested inc-build contexts but some required variants are
    # missing, retry metadata load in non-strict mode. This accepts variants
    # that were legitimately built via fallback (inc image unavailable or
    # replay fallback path) and prevents false infra_missing_build_context.
    if use_inc_build:
        missing_after_strict = required_variant_names - set(build_results.keys())
        if missing_after_strict:
            logger.info(
                "Strict inc-build context load missed %d variant(s) for %s; "
                "retrying with fallback-compatible context load",
                len(missing_after_strict),
                benchmark_path.name,
            )
            for san in adapter.get_all_cpv_sanitizers():
                results = engine.get_or_build_results(
                    adapter,
                    sanitizer=san,
                    use_inc_build=False,
                    allow_build=False,
                )
                for variant_name, result in results.items():
                    if variant_name in missing_after_strict:
                        build_results[variant_name] = result

    if not build_results:
        raise InfraMissingBuildContextError(
            benchmark=benchmark_path.name,
            build_job_ids_requested=build_job_ids,
            missing_build_job_ids=build_job_ids,
            available_variants=[],
            source_mode=source_mode,
            use_inc_build=use_inc_build,
        )

    # Populate context.shared for each build_job_id
    # Build job IDs have format "build-single/{benchmark}/{variant_name}"
    missing_build_job_ids: list[str] = []
    for bid in build_job_ids:
        parts = bid.split("/")
        variant_name = parts[2] if len(parts) > 2 else None
        if variant_name and variant_name in build_results:
            context.shared[bid] = {
                "build_result": build_results[variant_name],
                "adapter": adapter,
            }
        else:
            missing_build_job_ids.append(bid)

    if missing_build_job_ids:
        raise InfraMissingBuildContextError(
            benchmark=benchmark_path.name,
            build_job_ids_requested=build_job_ids,
            missing_build_job_ids=missing_build_job_ids,
            available_variants=sorted(build_results.keys()),
            source_mode=source_mode,
            use_inc_build=use_inc_build,
        )

    logger.info(
        f"Loaded {len(context.shared)} build context entries for {benchmark_path.name}"
    )


def _load_patch_build_context(
    context: Any,
    job: Any,
    source_mode: str,
) -> None:
    """Load patch build context from disk for patch verify jobs.

    For patch verify/test jobs, the BuildPatchVariantJob already ran and
    stored its results. We reconstruct the expected context.shared entry.

    Args:
        context: JobContext whose shared dict will be populated
        job: Patch verify/test job with build_patch_job_id
        source_mode: Source mode for VerificationEngine
    """
    from crsbench.builder.infrastructure import OSSFuzzInfrastructure
    from crsbench.builder.types import BuildConfig, VariantType
    from crsbench.evaluation.verification.pov import VerificationEngine
    from crsbench.utils.run_helper import ensure_oss_fuzz_root

    bid = job.build_patch_job_id
    if not bid:
        return

    oss_fuzz_path = Path(ensure_oss_fuzz_root())
    engine = VerificationEngine(oss_fuzz_path, source_mode=source_mode)
    adapter = engine.load_adapter(job.benchmark_path)
    if not adapter:
        return

    # Compute expected variant_name for the patched build
    sanitizer = adapter.get_cpv_sanitizer(job.harness, job.cpv_id)
    config = BuildConfig(
        benchmark_name=job.benchmark_name,
        benchmark_path=job.benchmark_path,
        variant_type=VariantType.PATCHED,
        mode=adapter.get_mode(),
        sanitizer=sanitizer,
        language=adapter.lang,
        commit=adapter.get_ref_commit() or adapter.get_base_commit(),
        main_repo=adapter.main_repo,
        patch_id=job.patch_id,
        pov_id=job.cpv_id,
    )

    variant_name = config.variant_name
    inc_build_available = bool(getattr(job, "use_inc_build", False))

    # Prefer the actual upstream build result when available. This preserves
    # fallback/inc-build decisions made during build execution.
    try:
        import rq

        current_job = rq.get_current_job()
        if current_job and current_job.connection:
            upstream = rq.job.Job.fetch(bid, connection=current_job.connection)
            upstream_result = upstream.result
            if isinstance(upstream_result, dict):
                details = upstream_result.get("details", {})
                if isinstance(details, dict):
                    variant_name = details.get("variant_name", variant_name)
                    sanitizer = details.get("sanitizer", sanitizer)
                    upstream_inc = details.get("inc_build_available")
                    if isinstance(upstream_inc, bool):
                        inc_build_available = upstream_inc
    except Exception as e:
        logger.debug(f"Could not load upstream patch build result for {bid}: {e}")

    infra = OSSFuzzInfrastructure(oss_fuzz_path)
    variant_ready = infra.is_variant_built(
        variant_name,
        require_inc_build=inc_build_available,
        require_source_image=True,
    )
    if not variant_ready:
        raise InfraMissingBuildContextError(
            benchmark=job.benchmark_name,
            build_job_ids_requested=[bid],
            missing_build_job_ids=[bid],
            available_variants=[],
            source_mode=source_mode,
            use_inc_build=inc_build_available,
            error_code="infra_missing_patch_build_context",
        )

    context.shared[bid] = {
        "variant_name": variant_name,
        "sanitizer": sanitizer,
        "fallback_used": False,
        "inc_build_available": inc_build_available,
    }

    logger.info(f"Loaded patch build context for {bid}: variant={variant_name}")


def execute_ci_job(params: dict[str, Any]) -> dict[str, Any]:
    """Execute a CI verify/test job via RQ.

    RQ-compatible entry point that deserializes a CI job, pre-populates
    context.shared from disk (Docker images already exist from build queue),
    and returns serialized JobResult.

    Args:
        params: Serialized CI job parameters from serialize_ci_job()

    Returns:
        Dict with job result fields (from JobResult.to_dict())
    """
    cls_name = params.get("_job_class", "")

    # BuildSingleVariantJob uses its own execution path (no context pre-population)
    if cls_name == "BuildSingleVariantJob":
        from crsbench.distributed.build_jobs import execute_ci_build

        return execute_ci_build(params)

    from crsbench.benchmark_ci.jobs.base import JobContext

    job = _reconstruct_job(params)
    context = JobContext()
    output_dir = params.get("output_dir")
    if output_dir:
        context.output_dir = Path(output_dir)

    # Pre-populate context.shared from disk so verify jobs find build results
    source_mode = params.get("source_mode", "pkgs")

    try:
        if hasattr(job, "build_job_ids") and job.build_job_ids:
            benchmark_path = getattr(job, "benchmark_path", None)
            if benchmark_path:
                _load_build_context_from_disk(
                    context,
                    job.build_job_ids,
                    benchmark_path,
                    source_mode,
                    getattr(job, "use_inc_build", True),
                )

        if hasattr(job, "build_patch_job_id") and job.build_patch_job_id:
            if job.build_patch_job_id not in context.shared:
                _load_patch_build_context(context, job, source_mode)
    except InfraMissingBuildContextError as e:
        logger.warning(str(e))
        now = datetime.now().isoformat()
        return {
            "job_id": getattr(job, "job_id", "unknown"),
            "job_type": "verify",
            "success": False,
            "started_at": now,
            "finished_at": now,
            "elapsed_seconds": 0.0,
            "error": str(e),
            "details": e.to_details(),
        }

    result = job.execute(context)
    return result.to_dict()


def _recover_orphaned_deferred_jobs(
    queue: "rq.Queue",
    rq_jobs: dict[str, rq.job.Job],
    pending: set[str],
) -> int:
    """Recover deferred jobs whose dependencies are all satisfied.

    RQ 2.x has a race condition where finished jobs clear their dependents'
    dependency sets but fail to move them from the deferred registry to the
    queue. This function detects such orphaned jobs and force-enqueues them.

    Returns number of recovered jobs.
    """
    import rq.registry
    from rq.exceptions import NoSuchJobError
    from rq.job import Job as RqJob
    from rq.job import JobStatus

    deferred_registry = rq.registry.DeferredJobRegistry(queue=queue)
    deferred_ids = set(deferred_registry.get_job_ids())

    # Only check jobs we're tracking
    orphaned_ids = deferred_ids & pending
    if not orphaned_ids:
        return 0

    recovered = 0
    for job_id in orphaned_ids:
        rq_job = rq_jobs[job_id]
        rq_job.refresh()
        if rq_job.get_status() != JobStatus.DEFERRED:
            continue

        # Check if all dependencies are finished
        all_deps_done = True
        for dep_id in rq_job.dependency_ids:
            try:
                dep_job = RqJob.fetch(dep_id, connection=queue.connection)
                if dep_job.get_status() != JobStatus.FINISHED:
                    all_deps_done = False
                    break
            except NoSuchJobError:
                logger.debug(f"Dependency {dep_id} not found in Redis")
                all_deps_done = False
                break

        if all_deps_done:
            # Force-enqueue atomically via pipeline to prevent partial state
            # if the process crashes between steps
            pipe = queue.connection.pipeline()
            deferred_registry.remove(rq_job, pipeline=pipe)
            rq_job.set_status(JobStatus.QUEUED, pipeline=pipe)
            # Clear internal dep list so RQ doesn't re-defer when processing
            rq_job._dependency_ids = []
            rq_job.save(pipeline=pipe)
            pipe.delete(rq_job.dependencies_key)
            queue.push_job_id(job_id, pipeline=pipe)
            pipe.execute()
            recovered += 1

    if recovered:
        logger.info(f"Recovered {recovered} orphaned deferred jobs")
    return recovered


def _is_orphaned_started_job(
    rq_job: "rq.job.Job",
    stale_started_grace_seconds: int,
) -> tuple[bool, float, int]:
    """Return whether a STARTED job is stale/orphaned.

    A job is considered stale/orphaned when:
    - status is STARTED
    - started_at is known
    - age exceeds (timeout + grace)
    """
    status_value = getattr(rq_job.get_status(), "value", rq_job.get_status())
    if str(status_value).lower() != "started":
        return False, 0.0, int(getattr(rq_job, "timeout", 3600) or 3600)

    started_at = getattr(rq_job, "started_at", None)
    timeout_seconds = int(getattr(rq_job, "timeout", 3600) or 3600)
    if started_at is None:
        return False, 0.0, timeout_seconds
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    age_seconds = (datetime.now(timezone.utc) - started_at).total_seconds()
    is_stale = age_seconds > (timeout_seconds + stale_started_grace_seconds)
    return is_stale, age_seconds, timeout_seconds


def _mark_blocked_deferred_jobs(
    queue: "rq.Queue",
    rq_jobs: dict[str, "rq.job.Job"],
    pending: set[str],
    missing_job_failures: dict[str, dict[str, Any]],
) -> int:
    """Mark deferred jobs as infra-failed if a dependency is missing/failed."""
    import rq.registry
    from rq.exceptions import NoSuchJobError

    deferred_registry = rq.registry.DeferredJobRegistry(queue=queue)
    deferred_ids = set(deferred_registry.get_job_ids()) & pending
    if not deferred_ids:
        return 0

    marked = 0
    blocked_deferred_grace_seconds = 120
    now_utc = datetime.now(timezone.utc)
    for job_id in deferred_ids:
        rq_job = rq_jobs.get(job_id)
        if rq_job is None:
            continue
        rq_job.refresh()
        status_value = getattr(rq_job.get_status(), "value", rq_job.get_status())
        if str(status_value).lower() != "deferred":
            continue
        dep_ids = list(rq_job.dependency_ids or [])
        blocked_reason: str | None = None
        blocked_dep: str | None = None
        for dep_id in dep_ids:
            if dep_id in missing_job_failures:
                blocked_reason = "infra_dependency_failed"
                blocked_dep = dep_id
                break
            try:
                dep_job = rq_jobs.get(dep_id) or rq.job.Job.fetch(
                    dep_id, connection=queue.connection
                )
            except NoSuchJobError:
                blocked_reason = "infra_missing_dependency_job"
                blocked_dep = dep_id
                break
            dep_status_value = getattr(
                dep_job.get_status(), "value", dep_job.get_status()
            )
            dep_status = str(dep_status_value).lower()
            if dep_status in {"failed", "stopped", "canceled"}:
                blocked_reason = "infra_dependency_failed"
                blocked_dep = dep_id
                break

        if blocked_reason is None:
            # Only apply grace when dependency state is not yet conclusively blocked.
            created_at = getattr(rq_job, "created_at", None)
            if isinstance(created_at, datetime):
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=timezone.utc)
                age_seconds = (now_utc - created_at).total_seconds()
                if age_seconds < blocked_deferred_grace_seconds:
                    continue
            continue

        missing_job_failures[job_id] = {
            "job_id": job_id,
            "job_type": "unknown",
            "success": False,
            "error": (
                f"{blocked_reason}: deferred job is blocked by dependency {blocked_dep}"
            ),
            "elapsed_seconds": 0.0,
            "details": {
                "error_code": blocked_reason,
                "retryable": True,
                "dependency_job_id": blocked_dep,
            },
            "started_at": None,
            "finished_at": now_utc.isoformat(),
        }
        pending.discard(job_id)
        marked += 1

    if marked:
        logger.warning(
            f"Marked {marked} blocked deferred CI jobs as infra failures "
            f"for queue {queue.name}"
        )
    return marked


def enqueue_and_poll_ci_jobs(
    jobs: list[Any],
    redis_host: str,
    queue_name: str | Callable[[Any], str] = "crsbench_ci_verify",
    output_dir: str | None = None,
    stale_terminal_policy: str = "refresh_all",
) -> dict[str, dict[str, Any]]:
    """Enqueue CI jobs to Redis and poll until all complete.

    Supports per-job queue routing via a callable ``queue_name``.
    When ``queue_name`` is a string, all jobs go to that queue.
    When ``queue_name`` is a callable, it receives each job and
    returns the queue name for that job.

    Args:
        jobs: List of flat.py Job instances (build, verify, patch, coverage)
        redis_host: Redis server hostname
        queue_name: Redis queue name (str) or callable(job) -> queue name
        output_dir: Optional directory for per-job stdout/stderr logs on worker

    Returns:
        Dict mapping job_id -> serialized JobResult dict
    """
    try:
        import rq
    except ImportError as exc:
        raise RuntimeError(
            "Redis and RQ packages are required for distributed CI jobs. "
            "Install with: pip install redis rq"
        ) from exc

    from crsbench.distributed.queue import create_redis_connection

    redis_conn = create_redis_connection(redis_host)
    logger.info(f"Connected to Redis at {redis_host}")

    # Serialize and enqueue jobs respecting dependency order
    # Build-type jobs go first, then verify jobs
    build_type_jobs = [j for j in jobs if j.job_type == "build"]
    verify_type_jobs = [j for j in jobs if j.job_type != "build"]
    ordered_jobs = build_type_jobs + verify_type_jobs

    # Per-job queue routing: lazily create queues as needed
    queues: dict[str, rq.Queue] = {}

    # Resolve routing function once upfront for type safety
    if callable(queue_name):
        _route_fn: Callable[[Any], str] = cast("Callable[[Any], str]", queue_name)
    else:
        _fixed_name = cast("str", queue_name)
        _route_fn = lambda _job: _fixed_name  # noqa: E731

    def _get_queue(job: Any) -> "rq.Queue":
        name = _route_fn(job)
        if name not in queues:
            queues[name] = rq.Queue(name, connection=redis_conn)
        return queues[name]

    rq_jobs: dict[str, rq.job.Job] = {}
    all_job_ids = {job.job_id for job in ordered_jobs}
    terminal_statuses = {"finished", "failed", "stopped", "canceled"}
    reenqueue_safe_statuses = {"queued", "deferred", "scheduled"}
    stale_started_grace_seconds = 120
    terminal_conflicts: dict[str, list[str]] = {
        "finished": [],
        "failed": [],
        "stopped": [],
        "canceled": [],
    }
    existing_jobs: dict[str, rq.job.Job] = {}

    def _status_name(status: object) -> str:
        """Normalize RQ status values (str/enum) to lowercase string."""
        value = getattr(status, "value", status)
        return str(value).lower()

    def _should_refresh(status_name: str, policy: str) -> bool:
        if policy == "refresh_all":
            return status_name in {"finished", "failed", "stopped", "canceled"}
        if policy == "refresh_stopped_canceled_failed":
            return status_name in {"failed", "stopped", "canceled"}
        if policy == "refresh_failed":
            return status_name == "failed"
        if policy == "refresh_stopped_canceled":
            return status_name in {"stopped", "canceled"}
        return False

    # Pre-scan existing jobs so prompt (if needed) happens once at startup.
    for job in ordered_jobs:
        existing_job: rq.job.Job | None = None
        try:
            existing_job = rq.job.Job.fetch(job.job_id, connection=redis_conn)
        except Exception:
            existing_job = None
        if existing_job is None:
            continue
        existing_jobs[job.job_id] = existing_job
        status_name = _status_name(existing_job.get_status())
        if status_name in terminal_conflicts:
            terminal_conflicts[status_name].append(job.job_id)

    has_stale_terminals = any(terminal_conflicts[k] for k in terminal_conflicts)
    policy = stale_terminal_policy
    if policy not in {
        "refresh_stopped_canceled",
        "refresh_failed",
        "refresh_stopped_canceled_failed",
        "refresh_all",
        "quit",
    }:
        logger.warning(
            f"Unknown stale_terminal_policy={policy!r}; defaulting to refresh_all"
        )
        policy = "refresh_all"
    if has_stale_terminals:
        logger.warning(
            "Detected stale terminal CI jobs in Redis: "
            f"FINISHED={len(terminal_conflicts['finished'])}, "
            f"FAILED={len(terminal_conflicts['failed'])}, "
            "STOPPED/CANCELED="
            f"{len(terminal_conflicts['stopped']) + len(terminal_conflicts['canceled'])}. "
            f"Applying policy: {policy}"
        )
    if policy == "quit" and has_stale_terminals:
        raise RuntimeError(
            "Stale terminal CI jobs detected; quitting per selected policy."
        )

    for job in ordered_jobs:
        params = serialize_ci_job(job)
        if output_dir:
            params["output_dir"] = output_dir

        job_queue = _get_queue(job)
        existing_job = existing_jobs.get(job.job_id)

        if existing_job is not None:
            existing_status = existing_job.get_status()
            status_name = _status_name(existing_status)
            if status_name == "finished":
                if job.job_type == "build":
                    logger.warning(
                        f"CI build job {job.job_id} already finished; deleting stale "
                        "record before enqueue to guarantee fresh build artifacts"
                    )
                    existing_job.delete()
                elif _should_refresh(status_name, policy):
                    logger.warning(
                        f"CI job {job.job_id} already finished; deleting stale "
                        "record before enqueue per selected policy"
                    )
                    existing_job.delete()
                else:
                    logger.warning(
                        f"CI job {job.job_id} already finished; reusing existing result"
                    )
                    rq_jobs[job.job_id] = existing_job
                    continue
            elif status_name in {"failed", "stopped", "canceled"}:
                if _should_refresh(status_name, policy):
                    logger.warning(
                        f"CI job {job.job_id} has terminal status "
                        f"({existing_status}), deleting stale record before enqueue"
                    )
                    existing_job.delete()
                elif policy == "quit":
                    raise RuntimeError(
                        "Stale terminal CI jobs detected; quitting per selected policy."
                    )
                else:
                    logger.warning(
                        f"CI job {job.job_id} has terminal status "
                        f"({existing_status}); reusing existing result"
                    )
                    rq_jobs[job.job_id] = existing_job
                    continue
            elif status_name in reenqueue_safe_statuses:
                logger.warning(
                    f"CI job {job.job_id} already exists with status "
                    f"({existing_status}), deleting stale queued/deferred record "
                    "before enqueue"
                )
                existing_job.delete()
            else:
                is_stale_started, age_seconds, timeout_seconds = (
                    _is_orphaned_started_job(existing_job, stale_started_grace_seconds)
                )
                if is_stale_started:
                    logger.warning(
                        "CI job %s has stale STARTED status "
                        "(age=%.1fs timeout=%ss); deleting and re-enqueuing",
                        job.job_id,
                        age_seconds,
                        timeout_seconds,
                    )
                    existing_job.delete()
                else:
                    logger.warning(
                        f"CI job {job.job_id} already active with status="
                        f"{existing_status}; reusing existing job for this run"
                    )
                    rq_jobs[job.job_id] = existing_job
                    continue

        # Map depends_on to RQ dependency IDs
        depends_on_rq: Optional[list[rq.job.Job]] = None
        if job.depends_on:
            unknown_dep_ids = [
                dep_id for dep_id in job.depends_on if dep_id not in all_job_ids
            ]
            if unknown_dep_ids:
                missing_csv = ", ".join(unknown_dep_ids)
                raise ValueError(
                    f"CI job {job.job_id} has unknown dependencies: {missing_csv}"
                )
            unresolved_dep_ids = [
                dep_id for dep_id in job.depends_on if dep_id not in rq_jobs
            ]
            if unresolved_dep_ids:
                unresolved_csv = ", ".join(unresolved_dep_ids)
                raise ValueError(
                    f"CI job {job.job_id} has dependencies that were not enqueued "
                    f"before it (invalid DAG ordering): {unresolved_csv}"
                )
            depends_on_rq = [rq_jobs[d] for d in job.depends_on]

        try:
            rq_job = job_queue.enqueue(
                "crsbench.distributed.ci_jobs.execute_ci_job",
                params,
                job_timeout=3600,
                result_ttl=-1,
                job_id=job.job_id,
                depends_on=depends_on_rq,
            )
            rq_jobs[job.job_id] = rq_job
            logger.info(f"Enqueued CI job {job.job_id} -> {job_queue.name}")
        except Exception as err:
            existing = rq.job.Job.fetch(job.job_id, connection=redis_conn)
            status = existing.get_status()
            status_name = _status_name(status)
            if status_name in terminal_statuses:
                if status_name == "finished" and job.job_type == "build":
                    logger.warning(
                        f"CI build job {job.job_id} already finished; "
                        "deleting and re-enqueuing to guarantee fresh build artifacts"
                    )
                    existing.delete()
                    rq_job = job_queue.enqueue(
                        "crsbench.distributed.ci_jobs.execute_ci_job",
                        params,
                        job_timeout=3600,
                        result_ttl=-1,
                        job_id=job.job_id,
                        depends_on=depends_on_rq,
                    )
                    rq_jobs[job.job_id] = rq_job
                    continue
                if _should_refresh(status_name, policy):
                    logger.warning(
                        f"CI job {job.job_id} has terminal status ({status}), "
                        "deleting and re-enqueuing per selected policy"
                    )
                    existing.delete()
                    rq_job = job_queue.enqueue(
                        "crsbench.distributed.ci_jobs.execute_ci_job",
                        params,
                        job_timeout=3600,
                        result_ttl=-1,
                        job_id=job.job_id,
                        depends_on=depends_on_rq,
                    )
                    rq_jobs[job.job_id] = rq_job
                elif policy == "quit":
                    raise RuntimeError(
                        "Stale terminal CI jobs detected; quitting per selected policy."
                    ) from err
                else:
                    logger.warning(
                        f"CI job {job.job_id} has terminal status ({status}); "
                        "reusing existing result"
                    )
                    rq_jobs[job.job_id] = existing
            elif status_name in reenqueue_safe_statuses:
                logger.warning(
                    f"CI job {job.job_id} already exists with status ({status}), "
                    "deleting stale queued/deferred record and re-enqueuing"
                )
                existing.delete()
                rq_job = job_queue.enqueue(
                    "crsbench.distributed.ci_jobs.execute_ci_job",
                    params,
                    job_timeout=3600,
                    result_ttl=-1,
                    job_id=job.job_id,
                    depends_on=depends_on_rq,
                )
                rq_jobs[job.job_id] = rq_job
            else:
                is_stale_started, age_seconds, timeout_seconds = (
                    _is_orphaned_started_job(existing, stale_started_grace_seconds)
                )
                if is_stale_started:
                    logger.warning(
                        "CI job %s has stale STARTED status "
                        "(age=%.1fs timeout=%ss); deleting and re-enqueuing",
                        job.job_id,
                        age_seconds,
                        timeout_seconds,
                    )
                    existing.delete()
                    rq_job = job_queue.enqueue(
                        "crsbench.distributed.ci_jobs.execute_ci_job",
                        params,
                        job_timeout=3600,
                        result_ttl=-1,
                        job_id=job.job_id,
                        depends_on=depends_on_rq,
                    )
                    rq_jobs[job.job_id] = rq_job
                else:
                    logger.warning(
                        f"CI job {job.job_id} already active with status={status}; "
                        "reusing existing job for this run"
                    )
                    rq_jobs[job.job_id] = existing

    logger.info(
        f"Enqueued {len(rq_jobs)} CI jobs ({len(build_type_jobs)} build, "
        f"{len(verify_type_jobs)} verify/test) across {len(queues)} queue(s), "
        f"waiting for completion..."
    )

    # Poll for results with deferred job recovery
    pending = set(rq_jobs.keys())
    missing_job_failures: dict[str, dict[str, Any]] = {}
    recovery_interval = 30  # seconds between recovery sweeps
    last_recovery = time.monotonic()

    while pending:
        # Batch-fetch all pending jobs in one Redis pipeline round-trip
        pending_ids = list(pending)
        fetched = rq.job.Job.fetch_many(pending_ids, connection=redis_conn)
        for pending_id, rq_job in zip(pending_ids, fetched, strict=False):
            if rq_job is None:
                missing_job_failures[pending_id] = {
                    "job_id": pending_id,
                    "job_type": "unknown",
                    "success": False,
                    "error": (
                        "infra_missing_rq_job: job metadata missing from Redis during "
                        "polling (fetch_many returned None)"
                    ),
                    "elapsed_seconds": 0.0,
                    "details": {
                        "error_code": "infra_missing_rq_job",
                        "retryable": False,
                    },
                    "started_at": None,
                    "finished_at": datetime.now().isoformat(),
                }
                pending.discard(pending_id)
                continue
            rq_jobs[rq_job.id] = rq_job
            status = rq_job.get_status()
            status_name = _status_name(status)
            if status_name == "started":
                is_stale_started, age_seconds, timeout_seconds = (
                    _is_orphaned_started_job(rq_job, stale_started_grace_seconds)
                )
                if is_stale_started:
                    started_at = getattr(rq_job, "started_at", None)
                    if started_at is not None and started_at.tzinfo is None:
                        started_at = started_at.replace(tzinfo=timezone.utc)
                    logger.warning(
                        "Marking stale STARTED CI job as infra failure: "
                        f"{pending_id} (age={age_seconds:.1f}s, "
                        f"timeout={timeout_seconds}s)"
                    )
                    missing_job_failures[pending_id] = {
                        "job_id": pending_id,
                        "job_type": "unknown",
                        "success": False,
                        "error": (
                            "infra_stale_started_job: job remained STARTED past "
                            "timeout window; likely orphaned worker or crashed "
                            "supervisor"
                        ),
                        "elapsed_seconds": 0.0,
                        "details": {
                            "error_code": "infra_stale_started_job",
                            "retryable": True,
                            "started_age_seconds": age_seconds,
                            "timeout_seconds": timeout_seconds,
                        },
                        "started_at": started_at.isoformat()
                        if started_at is not None
                        else None,
                        "finished_at": datetime.now(timezone.utc).isoformat(),
                    }
                    pending.discard(pending_id)
                    continue
            if status_name in terminal_statuses:
                pending.discard(rq_job.id)

        # Periodically recover orphaned deferred jobs (RQ race condition)
        now = time.monotonic()
        if pending and (now - last_recovery) >= recovery_interval:
            for q in queues.values():
                try:
                    _recover_orphaned_deferred_jobs(q, rq_jobs, pending)
                except Exception:
                    logger.warning(
                        f"Recovery sweep failed for queue {q.name}, "
                        f"will retry next interval",
                        exc_info=True,
                    )
                try:
                    _mark_blocked_deferred_jobs(
                        q, rq_jobs, pending, missing_job_failures
                    )
                except Exception:
                    logger.warning(
                        f"Blocked deferred sweep failed for queue {q.name}, "
                        "will retry next interval",
                        exc_info=True,
                    )
            last_recovery = now

        if pending:
            logger.info(f"Waiting for {len(pending)}/{len(rq_jobs)} CI jobs...")
            time.sleep(5)

    # Collect results
    raw_results: dict[str, dict[str, Any]] = {}
    raw_results.update(missing_job_failures)
    for job_id, rq_job in rq_jobs.items():
        if job_id in missing_job_failures:
            continue
        rq_job.refresh()
        status = rq_job.get_status()

        status_name = _status_name(status)
        if status_name == "finished" and rq_job.result:
            raw_results[job_id] = rq_job.result
        else:
            exc_info = rq_job.exc_info or "Unknown error"
            raw_results[job_id] = {
                "job_id": job_id,
                "job_type": "unknown",
                "success": False,
                "error": str(exc_info)[:500],
                "elapsed_seconds": 0.0,
                "details": {},
                "started_at": None,
                "finished_at": None,
            }

    return raw_results


def ci_results_to_executor_results(
    raw_results: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Convert raw Redis CI result dicts to ExecutorResult format.

    Args:
        raw_results: Dict mapping job_id -> serialized JobResult dict

    Returns:
        Dict mapping job_id -> ExecutorResult
    """
    from datetime import datetime

    from crsbench.benchmark_ci.jobs.base import JobResult
    from crsbench.executor.types import ExecutorResult, JobStatus

    def _parse_timestamp(value: Any) -> datetime:
        if not value:
            return datetime.now()
        if not isinstance(value, str):
            return datetime.now()
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            logger.warning(f"Invalid CI job timestamp: {value!r}; using current time")
            return datetime.now()

    results: dict[str, ExecutorResult] = {}
    for job_id, r in raw_results.items():
        if r.get("success"):
            job_result = JobResult(
                job_id=r["job_id"],
                job_type=r.get("job_type", "verify"),
                success=r["success"],
                started_at=_parse_timestamp(r.get("started_at")),
                finished_at=_parse_timestamp(r.get("finished_at")),
                elapsed_seconds=r.get("elapsed_seconds", 0.0),
                error=r.get("error"),
                details=r.get("details", {}),
            )
            results[job_id] = ExecutorResult(
                job_id=job_id,
                status=JobStatus.SUCCESS,
                elapsed_seconds=r.get("elapsed_seconds", 0.0),
                job_result=job_result,
            )
        else:
            job_result = JobResult(
                job_id=r.get("job_id", job_id),
                job_type=r.get("job_type", "verify"),
                success=False,
                started_at=_parse_timestamp(r.get("started_at")),
                finished_at=_parse_timestamp(r.get("finished_at")),
                elapsed_seconds=r.get("elapsed_seconds", 0.0),
                error=r.get("error", "Unknown error"),
                details=r.get("details", {}),
            )
            results[job_id] = ExecutorResult(
                job_id=job_id,
                status=JobStatus.FAILED,
                elapsed_seconds=r.get("elapsed_seconds", 0.0),
                error=r.get("error", "Unknown error"),
                job_result=job_result,
            )

    return results
