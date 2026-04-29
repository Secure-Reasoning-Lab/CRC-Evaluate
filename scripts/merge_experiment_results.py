#!/usr/bin/env python3
"""Merge CRS evaluation results from multiple worker machines.

When running distributed experiments, each worker creates its own timestamped
result directory. This script merges multiple such directories into a single
unified experiment-data directory.

Usage:
    # Option 1: Explicit input directories
    python scripts/merge_experiment_results.py \
        --input-dirs /path/to/exp1 /path/to/exp2 ... \
        --output-dir /path/to/merged/experiment-data

    # Option 2: Glob pattern
    python scripts/merge_experiment_results.py \
        --input-pattern "/path/to/{experiment}_*_*/experiment-data" \
        --output-dir /path/to/merged/experiment-data
"""

import argparse
import json
import re
import shutil
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from crsbench.utils.logger import get_logger
from crsbench.validation.schemas import TrialMetadata

logger = get_logger(__name__)


@dataclass
class TrialInfo:
    """Information about a trial directory."""

    path: Path
    relative_path: Path  # Relative to experiment-data/
    status: str  # "success", "fail", "unknown"
    crs: str
    benchmark: str
    harness: str
    mode: str
    sanitizer: str
    trial_num: int
    cpv: str | None = None  # e.g. "cpv_0"; None for layouts without a cpv level

    @property
    def identity(self) -> tuple[str, str, str, str | None, str, str, int]:
        """Return unique identity tuple for conflict detection."""
        return (
            self.crs,
            self.benchmark,
            self.harness,
            self.cpv,
            self.mode,
            self.sanitizer,
            self.trial_num,
        )

    @property
    def logical_identity(self) -> tuple[str, str, str, str | None, str, str]:
        """Return trial identity without the trial number."""
        return (
            self.crs,
            self.benchmark,
            self.harness,
            self.cpv,
            self.mode,
            self.sanitizer,
        )


@dataclass
class Conflict:
    """Represents a trial conflict (multiple successful trials with same identity)."""

    identity: tuple[str, str, str, str | None, str, str, int]
    trials: list[TrialInfo]


@dataclass
class MergeResult:
    """Result of merge operation."""

    merged_count: int
    skipped_count: int
    skipped_trials: list[TrialInfo]
    trial_number_by_source_path: dict[Path, int] = field(default_factory=dict)


def find_experiment_data_dirs(input_paths: list[Path]) -> list[Path]:
    """Find all experiment-data directories recursively in input paths."""
    exp_data_dirs = []

    for path in input_paths:
        if not path.exists():
            logger.warning(f"Path does not exist: {path}")
            continue

        if path.name == "experiment-data" and path.is_dir():
            exp_data_dirs.append(path)
        else:
            # Search recursively for experiment-data directories
            for exp_data in sorted(path.rglob("experiment-data")):
                if exp_data.is_dir():
                    exp_data_dirs.append(exp_data)

    return exp_data_dirs


def get_trial_status(trial_dir: Path) -> str:
    """Check for .success or .fail marker, return status."""
    if (trial_dir / ".success").exists():
        return "success"
    if (trial_dir / ".fail").exists():
        return "fail"
    return "unknown"


def parse_trial_path(
    relative_path: Path,
) -> tuple[str, str, str, str | None, str, str, int]:
    """Parse trial path to extract components.

    Supports two layouts:
      - Legacy (6 levels): {crs}/{benchmark}/{harness}/{mode}/{sanitizer}/trial-{N}
      - With CPV (7 levels): {crs}/{benchmark}/{harness}/{cpv}/{mode}/{sanitizer}/trial-{N}

    Returns: (crs, benchmark, harness, cpv, mode, sanitizer, trial_num)
    where cpv is None for the legacy layout.
    """
    parts = relative_path.parts

    trial_dirname = parts[-1] if parts else ""
    if not trial_dirname.startswith("trial-"):
        raise ValueError(f"Invalid trial directory name: {trial_dirname}")

    try:
        trial_num = int(trial_dirname.split("-")[1])
    except (IndexError, ValueError) as e:
        raise ValueError(f"Invalid trial directory name: {trial_dirname}") from e

    if len(parts) == 7:
        crs, benchmark, harness, cpv, mode, sanitizer = parts[:6]
        return (crs, benchmark, harness, cpv, mode, sanitizer, trial_num)
    if len(parts) == 6:
        crs, benchmark, harness, mode, sanitizer = parts[:5]
        return (crs, benchmark, harness, None, mode, sanitizer, trial_num)
    raise ValueError(
        f"Invalid trial path format: expected 6 or 7 path components, "
        f"got {len(parts)}: {relative_path}"
    )


def _trial_sort_key(trial_path: Path) -> tuple[tuple[str, ...], int, str]:
    """Sort trial paths by parent path and numeric trial number."""
    try:
        trial_num = int(trial_path.name.removeprefix("trial-"))
    except ValueError:
        trial_num = -1
    return trial_path.parent.parts, trial_num, trial_path.name


def enumerate_trials(exp_data_dir: Path) -> list[TrialInfo]:
    """List all trial directories with their relative paths and status.

    Matches both the legacy 6-level layout and the 7-level layout with a CPV level.
    """
    trials = []

    # Match trial-* directories at either 6 or 7 component depths.
    patterns = ("*/*/*/*/*/trial-*", "*/*/*/*/*/*/trial-*")
    seen: set[Path] = set()
    for pattern in patterns:
        for trial_path in sorted(exp_data_dir.glob(pattern), key=_trial_sort_key):
            if trial_path in seen or not trial_path.is_dir():
                continue
            seen.add(trial_path)

            relative_path = trial_path.relative_to(exp_data_dir)

            try:
                crs, benchmark, harness, cpv, mode, sanitizer, trial_num = (
                    parse_trial_path(relative_path)
                )
            except ValueError as e:
                logger.warning(f"Skipping invalid trial path {relative_path}: {e}")
                continue

            status = get_trial_status(trial_path)

            trial_info = TrialInfo(
                path=trial_path,
                relative_path=relative_path,
                status=status,
                crs=crs,
                benchmark=benchmark,
                harness=harness,
                cpv=cpv,
                mode=mode,
                sanitizer=sanitizer,
                trial_num=trial_num,
            )
            trials.append(trial_info)

    return trials


def detect_conflicts(trials: list[TrialInfo]) -> list[Conflict]:
    """Group trials by identity and return conflicts.

    Conflict = multiple trials with .success status for same identity.
    """
    # Group by identity
    by_identity = defaultdict(list)
    for trial in trials:
        by_identity[trial.identity].append(trial)

    # Find conflicts
    conflicts = []
    for identity, trial_group in by_identity.items():
        # Count successful trials
        successful = [t for t in trial_group if t.status == "success"]

        if len(successful) > 1:
            conflicts.append(Conflict(identity=identity, trials=successful))

    return conflicts


def print_conflict_report(conflicts: list[Conflict]) -> None:
    """Print detailed conflict report."""
    logger.error("=" * 80)
    logger.error(
        f"CONFLICT DETECTED: {len(conflicts)} trial(s) have multiple successful runs"
    )
    logger.error("=" * 80)

    for i, conflict in enumerate(conflicts, 1):
        crs, benchmark, harness, cpv, mode, sanitizer, trial_num = conflict.identity

        identity_parts = [crs, benchmark, harness]
        if cpv is not None:
            identity_parts.append(cpv)
        identity_parts.extend([mode, sanitizer, f"trial-{trial_num}"])
        logger.error(f"\nConflict #{i}:")
        logger.error(f"  Identity: {'/'.join(identity_parts)}")
        logger.error(f"  Found {len(conflict.trials)} successful trials:")

        for trial in conflict.trials:
            logger.error(f"    - {trial.path}")

            # Try to read metadata timestamp
            metadata_file = trial.path / "metadata.json"
            if metadata_file.exists():
                try:
                    metadata = TrialMetadata.model_validate_json(
                        metadata_file.read_text()
                    )
                    logger.error(f"      Timestamp: {metadata.timestamp}")
                except Exception as e:
                    logger.error(f"      (Could not read metadata: {e})")

    logger.error("\n" + "=" * 80)
    logger.error("ACTION REQUIRED:")
    logger.error("Please manually remove one of the conflicting source directories")
    logger.error("or trial directories, then re-run the merge.")
    logger.error("=" * 80)


def copy_trial(trial_path: Path, dest_path: Path) -> bool:
    """Copy a trial directory, excluding large build artifacts.

    Excludes: crs-build/ directory (large build artifacts)
    """
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    # Copy entire directory first
    if dest_path.exists():
        logger.warning(f"Destination already exists, skipping: {dest_path}")
        return False

    shutil.copytree(trial_path, dest_path)

    # Remove crs-build/ if it exists (large build artifacts)
    crs_build = dest_path / "crs-build"
    if crs_build.exists():
        shutil.rmtree(crs_build)
        logger.debug(f"Removed crs-build/ from {dest_path}")
    return True


def _relative_path_with_trial_num(relative_path: Path, trial_num: int) -> Path:
    """Return relative path with its final trial directory replaced."""
    parts = list(relative_path.parts)
    if not parts:
        raise ValueError("Cannot renumber empty trial path")
    parts[-1] = f"trial-{trial_num}"
    return Path(*parts)


def _renumber_trial_id(value: str, old_trial_num: int, new_trial_num: int) -> str:
    """Best-effort rewrite for LLM trial_id strings."""
    value = re.sub(
        rf"(?<![A-Za-z0-9])trial-{old_trial_num}(?![0-9])",
        f"trial-{new_trial_num}",
        value,
    )
    return re.sub(
        rf"(?<![A-Za-z0-9])trial{old_trial_num}(?![0-9])",
        f"trial{new_trial_num}",
        value,
    )


def _rewrite_json_object(path: Path, rewrite) -> None:
    if not path.exists():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logger.warning(f"Could not parse JSON for renumbering: {path}")
        return
    if not isinstance(payload, dict):
        return
    updated = rewrite(payload)
    path.write_text(json.dumps(updated, indent=2) + "\n", encoding="utf-8")


def _rewrite_copied_trial_metadata(dest_path: Path, *, new_trial_num: int) -> None:
    def _rewrite(payload: dict) -> dict:
        payload["trial_num"] = new_trial_num
        return payload

    _rewrite_json_object(dest_path / "metadata.json", _rewrite)


def _rewrite_copied_llm_usage(
    dest_path: Path, *, old_trial_num: int, new_trial_num: int
) -> None:
    def _rewrite(payload: dict) -> dict:
        _renumber_llm_identity_fields(payload, old_trial_num, new_trial_num)
        return payload

    _rewrite_json_object(dest_path / "llm-usage.json", _rewrite)


def _renumber_llm_identity_fields(
    payload: dict, old_trial_num: int, new_trial_num: int
) -> None:
    for key, value in list(payload.items()):
        if key in {"trial_id", "key_alias"} and isinstance(value, str):
            payload[key] = _renumber_trial_id(value, old_trial_num, new_trial_num)
        elif key == "trial_num" and value == old_trial_num:
            payload[key] = new_trial_num
        elif isinstance(value, dict):
            _renumber_llm_identity_fields(value, old_trial_num, new_trial_num)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _renumber_llm_identity_fields(item, old_trial_num, new_trial_num)


def merge_trials(
    trials: list[TrialInfo], output_dir: Path, *, renumber_trials: bool = False
) -> MergeResult:
    """Filter to successful trials and copy to output directory."""
    merged_count = 0
    skipped_trials = []
    trial_number_by_source_path: dict[Path, int] = {}
    next_trial_num_by_identity: defaultdict[
        tuple[str, str, str, str | None, str, str], int
    ] = defaultdict(int)

    for trial in trials:
        if trial.status != "success":
            skipped_trials.append(trial)
            continue

        if renumber_trials:
            next_trial_num_by_identity[trial.logical_identity] += 1
            destination_trial_num = next_trial_num_by_identity[trial.logical_identity]
            relative_path = _relative_path_with_trial_num(
                trial.relative_path, destination_trial_num
            )
        else:
            destination_trial_num = trial.trial_num
            relative_path = trial.relative_path

        dest_path = output_dir / relative_path

        logger.info(f"Copying {trial.relative_path} -> {relative_path}")
        copied = copy_trial(trial.path, dest_path)
        if not copied:
            skipped_trials.append(trial)
            continue
        trial_number_by_source_path[trial.path] = destination_trial_num
        if renumber_trials:
            _rewrite_copied_trial_metadata(
                dest_path,
                new_trial_num=destination_trial_num,
            )
            _rewrite_copied_llm_usage(
                dest_path,
                old_trial_num=trial.trial_num,
                new_trial_num=destination_trial_num,
            )
        merged_count += 1

    return MergeResult(
        merged_count=merged_count,
        skipped_count=len(skipped_trials),
        skipped_trials=skipped_trials,
        trial_number_by_source_path=trial_number_by_source_path,
    )


def _matrix_entry_trial_path(exp_data_dir: Path, entry: dict) -> Path | None:
    try:
        crs = entry["crs"]
        benchmark = entry["benchmark"]
        harness = entry["harness"]
        mode = entry["mode"]
        sanitizer = entry["sanitizer"]
        trial_num = int(entry["trial_num"])
    except (KeyError, TypeError, ValueError):
        return None

    parts = [exp_data_dir, crs, benchmark, harness]
    target_cpv_id = entry.get("target_cpv_id")
    if target_cpv_id:
        parts.append(str(target_cpv_id))
    parts.extend([mode, sanitizer, f"trial-{trial_num}"])
    return Path(*parts)


def merge_trial_matrices(
    source_dirs: list[Path],
    output_dir: Path,
    *,
    experiment_name: str | None = None,
    trial_number_by_source_path: dict[Path, int] | None = None,
) -> Path | None:
    """Write a merged trial_matrix.json, rewriting copied trial numbers."""
    filter_to_copied_trials = trial_number_by_source_path is not None
    trial_number_by_source_path = trial_number_by_source_path or {}
    merged_entries: list[dict] = []
    first_experiment_name: str | None = None

    for source_dir in source_dirs:
        matrix_path = source_dir / "trial_matrix.json"
        if not matrix_path.exists():
            continue
        try:
            matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning(f"Could not parse trial matrix: {matrix_path}")
            continue
        if not isinstance(matrix, dict):
            continue
        if first_experiment_name is None and isinstance(matrix.get("experiment"), str):
            first_experiment_name = matrix["experiment"]
        entries = matrix.get("trials")
        if not isinstance(entries, list):
            continue

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            source_trial_path = _matrix_entry_trial_path(source_dir, entry)
            if source_trial_path is None:
                continue
            if filter_to_copied_trials:
                new_trial_num = trial_number_by_source_path.get(source_trial_path)
                if new_trial_num is None:
                    continue
            else:
                new_trial_num = entry.get("trial_num")

            updated = dict(entry)
            updated["trial_num"] = new_trial_num
            merged_entries.append(updated)

    if not merged_entries:
        return None

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "trial_matrix.json"
    payload = {
        "experiment": experiment_name or first_experiment_name or output_dir.name,
        "total_trials": len(merged_entries),
        "trials": merged_entries,
    }
    output_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return output_path


def print_summary(
    result: MergeResult,
    source_dirs: list[Path],
    output_dir: Path,
) -> None:
    """Print merge summary."""
    logger.info("=" * 80)
    logger.info("MERGE SUMMARY")
    logger.info("=" * 80)
    logger.info(f"Source directories: {len(source_dirs)}")
    for src in source_dirs:
        logger.info(f"  - {src}")
    logger.info("")
    logger.info(f"Trials merged (successful): {result.merged_count}")
    logger.info(f"Trials skipped (failed): {result.skipped_count}")
    logger.info("")
    logger.info(f"Output directory: {output_dir}")
    logger.info("=" * 80)

    if result.skipped_trials:
        logger.info("\nSkipped trials (failed):")
        for trial in result.skipped_trials:
            logger.info(f"  - {trial.relative_path} (status: {trial.status})")


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Merge CRS evaluation results from multiple worker machines",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Input options (mutually exclusive)
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument(
        "--input-dirs",
        nargs="+",
        type=Path,
        help="Explicit list of input directories to merge",
    )
    input_group.add_argument(
        "--input-pattern",
        type=str,
        help="Glob pattern to find input directories (e.g., '/path/to/*_*/experiment-data')",
    )

    # Output
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Output directory for merged results",
    )
    parser.add_argument(
        "--renumber-trials",
        action="store_true",
        help=(
            "Allow duplicate successful trial numbers by assigning sequential "
            "trial numbers per logical CRS/benchmark/harness/mode/sanitizer stream"
        ),
    )
    parser.add_argument(
        "--experiment-name",
        type=str,
        default=None,
        help="Experiment name to write into the merged trial_matrix.json",
    )

    args = parser.parse_args()

    # Resolve input paths
    if args.input_pattern:
        # Use Path.glob() on parent directory
        # Parse pattern to get base path and glob pattern
        pattern_path = Path(args.input_pattern)
        if "*" in str(pattern_path):
            # Find the first part without wildcards
            parts = []
            for part in pattern_path.parts:
                if "*" in part or "?" in part or "[" in part:
                    break
                parts.append(part)

            if parts:
                base_path = Path(*parts) if len(parts) > 1 else Path(parts[0])
                # Rest is the glob pattern
                remaining = str(pattern_path.relative_to(base_path))
                input_paths = sorted(base_path.glob(remaining))
            else:
                # Pattern starts with wildcard, use current directory
                input_paths = sorted(Path().glob(str(pattern_path)))
        else:
            # No wildcards, just a single path
            input_paths = [pattern_path]

        if not input_paths:
            logger.error(f"No directories found matching pattern: {args.input_pattern}")
            return 1
    else:
        input_paths = args.input_dirs

    # Find all experiment-data directories
    logger.info(f"Scanning {len(input_paths)} input path(s)...")
    exp_data_dirs = find_experiment_data_dirs(input_paths)

    if not exp_data_dirs:
        logger.error("No experiment-data directories found in input paths")
        return 1

    logger.info(f"Found {len(exp_data_dirs)} experiment-data director(ies)")

    # Enumerate all trials
    all_trials = []
    for exp_data_dir in exp_data_dirs:
        logger.info(f"Enumerating trials in {exp_data_dir}")
        trials = enumerate_trials(exp_data_dir)
        logger.info(f"  Found {len(trials)} trial(s)")
        all_trials.extend(trials)

    logger.info(f"\nTotal trials found: {len(all_trials)}")

    if args.renumber_trials:
        logger.info("Renumber-trials mode enabled; duplicate successes will be kept")
    else:
        # Detect conflicts
        logger.info("Checking for conflicts...")
        conflicts = detect_conflicts(all_trials)

        if conflicts:
            print_conflict_report(conflicts)
            return 1

        logger.info("No conflicts detected")

    # Merge trials
    logger.info(f"\nMerging trials to {args.output_dir}...")
    result = merge_trials(
        all_trials, args.output_dir, renumber_trials=args.renumber_trials
    )
    merge_trial_matrices(
        exp_data_dirs,
        args.output_dir,
        experiment_name=args.experiment_name,
        trial_number_by_source_path=result.trial_number_by_source_path,
    )

    # Print summary
    print_summary(result, exp_data_dirs, args.output_dir)

    return 0


if __name__ == "__main__":
    sys.exit(main())
