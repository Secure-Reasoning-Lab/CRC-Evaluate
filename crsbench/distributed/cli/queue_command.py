"""Queue management command for distributed execution."""

from __future__ import annotations

import argparse
import os

from crsbench.distributed.queue import create_redis_connection
from crsbench.distributed.queue_cleanup import clean_experiment_queues
from crsbench.utils.logger import configure_logger, get_logger

logger = get_logger(__name__)


def add_queue_subparser(subparsers) -> None:
    """Add 'queue' subcommand to the CLI."""
    parser = subparsers.add_parser(
        "queue",
        help="Manage distributed queues",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s clean --experiment exp-1 --yes
  %(prog)s clean --experiment exp-1 --queues trial,verify --dry-run
        """,
    )
    queue_subparsers = parser.add_subparsers(dest="queue_command", required=True)

    clean = queue_subparsers.add_parser(
        "clean", help="Clean one experiment from queue/registry state"
    )
    clean.add_argument(
        "--experiment",
        required=True,
        help="Experiment name to clean",
    )
    clean.add_argument(
        "--redis-host",
        default=None,
        help="Redis host[:port] (default: CRSBENCH_REDIS_HOST or localhost)",
    )
    clean.add_argument(
        "--queues",
        default="trial,build,verify",
        help="Comma-separated scopes: trial,build,verify",
    )
    clean.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be removed without mutating state",
    )
    clean.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation prompt",
    )
    clean.add_argument(
        "--no-registry",
        action="store_true",
        help="Do not remove experiment entry from Redis registry",
    )
    clean.add_argument(
        "--no-lock",
        action="store_true",
        help="Do not remove distributed lock key",
    )
    parser.set_defaults(command="queue")


def run_queue(args: argparse.Namespace) -> int:
    """Execute queue subcommands."""
    configure_logger(level="DEBUG" if getattr(args, "verbose", False) else "INFO")

    if args.queue_command != "clean":
        logger.error(f"Unsupported queue command: {args.queue_command}")
        return 2

    redis_host = args.redis_host or os.environ.get("CRSBENCH_REDIS_HOST", "localhost")
    scopes = [s.strip() for s in args.queues.split(",") if s.strip()]
    if not scopes:
        scopes = ["trial", "build", "verify"]
    include_registry = not args.no_registry
    include_lock = not args.no_lock
    if set(scopes) != {"trial", "build", "verify"}:
        # Partial scope cleanup should not drop experiment coordination state.
        include_registry = False
        include_lock = False

    if not args.yes and not args.dry_run:
        print("This will remove experiment-scoped jobs from Redis state.")  # noqa: T201
        confirm = input("Type 'yes' to continue: ").strip().lower()
        if confirm != "yes":
            print("Cancelled.")  # noqa: T201
            return 0

    try:
        redis_conn = create_redis_connection(redis_host)
        result = clean_experiment_queues(
            redis_conn,
            experiment_name=args.experiment,
            scopes=scopes,
            include_registry=include_registry,
            include_lock=include_lock,
            dry_run=args.dry_run,
        )
    except Exception as e:
        logger.error(f"Queue cleanup failed: {e}")
        return 1

    mode = "DRY-RUN" if args.dry_run else "APPLIED"
    print(f"[{mode}] experiment={args.experiment}")  # noqa: T201
    print(f"queues: {', '.join(result.queue_names)}")  # noqa: T201
    print(f"matched_jobs: {result.matched_jobs}")  # noqa: T201
    print(f"removed_jobs: {result.removed_jobs}")  # noqa: T201
    print(f"registry_removed: {result.removed_registry_entry}")  # noqa: T201
    print(f"lock_removed: {result.removed_lock}")  # noqa: T201
    return 0
