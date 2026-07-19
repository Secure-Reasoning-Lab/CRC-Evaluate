"""CLI commands for validating and registering CRC submissions."""

from __future__ import annotations

import argparse
from pathlib import Path

from crsbench.submission.manifest import (
    SubmissionError,
    ValidatedSubmission,
    load_submission,
    register_submission,
)
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


def add_submission_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Add the top-level ``submission`` command."""
    parser = subparsers.add_parser(
        "submission",
        help="Validate and register CRC-Template-compatible submissions",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s validate /data/submissions/team-001
  %(prog)s register /data/submissions/team-001 --team-id team-001 --registry-dir /data/crs-registry
        """,
    )
    action_parsers = parser.add_subparsers(
        dest="submission_action",
        required=True,
    )

    validate_parser = action_parsers.add_parser(
        "validate",
        help="Validate submission.yaml and selected CRS directories",
    )
    validate_parser.add_argument(
        "submission_root",
        type=Path,
        help="CRC-Template-compatible submission root",
    )

    register_parser = action_parsers.add_parser(
        "register",
        help="Generate evaluator-local registry entries",
    )
    register_parser.add_argument(
        "submission_root",
        type=Path,
        help="CRC-Template-compatible submission root",
    )
    register_parser.add_argument(
        "--team-id",
        required=True,
        help="Evaluator-owned lowercase registry namespace",
    )
    register_parser.add_argument(
        "--registry-dir",
        type=Path,
        required=True,
        help="Directory for generated OSS-CRS registry entries",
    )
    register_parser.add_argument(
        "--force",
        action="store_true",
        help="Replace existing registry entries for this team ID",
    )
    parser.set_defaults(command="submission")


def _log_validated_submission(submission: ValidatedSubmission) -> None:
    logger.info(f"Submission: {submission.name}")
    for crs in (submission.finder, submission.patcher):
        models = ", ".join(crs.required_llms) if crs.required_llms else "none"
        logger.info(
            f"{crs.role.capitalize()}: path={crs.path}, name={crs.name}, "
            f"type={crs.crs_type}, required_llms={models}"
        )


def run_submission(args: argparse.Namespace) -> int:
    """Execute a submission validation or registration command."""
    try:
        if args.submission_action == "validate":
            submission = load_submission(args.submission_root)
            _log_validated_submission(submission)
            logger.info("Submission validation passed")
            return 0

        if args.submission_action == "register":
            registered = register_submission(
                args.submission_root,
                team_id=args.team_id,
                registry_dir=args.registry_dir,
                force=args.force,
            )
            _log_validated_submission(registered.submission)
            logger.info(f"Registry directory: {registered.registry_dir}")
            logger.info(
                f"Finder registry ID: {registered.finder_registry_name} "
                f"({registered.finder_registry_path})"
            )
            logger.info(
                f"Patcher registry ID: {registered.patcher_registry_name} "
                f"({registered.patcher_registry_path})"
            )
            return 0
    except SubmissionError as exc:
        logger.error(str(exc))
        return 1

    logger.error(f"Unknown submission action: {args.submission_action}")
    return 2
