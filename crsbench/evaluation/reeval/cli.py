"""CLI command for re-evaluation of completed experiment trials.

Re-runs verification on existing trial outputs without re-running CRS.
Discovers trials from an experiment directory structure and dispatches to
VerificationEngine (bug_finding) or PatchVerificationEngine (patch_generation).

Usage:
    crsbench re-eval --experiment-config experiment-config.yaml
    crsbench re-eval -c experiment-config.yaml --output /tmp/reeval-results
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import yaml

from crsbench.distributed.common import normalize_redis_host
from crsbench.evaluation.trial_paths import (
    ExperimentDir,
    TrialDir,
    resolve_benchmark_path,
)
from crsbench.evaluation.verification.models import (
    PatchVerificationOutput,
    PatchVerificationResult,
    PatchVerificationStatus,
    PovVerificationResult,
    PovVerificationStatus,
)
from crsbench.utils.logger import get_logger

if TYPE_CHECKING:
    import rq

logger = get_logger(__name__)


def add_reeval_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Add the re-eval subcommand to argparse.

    Args:
        subparsers: Subparsers action from main argument parser
    """
    parser = subparsers.add_parser(
        "re-eval",
        help="Re-run verification on completed experiment trials",
        description=(
            "Re-evaluate completed experiment trials by re-running POV or patch "
            "verification using existing CRS outputs. Does not re-run CRS itself."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Re-evaluate all trials in an experiment
  crsbench re-eval --experiment-config experiment-config.yaml

  # Re-evaluate with custom output directory
  crsbench re-eval -c experiment-config.yaml --output /tmp/reeval-results

  # Re-evaluate with verbose logging
  crsbench re-eval -c experiment-config.yaml -v
        """,
    )

    parser.add_argument(
        "--experiment-config",
        "-c",
        type=Path,
        required=True,
        help="Path to experiment config YAML",
    )
    parser.add_argument(
        "--oss-fuzz-path",
        type=Path,
        default=None,
        help="Path to oss-fuzz directory (default: ./oss-fuzz)",
    )
    parser.add_argument(
        "--source",
        choices=["pkgs", "main_repo"],
        default="pkgs",
        help="Source mode for builds (default: pkgs)",
    )
    parser.add_argument(
        "--build-workers",
        type=int,
        default=None,
        help="Number of parallel build workers",
    )
    parser.add_argument(
        "--verify-workers",
        type=int,
        default=None,
        help="Number of parallel verify workers",
    )
    parser.add_argument(
        "--force-rebuild",
        action="store_true",
        help="Force rebuild of variants",
    )
    parser.add_argument(
        "--inc-build",
        action="store_true",
        help="Use incremental build instead of full build",
    )
    parser.add_argument(
        "--per-pov-verify-timeout",
        type=int,
        default=None,
        help="Timeout in seconds per POV verification (default: from config, or 180)",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=None,
        help="Output directory (default: write results to trial dirs)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable verbose logging",
    )

    parser.set_defaults(func=run_reeval)


def _load_experiment_config(config_path: Path) -> dict:
    """Load and return raw experiment config dict from YAML.

    Args:
        config_path: Path to experiment config YAML

    Returns:
        Parsed YAML dict

    Raises:
        SystemExit: If file not found or YAML invalid
    """
    if not config_path.exists():
        logger.error(f"Config file not found: {config_path}")
        sys.exit(1)

    with config_path.open() as f:
        return yaml.safe_load(f)


def _resolve_experiment_dir(config: dict) -> Path:
    """Resolve experiment directory from config.

    Args:
        config: Raw experiment config dict

    Returns:
        Path to experiment directory
    """
    return ExperimentDir.from_config_dict(config).path


def _resolve_benchmark_path(
    benchmark_name: str, benchmarks_root: Optional[Path]
) -> Path:
    """Resolve benchmark directory path from name.

    Args:
        benchmark_name: Benchmark identifier (e.g., "afc-curl-delta-01")
        benchmarks_root: Root directory containing benchmarks

    Returns:
        Path to benchmark directory
    """
    return resolve_benchmark_path(benchmark_name, benchmarks_root)


def _resolve_output_dir(
    trial_dir: Path, output_base: Optional[Path], experiment_dir: Path
) -> Path:
    """Resolve where to write results for a trial.

    If output_base is set, mirrors trial path structure under it.
    Otherwise writes directly to trial_dir.

    Args:
        trial_dir: Original trial directory
        output_base: Optional output base directory
        experiment_dir: Experiment root directory

    Returns:
        Path to write results
    """
    return ExperimentDir(experiment_dir).mirror_trial_dir(trial_dir, output_base)


def _load_crs_run_start_time(store_dir: Path) -> Optional[float]:
    """Load crs_run_start_time from an existing pov_store.json.

    Re-eval creates a fresh POVStore but needs the original CRS start time
    to compute correct relative_time values.

    Args:
        store_dir: Directory containing pov_store.json

    Returns:
        crs_run_start_time if found, None otherwise
    """
    store_path = store_dir / "pov_store.json"
    if not store_path.exists():
        return None
    try:
        data = json.loads(store_path.read_text())
        ts = data.get("crs_run_start_time")
        if ts is not None:
            logger.debug(f"Loaded crs_run_start_time={ts} from {store_path}")
        return ts
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Failed to read crs_run_start_time from {store_path}: {e}")
        return None


def _save_pov_results(results: list, dest_dir: Path) -> Path:
    """Save POV verification results to JSON.

    Args:
        results: List of PovVerificationResult
        dest_dir: Directory to write results into

    Returns:
        Path to written file
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    results_path = dest_dir / "pov_verification_results.json"
    result_dicts = [r.to_dict(include_logs=True) for r in results]
    results_path.write_text(json.dumps(result_dicts, indent=2))
    return results_path


def _save_patch_results(results: list, total_input_povs: int, dest_dir: Path) -> Path:
    """Save patch verification results to JSON.

    Args:
        results: List of PatchVerificationResult
        total_input_povs: Number of input POVs
        dest_dir: Directory to write results into

    Returns:
        Path to written file
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    output = PatchVerificationOutput.from_results(results, total_input_povs)
    results_path = dest_dir / "patch_verification_results.json"
    results_path.write_text(output.model_dump_json(indent=2))
    return results_path


def _reeval_bug_finding(
    trial_dir: Path,
    benchmark_path: Path,
    oss_fuzz_path: Path,
    harness: str,
    dest_dir: Path,
    source_mode: str,
    build_workers: Optional[int],
    verify_workers: Optional[int],
    per_pov_verify_timeout: int = 180,
    *,
    force_rebuild: bool,
    use_inc_build: bool,
) -> int:
    """Re-evaluate a bug_finding trial.

    Args:
        trial_dir: Trial directory with CRS outputs
        benchmark_path: Path to benchmark project
        oss_fuzz_path: Path to oss-fuzz
        harness: Harness name
        dest_dir: Where to write results
        source_mode: Source mode (pkgs/main_repo)
        build_workers: Parallel build workers
        verify_workers: Parallel verify workers
        force_rebuild: Force variant rebuild
        use_inc_build: Use incremental builds

    Returns:
        Number of results produced
    """
    from crsbench.evaluation.verification import (
        PatchBasedDedup,
        VerificationEngine,
    )
    from crsbench.evaluation.verification.pov.store import POVStore
    from crsbench.evaluation.verification.utils import compute_content_hash

    pov_dir = TrialDir(trial_dir).output_povs
    if not pov_dir.exists():
        logger.warning(f"No POV directory found: {pov_dir}")
        return 0

    engine = VerificationEngine(
        oss_fuzz_path=oss_fuzz_path,
        timeout=per_pov_verify_timeout,
        dedup_strategy=PatchBasedDedup(),
        build_workers=build_workers,
        verify_workers=verify_workers,
        source_mode=source_mode,
    )

    output = engine.verify_benchmark(
        benchmark_path=benchmark_path,
        pov_dir=pov_dir,
        harness_filter=harness,
        force_rebuild=force_rebuild,
        use_inc_build=use_inc_build,
    )

    if output.results:
        path = _save_pov_results(output.results, dest_dir)
        logger.info(f"Wrote {len(output.results)} POV results to {path}")

        # Populate POVStore with blobs and crash logs (same as live evaluator)
        # Preserve crs_run_start_time from original run for correct relative_time
        povs_dir = dest_dir / "povs"
        crs_start = _load_crs_run_start_time(povs_dir)
        store = POVStore(povs_dir, crs_run_start_time=crs_start)
        for result in output.results:
            if not result.pov_id:
                continue
            pov_file = pov_dir / result.pov_id
            if not pov_file.exists():
                continue
            pov_hash = compute_content_hash(pov_file)

            store.add_pov(
                pov_file, result.status, result.cpv_matched, pov_hash=pov_hash
            )

            # Store per-variant crash logs (for all statuses)
            if result.crash_info and "stdout" in result.crash_info:
                for variant_name, crash_log in result.crash_info["stdout"].items():
                    store.store_crash_log(
                        pov_hash,
                        crash_log,
                        result.status,
                        result.cpv_matched,
                        variant_name=variant_name,
                    )

            # Store POV blob for CPV matches only
            if result.status == PovVerificationStatus.CPV:
                store.store_unique_pov(
                    pov_file, pov_hash, result.status, result.cpv_matched
                )

        store.save()
        logger.info(f"Stored POV results to {dest_dir / 'povs'}: {store.get_stats()}")

    return len(output.results)


@dataclass
class _AsyncTrialState:
    """Per-trial tracking data accumulated during the enqueue phase."""

    trial_dir: Path
    benchmark_name: str
    harness: str
    dest_dir: Path
    job_ids: list[str] = field(default_factory=list)
    pov_hash_to_path: dict[str, Path] = field(default_factory=dict)

    @property
    def trial_id(self) -> str:
        return f"{self.benchmark_name}__{self.harness}__{self.trial_dir.name}"


@dataclass
class _AsyncPatchTrialState:
    """Per-trial patch verify tracking data accumulated during enqueue."""

    trial_dir: Path
    benchmark_name: str
    harness: str
    dest_dir: Path
    total_input_povs: int
    job_ids: list[str] = field(default_factory=list)

    @property
    def trial_id(self) -> str:
        return f"{self.benchmark_name}__{self.harness}__{self.trial_dir.name}"


def _infer_target_cpv_id_from_trial_path(trial_dir: Path) -> Optional[str]:
    """Infer cpv_id from trial path components (e.g., .../cpv_0/.../trial-1)."""
    for part in trial_dir.parts:
        if re.fullmatch(r"cpv_\d+", part):
            return part
    return None


def _load_target_cpv_id_from_trial_metadata(trial_dir: Path) -> Optional[str]:
    """Load target_cpv_id from trial metadata.json when available."""
    metadata_path = trial_dir / "metadata.json"
    if not metadata_path.exists():
        return None
    try:
        metadata = json.loads(metadata_path.read_text())
    except Exception:
        return None
    target = metadata.get("target_cpv_id")
    return target if isinstance(target, str) and target else None


def _discover_trial_patches(
    patch_dir: Path,
    *,
    target_cpv_id: Optional[str],
) -> list[tuple[str, str, Path]]:
    """Discover trial patches from output/patches in structured or flat layout."""
    if not patch_dir.exists():
        return []

    discovered: list[tuple[str, str, Path]] = []
    seen_patch_keys: set[tuple[str, str]] = set()
    # Structured layout: output/patches/<cpv_id>/*.diff
    for cpv_dir in sorted(p for p in patch_dir.iterdir() if p.is_dir()):
        for patch_path in sorted(
            p for p in cpv_dir.iterdir() if p.is_file() and p.suffix == ".diff"
        ):
            patch_key = (cpv_dir.name, patch_path.stem)
            if patch_key in seen_patch_keys:
                continue
            seen_patch_keys.add(patch_key)
            discovered.append((cpv_dir.name, patch_path.stem, patch_path))

    # Flat layout: output/patches/*.diff (map to target CPV)
    flat_patches = sorted(
        p for p in patch_dir.iterdir() if p.is_file() and p.suffix == ".diff"
    )
    if flat_patches:
        if not target_cpv_id:
            raise ValueError(
                "Found flat patch layout but target CPV could not be resolved "
                f"for {patch_dir}"
            )
        cpv_id = target_cpv_id
        for patch_path in flat_patches:
            patch_key = (cpv_id, patch_path.stem)
            if patch_key in seen_patch_keys:
                continue
            seen_patch_keys.add(patch_key)
            discovered.append((cpv_id, patch_path.stem, patch_path))

    return discovered


def _patch_result_from_dict(result: dict) -> PatchVerificationResult:
    """Convert distributed patch verdict dict into PatchVerificationResult."""
    status_str = result.get("status", "error")
    try:
        status = PatchVerificationStatus(status_str)
    except ValueError:
        status = PatchVerificationStatus.ERROR

    patch_path_str = result.get("patch_path", "")
    patch_path = Path(patch_path_str) if patch_path_str else Path()
    return PatchVerificationResult(
        status=status,
        patch_id=result.get("patch_id", ""),
        pov_id=result.get("cpv_id", ""),
        benchmark=result.get("benchmark", ""),
        patch_path=patch_path,
        harness=result.get("harness", ""),
        details=result.get("details", ""),
        pov_test_passed=result.get("pov_test_passed"),
        unit_tests_passed=result.get("unit_test_passed"),
        build_time=result.get("build_time", 0.0),
        pov_test_time=result.get("pov_test_time", 0.0),
        unit_test_time=result.get("unit_test_time", 0.0),
        elapsed_seconds=result.get("elapsed_seconds", 0.0),
        cpv_fixed=result.get("cpv_fixed", []),
        security_verdict=result.get("security_verdict", "FAIL"),
        failed_tests=result.get("failed_tests", []),
    )


def _write_async_patch_logs(dest_dir: Path, result: dict) -> None:
    """Persist async patch verify logs under trial patch log directory."""
    logs = result.get("logs")
    if not isinstance(logs, dict) or not logs:
        return

    logs_dir = TrialDir(dest_dir).patch_verify_logs
    logs_dir.mkdir(parents=True, exist_ok=True)
    for filename, content in logs.items():
        if not isinstance(filename, str):
            continue
        if not filename.endswith((".stdout", ".stderr")):
            continue
        if not isinstance(content, str):
            continue
        safe = Path(filename).name
        target = logs_dir / safe
        if target.exists():
            stem = target.stem
            suffix = target.suffix
            idx = 1
            while target.exists():
                target = logs_dir / f"{stem}-{idx}{suffix}"
                idx += 1
        target.write_text(content)


def _enqueue_trial_povs(
    trial_dir: Path,
    benchmark_name: str,
    harness: str,
    dest_dir: Path,
    verify_queue: rq.Queue,
    experiment_name: str,
) -> Optional[_AsyncTrialState]:
    """Enqueue all POVs for a single trial without polling.

    Args:
        trial_dir: Trial directory with CRS outputs
        benchmark_name: Benchmark identifier
        harness: Harness name
        dest_dir: Where to write results
        verify_queue: RQ verify queue instance
        experiment_name: Experiment identifier

    Returns:
        Trial state with enqueued job IDs, or None if nothing to enqueue
    """
    from crsbench.distributed.verify_queue import enqueue_single_pov
    from crsbench.evaluation.verification.utils import compute_content_hash

    pov_dir = TrialDir(trial_dir).output_povs
    if not pov_dir.exists():
        logger.warning(f"No POV directory found: {pov_dir}")
        return None

    pov_files = [
        p
        for p in sorted(pov_dir.iterdir())
        if p.is_file() and not p.name.startswith(".")
    ]
    if not pov_files:
        logger.warning(f"No POV files found in {pov_dir}")
        return None

    state = _AsyncTrialState(
        trial_dir=trial_dir,
        benchmark_name=benchmark_name,
        harness=harness,
        dest_dir=dest_dir,
    )
    trial_id = state.trial_id

    for pov_file in pov_files:
        pov_data = pov_file.read_bytes()
        pov_hash = compute_content_hash(pov_file)
        pov_id = f"{pov_file.name}:{pov_hash}"
        state.pov_hash_to_path[pov_hash] = pov_file

        job_id = enqueue_single_pov(
            verify_queue=verify_queue,
            experiment_name=experiment_name,
            trial_id=trial_id,
            benchmark=benchmark_name,
            harness=harness,
            pov_id=pov_id,
            pov_data=pov_data,
        )
        if job_id:
            state.job_ids.append(job_id)
            logger.debug(f"Enqueued POV {pov_id} as job {job_id}")
        else:
            logger.warning(f"Failed to enqueue POV {pov_file.name}")

    if not state.job_ids:
        logger.warning("No POVs were enqueued")
        return None

    logger.info(
        f"Enqueued {len(state.job_ids)} POVs for async verification (trial {trial_id})"
    )
    return state


def _enqueue_trial_patches(
    trial_dir: Path,
    benchmark_name: str,
    harness: str,
    dest_dir: Path,
    build_queue: rq.Queue,
    verify_queue: rq.Queue,
    experiment_name: str,
    source_mode: str,
    *,
    patch_verify_variants: bool,
    use_inc_build: bool,
) -> Optional[_AsyncPatchTrialState]:
    """Enqueue all patch build+verify jobs for a single patch-generation trial."""
    from crsbench.distributed.patch_queue import enqueue_patch_jobs

    patch_dir = TrialDir(trial_dir).output_patches
    if not patch_dir.exists():
        logger.info(
            f"No patches directory found (CRS produced no patches): {patch_dir}"
        )
        return None

    target_cpv_id = _infer_target_cpv_id_from_trial_path(
        trial_dir
    ) or _load_target_cpv_id_from_trial_metadata(trial_dir)
    patches = _discover_trial_patches(patch_dir, target_cpv_id=target_cpv_id)
    if not patches:
        logger.warning(f"No patches found in {patch_dir}")
        return None

    pov_dir = TrialDir(trial_dir).input_povs
    if not pov_dir.exists():
        logger.warning(f"No POVs directory found: {pov_dir}")
        return None

    state = _AsyncPatchTrialState(
        trial_dir=trial_dir,
        benchmark_name=benchmark_name,
        harness=harness,
        dest_dir=dest_dir,
        total_input_povs=TrialDir(trial_dir).count_visible_input_povs(),
    )

    job_ids = enqueue_patch_jobs(
        build_queue,
        verify_queue,
        experiment_name,
        state.trial_id,
        benchmark_name,
        harness,
        patches,
        source_mode=source_mode,
        verify_variants=patch_verify_variants,
        use_inc_build=use_inc_build,
    )
    state.job_ids.extend(job_ids)

    if not state.job_ids:
        logger.warning("No patch jobs were enqueued")
        return None

    logger.info(
        f"Enqueued {len(state.job_ids)} patch jobs for async verification "
        f"(trial {state.trial_id})"
    )
    return state


def _drain_all_async_results(
    trials: list[_AsyncTrialState],
    redis_host: str,
) -> int:
    """Poll all enqueued trials in a single unified loop.

    Routes completed results to the correct trial's POVStore via trial_id.

    Args:
        trials: All trial states with enqueued jobs
        redis_host: Redis server hostname

    Returns:
        Total number of results processed across all trials
    """
    from crsbench.distributed.evaluator_jobs import SinglePovResult
    from crsbench.distributed.verify_queue import poll_single_pov_verdicts
    from crsbench.evaluation.verification.pov.store import POVStore

    # Build trial_id → state lookup and per-trial stores
    trial_id_map: dict[str, _AsyncTrialState] = {}
    trial_stores: dict[str, POVStore] = {}
    trial_results: dict[str, list[PovVerificationResult]] = {}

    all_job_ids: list[str] = []
    for state in trials:
        tid = state.trial_id
        trial_id_map[tid] = state
        # Preserve crs_run_start_time from original run
        povs_dir = state.dest_dir / "povs"
        crs_start = _load_crs_run_start_time(povs_dir)
        trial_stores[tid] = POVStore(povs_dir, crs_run_start_time=crs_start)
        trial_results[tid] = []
        all_job_ids.extend(state.job_ids)

    logger.info(f"Draining {len(all_job_ids)} async jobs across {len(trials)} trials")

    remaining = all_job_ids

    while remaining:
        completed, remaining = poll_single_pov_verdicts(redis_host, remaining)

        for result_dict in completed:
            try:
                result = SinglePovResult.from_dict(result_dict)
                tid = result.trial_id
                state = trial_id_map.get(tid)
                if not state:
                    logger.warning(f"Cannot route result for trial_id={tid}, skipping")
                    continue

                store = trial_stores[tid]
                status = PovVerificationStatus(result.verdict.status)
                cpv_matched = result.verdict.cpv_matches
                pov_hash = POVStore._extract_hash(result.verdict.pov_id)
                pov_path = state.pov_hash_to_path.get(pov_hash)

                if pov_path and pov_path.exists():
                    store.add_pov(pov_path, status, cpv_matched, pov_hash=pov_hash)

                for variant_name, crash_log in result.verdict.crash_logs.items():
                    store.store_crash_log(
                        pov_hash,
                        crash_log,
                        status,
                        cpv_matched,
                        variant_name=variant_name,
                    )

                if status == PovVerificationStatus.CPV:
                    if pov_path and pov_path.exists():
                        store.store_unique_pov(pov_path, pov_hash, status, cpv_matched)

                crash_info = None
                if result.verdict.crash_logs:
                    crash_info = {"stdout": result.verdict.crash_logs}

                trial_results[tid].append(
                    PovVerificationResult(
                        status=status,
                        benchmark=state.benchmark_name,
                        cpv_matched=cpv_matched,
                        pov_id=result.verdict.pov_id,
                        crash_info=crash_info,
                    )
                )
            except Exception as e:
                logger.warning(f"Failed to process async verdict: {e}")

        if completed:
            logger.info(
                f"Processed {len(completed)} async verdicts, {len(remaining)} pending"
            )

        if remaining:
            time.sleep(2)

    # Save per-trial results
    total = 0
    for state in trials:
        tid = state.trial_id
        results = trial_results[tid]
        store = trial_stores[tid]

        if results:
            path = _save_pov_results(results, state.dest_dir)
            logger.info(f"Wrote {len(results)} POV results to {path}")
            store.save()
            logger.info(
                f"Stored POV results to {state.dest_dir / 'povs'}: {store.get_stats()}"
            )

        total += len(results)

    return total


def _drain_all_async_patch_results(
    trials: list[_AsyncPatchTrialState],
    redis_host: str,
    timeout_seconds: float = 7200.0,
) -> int:
    """Poll all enqueued patch jobs and persist per-trial results."""
    from crsbench.distributed.patch_queue import poll_patch_verdicts

    trial_id_map: dict[str, _AsyncPatchTrialState] = {
        state.trial_id: state for state in trials
    }
    trial_results: dict[str, list[PatchVerificationResult]] = {
        state.trial_id: [] for state in trials
    }

    all_job_ids: list[str] = []
    for state in trials:
        all_job_ids.extend(state.job_ids)

    logger.info(
        f"Draining {len(all_job_ids)} async patch jobs across {len(trials)} trials"
    )
    remaining = all_job_ids
    start_time = time.monotonic()
    timed_out = False

    while remaining:
        if time.monotonic() - start_time > timeout_seconds:
            timed_out = True
            logger.warning(
                "Timed out draining async patch jobs (%d job(s) still pending)",
                len(remaining),
            )
            break
        completed, remaining = poll_patch_verdicts(redis_host, remaining)
        for result_dict in completed:
            try:
                tid = result_dict.get("trial_id")
                if not tid or tid not in trial_id_map:
                    logger.warning(
                        f"Cannot route patch result for trial_id={tid}, skipping"
                    )
                    continue
                trial_results[tid].append(_patch_result_from_dict(result_dict))
                try:
                    _write_async_patch_logs(trial_id_map[tid].dest_dir, result_dict)
                except Exception as e:
                    logger.warning(f"Failed to persist async patch logs: {e}")
            except Exception as e:
                logger.warning(f"Failed to process async patch verdict: {e}")

        if completed:
            logger.info(
                f"Processed {len(completed)} async patch verdicts, "
                f"{len(remaining)} pending"
            )
        if remaining:
            time.sleep(2)

    total = 0
    for state in trials:
        results = trial_results[state.trial_id]
        if results:
            path = _save_patch_results(results, state.total_input_povs, state.dest_dir)
            logger.info(f"Wrote {len(results)} patch results to {path}")
        total += len(results)

    if timed_out:
        raise TimeoutError(
            "Timed out draining async patch jobs "
            f"({len(remaining)} job(s) still pending)"
        )
    return total


def _reeval_patch_generation(
    trial_dir: Path,
    benchmark_path: Path,
    oss_fuzz_path: Path,
    harness: str,
    dest_dir: Path,
    source_mode: str,
    build_workers: Optional[int],
    verify_workers: Optional[int],
    per_pov_verify_timeout: int = 180,
    *,
    patch_verify_variants: bool = False,
    force_rebuild: bool,
    use_inc_build: bool,
) -> int:
    """Re-evaluate a patch_generation trial.

    Args:
        trial_dir: Trial directory with CRS outputs
        benchmark_path: Path to benchmark project
        oss_fuzz_path: Path to oss-fuzz
        harness: Harness name
        dest_dir: Where to write results
        source_mode: Source mode (pkgs/main_repo)
        build_workers: Parallel build workers
        verify_workers: Parallel verify workers
        patch_verify_variants: Whether to verify patches against all POV
            variants per CPV (False = single POV)
        force_rebuild: Force variant rebuild
        use_inc_build: Use incremental builds

    Returns:
        Number of results produced
    """
    from crsbench.evaluation.verification.patch.engine import (
        PatchVerificationEngine,
    )

    patch_dir = TrialDir(trial_dir).output_patches
    if not patch_dir.exists():
        logger.info(
            f"No patches directory found (CRS produced no patches): {patch_dir}"
        )
        return 0

    pov_dir = TrialDir(trial_dir).input_povs
    if not pov_dir.exists():
        logger.warning(f"No POVs directory found: {pov_dir}")
        return 0

    work_dir = TrialDir(dest_dir).patch_verify_dir
    work_dir.mkdir(parents=True, exist_ok=True)

    engine = PatchVerificationEngine(
        oss_fuzz_path=oss_fuzz_path,
        timeout=per_pov_verify_timeout,
        log_dir=work_dir,
        force_rebuild=force_rebuild,
        use_inc_build=use_inc_build,
        build_workers=build_workers,
        verify_workers=verify_workers,
        verify_variants=patch_verify_variants,
        source_mode=source_mode,
    )

    results = engine.verify_patches(
        benchmark_path=benchmark_path,
        patch_dir=patch_dir,
        harness=harness,
        pov_dir=pov_dir,
    )

    if results:
        total_input_povs = TrialDir(trial_dir).count_visible_input_povs()
        path = _save_patch_results(results, total_input_povs, dest_dir)
        logger.info(f"Wrote {len(results)} patch results to {path}")

    return len(results)


def run_reeval(args: argparse.Namespace) -> int:
    """Execute the re-eval command.

    Args:
        args: Parsed command-line arguments

    Returns:
        Exit code (0 for success, non-zero for errors)
    """
    from dotenv import load_dotenv

    from crsbench.reporting.snapshot_loader import discover_trials
    from crsbench.utils.logger import configure_logger
    from crsbench.validation.schemas import TrialMode

    load_dotenv(override=True)

    log_level = "DEBUG" if args.verbose else "INFO"
    configure_logger(level=log_level)

    config = _load_experiment_config(args.experiment_config)

    experiment_dir = _resolve_experiment_dir(config)
    if not experiment_dir.exists():
        logger.error(f"Experiment directory not found: {experiment_dir}")
        return 1

    oss_fuzz_path = args.oss_fuzz_path or Path("./oss-fuzz")
    if not oss_fuzz_path.exists():
        logger.error(f"OSS-Fuzz directory not found: {oss_fuzz_path}")
        return 1

    benchmarks_root = config.get("benchmarks_root")
    if benchmarks_root:
        benchmarks_root = Path(benchmarks_root)

    source_mode = args.source
    use_inc_build = args.inc_build

    # Resolve per-POV verify timeout: CLI flag > config > default 180s
    per_pov_verify_timeout = (
        args.per_pov_verify_timeout or config.get("per_pov_verify_timeout") or 180
    )
    logger.info(f"Per-POV verify timeout: {per_pov_verify_timeout}s")
    patch_verify_variants = bool(config.get("patch_verify_variants", False))
    logger.info(f"Patch verify variants: {patch_verify_variants}")

    # Async mode: initialize Redis queues when redis_host is configured
    redis_host = normalize_redis_host(config.get("redis_host"))
    experiment_name = config.get("experiment", "default")
    verify_queue = None
    patch_build_queue = None
    patch_verify_queue = None
    runtime_session = None
    if redis_host:
        from crsbench.distributed.queue import resolve_queue_names
        from crsbench.distributed.registry import RuntimeRegistration
        from crsbench.distributed.runtime_session import (
            DistributedRuntimeSession,
            LockContentionError,
        )

        runtime_session = DistributedRuntimeSession.for_reeval(
            redis_host=redis_host, experiment_name=experiment_name
        )
        if runtime_session is None:
            logger.error("Failed to connect to Redis re-eval queues")
            return 1
        # for_reeval wires trial_queue to the regular verify queue
        verify_queue = runtime_session.trial_queue
        patch_build_queue = runtime_session.build_queue
        patch_verify_queue = runtime_session.verify_queue

        # Configless evaluators discover queues via Redis registry.
        existing = runtime_session.registry.get_experiment(experiment_name)
        if existing is None:
            trial_queue, build_queue, verify_queue_name = resolve_queue_names(
                experiment_name
            )
            registration = RuntimeRegistration(
                experiment=experiment_name,
                trial_queue=trial_queue,
                build_queue=build_queue,
                verify_queue=verify_queue_name,
                oss_fuzz_path=str(oss_fuzz_path),
                benchmarks_root=str(benchmarks_root or Path("benchmarks")),
                source_mode=source_mode,
                per_pov_verify_timeout=per_pov_verify_timeout,
            )
            try:
                runtime_session.register_or_raise(registration)
            except LockContentionError:
                logger.error(
                    f"Experiment '{experiment_name}' is already locked. "
                    "Cannot register re-eval runtime."
                )
                return 1
            except Exception as exc:
                runtime_session.cleanup()
                logger.error(
                    f"Failed to register re-eval experiment in Redis registry: {exc}"
                )
                return 1
            logger.info(
                f"Registered re-eval experiment in Redis registry: {experiment_name}"
            )
        else:
            logger.info(
                f"Experiment already registered in Redis registry: {experiment_name}"
            )
        logger.info(f"Async mode: using Redis queues at {redis_host}")

    exit_code = 0
    try:
        # Discover trials
        trials = discover_trials(experiment_dir)
        if not trials:
            logger.error(f"No trials found in {experiment_dir}")
            return 1

        valid_trials = [t for t in trials if t.status == "valid"]
        reeval_trials = [t for t in valid_trials if t.reeval_ready]
        skipped_trials = [t for t in valid_trials if not t.reeval_ready]
        logger.info(
            f"Discovered {len(trials)} trials ({len(valid_trials)} valid) "
            f"in {experiment_dir}"
        )
        if skipped_trials:
            logger.info(
                f"Skipping {len(skipped_trials)} valid trial(s) without re-eval inputs"
            )
            for trial in skipped_trials:
                logger.debug(
                    f"Skip trial {trial.trial_dir}: {trial.reeval_reason or 'not ready'}"
                )

        total_results = 0
        errors = 0
        drain_failed = False
        async_trials: list[_AsyncTrialState] = []
        async_patch_trials: list[_AsyncPatchTrialState] = []

        for trial in reeval_trials:
            trial_dir = trial.trial_dir
            benchmark_name = trial.benchmark
            harness = trial.harness
            mode = trial.mode

            logger.info(
                f"Re-evaluating: {benchmark_name} / {harness} / {mode.value} "
                f"(trial {trial.trial_num})"
            )

            benchmark_path = _resolve_benchmark_path(benchmark_name, benchmarks_root)
            if not benchmark_path.exists():
                logger.error(f"Benchmark not found: {benchmark_path}, skipping")
                errors += 1
                continue

            dest_dir = _resolve_output_dir(trial_dir, args.output, experiment_dir)

            try:
                if mode == TrialMode.bug_finding:
                    if verify_queue and redis_host:
                        # Phase 1: enqueue only, defer polling
                        state = _enqueue_trial_povs(
                            trial_dir=trial_dir,
                            benchmark_name=benchmark_name,
                            harness=harness,
                            dest_dir=dest_dir,
                            verify_queue=verify_queue,
                            experiment_name=experiment_name,
                        )
                        if state:
                            async_trials.append(state)
                        continue
                    count = _reeval_bug_finding(
                        trial_dir=trial_dir,
                        benchmark_path=benchmark_path,
                        oss_fuzz_path=oss_fuzz_path,
                        harness=harness,
                        dest_dir=dest_dir,
                        source_mode=source_mode,
                        build_workers=args.build_workers,
                        verify_workers=args.verify_workers,
                        per_pov_verify_timeout=per_pov_verify_timeout,
                        force_rebuild=args.force_rebuild,
                        use_inc_build=use_inc_build,
                    )
                    total_results += count

                elif mode == TrialMode.patch_generation:
                    if patch_build_queue and patch_verify_queue and redis_host:
                        state = _enqueue_trial_patches(
                            trial_dir=trial_dir,
                            benchmark_name=benchmark_name,
                            harness=harness,
                            dest_dir=dest_dir,
                            build_queue=patch_build_queue,
                            verify_queue=patch_verify_queue,
                            experiment_name=experiment_name,
                            source_mode=source_mode,
                            patch_verify_variants=patch_verify_variants,
                            use_inc_build=use_inc_build,
                        )
                        if state:
                            async_patch_trials.append(state)
                        continue
                    count = _reeval_patch_generation(
                        trial_dir=trial_dir,
                        benchmark_path=benchmark_path,
                        oss_fuzz_path=oss_fuzz_path,
                        harness=harness,
                        dest_dir=dest_dir,
                        source_mode=source_mode,
                        build_workers=args.build_workers,
                        verify_workers=args.verify_workers,
                        per_pov_verify_timeout=per_pov_verify_timeout,
                        patch_verify_variants=patch_verify_variants,
                        force_rebuild=args.force_rebuild,
                        use_inc_build=use_inc_build,
                    )
                    total_results += count

                else:
                    logger.warning(
                        f"Unknown mode '{mode.value}' for trial {trial_dir}, skipping"
                    )

            except Exception:
                logger.exception(f"Error re-evaluating trial {trial_dir}")
                errors += 1

        # Phase 2: drain all async results in a single unified poll loop
        # async_trials is non-empty only in async mode.
        if async_trials and redis_host:
            try:
                total_results += _drain_all_async_results(async_trials, redis_host)
            except Exception:
                logger.exception("Error draining async POV verification results")
                errors += 1
                drain_failed = True
        if async_patch_trials and redis_host:
            try:
                total_results += _drain_all_async_patch_results(
                    async_patch_trials, redis_host
                )
            except Exception:
                logger.exception("Error draining async patch verification results")
                errors += 1
                drain_failed = True

        logger.info(
            f"Re-evaluation complete: {total_results} results from "
            f"{len(reeval_trials)} trials ({errors} errors)"
        )
        exit_code = 1 if drain_failed or (errors > 0 and total_results == 0) else 0
    finally:
        if runtime_session:
            runtime_session.cleanup()

    return exit_code
