from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Iterable

from crsbench.evaluation.trial_paths import TrialDir
from crsbench.reporting.snapshot_loader import discover_trials

from .models import SourcePovRecord

_VALID_SANITIZERS = {"address", "memory", "undefined", "thread", "leak"}


def make_source_id(source_dir: Path) -> str:
    digest = hashlib.sha256(str(source_dir.resolve()).encode("utf-8")).hexdigest()[:12]
    return f"source-{digest}"


def _visible_povs(pov_dir: Path) -> Iterable[Path]:
    return sorted(
        path
        for path in pov_dir.iterdir()
        if path.is_file() and not path.name.startswith(".")
    )


def _resolve_trial_sanitizer(trial: object, trial_dir: Path) -> str | None:
    trial_sanitizer = getattr(trial, "sanitizer", None)
    if hasattr(trial_sanitizer, "value"):
        trial_sanitizer = trial_sanitizer.value
    if isinstance(trial_sanitizer, str) and trial_sanitizer:
        return trial_sanitizer

    metadata = getattr(trial, "metadata", None)
    metadata_sanitizer = getattr(metadata, "sanitizer", None)
    if isinstance(metadata_sanitizer, str) and metadata_sanitizer in _VALID_SANITIZERS:
        return metadata_sanitizer

    for index, part in enumerate(trial_dir.parts):
        if part.startswith("trial-") and index > 0:
            candidate = trial_dir.parts[index - 1]
            if candidate in _VALID_SANITIZERS:
                return candidate
    return "address"


def discover_source_povs(
    source_dirs: list[Path],
    *,
    benchmark_filters: set[str] | None = None,
    trial_filters: set[str] | None = None,
) -> tuple[list[SourcePovRecord], dict[str, int]]:
    records: list[SourcePovRecord] = []
    trials_processed = 0
    trials_skipped = 0

    for source_dir in [Path(item).resolve() for item in source_dirs]:
        source_id = make_source_id(source_dir)
        for trial in discover_trials(source_dir):
            if trial.status != "valid" or trial.mode.value != "bug_finding":
                trials_skipped += 1
                continue
            if getattr(trial, "execution_status", None) != "success":
                trials_skipped += 1
                continue
            if benchmark_filters and trial.benchmark not in benchmark_filters:
                trials_skipped += 1
                continue
            if trial_filters and trial.trial_dir.name not in trial_filters:
                trials_skipped += 1
                continue

            trial_dir = trial.trial_dir.resolve()
            paths = TrialDir(trial_dir)
            if not paths.output_povs.exists():
                trials_skipped += 1
                continue

            trials_processed += 1
            source_sanitizer = _resolve_trial_sanitizer(trial, trial_dir)
            trial_relative = str(trial_dir.relative_to(source_dir))
            metadata = getattr(trial, "metadata", None)
            experiment_name = (
                getattr(metadata, "experiment_name", None) or source_dir.name
            )
            for pov_path in _visible_povs(paths.output_povs):
                payload = pov_path.read_bytes()
                records.append(
                    SourcePovRecord(
                        source_id=source_id,
                        source_dir=source_dir,
                        experiment_name=experiment_name,
                        trial_relative_path=trial_relative,
                        benchmark=trial.benchmark,
                        source_harness=trial.harness,
                        source_sanitizer=source_sanitizer,
                        original_pov_path=pov_path,
                        original_pov_relpath=str(pov_path.relative_to(source_dir)),
                        pov_filename=pov_path.name,
                        pov_content_hash=hashlib.sha256(payload).hexdigest(),
                    )
                )

    stats = {
        "source_roots_processed": len(source_dirs),
        "trials_processed": trials_processed,
        "trials_skipped": trials_skipped,
        "original_pov_instances": len(records),
    }
    return records, stats
