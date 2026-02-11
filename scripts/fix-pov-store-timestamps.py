#!/usr/bin/env python3
"""Fix crs_run_start_time in existing pov_store.json files.

The crs_run_start_time was incorrectly set to trial start time (before build),
but it should be set to CRS run start time (after build). This script corrects
the timestamps by extracting the actual build time from worker.log.

The metadata.json build_time field is often wrong (records ~0s when pre-build
was done separately), so we extract the real build time from worker.log:
    "Successfully built CRS for ... in 1078.7s"

Formula:
    correct_crs_run_start_time = old_crs_run_start_time + actual_build_time
    correct_relative_time = discovery_ts - correct_crs_run_start_time

Usage:
    # Dry run (show what would be changed)
    python scripts/fix-pov-store-timestamps.py /path/to/experiment-data

    # Actually fix the files
    python scripts/fix-pov-store-timestamps.py /path/to/experiment-data --fix
"""

import argparse
import json
import re
import sys
from pathlib import Path


def find_trial_dirs(experiment_dir: Path) -> list[Path]:
    """Find all trial directories containing both metadata.json and pov_store.json."""
    trial_dirs = []
    for metadata_path in experiment_dir.rglob("trial-*/metadata.json"):
        trial_dir = metadata_path.parent
        pov_store_path = trial_dir / "povs" / "pov_store.json"
        if pov_store_path.exists():
            trial_dirs.append(trial_dir)
    return sorted(trial_dirs)


def load_json(path: Path) -> dict:
    """Load JSON file."""
    return json.loads(path.read_text())


def save_json(path: Path, data: dict) -> None:
    """Save JSON file with pretty formatting."""
    path.write_text(json.dumps(data, indent=2))


def extract_build_time_from_log(trial_dir: Path) -> float | None:
    """Extract actual build time from worker.log.

    Looks for: "Successfully built CRS for ... in 1078.7s"

    Args:
        trial_dir: Path to trial directory

    Returns:
        Build time in seconds, or None if not found
    """
    worker_log = trial_dir / "worker.log"
    if not worker_log.exists():
        return None

    # Pattern: "Successfully built CRS for ... in 1078.7s"
    pattern = r"Successfully built CRS for .+ in (\d+\.?\d*)s"

    try:
        content = worker_log.read_text()
        match = re.search(pattern, content)
        if match:
            return float(match.group(1))
    except (OSError, ValueError):
        pass

    return None


def fix_trial_timestamps(
    trial_dir: Path, *, dry_run: bool = True, min_build_time: float = 1.0
) -> dict | None:
    """Fix timestamps in pov_store.json and build_time in metadata.json.

    Args:
        trial_dir: Path to trial directory
        dry_run: If True, don't actually modify files
        min_build_time: Only fix if build_time >= this value

    Returns:
        Dict with fix details or None if no fix needed
    """
    pov_store_path = trial_dir / "povs" / "pov_store.json"
    metadata_path = trial_dir / "metadata.json"

    # Extract actual build time from worker.log
    build_time = extract_build_time_from_log(trial_dir)
    if build_time is None:
        return None

    # Skip if build_time is below threshold
    if build_time < min_build_time:
        return None

    # Load metadata to check/fix build_time
    metadata = load_json(metadata_path) if metadata_path.exists() else None
    old_metadata_build_time = metadata.get("build_time") if metadata else None

    # Check if metadata.json already has correct build_time (within 1s tolerance)
    metadata_already_fixed = (
        old_metadata_build_time is not None
        and abs(old_metadata_build_time - build_time) < 1.0
    )

    # Load pov_store
    pov_store = load_json(pov_store_path) if pov_store_path.exists() else None
    old_crs_run_start = pov_store.get("crs_run_start_time") if pov_store else None

    # Determine if pov_store needs fixing by checking if relative_times make sense
    # If max relative_time < run_time, pov_store was likely already fixed
    pov_store_already_fixed = False
    if pov_store and metadata:
        run_time = metadata.get("run_time", 0)
        cpv_to_first_pov = pov_store.get("cpv_to_first_pov", {})
        if cpv_to_first_pov and run_time > 0:
            max_relative = max(
                (info.get("relative_time", 0) for info in cpv_to_first_pov.values()),
                default=0
            )
            # If relative_time is reasonable (within run_time + small buffer), already fixed
            pov_store_already_fixed = max_relative < run_time + 60

    pov_store_needs_fix = not pov_store_already_fixed and old_crs_run_start is not None

    # Calculate correct crs_run_start_time (only if pov_store needs fixing)
    new_crs_run_start = old_crs_run_start + build_time if pov_store_needs_fix else None

    # Skip if nothing to fix
    if metadata_already_fixed and not pov_store_needs_fix:
        return None

    # Track changes
    changes = {
        "trial_dir": str(trial_dir),
        "build_time": build_time,
        "old_metadata_build_time": old_metadata_build_time,
        "metadata_fixed": not metadata_already_fixed,
        "old_crs_run_start_time": old_crs_run_start if pov_store_needs_fix else None,
        "new_crs_run_start_time": new_crs_run_start,
        "cpv_changes": [],
    }

    # Update metadata.json build_time (if not already correct)
    if metadata and not metadata_already_fixed:
        metadata["build_time"] = build_time
        if not dry_run:
            save_json(metadata_path, metadata)

    # Update pov_store.json if needed
    if pov_store and pov_store_needs_fix:
        pov_store["crs_run_start_time"] = new_crs_run_start

        # Update relative_time for each CPV in cpv_to_first_pov
        cpv_to_first_pov = pov_store.get("cpv_to_first_pov", {})
        for cpv_id, cpv_info in cpv_to_first_pov.items():
            discovery_ts = cpv_info.get("discovery_ts")
            if discovery_ts is not None:
                old_relative = cpv_info.get("relative_time")
                new_relative = discovery_ts - new_crs_run_start
                cpv_info["relative_time"] = new_relative

                changes["cpv_changes"].append({
                    "cpv_id": cpv_id,
                    "old_relative_time": old_relative,
                    "new_relative_time": new_relative,
                    "diff": old_relative - new_relative if old_relative else None,
                })

        if not dry_run:
            save_json(pov_store_path, pov_store)

    return changes


def main():
    parser = argparse.ArgumentParser(
        description="Fix crs_run_start_time in pov_store.json files"
    )
    parser.add_argument(
        "experiment_dir",
        type=Path,
        help="Path to experiment data directory",
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="Actually fix the files (default is dry run)",
    )
    parser.add_argument(
        "--min-build-time",
        type=float,
        default=1.0,
        help="Only fix trials with build_time >= this value (default: 1.0)",
    )
    parser.add_argument(
        "--show-stats",
        action="store_true",
        help="Show build time statistics from worker.log",
    )
    args = parser.parse_args()

    if not args.experiment_dir.exists():
        print(f"Error: {args.experiment_dir} does not exist", file=sys.stderr)
        sys.exit(1)

    print(f"Scanning: {args.experiment_dir}")
    print(f"Mode: {'FIX' if args.fix else 'DRY RUN'}")
    print(f"Min build time: {args.min_build_time}s")
    print()

    trial_dirs = find_trial_dirs(args.experiment_dir)
    print(f"Found {len(trial_dirs)} trials with pov_store.json")
    print()

    # Collect build times for stats
    if args.show_stats:
        build_times = []
        for trial_dir in trial_dirs:
            bt = extract_build_time_from_log(trial_dir)
            if bt is not None:
                build_times.append(bt)

        if build_times:
            print("Build time statistics (from worker.log):")
            print(f"  Found:  {len(build_times)}/{len(trial_dirs)} trials")
            print(f"  Min:    {min(build_times):.1f}s")
            print(f"  Max:    {max(build_times):.1f}s")
            print(f"  Mean:   {sum(build_times) / len(build_times):.1f}s")
            print(f"  >= 1s:  {sum(1 for bt in build_times if bt >= 1.0)}")
            print(f"  >= 60s: {sum(1 for bt in build_times if bt >= 60.0)}")
            print(f"  >= 300s: {sum(1 for bt in build_times if bt >= 300.0)}")
            print()

    fixed_count = 0
    skipped_count = 0
    no_log_count = 0

    for trial_dir in trial_dirs:
        changes = fix_trial_timestamps(
            trial_dir, dry_run=not args.fix, min_build_time=args.min_build_time
        )

        if changes is None:
            # Check if it's because of missing log or low build time
            bt = extract_build_time_from_log(trial_dir)
            if bt is None:
                no_log_count += 1
            else:
                skipped_count += 1
            continue

        fixed_count += 1

        # Print changes
        rel_path = trial_dir.relative_to(args.experiment_dir)
        print(f"{'Fixed' if args.fix else 'Would fix'}: {rel_path}")
        old_meta_bt = changes.get('old_metadata_build_time')
        if old_meta_bt is not None:
            print(f"  metadata.json build_time: {old_meta_bt:.3f}s -> {changes['build_time']:.1f}s")
        if changes.get('old_crs_run_start_time') is not None:
            print(f"  pov_store crs_run_start_time: {changes['old_crs_run_start_time']:.2f} -> {changes['new_crs_run_start_time']:.2f}")

        for cpv_change in changes["cpv_changes"]:
            old_rt = cpv_change["old_relative_time"]
            new_rt = cpv_change["new_relative_time"]
            diff = cpv_change["diff"]
            if old_rt is not None and diff is not None:
                print(f"  {cpv_change['cpv_id']}: relative_time {old_rt:.1f}s -> {new_rt:.1f}s (diff: {diff:.1f}s)")
            else:
                print(f"  {cpv_change['cpv_id']}: relative_time -> {new_rt:.1f}s")
        print()

    print("=" * 60)
    print(f"Summary: {fixed_count} fixed, {skipped_count} skipped (build_time < {args.min_build_time}s), {no_log_count} no worker.log")

    if not args.fix and fixed_count > 0:
        print()
        print("Run with --fix to apply changes")


if __name__ == "__main__":
    main()
