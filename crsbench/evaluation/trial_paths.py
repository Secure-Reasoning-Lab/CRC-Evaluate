"""Shared helpers for resolving standard trial artifact paths.

Centralizing these paths prevents drift between evaluator, re-eval,
and reporting code paths.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional


@dataclass(frozen=True)
class TrialDir:
    """Canonical path resolver for a single trial directory."""

    path: Path

    @property
    def output(self) -> Path:
        return self.path / "output"

    @property
    def output_povs(self) -> Path:
        return self.output / "povs"

    @property
    def output_patches(self) -> Path:
        return self.output / "patches"

    @property
    def input_povs(self) -> Path:
        return self.path / "crs-input" / "povs"

    @property
    def patch_verify_dir(self) -> Path:
        return self.path / "patches"

    @property
    def patch_verify_logs(self) -> Path:
        return self.patch_verify_dir / "logs"

    @property
    def metadata_path(self) -> Path:
        return self.path / "metadata.json"

    @property
    def patch_verification_results_path(self) -> Path:
        return self.path / "patch_verification_results.json"

    @property
    def worker_log_path(self) -> Path:
        return self.path / "worker.log"

    def count_visible_files(self, dir_path: Path) -> int:
        if not dir_path.exists():
            return 0
        return sum(
            1 for p in dir_path.iterdir() if p.is_file() and not p.name.startswith(".")
        )

    def count_visible_input_povs(self) -> int:
        return self.count_visible_files(self.input_povs)

    def count_visible_patch_diffs(self) -> int:
        patch_root = self.output_patches
        if not patch_root.exists():
            return 0

        count = 0
        for diff_file in patch_root.rglob("*.diff"):
            rel_parts = diff_file.relative_to(patch_root).parts
            if any(part.startswith(".") for part in rel_parts):
                continue
            if diff_file.is_file():
                count += 1
        return count


@dataclass(frozen=True)
class ExperimentDir:
    """Canonical path resolver for experiment directory and trial mirroring."""

    path: Path

    @classmethod
    def from_config_dict(cls, config: Mapping[str, Any]) -> "ExperimentDir":
        filestore = config["experiment_filestore"]
        experiment_name = config["experiment"]
        return cls(Path(filestore) / experiment_name)

    def trial_relative_path(self, trial_dir: Path) -> Path:
        return trial_dir.relative_to(self.path)

    def mirror_trial_dir(
        self,
        trial_dir: Path,
        output_base: Optional[Path],
    ) -> Path:
        if output_base is None:
            return trial_dir
        return output_base / self.trial_relative_path(trial_dir)


def trial_output_dir(trial_dir: Path) -> Path:
    """Return trial output directory."""
    return TrialDir(trial_dir).output


def trial_output_povs_dir(trial_dir: Path) -> Path:
    """Return directory containing discovered POVs from CRS outputs."""
    return TrialDir(trial_dir).output_povs


def trial_output_patches_dir(trial_dir: Path) -> Path:
    """Return directory containing discovered patches from CRS outputs."""
    return TrialDir(trial_dir).output_patches


def visible_patch_diff_count(trial_dir: Path) -> int:
    """Count non-hidden patch diff files under output/patches."""
    return TrialDir(trial_dir).count_visible_patch_diffs()


def trial_input_povs_dir(trial_dir: Path) -> Path:
    """Return directory containing prepared input POVs for patch verification."""
    return TrialDir(trial_dir).input_povs


def trial_patch_verify_work_dir(dest_dir: Path) -> Path:
    """Return base directory for patch verification artifacts."""
    return TrialDir(dest_dir).patch_verify_dir


def trial_patch_verify_logs_dir(dest_dir: Path) -> Path:
    """Return directory for patch verification stdout/stderr logs."""
    return TrialDir(dest_dir).patch_verify_logs


def count_visible_files(dir_path: Path) -> int:
    """Count non-hidden files in a directory."""
    if not dir_path.exists():
        return 0
    return sum(
        1 for p in dir_path.iterdir() if p.is_file() and not p.name.startswith(".")
    )


def experiment_dir(experiment_filestore: str | Path, experiment_name: str) -> Path:
    """Return canonical experiment directory path."""
    return Path(experiment_filestore) / experiment_name


def experiment_dir_from_config_dict(config: Mapping[str, Any]) -> Path:
    """Resolve experiment directory from config-like mapping."""
    return ExperimentDir.from_config_dict(config).path


def trial_relative_to_experiment(trial_dir: Path, exp_dir: Path) -> Path:
    """Return trial path relative to experiment directory."""
    return ExperimentDir(exp_dir).trial_relative_path(trial_dir)


def mirrored_trial_output_dir(
    trial_dir: Path,
    output_base: Optional[Path],
    exp_dir: Path,
) -> Path:
    """Return trial destination path under output_base, preserving layout."""
    return ExperimentDir(exp_dir).mirror_trial_dir(trial_dir, output_base)
