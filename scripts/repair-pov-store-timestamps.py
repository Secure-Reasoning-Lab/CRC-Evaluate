#!/usr/bin/env python3
"""Repair corrupted crs_run_start_time in pov_store.json files.

Re-eval previously overwrote crs_run_start_time with time.time() instead of
preserving the original value. This script recovers the correct value from
snapshot JSON files (timestamp - elapsed_time) and recomputes cpv_to_first_pov
relative_time using on-disk file_mtime.

Usage:
    # Dry run (default) — show what would change
    python3 scripts/repair-pov-store-timestamps.py /path/to/experiment-dir

    # Apply fixes
    python3 scripts/repair-pov-store-timestamps.py /path/to/experiment-dir --apply
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path


def recover_crs_start_from_snapshots(povs_dir: Path) -> float | None:
    """Recover crs_run_start_time from snapshot JSON files.

    Each snapshot records both wall-clock timestamp and elapsed_time since
    CRS start, so: crs_run_start_time = timestamp - elapsed_time.
    """
    snap_dir = povs_dir / "snapshots"
    if not snap_dir.exists():
        return None

    for snap_file in sorted(snap_dir.glob("snapshot-*.json")):
        try:
            data = json.loads(snap_file.read_text())
            ts = data.get("timestamp")
            elapsed = data.get("elapsed_time")
            if ts is not None and elapsed is not None:
                return ts - elapsed
        except (json.JSONDecodeError, OSError):
            continue

    return None


def is_corrupted(
    crs_start: float, exec_ts: float, run_time: float, tolerance: float = 60.0
) -> bool:
    """Check if crs_run_start_time is outside the valid execution window."""
    return crs_start > exec_ts + run_time + tolerance


def repair_pov_store(pov_store_path: Path, new_crs_start: float) -> dict:
    """Rewrite pov_store.json with corrected crs_run_start_time.

    Recomputes cpv_to_first_pov using file_mtime from each POV entry.

    Returns:
        Summary dict with old/new values and changes made.
    """
    data = json.loads(pov_store_path.read_text())
    old_crs_start = data.get("crs_run_start_time")

    # Fix crs_run_start_time
    data["crs_run_start_time"] = new_crs_start

    # Recompute cpv_to_first_pov from POV entries
    # Find earliest file_mtime per CPV
    cpv_earliest: dict[str, tuple[str, float]] = {}  # cpv_id -> (pov_hash, file_mtime)

    for pov_hash, entry in data.get("povs", {}).items():
        mtime = entry.get("file_mtime")
        if mtime is None:
            continue
        for cpv_id in entry.get("cpv_matched", []):
            if cpv_id not in cpv_earliest or mtime < cpv_earliest[cpv_id][1]:
                cpv_earliest[cpv_id] = (pov_hash, mtime)

    old_cpv_map = data.get("cpv_to_first_pov", {})
    new_cpv_map = {}

    for cpv_id, (pov_hash, mtime) in cpv_earliest.items():
        new_cpv_map[cpv_id] = {
            "pov_hash": pov_hash,
            "discovery_ts": mtime,
            "relative_time": mtime - new_crs_start,
        }

    data["cpv_to_first_pov"] = new_cpv_map

    return {
        "data": data,
        "old_crs_start": old_crs_start,
        "new_crs_start": new_crs_start,
        "old_cpv_map": old_cpv_map,
        "new_cpv_map": new_cpv_map,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("experiment_dir", type=Path, help="Experiment directory root")
    parser.add_argument(
        "--apply", action="store_true", help="Apply fixes (default: dry run)"
    )
    args = parser.parse_args()

    if not args.experiment_dir.exists():
        print(f"ERROR: Directory not found: {args.experiment_dir}", file=sys.stderr)
        return 1

    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"Mode: {mode}")
    print(f"Scanning: {args.experiment_dir}")
    print()

    fixed = 0
    skipped = 0
    errors = 0

    for pov_store_path in sorted(args.experiment_dir.rglob("pov_store.json")):
        povs_dir = pov_store_path.parent
        trial_dir = povs_dir.parent

        recovered = recover_crs_start_from_snapshots(povs_dir)
        if recovered is None:
            rel = trial_dir.relative_to(args.experiment_dir)
            print(f"SKIP {rel}: no snapshot data to recover from")
            skipped += 1
            continue

        result = repair_pov_store(pov_store_path, recovered)
        rel = trial_dir.relative_to(args.experiment_dir)

        print(f"FIX  {rel}")
        print(f"     crs_start: {result['old_crs_start']:.2f} -> {recovered:.2f}")
        for cpv_id in sorted(
            set(list(result["old_cpv_map"]) + list(result["new_cpv_map"]))
        ):
            old = result["old_cpv_map"].get(cpv_id, {})
            new = result["new_cpv_map"].get(cpv_id, {})
            old_rt = old.get("relative_time", old.get("discovery_elapsed", "?"))
            new_rt = new.get("relative_time", "?")
            print(f"     {cpv_id}: relative_time {old_rt} -> {new_rt}")

        if args.apply:
            backup = pov_store_path.with_suffix(".json.bak")
            if not backup.exists():
                pov_store_path.rename(backup)
            else:
                # Backup already exists from previous run, just overwrite
                pass
            pov_store_path.write_text(json.dumps(result["data"], indent=2))

        fixed += 1

    print()
    print(f"Summary: {fixed} fixed, {skipped} skipped, {errors} errors")
    if not args.apply and fixed > 0:
        print(f"Run with --apply to write changes")

    return 0


if __name__ == "__main__":
    sys.exit(main())
