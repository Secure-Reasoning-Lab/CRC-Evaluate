from __future__ import annotations

import importlib.util
import json
from pathlib import Path

SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "check_experiment_completeness.py"
)


def _load_script_module():
    spec = importlib.util.spec_from_file_location(
        "check_experiment_completeness_script", SCRIPT_PATH
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_trial(
    trial_dir: Path, *, success: bool = True, total_cost_usd: float = 1.0
) -> None:
    trial_dir.mkdir(parents=True, exist_ok=True)
    if success:
        (trial_dir / ".success").touch()
    (trial_dir / "llm-usage.json").write_text(
        json.dumps({"total_cost_usd": total_cost_usd}),
        encoding="utf-8",
    )


def _build_wrapped_experiment_root(tmp_path: Path) -> Path:
    experiment_root = tmp_path / "exp-audit"
    leaf_root = (
        experiment_root
        / "exp-audit"
        / "exp-audit"
        / "crs-a"
        / "bench-a"
        / "harness-a"
        / "delta"
        / "address"
    )
    _write_trial(leaf_root / "trial-1", total_cost_usd=0.0)
    _write_trial(leaf_root / "trial-2", total_cost_usd=3.5)
    return experiment_root


def test_main_default_output_preserves_existing_summary_shape(
    tmp_path: Path, capsys
) -> None:
    module = _load_script_module()
    experiment_root = _build_wrapped_experiment_root(tmp_path)

    exit_code = module.main([str(experiment_root)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "benchmarks:     1" in captured.out
    assert "trial-keys-output" not in captured.out


def test_main_writes_canonical_trial_keys_for_problem_trials_when_flag_is_set(
    tmp_path: Path, capsys
) -> None:
    module = _load_script_module()
    experiment_root = _build_wrapped_experiment_root(tmp_path)
    selector_path = tmp_path / "bad-trials.txt"

    exit_code = module.main(
        [str(experiment_root), "--trial-keys-output", str(selector_path)]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "benchmarks:     1" in captured.out
    assert selector_path.read_text(encoding="utf-8") == (
        "crs-a:bench-a:harness-a:delta:address:1:-\n"
        "crs-a:bench-a:harness-a:delta:address:3:-\n"
    )


def test_main_writes_keys_for_extra_trials_when_count_exceeds_expected(
    tmp_path: Path, capsys
) -> None:
    module = _load_script_module()
    experiment_root = _build_wrapped_experiment_root(tmp_path)
    extra_trial = (
        experiment_root
        / "exp-audit"
        / "exp-audit"
        / "crs-a"
        / "bench-a"
        / "harness-a"
        / "delta"
        / "address"
        / "trial-4"
    )
    _write_trial(extra_trial, total_cost_usd=2.0)
    selector_path = tmp_path / "extra-bad-trials.txt"

    exit_code = module.main(
        [str(experiment_root), "--trial-keys-output", str(selector_path)]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "trials seen:    3  (expected per leaf: 3)" in captured.out
    assert selector_path.read_text(encoding="utf-8") == (
        "crs-a:bench-a:harness-a:delta:address:1:-\n"
        "crs-a:bench-a:harness-a:delta:address:3:-\n"
        "crs-a:bench-a:harness-a:delta:address:4:-\n"
    )
