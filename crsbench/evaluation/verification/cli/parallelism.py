"""Shared parallelism flag handling for top-level verification CLIs."""

import argparse


def add_parallelism_arguments(parser: argparse.ArgumentParser) -> None:
    """Add primary and legacy parallelism arguments to a parser."""
    parser.add_argument(
        "--jobs",
        type=int,
        default=None,
        help="Parallel verification jobs.",
    )
    parser.add_argument(
        "--cores-per-job",
        type=int,
        default=None,
        help="CPUs reserved per verification job.",
    )
    parser.add_argument(
        "--build-workers",
        type=int,
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--verify-workers",
        type=int,
        default=None,
        help=argparse.SUPPRESS,
    )


def resolve_jobs(args: argparse.Namespace) -> int:
    """Resolve primary jobs flag with legacy compatibility."""
    jobs = getattr(args, "jobs", None)
    legacy_jobs = getattr(args, "build_workers", None)
    if jobs is not None and legacy_jobs is not None and jobs != legacy_jobs:
        raise ValueError("--jobs conflicts with legacy --build-workers")
    resolved = int(jobs if jobs is not None else legacy_jobs or 1)
    if resolved < 1:
        raise ValueError("--jobs must be >= 1")
    return resolved


def resolve_cores_per_job(args: argparse.Namespace) -> int:
    """Resolve primary cores-per-job flag with legacy compatibility."""
    cores_per_job = getattr(args, "cores_per_job", None)
    legacy_cores = getattr(args, "verify_workers", None)
    if (
        cores_per_job is not None
        and legacy_cores is not None
        and cores_per_job != legacy_cores
    ):
        raise ValueError("--cores-per-job conflicts with legacy --verify-workers")
    resolved = int(cores_per_job if cores_per_job is not None else legacy_cores or 1)
    if resolved < 1:
        raise ValueError("--cores-per-job must be >= 1")
    return resolved
