"""CLI command for re-evaluation of completed experiment trials.

Re-runs verification on existing trial outputs without re-running CRS.
Discovers trials from an experiment directory structure and dispatches to
VerificationEngine (bug_finding) or PatchVerificationEngine (patch_generation).

Usage:
    crsbench re-eval --experiment-config experiment-config.yaml
    crsbench re-eval -c experiment-config.yaml --output /tmp/reeval-results
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

import yaml

from crsbench.evaluation.verification.models import (
    PatchVerificationOutput,
)
from crsbench.utils.logger import get_logger

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
        default="main_repo",
        help="Source mode for builds (default: main_repo)",
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

    pov_dir = trial_dir / "output" / "povs"
    if not pov_dir.exists():
        logger.warning(f"No POV directory found: {pov_dir}")
        return 0

    engine = VerificationEngine(
        oss_fuzz_path=oss_fuzz_path,
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

    return len(output.results)


def _reeval_patch_generation(
    trial_dir: Path,
    benchmark_path: Path,
    oss_fuzz_path: Path,
    harness: str,
    dest_dir: Path,
    source_mode: str,
    build_workers: Optional[int],
    verify_workers: Optional[int],
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
    from crsbench.reporting.snapshot_loader import discover_trials
    from crsbench.utils.logger import configure_logger
    from crsbench.validation.schemas import TrialMode

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
                count = _reeval_bug_finding(
                    trial_dir=trial_dir,
                    benchmark_path=benchmark_path,
                    oss_fuzz_path=oss_fuzz_path,
                    harness=harness,
                    dest_dir=dest_dir,
                    source_mode=source_mode,
                    build_workers=args.build_workers,
                    verify_workers=args.verify_workers,
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

    logger.info(
        f"Re-evaluation complete: {total_results} results from "
        f"{len(valid_trials)} trials ({errors} errors)"
    )

    return 1 if errors > 0 and total_results == 0 else 0
