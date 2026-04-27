"""CLI scaffold for replaying discovered POVs against OSS-Fuzz projects."""

from __future__ import annotations

import argparse
from pathlib import Path


def add_replay_povs_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``replay-povs`` top-level command."""
    parser = subparsers.add_parser(
        "replay-povs",
        help="Replay discovered POVs against mapped OSS-Fuzz projects",
        description=(
            "Replay discovered POVs from prior experiment outputs against the "
            "latest mapped OSS-Fuzz projects."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--source-dir",
        dest="source_dirs",
        type=Path,
        action="append",
        required=True,
        help="Experiment output directory to scan for replayable POVs",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Directory where replay outputs should be written",
    )
    parser.add_argument(
        "--oss-fuzz-path",
        type=Path,
        default=None,
        help="Path to an existing oss-fuzz checkout",
    )
    projects_group = parser.add_mutually_exclusive_group()
    projects_group.add_argument(
        "--projects-root",
        type=Path,
        default=None,
        help="Directory containing synced OSS-Fuzz project checkouts",
    )
    projects_group.add_argument(
        "--sync-projects",
        action="store_true",
        help="Sync mapped OSS-Fuzz projects before replaying",
    )
    parser.add_argument(
        "--benchmark",
        dest="benchmarks",
        action="append",
        help="Replay only matching benchmark names",
    )
    parser.add_argument(
        "--trial",
        dest="trials",
        action="append",
        help="Replay only matching trial identifiers",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=1,
        help="Number of replay jobs to run in parallel",
    )
    parser.add_argument(
        "--per-pov-timeout",
        type=int,
        default=180,
        help="Timeout in seconds for each replayed POV",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )
    parser.set_defaults(command="replay-povs", func=run_replay_povs)


def run_replay_povs(args: argparse.Namespace) -> int:
    """Placeholder replay entry point for the Task 1 scaffold."""
    raise NotImplementedError("replay-povs wiring is added in later tasks")
