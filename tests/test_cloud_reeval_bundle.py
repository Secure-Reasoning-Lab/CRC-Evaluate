from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
import yaml

if TYPE_CHECKING:
    from pathlib import Path


def _write_source_config(config_path: Path, *, experiment: str) -> None:
    config_path.write_text(
        yaml.safe_dump(
            {
                "experiment": experiment,
                "experiment_filestore": str(config_path.parent / "filestore"),
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _write_trial_metadata(
    trial_dir: Path,
    *,
    benchmark: str,
    harness: str,
    mode: str,
    trial_num: int,
) -> None:
    (trial_dir / "metadata.json").write_text(
        json.dumps(
            {
                "timestamp": "2026-04-24T00:00:00",
                "trial_num": trial_num,
                "crs": "ensemble",
                "benchmark": benchmark,
                "harness": harness,
                "mode": mode,
                "source": {"path": "/src", "commit": "abc123"},
            }
        ),
        encoding="utf-8",
    )


def test_build_reeval_bundle_selects_only_valid_ready_trials(tmp_path: Path) -> None:
    from crsbench.cloud.reeval_bundle import build_reeval_bundle

    config_path = tmp_path / "config.yaml"
    experiment_root = tmp_path / "source-exp"
    bundle_root = tmp_path / "bundle"
    experiment_root.mkdir()
    _write_source_config(config_path, experiment="source-exp")

    bug_trial = experiment_root / "bugbench__ensemble" / "trial-1"
    bug_trial.mkdir(parents=True)
    _write_trial_metadata(
        bug_trial,
        benchmark="bugbench",
        harness="fuzz_bug",
        mode="bug_finding",
        trial_num=1,
    )
    (bug_trial / "output" / "povs").mkdir(parents=True)
    (bug_trial / "output" / "povs" / "pov-1.bin").write_text(
        "bug-pov",
        encoding="utf-8",
    )
    (bug_trial / "povs").mkdir(parents=True)
    (bug_trial / "povs" / "pov_store.json").write_text("{}", encoding="utf-8")
    (bug_trial / ".success").write_text("", encoding="utf-8")
    (bug_trial / "snapshot-0001.tar.gz").write_text("skip-me", encoding="utf-8")

    patch_trial = (
        experiment_root
        / "ensemble"
        / "patchbench"
        / "fuzz_patch"
        / "patch_generation"
        / "trial-2"
    )
    patch_trial.mkdir(parents=True)
    _write_trial_metadata(
        patch_trial,
        benchmark="patchbench",
        harness="fuzz_patch",
        mode="patch_generation",
        trial_num=2,
    )
    (patch_trial / "output" / "patches" / "cpv-0").mkdir(parents=True)
    (patch_trial / "output" / "patches" / "cpv-0" / "patch.diff").write_text(
        "--- a/a.c\n+++ b/a.c\n",
        encoding="utf-8",
    )
    (patch_trial / "crs-input" / "povs").mkdir(parents=True)
    (patch_trial / "crs-input" / "povs" / "seed-1.bin").write_text(
        "seed",
        encoding="utf-8",
    )
    (patch_trial / ".fail").write_text("", encoding="utf-8")

    skipped_trial = (
        experiment_root
        / "ensemble"
        / "patchbench"
        / "fuzz_patch"
        / "patch_generation"
        / "trial-3"
    )
    skipped_trial.mkdir(parents=True)
    _write_trial_metadata(
        skipped_trial,
        benchmark="patchbench",
        harness="fuzz_patch",
        mode="patch_generation",
        trial_num=3,
    )
    (skipped_trial / "output" / "patches" / "cpv-1").mkdir(parents=True)
    (skipped_trial / "output" / "patches" / "cpv-1" / "patch.diff").write_text(
        "--- a/b.c\n+++ b/b.c\n",
        encoding="utf-8",
    )

    result = build_reeval_bundle(
        config_path=config_path,
        source_experiment_root=experiment_root,
        remote_experiment_name="source-exp-reeval-20260424",
        bundle_root=bundle_root,
    )

    assert result.selected_trial_count == 2
    assert result.skipped_trial_count == 1
    assert (bundle_root / "config" / "source-config.yaml").is_file()
    assert (
        bundle_root / "trials" / "bugbench__ensemble" / "trial-1" / "metadata.json"
    ).is_file()
    assert (
        bundle_root / "trials" / "bugbench__ensemble" / "trial-1" / ".success"
    ).is_file()
    assert (
        bundle_root
        / "trials"
        / "bugbench__ensemble"
        / "trial-1"
        / "output"
        / "povs"
        / "pov-1.bin"
    ).is_file()
    assert (
        bundle_root
        / "trials"
        / "bugbench__ensemble"
        / "trial-1"
        / "povs"
        / "pov_store.json"
    ).is_file()
    assert not (
        bundle_root
        / "trials"
        / "bugbench__ensemble"
        / "trial-1"
        / "snapshot-0001.tar.gz"
    ).exists()
    assert (
        bundle_root
        / "trials"
        / "ensemble"
        / "patchbench"
        / "fuzz_patch"
        / "patch_generation"
        / "trial-2"
        / "output"
        / "patches"
        / "cpv-0"
        / "patch.diff"
    ).is_file()
    assert (
        bundle_root
        / "trials"
        / "ensemble"
        / "patchbench"
        / "fuzz_patch"
        / "patch_generation"
        / "trial-2"
        / "crs-input"
        / "povs"
        / "seed-1.bin"
    ).is_file()
    manifest = json.loads((bundle_root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["source_experiment_name"] == "source-exp"
    assert manifest["remote_experiment_name"] == "source-exp-reeval-20260424"
    assert manifest["selected_trial_count"] == 2
    assert manifest["skipped_trial_count"] == 1
    assert "source_experiment_root" not in manifest
    assert manifest["skipped_trials"][0]["reason"] == "missing crs-input/povs directory"


def test_build_reeval_bundle_raises_when_no_ready_trials(tmp_path: Path) -> None:
    from crsbench.cloud.reeval_bundle import build_reeval_bundle

    config_path = tmp_path / "config.yaml"
    experiment_root = tmp_path / "source-exp"
    bundle_root = tmp_path / "bundle"
    experiment_root.mkdir()
    _write_source_config(config_path, experiment="source-exp")

    trial_dir = experiment_root / "patchbench__ensemble" / "trial-1"
    trial_dir.mkdir(parents=True)
    _write_trial_metadata(
        trial_dir,
        benchmark="patchbench",
        harness="fuzz_patch",
        mode="patch_generation",
        trial_num=1,
    )
    (trial_dir / "output" / "patches" / "cpv-0").mkdir(parents=True)
    (trial_dir / "output" / "patches" / "cpv-0" / "patch.diff").write_text(
        "--- a/a.c\n+++ b/a.c\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="No re-eval-ready trials"):
        build_reeval_bundle(
            config_path=config_path,
            source_experiment_root=experiment_root,
            remote_experiment_name="source-exp-reeval-20260424",
            bundle_root=bundle_root,
        )
