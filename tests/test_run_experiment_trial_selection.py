from __future__ import annotations

from argparse import Namespace
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import patch

import crsbench.run_experiment as run_experiment
import pytest
from crsbench.experiment.trial_selection import (
    TRIAL_KEY_ALLOWLIST_ENV_VAR,
    encode_trial_key_allowlist,
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


def _mk_config(tmp_path: Path):
    return SimpleNamespace(
        experiment="exp-test",
        benchmark_suite=None,
        benchmarks_root=tmp_path / "benchmarks",
        mode=SimpleNamespace(value="all"),
        registry_dir=tmp_path / "registry",
        worker=None,
        get_crs_registry_ids=lambda: ["crs-a"],
        get_benchmark_entries=lambda: [SimpleNamespace(name="bench-a")],
        get_benchmark_list=lambda: ["bench-a"],
    )


def test_filter_trial_matrix_from_env_selects_requested_trials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    t1 = _mk_trial(crs="crs-1", trial_num=1)
    t2 = _mk_trial(crs="crs-2", trial_num=2)
    t3 = _mk_trial(crs="crs-3", trial_num=3)
    selected_key = trial_key_for_trial(t2)
    monkeypatch.setenv(
        TRIAL_KEY_ALLOWLIST_ENV_VAR,
        encode_trial_key_allowlist([selected_key]),
    )

    with patch.object(run_experiment, "logger") as logger_mock:
        filtered = run_experiment._filter_trial_matrix_from_env([t1, t2, t3])

    assert filtered == [t2]
    logger_mock.info.assert_called_once()
    assert "selected 1 of 3 trial(s)" in logger_mock.info.call_args.args[0]


def test_filter_trial_matrix_from_env_rejects_unknown_trial_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        TRIAL_KEY_ALLOWLIST_ENV_VAR,
        encode_trial_key_allowlist(["crs-x:bench-x:h-x:delta:address:99:-"]),
    )

    with pytest.raises(ValueError, match="Unknown trial keys requested"):
        run_experiment._filter_trial_matrix_from_env([_mk_trial()])


def test_filter_trial_matrix_from_env_accepts_empty_selector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TRIAL_KEY_ALLOWLIST_ENV_VAR, "")

    assert run_experiment._filter_trial_matrix_from_env([_mk_trial()]) == []


def test_main_dispatches_filtered_trial_matrix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("experiment: exp-test\n", encoding="utf-8")

    args = Namespace(
        command="run",
        verbose=False,
        experiment_config=str(config_path),
        local_only=True,
        distributed=False,
        dry_run=False,
        queue_mode=None,
        retry_failed=False,
    )
    config = _mk_config(tmp_path)
    t1 = _mk_trial(crs="crs-1", trial_num=1)
    t2 = _mk_trial(crs="crs-2", trial_num=2)

    monkeypatch.setenv(
        TRIAL_KEY_ALLOWLIST_ENV_VAR,
        encode_trial_key_allowlist([trial_key_for_trial(t2)]),
    )

    monkeypatch.setattr(run_experiment, "parse_arguments", lambda: args)
    monkeypatch.setattr(run_experiment, "validate_arguments", lambda _args: None)
    monkeypatch.setattr(
        run_experiment, "load_experiment_config", lambda _config_path: config
    )
    monkeypatch.setattr(
        run_experiment, "validate_filestore_permissions", lambda _config: None
    )
    monkeypatch.setattr(
        run_experiment,
        "resolve_benchmarks_root",
        lambda _benchmarks_root: tmp_path / "benchmarks",
    )
    monkeypatch.setattr(
        run_experiment,
        "resolve_benchmark_harnesses",
        lambda _entries, _benchmarks_root: [SimpleNamespace(name="bench-a")],
    )
    monkeypatch.setattr(
        run_experiment,
        "generate_trial_matrix",
        lambda *_args, **_kwargs: [t1, t2],
    )
    monkeypatch.setattr(
        run_experiment,
        "should_use_distributed_mode",
        lambda _args, _config, _total_jobs: False,
    )
    monkeypatch.setattr(
        run_experiment, "display_estimated_runtime", lambda *_args, **_kwargs: None
    )

    with (
        patch.object(run_experiment, "run_experiment_local") as run_local,
        patch.object(run_experiment, "run_experiment_distributed") as run_distributed,
    ):
        run_experiment.main()

    run_local.assert_called_once_with(config.experiment, config, [t2])
    run_distributed.assert_not_called()
