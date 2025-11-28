#!/usr/bin/env python3
"""Snapshot utility helper script.

Provides CLI access to snapshot inspection and manipulation functions.

Usage:
    python scripts/snapshot-utils.py list /path/to/trial
    python scripts/snapshot-utils.py inspect /path/to/snapshot-0001.tar.gz
    python scripts/snapshot-utils.py validate /path/to/snapshot-0001.tar.gz
    python scripts/snapshot-utils.py extract /path/to/snapshot-0001.tar.gz /output/dir
"""

import argparse
import sys
from pathlib import Path


def cmd_list(args):
    """List all snapshots in a trial directory."""
    from crsbench.evaluation.snapshot import list_snapshots

    trial_dir = Path(args.trial_dir)
    if not trial_dir.exists():
        print(f"Error: Trial directory not found: {trial_dir}")
        sys.exit(1)

    snapshots = list_snapshots(trial_dir)

    if not snapshots:
        print(f"No snapshots found in {trial_dir}")
        return

    print(f"Found {len(snapshots)} snapshot(s) in {trial_dir}:\n")
    print(f"{'Cycle':<8} {'Complete':<10} {'Size':<12} {'Path'}")
    print("-" * 70)

    for snap in snapshots:
        size_str = f"{snap.archive_size_bytes / 1024:.1f} KB" if snap.archive_size_bytes else "N/A"
        complete_str = "Yes" if snap.is_complete else "No"
        print(f"{snap.cycle:<8} {complete_str:<10} {size_str:<12} {snap.archive_path.name}")


def cmd_inspect(args):
    """Inspect a specific snapshot."""
    from crsbench.evaluation.snapshot import inspect_snapshot

    archive_path = Path(args.snapshot_path)
    if not archive_path.exists():
        print(f"Error: Snapshot not found: {archive_path}")
        sys.exit(1)

    summary = inspect_snapshot(archive_path)

    if not summary:
        print(f"Error: Failed to inspect snapshot: {archive_path}")
        sys.exit(1)

    print(f"Snapshot: {archive_path.name}")
    print(f"  Cycle: {summary.cycle}")
    print(f"  Complete: {'Yes' if summary.is_complete else 'No'}")
    print(f"  File count: {summary.file_count or 'N/A'}")
    print(f"  Archive size: {summary.archive_size_bytes / 1024:.1f} KB" if summary.archive_size_bytes else "  Archive size: N/A")

    if summary.metadata:
        print(f"  Elapsed time: {summary.metadata.elapsed_time:.1f}s")
        print(f"  Snapshot period: {summary.metadata.snapshot_period}s")


def cmd_validate(args):
    """Validate snapshot structure."""
    from crsbench.evaluation.snapshot import validate_snapshot_structure

    archive_path = Path(args.snapshot_path)
    if not archive_path.exists():
        print(f"Error: Snapshot not found: {archive_path}")
        sys.exit(1)

    is_valid, errors = validate_snapshot_structure(archive_path)

    if is_valid:
        print(f"Valid: {archive_path.name}")
    else:
        print(f"Invalid: {archive_path.name}")
        for error in errors:
            print(f"  - {error}")
        sys.exit(1)


def cmd_extract(args):
    """Extract snapshot to directory."""
    from crsbench.evaluation.snapshot import extract_snapshot

    archive_path = Path(args.snapshot_path)
    extract_dir = Path(args.output_dir)

    if not archive_path.exists():
        print(f"Error: Snapshot not found: {archive_path}")
        sys.exit(1)

    success = extract_snapshot(archive_path, extract_dir)

    if success:
        print(f"Extracted to: {extract_dir}")
    else:
        print(f"Error: Failed to extract snapshot")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Snapshot utility helper",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # list command
    list_parser = subparsers.add_parser("list", help="List snapshots in trial directory")
    list_parser.add_argument("trial_dir", help="Trial directory path")

    # inspect command
    inspect_parser = subparsers.add_parser("inspect", help="Inspect a snapshot")
    inspect_parser.add_argument("snapshot_path", help="Path to snapshot tar.gz")

    # validate command
    validate_parser = subparsers.add_parser("validate", help="Validate snapshot structure")
    validate_parser.add_argument("snapshot_path", help="Path to snapshot tar.gz")

    # extract command
    extract_parser = subparsers.add_parser("extract", help="Extract snapshot to directory")
    extract_parser.add_argument("snapshot_path", help="Path to snapshot tar.gz")
    extract_parser.add_argument("output_dir", help="Output directory")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    commands = {
        "list": cmd_list,
        "inspect": cmd_inspect,
        "validate": cmd_validate,
        "extract": cmd_extract,
    }

    commands[args.command](args)


if __name__ == "__main__":
    main()
