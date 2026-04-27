"""Interactive cleanup of failed variant POVs after `benchmark ci pov`.

Variant POVs (pov_1+) whose verdict is anything other than ``cpv`` are removed
from the benchmark on user confirmation. The ground-truth ``pov_0`` is never
deleted: a failing pov_0 means the CPV itself is broken and needs human review,
not silent removal.

Each affected benchmark gets an entry appended to ``.aixcc/CHANGELOG.md`` per
the project's benchmark change-logging policy.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Iterable

from crsbench.benchmark_ci.jobs.flat import VerifyCpvPovJob, VerifyCpvVarJob
from crsbench.utils.logger import get_logger

if TYPE_CHECKING:
    from pathlib import Path

    from crsbench.benchmark_ci.jobs.base import Job
    from crsbench.executor.types import ExecutorResult

logger = get_logger(__name__)

_PASS_STATUS = "cpv"


@dataclass(frozen=True)
class FailedPov0:
    benchmark_path: Path
    harness: str
    cpv_id: str
    pov_id: str
    status: str


@dataclass(frozen=True)
class FailedVariantPov:
    benchmark_path: Path
    harness: str
    cpv_id: str
    pov_id: str
    status: str
    blob_path: Path
    log_path: Path


def collect_failed_povs(
    jobs: Iterable[Job],
    dag_results: dict[str, ExecutorResult],
) -> tuple[list[FailedPov0], list[FailedVariantPov]]:
    """Scan verify jobs and return (pov_0 warnings, deletable variant POVs)."""
    pov0_warnings: list[FailedPov0] = []
    to_delete: list[FailedVariantPov] = []

    for job in jobs:
        result = dag_results.get(job.job_id)
        if not result or not result.job_result:
            continue
        verdicts = result.job_result.details.get("verdicts", []) or []

        if isinstance(job, VerifyCpvPovJob):
            for verdict in verdicts:
                status = (verdict.get("status") or "").lower()
                if status == _PASS_STATUS:
                    continue
                if not job.benchmark_path:
                    continue
                pov0_warnings.append(
                    FailedPov0(
                        benchmark_path=job.benchmark_path,
                        harness=job.harness,
                        cpv_id=job.cpv_id,
                        pov_id=verdict.get("pov_id") or "pov_0",
                        status=status or "unknown",
                    )
                )
            continue

        if isinstance(job, VerifyCpvVarJob):
            if not job.benchmark_path:
                continue
            failed_status_by_pov_id: dict[str, str] = {
                v.get("pov_id"): (v.get("status") or "").lower()
                for v in verdicts
                if v.get("pov_id") and (v.get("status") or "").lower() != _PASS_STATUS
            }
            if not failed_status_by_pov_id:
                continue
            for pov_path in job.pov_paths:
                status = failed_status_by_pov_id.get(pov_path.stem)
                if status is None:
                    continue
                log_path = (
                    job.benchmark_path
                    / ".aixcc"
                    / job.harness
                    / job.cpv_id
                    / "logs"
                    / f"{pov_path.stem}.log"
                )
                to_delete.append(
                    FailedVariantPov(
                        benchmark_path=job.benchmark_path,
                        harness=job.harness,
                        cpv_id=job.cpv_id,
                        pov_id=pov_path.stem,
                        status=status or "unknown",
                        blob_path=pov_path,
                        log_path=log_path,
                    )
                )

    return pov0_warnings, to_delete


def _confirm(prompt: str) -> bool:
    if not sys.stdin.isatty():
        logger.error(
            "--delete-failed-povs requires an interactive terminal (stdin is not a TTY)"
        )
        return False
    try:
        answer = input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer in ("y", "yes")


def _append_changelog(
    benchmark_path: Path,
    deleted: list[tuple[FailedVariantPov, list[Path]]],
) -> None:
    changelog = benchmark_path / ".aixcc" / "CHANGELOG.md"
    today = datetime.now().strftime("%Y-%m-%d")

    body_lines = [
        f"## {today}",
        f"- Date: {today}",
        "- Description: Removed failed variant POVs (and matching logs) via "
        "`crsbench benchmark ci pov --delete-failed-povs`.",
        "",
        "### Removed files",
    ]
    for entry, paths in deleted:
        for path in paths:
            try:
                rel = path.relative_to(benchmark_path)
            except ValueError:
                rel = path
            body_lines.append(
                f"- {rel} (cpv={entry.cpv_id}, harness={entry.harness}, "
                f"verdict={entry.status})"
            )
    body_lines.append("")
    new_block = "\n".join(body_lines) + "\n"

    if changelog.exists():
        existing = changelog.read_text()
        if existing.startswith("# CHANGELOG"):
            header_line, _, rest = existing.partition("\n")
            updated = f"{header_line}\n\n{new_block}{rest.lstrip()}"
        else:
            updated = f"{existing.rstrip()}\n\n{new_block}"
        changelog.write_text(updated)
    else:
        changelog.parent.mkdir(parents=True, exist_ok=True)
        changelog.write_text(f"# CHANGELOG\n\n{new_block}")


def _delete_files(to_delete: list[FailedVariantPov]) -> int:
    by_benchmark: dict[Path, list[tuple[FailedVariantPov, list[Path]]]] = {}
    total_files = 0

    for entry in to_delete:
        removed_paths: list[Path] = []
        for path in (entry.blob_path, entry.log_path):
            if not path.exists():
                continue
            try:
                path.unlink()
            except OSError as exc:
                logger.error(f"Failed to delete {path}: {exc}")
                continue
            removed_paths.append(path)
            total_files += 1
            logger.info(f"Deleted {path}")
        if removed_paths:
            by_benchmark.setdefault(entry.benchmark_path, []).append(
                (entry, removed_paths)
            )

    for benchmark_path, deleted in by_benchmark.items():
        _append_changelog(benchmark_path, deleted)

    return total_files


def prompt_and_delete_failed_povs(
    jobs: Iterable[Job],
    dag_results: dict[str, ExecutorResult],
) -> None:
    """Collect failed POVs, warn about pov_0 failures, prompt, then delete."""
    pov0_warnings, to_delete = collect_failed_povs(jobs, dag_results)

    for warn in pov0_warnings:
        logger.warning(
            f"pov_0 verdict={warn.status} for "
            f"{warn.benchmark_path.name}/{warn.harness}/{warn.cpv_id}/{warn.pov_id}: "
            "ground-truth POV will NOT be deleted; manual review required"
        )

    if not to_delete:
        logger.info("No failed variant POVs to delete.")
        return

    print()
    print(f"Failed variant POVs to delete ({len(to_delete)}):")
    for entry in to_delete:
        print(
            f"  [{entry.status}] {entry.benchmark_path.name}/"
            f"{entry.harness}/{entry.cpv_id}/{entry.pov_id}"
        )
    print()

    if not _confirm(
        f"Delete these {len(to_delete)} variant POV(s) and their logs? [y/N]: "
    ):
        logger.info("Deletion cancelled; no files removed.")
        return

    removed = _delete_files(to_delete)
    logger.info(f"Removed {removed} file(s).")
