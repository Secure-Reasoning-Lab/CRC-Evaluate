from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from crsbench.distributed.queue import build_trial_key
from crsbench.experiment.trial_selection import (
    TRIAL_KEY_ALLOWLIST_ENV_VAR,
    decode_trial_key_allowlist,
    default_collected_experiment_path,
    default_selector_output_path,
    derive_unfinished_trial_keys_from_config,
    encode_trial_key_allowlist,
    filter_trials_by_allowlist,
    load_trial_key_file,
    normalize_trial_key_lines,
    trial_key_for_trial,
)

if TYPE_CHECKING:
    from pathlib import Path


def _mk_trial(
    *,
    crs: str = "crs-a",
    benchmark: str = "bench-a",
    harness: str = "harness-a",
    mode: str = "delta",
    sanitizer: str = "address",
    trial_num: int = 1,
    target_cpv_id: str | None = None,
):
    return SimpleNamespace(
        crs=crs,
        benchmark_harness=SimpleNamespace(
            name=benchmark,
            harness=SimpleNamespace(name=harness),
        ),
        mode=mode,
        sanitizer=sanitizer,
        trial_num=trial_num,
        target_cpv_id=target_cpv_id,
    )


def _mk_trial_dir(root: Path, trial) -> Path:
    parts: list[str] = [
        trial.crs,
        trial.benchmark_harness.name,
        trial.benchmark_harness.harness.name,
    ]
    if trial.target_cpv_id:
        parts.append(trial.target_cpv_id)
    parts.extend([trial.mode, trial.sanitizer, f"trial-{trial.trial_num}"])
    trial_dir = root.joinpath(*parts)
    trial_dir.mkdir(parents=True, exist_ok=True)
    return trial_dir


def test_paths_and_env_constant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert TRIAL_KEY_ALLOWLIST_ENV_VAR == "CRSBENCH_ONLY_TRIAL_KEYS_B64"

    config = SimpleNamespace(
        experiment_filestore=tmp_path / "experiment-data",
        experiment="exp-1",
    )
    assert default_collected_experiment_path(config) == (
        tmp_path / "experiment-data" / "exp-1"
    )

    assert default_selector_output_path("exp-1", cwd=tmp_path) == (
        tmp_path / "exp-1-unfinished-trial-keys.txt"
    )
    monkeypatch.chdir(tmp_path)
    assert default_selector_output_path("exp-2") == (
        tmp_path / "exp-2-unfinished-trial-keys.txt"
    )


def test_normalize_trial_key_lines_dedupes_and_strips() -> None:
    text = "\n  key-1  \n\nkey-2\nkey-1\n   \nkey-3\nkey-2\n"
    assert normalize_trial_key_lines(text) == ["key-1", "key-2", "key-3"]


def test_load_trial_key_file_normalizes(tmp_path: Path) -> None:
    selector = tmp_path / "selector.txt"
    selector.write_text("\n key-a \nkey-b\n\nkey-a\n")

    assert load_trial_key_file(selector) == ["key-a", "key-b"]


def test_encode_decode_trial_key_allowlist_roundtrip_and_empty() -> None:
    encoded = encode_trial_key_allowlist(["key-a", " key-b ", "key-a", ""])
    assert decode_trial_key_allowlist(encoded) == ["key-a", "key-b"]
    assert decode_trial_key_allowlist(encode_trial_key_allowlist([])) == []
    assert decode_trial_key_allowlist("") == []


def test_trial_key_for_trial_uses_canonical_build_trial_key() -> None:
    trial = _mk_trial(target_cpv_id="cpv-1")
    assert trial_key_for_trial(trial) == build_trial_key(
        crs="crs-a",
        benchmark="bench-a",
        harness="harness-a",
        mode="delta",
        sanitizer="address",
        trial_num=1,
        target_cpv_id="cpv-1",
    )

    trial_untargeted = _mk_trial(target_cpv_id=None)
    assert trial_key_for_trial(trial_untargeted).endswith(":-")


def test_filter_trials_by_allowlist_returns_unknown_sorted_and_preserves_matrix_order() -> (
    None
):
    t1 = _mk_trial(crs="crs-1", trial_num=1)
    t2 = _mk_trial(crs="crs-2", trial_num=2)
    t3 = _mk_trial(crs="crs-3", trial_num=3)

    key1 = trial_key_for_trial(t1)
    key2 = trial_key_for_trial(t2)

    filtered, unknown = filter_trials_by_allowlist(
        [t1, t2, t3],
        [key2, "zzz", key1, "aaa"],
    )

    assert filtered == [t1, t2]
    assert unknown == ["aaa", "zzz"]
    assert filter_trials_by_allowlist([t1], [])[0] == []


def test_derive_unfinished_trial_keys_supports_targeted_and_untargeted_layouts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    t1 = _mk_trial(crs="crs-1", benchmark="b1", harness="h1", trial_num=1)
    t2 = _mk_trial(
        crs="crs-1",
        benchmark="b1",
        harness="h1",
        target_cpv_id="cpv-7",
        trial_num=2,
    )
    t3 = _mk_trial(
        crs="crs-2",
        benchmark="b2",
        harness="h2",
        target_cpv_id="cpv-9",
        trial_num=3,
    )
    t4 = _mk_trial(crs="crs-3", benchmark="b3", harness="h3", trial_num=4)
    trial_matrix = [t1, t2, t3, t4]

    from crsbench.experiment import trial_selection as mod

    monkeypatch.setattr(mod, "_build_trial_matrix_from_config", lambda _: trial_matrix)

    (_mk_trial_dir(tmp_path, t1) / ".success").touch()
    (_mk_trial_dir(tmp_path, t2) / ".success").touch()
    (_mk_trial_dir(tmp_path, t3) / ".fail").touch()

    derived = derive_unfinished_trial_keys_from_config(
        config=SimpleNamespace(),
        collected_root=tmp_path,
    )

    assert derived.selected_keys == [trial_key_for_trial(t4)]
    assert derived.finished_success_keys == [
        trial_key_for_trial(t1),
        trial_key_for_trial(t2),
    ]
    assert derived.finished_fail_keys == [trial_key_for_trial(t3)]


def test_derive_unfinished_trial_keys_rerun_failed_trials(
    tmp_path: Path, monkeypatch
) -> None:
    t1 = _mk_trial(crs="crs-1", trial_num=1)
    t2 = _mk_trial(crs="crs-2", trial_num=2)
    trial_matrix = [t1, t2]

    from crsbench.experiment import trial_selection as mod

    monkeypatch.setattr(mod, "_build_trial_matrix_from_config", lambda _: trial_matrix)

    (_mk_trial_dir(tmp_path, t1) / ".success").touch()
    (_mk_trial_dir(tmp_path, t2) / ".fail").touch()
    (tmp_path / "unknown" / "bench" / "h" / "delta" / "address" / "trial-9").mkdir(
        parents=True, exist_ok=True
    )
    (
        tmp_path / "unknown" / "bench" / "h" / "delta" / "address" / "trial-9" / ".fail"
    ).touch()

    derived = derive_unfinished_trial_keys_from_config(
        config=SimpleNamespace(),
        collected_root=tmp_path,
        rerun_failed_trials=True,
    )
    assert derived.selected_keys == [trial_key_for_trial(t2)]
    assert derived.finished_success_keys == [trial_key_for_trial(t1)]
    assert derived.finished_fail_keys == []


def test_derive_unfinished_trial_keys_raises_for_unknown_finished_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    known = _mk_trial(crs="crs-known", benchmark="bench", harness="h", trial_num=1)
    trial_matrix = [known]

    from crsbench.experiment import trial_selection as mod

    monkeypatch.setattr(mod, "_build_trial_matrix_from_config", lambda _: trial_matrix)

    unknown_dir = (
        tmp_path / "crs-unknown" / "bench" / "h" / "delta" / "address" / "trial-1"
    )
    unknown_dir.mkdir(parents=True, exist_ok=True)
    (unknown_dir / ".success").touch()

    with pytest.raises(
        ValueError, match="finished trial keys not present in trial matrix"
    ):
        derive_unfinished_trial_keys_from_config(
            config=SimpleNamespace(),
            collected_root=tmp_path,
        )
