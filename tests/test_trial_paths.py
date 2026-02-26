"""Tests for experiment/trial path helper models."""

from pathlib import Path

from crsbench.evaluation.trial_paths import ExperimentDir, TrialDir


def test_trial_dir_paths_and_visible_counts(tmp_path: Path) -> None:
    trial = TrialDir(tmp_path / "trial-1")
    trial.path.mkdir(parents=True)
    trial.output_patches.mkdir(parents=True)
    trial.input_povs.mkdir(parents=True)

    (trial.output_patches / "patch_0.diff").write_text("diff")
    hidden_dir = trial.output_patches / ".tmp"
    hidden_dir.mkdir()
    (hidden_dir / "hidden.diff").write_text("diff")
    (trial.input_povs / "cpv_0").write_bytes(b"a")
    (trial.input_povs / ".hidden").write_bytes(b"b")
    (trial.input_povs / "nested").mkdir()

    assert trial.output == trial.path / "output"
    assert trial.patch_verify_logs == trial.path / "patches" / "logs"
    assert trial.patch_verification_results_path == (
        trial.path / "patch_verification_results.json"
    )
    assert trial.count_visible_patch_diffs() == 1
    assert trial.count_visible_input_povs() == 1


def test_experiment_dir_mirror_trial_path(tmp_path: Path) -> None:
    exp = ExperimentDir(tmp_path / "exp")
    trial_dir = exp.path / "crs" / "bench" / "trial-1"

    mirrored = exp.mirror_trial_dir(trial_dir, tmp_path / "out")

    assert exp.trial_relative_path(trial_dir) == Path("crs/bench/trial-1")
    assert mirrored == tmp_path / "out" / "crs" / "bench" / "trial-1"
