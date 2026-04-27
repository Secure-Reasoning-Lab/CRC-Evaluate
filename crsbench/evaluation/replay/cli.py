"""CLI scaffold for replaying discovered POVs against OSS-Fuzz projects."""

from __future__ import annotations

import argparse
from pathlib import Path

from crsbench.evaluation.replay.discovery import discover_source_povs
from crsbench.evaluation.replay.engine import ReplayEngine
from crsbench.evaluation.replay.projects import resolve_projects_root
from crsbench.utils.logger import configure_logger
from crsbench.utils.run_helper import ensure_oss_fuzz_root


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
        "--group-jobs",
        type=int,
        default=1,
        help=(
            "Number of (project, sanitizer) replay groups to process in parallel. "
            "Each group still uses up to --jobs warm replay sessions."
        ),
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help=(
            "Reuse completed replay groups from --output when the discovered "
            "input signature still matches, and rerun only unfinished groups"
        ),
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
    """Replay historical POVs against latest OSS-Fuzz projects and harnesses."""
    configure_logger(level="DEBUG" if args.verbose else "INFO")

    source_dirs = [Path(item).resolve() for item in args.source_dirs]
    if any(not source_dir.is_dir() for source_dir in source_dirs):
        raise SystemExit("All --source-dir values must exist and be directories")

    if args.jobs < 1:
        raise SystemExit("--jobs must be at least 1")
    if args.group_jobs < 1:
        raise SystemExit("--group-jobs must be at least 1")
    if args.per_pov_timeout < 1:
        raise SystemExit("--per-pov-timeout must be at least 1")

    output_dir = Path(args.output).resolve()
    if any(
        output_dir == source_dir or output_dir.is_relative_to(source_dir)
        for source_dir in source_dirs
    ):
        raise SystemExit("--output must be outside every source experiment tree")

    oss_fuzz_path = (
        Path(args.oss_fuzz_path).resolve()
        if args.oss_fuzz_path is not None
        else Path(ensure_oss_fuzz_root()).resolve()
    )
    repo_root = Path(__file__).resolve().parents[3]
    projects_root = resolve_projects_root(
        args.projects_root,
        sync_projects=args.sync_projects,
        repo_root=repo_root,
    )
    records, discovery_stats = discover_source_povs(
        source_dirs,
        benchmark_filters=set(args.benchmarks or []),
        trial_filters=set(args.trials or []),
    )
    engine = ReplayEngine(
        oss_fuzz_path=oss_fuzz_path,
        projects_root=projects_root,
        output_dir=output_dir,
        jobs=args.jobs,
        group_jobs=args.group_jobs,
        resume=args.resume,
        per_pov_timeout=args.per_pov_timeout,
    )
    engine.run(records, discovery_stats=discovery_stats, source_dirs=source_dirs)
    return 0
