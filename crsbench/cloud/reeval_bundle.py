"""Build minimal cloud re-eval bundles from an existing experiment tree."""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import yaml

from crsbench.evaluation.trial_paths import TrialDir, normalize_experiment_config_dict
from crsbench.reporting.snapshot_loader import discover_trials
from crsbench.validation.schemas import TrialMode

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class ReevalBundleBuildResult:
    """Summary of one local re-eval bundle build."""

    bundle_root: Path
    manifest_path: Path
    selected_trial_count: int
    skipped_trial_count: int


def build_reeval_bundle(
    *,
    config_path: Path,
    source_experiment_root: Path,
    remote_experiment_name: str,
    bundle_root: Path,
) -> ReevalBundleBuildResult:
    """Create a minimal re-eval bundle under bundle_root."""
    if bundle_root.exists():
        raise ValueError(f"Bundle root already exists: {bundle_root}")

    raw_config = _load_raw_config(config_path)
    normalized_config = normalize_experiment_config_dict(raw_config)
    source_experiment_name = normalized_config.get(
        "experiment",
        source_experiment_root.name,
    )

    trials = discover_trials(source_experiment_root)
    selected_trials = [
        trial for trial in trials if trial.status == "valid" and trial.reeval_ready
    ]
    skipped_trials = [
        _build_skipped_trial_record(trial, source_experiment_root)
        for trial in trials
        if trial not in selected_trials
    ]
    if not selected_trials:
        raise ValueError("No re-eval-ready trials found")

    (bundle_root / "config").mkdir(parents=True, exist_ok=False)
    (bundle_root / "trials").mkdir(parents=True, exist_ok=False)
    shutil.copy2(config_path, bundle_root / "config" / "source-config.yaml")

    selected_records: list[dict[str, Any]] = []
    for trial in selected_trials:
        rel_trial_path = trial.trial_dir.relative_to(source_experiment_root)
        dest_trial = bundle_root / "trials" / rel_trial_path
        _copy_required_trial_inputs(
            trial_dir=trial.trial_dir, mode=trial.mode, dest_trial=dest_trial
        )
        selected_records.append(
            {
                "relative_path": rel_trial_path.as_posix(),
                "benchmark": trial.benchmark,
                "harness": trial.harness,
                "mode": trial.mode.value,
                "trial_num": trial.trial_num,
                "sanitizer": getattr(trial.metadata, "sanitizer", None)
                if trial.metadata
                else None,
                "target_cpv_id": getattr(trial.metadata, "target_cpv_id", None)
                if trial.metadata
                else None,
            }
        )

    manifest = {
        "schema_version": 1,
        "bundle_id": _bundle_id(
            source_experiment_name=source_experiment_name,
            remote_experiment_name=remote_experiment_name,
            config_digest=_config_digest(normalized_config),
        ),
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "source_experiment_name": source_experiment_name,
        "remote_experiment_name": remote_experiment_name,
        "source_experiment_root": str(source_experiment_root),
        "source_config_digest": _config_digest(normalized_config),
        "selected_trial_count": len(selected_records),
        "skipped_trial_count": len(skipped_trials),
        "selected_trials": selected_records,
        "skipped_trials": skipped_trials,
    }
    manifest_path = bundle_root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    return ReevalBundleBuildResult(
        bundle_root=bundle_root,
        manifest_path=manifest_path,
        selected_trial_count=len(selected_records),
        skipped_trial_count=len(skipped_trials),
    )


def _load_raw_config(config_path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected YAML mapping in {config_path}")
    return payload


def _config_digest(config: dict[str, Any]) -> str:
    payload = json.dumps(config, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _bundle_id(
    *,
    source_experiment_name: str,
    remote_experiment_name: str,
    config_digest: str,
) -> str:
    digest_prefix = config_digest[:12]
    return f"{source_experiment_name}__{remote_experiment_name}__{digest_prefix}"


def _build_skipped_trial_record(trial, source_experiment_root: Path) -> dict[str, Any]:
    reason = trial.reeval_reason
    if not reason:
        if trial.status != "valid":
            reason = trial.error or trial.status
        else:
            reason = "not re-eval-ready"
    return {
        "relative_path": trial.trial_dir.relative_to(source_experiment_root).as_posix(),
        "status": trial.status,
        "reason": reason,
    }


def _copy_required_trial_inputs(
    *,
    trial_dir: Path,
    mode: TrialMode,
    dest_trial: Path,
) -> None:
    dest_trial.mkdir(parents=True, exist_ok=False)
    trial_paths = TrialDir(trial_dir)
    shutil.copy2(trial_paths.metadata_path, dest_trial / "metadata.json")

    for marker in (".success", ".fail"):
        source_marker = trial_dir / marker
        if source_marker.exists():
            shutil.copy2(source_marker, dest_trial / marker)

    if mode == TrialMode.bug_finding:
        _copy_tree_if_exists(trial_paths.output_povs, dest_trial / "output" / "povs")
        pov_store_path = trial_dir / "povs" / "pov_store.json"
        if pov_store_path.exists():
            (dest_trial / "povs").mkdir(parents=True, exist_ok=True)
            shutil.copy2(pov_store_path, dest_trial / "povs" / "pov_store.json")
        return

    if mode == TrialMode.patch_generation:
        _copy_tree_if_exists(
            trial_paths.output_patches,
            dest_trial / "output" / "patches",
        )
        _copy_tree_if_exists(
            trial_paths.input_povs,
            dest_trial / "crs-input" / "povs",
        )
        return

    raise ValueError(f"Unsupported re-eval trial mode: {mode.value}")


def _copy_tree_if_exists(source: Path, dest: Path) -> None:
    if not source.exists():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, dest)
