#!/usr/bin/env python3
"""Fix crs_run_start_time in existing pov_store.json files.

The crs_run_start_time was incorrectly set to trial start time (before build),
but it should be set to CRS run start time (after build). This script corrects
the timestamps by adding build_time from metadata.json.

Formula:
    correct_crs_run_start_time = old_crs_run_start_time + build_time
    correct_relative_time = discovery_ts - correct_crs_run_start_time

Usage:
    # Dry run (show what would be changed)
    python scripts/fix-pov-store-timestamps.py /path/to/experiment-data

    # Actually fix the files
    python scripts/fix-pov-store-timestamps.py /path/to/experiment-data --fix
"""

import argparse
import json
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


def fix_pov_store(
    trial_dir: Path, dry_run: bool = True, min_build_time: float = 1.0
) -> dict:
    """Fix crs_run_start_time in a single pov_store.json.

    Args:
        trial_dir: Path to trial directory
        dry_run: If True, don't actually modify files
        min_build_time: Only fix if build_time >= this value

    Returns:
        Dict with fix details or None if no fix needed
    """
    metadata_path = trial_dir / "metadata.json"
    pov_store_path = trial_dir / "povs" / "pov_store.json"

    # Load metadata
    metadata = load_json(metadata_path)
    build_time = metadata.get("build_time", 0.0)

    # Skip if build_time is below threshold
    if build_time < min_build_time:
        return None

    # Load pov_store
    pov_store = load_json(pov_store_path)

    old_crs_run_start = pov_store.get("crs_run_start_time")
    if old_crs_run_start is None:
        return None

    # Calculate correct crs_run_start_time
    new_crs_run_start = old_crs_run_start + build_time

    # Track changes
    changes = {
        "trial_dir": str(trial_dir),
        "build_time": build_time,
        "old_crs_run_start_time": old_crs_run_start,
        "new_crs_run_start_time": new_crs_run_start,
        "cpv_changes": [],
    }

    # Update crs_run_start_time
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

    # Save if not dry run
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
        help="Show build time statistics",
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
            metadata = load_json(trial_dir / "metadata.json")
            bt = metadata.get("build_time", 0.0)
            build_times.append(bt)

        if build_times:
            print("Build time statistics:")
            print(f"  Min:    {min(build_times):.3f}s")
            print(f"  Max:    {max(build_times):.3f}s")
            print(f"  Mean:   {sum(build_times) / len(build_times):.3f}s")
            print(f"  >= 1s:  {sum(1 for bt in build_times if bt >= 1.0)}")
            print(f"  >= 10s: {sum(1 for bt in build_times if bt >= 10.0)}")
            print(f"  >= 60s: {sum(1 for bt in build_times if bt >= 60.0)}")
            print()

    fixed_count = 0
    skipped_count = 0

    for trial_dir in trial_dirs:
        changes = fix_pov_store(
            trial_dir, dry_run=not args.fix, min_build_time=args.min_build_time
        )

        if changes is None:
            skipped_count += 1
            continue

        fixed_count += 1

        # Print changes
        rel_path = trial_dir.relative_to(args.experiment_dir)
        print(f"{'Fixed' if args.fix else 'Would fix'}: {rel_path}")
        print(f"  build_time: {changes['build_time']:.2f}s")
        print(f"  crs_run_start_time: {changes['old_crs_run_start_time']:.2f} -> {changes['new_crs_run_start_time']:.2f}")

        for cpv_change in changes["cpv_changes"]:
            old_rt = cpv_change["old_relative_time"]
            new_rt = cpv_change["new_relative_time"]
            diff = cpv_change["diff"]
            print(f"  {cpv_change['cpv_id']}: relative_time {old_rt:.2f}s -> {new_rt:.2f}s (diff: {diff:.2f}s)")
        print()

    print("=" * 60)
    print(f"Summary: {fixed_count} fixed, {skipped_count} skipped")

    if not args.fix and fixed_count > 0:
        print()
        print("Run with --fix to apply changes")


if __name__ == "__main__":
    main()
