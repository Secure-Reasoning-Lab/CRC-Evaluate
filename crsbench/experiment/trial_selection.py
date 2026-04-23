from __future__ import annotations

import base64
import binascii
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from crsbench.distributed.queue import build_trial_key

TRIAL_KEY_ALLOWLIST_ENV_VAR = "CRSBENCH_ONLY_TRIAL_KEYS_B64"


def default_collected_experiment_path(config) -> Path:
    """Return canonical path to collected experiment results."""
    return Path(config.experiment_filestore) / config.experiment


def default_selector_output_path(
    experiment_name: str, cwd: str | Path | None = None
) -> Path:
    """Return default output path for unfinished trial keys."""
    base = Path.cwd() if cwd is None else Path(cwd)
    return base / f"{experiment_name}-unfinished-trial-keys.txt"


def normalize_trial_key_lines(text: str) -> list[str]:
    """Strip blank lines and dedupe trial keys while preserving first-seen order."""
    normalized: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        key = line.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        normalized.append(key)
    return normalized


def load_trial_key_file(path: str | Path) -> list[str]:
    """Load and normalize newline-delimited logical trial keys from a file."""
    return normalize_trial_key_lines(Path(path).read_text(encoding="utf-8"))


def _normalize_trial_key_iterable(keys: Iterable[str]) -> list[str]:
    return normalize_trial_key_lines("\n".join(keys))


def encode_trial_key_allowlist(keys: Iterable[str]) -> str:
    """Encode trial key allowlist as base64 newline-delimited text."""
    normalized = _normalize_trial_key_iterable(keys)
    payload = "\n".join(normalized).encode("utf-8")
    return base64.b64encode(payload).decode("ascii")


def decode_trial_key_allowlist(value: str) -> list[str]:
    """Decode base64 newline-delimited trial key allowlist."""
    if not value:
        return []
    try:
        decoded_bytes = base64.b64decode(value.encode("ascii"), validate=True)
        decoded = decoded_bytes.decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError, binascii.Error) as exc:
        raise ValueError("Invalid base64 trial key allowlist payload") from exc
    return normalize_trial_key_lines(decoded)


def trial_key_for_trial(trial) -> str:
    """Return canonical logical trial key for a trial-matrix element."""
    return build_trial_key(
        crs=trial.crs,
        benchmark=trial.benchmark_harness.name,
        harness=trial.benchmark_harness.harness.name,
        mode=trial.mode,
        sanitizer=trial.sanitizer,
        trial_num=trial.trial_num,
        target_cpv_id=trial.target_cpv_id,
    )


def filter_trials_by_allowlist(
    trials: Sequence, allowed_keys: Iterable[str]
) -> tuple[list, list[str]]:
    """Filter trial matrix by logical trial keys.

    Returns filtered trials preserving trial-matrix order and unknown requested keys.
    """
    normalized_allowed_keys = _normalize_trial_key_iterable(allowed_keys)
    if not normalized_allowed_keys:
        return [], []

    allowed_key_set = set(normalized_allowed_keys)
    trial_with_keys = [(trial, trial_key_for_trial(trial)) for trial in trials]
    matrix_keys = {trial_key for _, trial_key in trial_with_keys}
    filtered_trials = [
        trial for trial, trial_key in trial_with_keys if trial_key in allowed_key_set
    ]
    unknown_keys = sorted(allowed_key_set - matrix_keys)
    return filtered_trials, unknown_keys


@dataclass(frozen=True)
class DerivedTrialKeys:
    selected_keys: list[str]
    finished_success_keys: list[str]
    finished_fail_keys: list[str]


def _build_trial_matrix_from_config(config) -> list:
    """Build trial matrix lazily to avoid import-time cycles."""
    from crsbench.evaluation.trial_paths import resolve_benchmarks_root
    from crsbench.run_experiment import (
        generate_trial_matrix,
        resolve_benchmark_harnesses,
    )

    benchmark_entries = config.get_benchmark_entries()
    benchmarks_root = resolve_benchmarks_root(config.benchmarks_root)
    benchmark_harnesses = resolve_benchmark_harnesses(
        benchmark_entries, benchmarks_root
    )
    return generate_trial_matrix(
        benchmark_harnesses=benchmark_harnesses,
        oss_crs_registry=config.get_crs_registry_ids(),
        config=config,
        registry_dir=config.registry_dir,
    )


def _trial_key_from_collected_trial_dir(
    trial_dir: Path, collected_root: Path
) -> str | None:
    """Parse canonical logical trial key from one collected trial directory path."""
    try:
        relative = trial_dir.relative_to(collected_root)
    except ValueError:
        return None

    parts = relative.parts
    if len(parts) == 6:
        crs, benchmark, harness, mode, sanitizer, trial_part = parts
        target_cpv_id: str | None = None
    elif len(parts) == 7:
        crs, benchmark, harness, target_cpv_id, mode, sanitizer, trial_part = parts
    else:
        return None

    if not trial_part.startswith("trial-"):
        return None

    trial_num_raw = trial_part.removeprefix("trial-")
    try:
        trial_num = int(trial_num_raw)
    except ValueError:
        return None

    return build_trial_key(
        crs=crs,
        benchmark=benchmark,
        harness=harness,
        mode=mode,
        sanitizer=sanitizer,
        trial_num=trial_num,
        target_cpv_id=target_cpv_id,
    )


def _collect_finished_trial_keys(
    collected_root: Path, *, rerun_failed_trials: bool
) -> tuple[set[str], set[str]]:
    if not collected_root.exists():
        return set(), set()

    success_keys: set[str] = set()
    fail_keys: set[str] = set()

    for dirpath, _, filenames in os.walk(collected_root):
        has_success = ".success" in filenames
        has_fail = not rerun_failed_trials and ".fail" in filenames
        if not has_success and not has_fail:
            continue

        key = _trial_key_from_collected_trial_dir(Path(dirpath), collected_root)
        if key is None:
            continue

        if has_success:
            success_keys.add(key)
        if has_fail:
            fail_keys.add(key)

    return success_keys, fail_keys


def derive_unfinished_trial_keys_from_config(
    config,
    collected_root: str | Path,
    *,
    rerun_failed_trials: bool = False,
) -> DerivedTrialKeys:
    """Derive unfinished logical trial keys from config matrix and collected results."""
    trial_matrix = _build_trial_matrix_from_config(config)
    matrix_keys_ordered = [trial_key_for_trial(trial) for trial in trial_matrix]
    matrix_key_set = set(matrix_keys_ordered)

    root = Path(collected_root)
    finished_success, finished_fail = _collect_finished_trial_keys(
        root,
        rerun_failed_trials=rerun_failed_trials,
    )

    unknown_finished = (finished_success | finished_fail) - matrix_key_set
    if unknown_finished:
        unknown_text = ", ".join(sorted(unknown_finished))
        raise ValueError(
            "Collected results contain finished trial keys not present in trial matrix: "
            f"{unknown_text}"
        )

    selected_keys: list[str] = []
    finished_success_keys: list[str] = []
    finished_fail_keys: list[str] = []
    for key in matrix_keys_ordered:
        if key in finished_success:
            finished_success_keys.append(key)
            continue
        if key in finished_fail:
            finished_fail_keys.append(key)
            continue
        selected_keys.append(key)

    return DerivedTrialKeys(
        selected_keys=selected_keys,
        finished_success_keys=finished_success_keys,
        finished_fail_keys=finished_fail_keys,
    )
