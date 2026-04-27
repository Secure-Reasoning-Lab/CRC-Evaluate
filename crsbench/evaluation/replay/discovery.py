from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

from crsbench.evaluation.trial_paths import TrialDir
from crsbench.reporting.snapshot_loader import discover_trials

from .models import SourcePovRecord

if TYPE_CHECKING:
    from crsbench.reporting.models import TrialInfo

_VALID_SANITIZERS = {"address", "memory", "undefined", "thread", "leak"}


@dataclass(frozen=True)
class _CandidateTrial:
    source_id: str
    source_dir: Path
    trial: "TrialInfo"
    trial_dir: Path
    trial_relative: str
    experiment_name: str
    source_sanitizer: str
    visible_povs: list[Path]


def make_source_id(source_dir: Path) -> str:
    digest = hashlib.sha256(str(source_dir.resolve()).encode("utf-8")).hexdigest()[:12]
    return f"source-{digest}"


def _visible_povs(pov_dir: Path) -> Iterable[Path]:
    return sorted(
        path
        for path in pov_dir.iterdir()
        if path.is_file() and not path.name.startswith(".")
    )


def _resolve_trial_sanitizer(trial: "TrialInfo", trial_dir: Path) -> str:
    """Return replay sanitizer, defaulting to address when discovery cannot infer one."""
    trial_sanitizer = getattr(trial, "sanitizer", None)
    if hasattr(trial_sanitizer, "value"):
        trial_sanitizer = trial_sanitizer.value
    if isinstance(trial_sanitizer, str) and trial_sanitizer:
        return trial_sanitizer

    metadata = getattr(trial, "metadata", None)
    metadata_sanitizer = getattr(metadata, "sanitizer", None)
    if isinstance(metadata_sanitizer, str) and metadata_sanitizer in _VALID_SANITIZERS:
        return metadata_sanitizer
    if isinstance(metadata_sanitizer, str) and metadata_sanitizer:
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
    candidate_trials: list[_CandidateTrial] = []
    trials_processed = 0
    trials_skipped = 0
    resolved_source_dirs: list[Path] = []
    seen_source_dirs: set[Path] = set()

    for item in source_dirs:
        resolved = Path(item).resolve()
        if resolved in seen_source_dirs:
            continue
        seen_source_dirs.add(resolved)
        resolved_source_dirs.append(resolved)

    for source_dir in resolved_source_dirs:
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

            trial_dir = trial.trial_dir.resolve()
            paths = TrialDir(trial_dir)
            if not paths.output_povs.exists():
                trials_skipped += 1
                continue

            visible_povs = list(_visible_povs(paths.output_povs))
            if not visible_povs:
                trials_skipped += 1
                continue

            source_sanitizer = _resolve_trial_sanitizer(trial, trial_dir)
            trial_relative = str(trial_dir.relative_to(source_dir))
            metadata = getattr(trial, "metadata", None)
            experiment_name = (
                getattr(metadata, "experiment_name", None) or source_dir.name
            )
            candidate_trials.append(
                _CandidateTrial(
                    source_id=source_id,
                    source_dir=source_dir,
                    trial=trial,
                    trial_dir=trial_dir,
                    trial_relative=trial_relative,
                    experiment_name=experiment_name,
                    source_sanitizer=source_sanitizer,
                    visible_povs=visible_povs,
                )
            )

    if trial_filters:
        leaf_counts = Counter(
            candidate.trial_dir.name for candidate in candidate_trials
        )
        relative_counts = Counter(
            candidate.trial_relative for candidate in candidate_trials
        )
        filtered_candidates: list[_CandidateTrial] = []
        for candidate in candidate_trials:
            trial_leaf = candidate.trial_dir.name
            trial_relative = candidate.trial_relative
            source_qualified_trial = f"{candidate.source_id}:{trial_relative}"
            if (
                source_qualified_trial in trial_filters
                or (
                    trial_relative in trial_filters
                    and relative_counts[trial_relative] == 1
                )
                or (trial_leaf in trial_filters and leaf_counts[trial_leaf] == 1)
            ):
                filtered_candidates.append(candidate)
            else:
                trials_skipped += 1
        candidate_trials = filtered_candidates

    for candidate in candidate_trials:
        trials_processed += 1
        for pov_path in candidate.visible_povs:
            payload = pov_path.read_bytes()
            records.append(
                SourcePovRecord(
                    source_id=candidate.source_id,
                    source_dir=candidate.source_dir,
                    experiment_name=candidate.experiment_name,
                    trial_relative_path=candidate.trial_relative,
                    benchmark=candidate.trial.benchmark,
                    source_harness=candidate.trial.harness,
                    source_sanitizer=candidate.source_sanitizer,
                    original_pov_path=pov_path,
                    original_pov_relpath=str(
                        pov_path.relative_to(candidate.source_dir)
                    ),
                    pov_filename=pov_path.name,
                    pov_content_hash=hashlib.sha256(payload).hexdigest(),
                )
            )

    stats = {
        "source_roots_processed": len(resolved_source_dirs),
        "trials_processed": trials_processed,
        "trials_skipped": trials_skipped,
        "original_pov_instances": len(records),
    }
    return records, stats
