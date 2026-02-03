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
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import yaml

from crsbench.evaluation.verification.models import (
    PatchVerificationOutput,
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
        "--oss-fuzz",
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
        default=False,
        help="Force rebuild of variants",
    )
    parser.add_argument(
        "--no-inc-build",
        action="store_true",
        default=False,
        help="Disable incremental builds",
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
        default=False,
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
    filestore = Path(config["experiment_filestore"])
    experiment = config["experiment"]
    return filestore / experiment


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
    root = benchmarks_root or Path("benchmarks")
    return root / benchmark_name


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
    if output_base is None:
        return trial_dir

    # Mirror path: output_base / <relative path from experiment_dir>
    relative = trial_dir.relative_to(experiment_dir)
    return output_base / relative


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

    pov_dir = trial_dir / "output" / "povs"
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
        store = POVStore(dest_dir / "povs")
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
            if result.crash_info and "logs" in result.crash_info:
                for variant_name, crash_log in result.crash_info["logs"].items():
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

    pov_dir = trial_dir / "output" / "povs"
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
        trial_stores[tid] = POVStore(state.dest_dir / "povs")
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
                    crash_info = {"logs": result.verdict.crash_logs}

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
        force_rebuild: Force variant rebuild
        use_inc_build: Use incremental builds

    Returns:
        Number of results produced
    """
    from crsbench.evaluation.verification.patch.engine import (
        PatchVerificationEngine,
    )

    patch_dir = trial_dir / "output" / "patches"
    if not patch_dir.exists():
        logger.warning(f"No patches directory found: {patch_dir}")
        return 0

    pov_dir = trial_dir / "crs-input" / "povs"
    if not pov_dir.exists():
        logger.warning(f"No POVs directory found: {pov_dir}")
        return 0

    work_dir = dest_dir / "patch-verify"
    work_dir.mkdir(parents=True, exist_ok=True)

    engine = PatchVerificationEngine(
        oss_fuzz_path=oss_fuzz_path,
        timeout=per_pov_verify_timeout,
        work_dir=work_dir,
        force_rebuild=force_rebuild,
        use_inc_build=use_inc_build,
        build_workers=build_workers,
        verify_workers=verify_workers,
        source_mode=source_mode,
    )

    results = engine.verify_patches(
        benchmark_path=benchmark_path,
        patch_dir=patch_dir,
        harness=harness,
        pov_dir=pov_dir,
    )

    if results:
        total_input_povs = len(list(pov_dir.iterdir())) if pov_dir.exists() else 0
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

    oss_fuzz_path = args.oss_fuzz or Path("./oss-fuzz")
    if not oss_fuzz_path.exists():
        logger.error(f"OSS-Fuzz directory not found: {oss_fuzz_path}")
        return 1

    benchmarks_root = config.get("benchmarks_root")
    if benchmarks_root:
        benchmarks_root = Path(benchmarks_root)

    source_mode = args.source
    use_inc_build = not args.no_inc_build

    # Resolve per-POV verify timeout: CLI flag > config > default 180s
    per_pov_verify_timeout = (
        args.per_pov_verify_timeout or config.get("per_pov_verify_timeout") or 180
    )
    logger.info(f"Per-POV verify timeout: {per_pov_verify_timeout}s")

    # Async mode: initialize Redis verify queue if redis_host is configured
    redis_host = config.get("redis_host")
    experiment_name = config.get("experiment", "default")
    verify_queue = None
    if redis_host:
        from crsbench.distributed.verify_queue import initialize_verify_queue

        verify_queue = initialize_verify_queue(redis_host, experiment_name)
        if verify_queue is None:
            logger.error("Failed to connect to Redis verify queue")
            return 1
        logger.info(f"Async mode: using Redis verify queue at {redis_host}")

    # Discover trials
    trials = discover_trials(experiment_dir)
    if not trials:
        logger.error(f"No trials found in {experiment_dir}")
        return 1

    valid_trials = [t for t in trials if t.status == "valid"]
    logger.info(
        f"Discovered {len(trials)} trials ({len(valid_trials)} valid) "
        f"in {experiment_dir}"
    )

    total_results = 0
    errors = 0
    async_trials: list[_AsyncTrialState] = []

    for trial in valid_trials:
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
    # async_trials is non-empty only when redis_host was truthy (str)
    if async_trials and redis_host:
        total_results += _drain_all_async_results(async_trials, redis_host)

    logger.info(
        f"Re-evaluation complete: {total_results} results from "
        f"{len(valid_trials)} trials ({errors} errors)"
    )

    return 1 if errors > 0 and total_results == 0 else 0
