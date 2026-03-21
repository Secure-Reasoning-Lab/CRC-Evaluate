"""Post-run verification helpers for smoke/CI experiment suites."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, cast

from crsbench.benchmark_ci.checks import check_patch_verify, check_verify
from crsbench.evaluation.trial_paths import TrialDir, resolve_benchmark_path
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)

SmokeSuite = Literal["bugfinding", "bugfixing"]


@dataclass(frozen=True)
class SmokeVerificationTask:
    """Resolved smoke-post-verification task for a single trial."""

    suite: SmokeSuite
    trial_dir: Path
    benchmark_path: Path
    benchmark_name: str
    harness: str

    @property
    def trial(self) -> TrialDir:
        return TrialDir(self.trial_dir)

    @property
    def results_path(self) -> Path:
        if self.suite == "bugfinding":
            return self.trial_dir / "smoke_verify_results.json"
        return self.trial_dir / "smoke_patch_verify_results.json"

    def build_command(self, runner_prefix: list[str]) -> list[str]:
        if self.suite == "bugfinding":
            return [
                *runner_prefix,
                "verify",
                str(self.benchmark_path),
                "--pov-dir",
                str(self.trial.output_povs),
                "--harness",
                self.harness,
                "--output",
                str(self.results_path),
                "--format",
                "json",
            ]

        return [
            *runner_prefix,
            "patch-verify",
            str(self.benchmark_path),
            "--patch-dir",
            str(self.trial.output_patches),
            "--pov-dir",
            str(self.trial.input_povs),
            "--harness",
            self.harness,
            "--output",
            str(self.results_path),
            "--format",
            "json",
        ]


def _load_trial_metadata(metadata_path: Path) -> dict[str, object]:
    with metadata_path.open() as handle:
        return json.load(handle)


def _resolve_trial_benchmark_path(
    metadata: dict[str, object],
    benchmarks_root: Path,
) -> Path:
    source = metadata.get("source")
    if isinstance(source, Mapping):
        source_mapping = cast("Mapping[str, object]", source)
        source_path = source_mapping.get("path")
        if isinstance(source_path, str):
            candidate = Path(source_path)
            if candidate.exists():
                return candidate

    benchmark_name = metadata.get("benchmark")
    if not isinstance(benchmark_name, str) or not benchmark_name:
        raise ValueError("trial metadata missing benchmark name")

    return resolve_benchmark_path(benchmark_name, benchmarks_root)


def discover_smoke_verification_tasks(
    *,
    experiment_dir: Path,
    benchmarks_root: Path,
    suite: SmokeSuite,
) -> list[SmokeVerificationTask]:
    """Discover smoke verification tasks from completed trial metadata."""
    tasks: list[SmokeVerificationTask] = []

    for metadata_path in sorted(experiment_dir.rglob("metadata.json")):
        metadata = _load_trial_metadata(metadata_path)
        benchmark_name = metadata.get("benchmark")
        harness = metadata.get("harness")
        if not isinstance(benchmark_name, str) or not isinstance(harness, str):
            logger.warning(
                f"Skipping {metadata_path}: missing benchmark or harness field"
            )
            continue

        tasks.append(
            SmokeVerificationTask(
                suite=suite,
                trial_dir=metadata_path.parent,
                benchmark_path=_resolve_trial_benchmark_path(metadata, benchmarks_root),
                benchmark_name=benchmark_name,
                harness=harness,
            )
        )

    return tasks


def _run_command(command: list[str]) -> int:
    completed = subprocess.run(command, check=False, timeout=1800)
    return int(completed.returncode)


def _validate_trial_outputs(task: SmokeVerificationTask) -> None:
    success_marker = task.trial_dir / ".success"
    fail_marker = task.trial_dir / ".fail"

    if fail_marker.exists():
        raise ValueError(f"trial failed: {task.trial_dir}")
    if not success_marker.exists():
        raise ValueError(f"missing .success marker: {task.trial_dir}")

    if task.suite == "bugfinding":
        if task.trial.count_visible_files(task.trial.output_povs) == 0:
            raise ValueError(f"missing output POVs: {task.trial.output_povs}")
        return

    if task.trial.count_visible_patch_diffs() == 0:
        raise ValueError(f"missing patch diffs: {task.trial.output_patches}")
    if task.trial.count_visible_input_povs() == 0:
        raise ValueError(f"missing input POVs: {task.trial.input_povs}")


def _validate_results(task: SmokeVerificationTask) -> bool:
    if task.suite == "bugfinding":
        return check_verify(
            task.benchmark_path,
            task.results_path,
            harness=task.harness,
        )
    return check_patch_verify(task.results_path)


def run_smoke_post_verification(
    *,
    experiment_dir: Path,
    benchmarks_root: Path,
    suite: SmokeSuite,
    run_command: Callable[[list[str]], int] = _run_command,
    runner_prefix: list[str] | None = None,
) -> int:
    """Run post-smoke verification for every discovered trial."""
    tasks = discover_smoke_verification_tasks(
        experiment_dir=experiment_dir,
        benchmarks_root=benchmarks_root,
        suite=suite,
    )
    if not tasks:
        logger.error(f"No smoke verification tasks discovered under {experiment_dir}")
        return 1

    command_prefix = runner_prefix or ["uv", "run", "crsbench"]
    failures: list[str] = []

    for task in tasks:
        try:
            _validate_trial_outputs(task)
        except ValueError as exc:
            failures.append(str(exc))
            continue

        command = task.build_command(command_prefix)
        logger.info(
            f"Smoke post-verify: suite={suite} benchmark={task.benchmark_name} "
            f"harness={task.harness}"
        )
        returncode = run_command(command)
        if returncode != 0:
            failures.append(
                f"command failed ({returncode}) for {task.trial_dir}: {' '.join(command)}"
            )
            continue

        if not task.results_path.exists():
            failures.append(f"missing verification results: {task.results_path}")
            continue

        if not _validate_results(task):
            failures.append(
                f"verification results failed validation: {task.results_path}"
            )

    if failures:
        for failure in failures:
            logger.error(failure)
        return 1

    logger.info(
        f"Smoke post-verification passed for {len(tasks)} trial(s) in {experiment_dir}"
    )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run post-smoke verify/patch-verify checks for trial outputs.",
    )
    parser.add_argument(
        "--suite",
        required=True,
        choices=["bugfinding", "bugfixing"],
        help="Smoke suite type to validate.",
    )
    parser.add_argument(
        "--experiment-dir",
        required=True,
        type=Path,
        help="Experiment output directory that contains trial subdirectories.",
    )
    parser.add_argument(
        "--benchmarks-root",
        required=True,
        type=Path,
        help="Benchmarks root used to resolve benchmark names from trial metadata.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return run_smoke_post_verification(
        experiment_dir=args.experiment_dir,
        benchmarks_root=args.benchmarks_root,
        suite=args.suite,
    )


if __name__ == "__main__":
    sys.exit(main())
