"""Tests for per-CRS from_experiment routing.

Covers:
- Schema mutual exclusion of from_experiment and from_experiment_by_crs.
- ExperimentPovInputs.resolve_from_experiment_for() dispatch rules.
- resolve_pov_from_experiment_for_crs() dict-level helper.
- EffectiveInputSettings resolution picks per-CRS path when crs kwarg set.
- Metadata rewrite translates from_experiment_by_crs map entries.
"""

from __future__ import annotations

import base64
import textwrap
from typing import TYPE_CHECKING

import pytest
import yaml
from crsbench.cloud.gce.metadata import (
    CRSBENCH_EXPERIMENT_CONFIG_B64_KEY,
    _render_transported_config_bytes,
    _rewrite_from_experiment_by_crs,
)
from crsbench.evaluation.trial_preparation import resolve_pov_from_experiment_for_crs
from crsbench.validation.schemas import ExperimentPovInputs
from pydantic import ValidationError

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# ExperimentPovInputs.resolve_from_experiment_for
# ---------------------------------------------------------------------------


def test_resolve_prefers_per_crs_map_when_present(tmp_path: Path) -> None:
    path_a = tmp_path / "a"
    path_b = tmp_path / "b"
    inputs = ExperimentPovInputs(
        from_experiment_by_crs={"crs-a": path_a, "crs-b": path_b},
    )
    assert inputs.resolve_from_experiment_for("crs-a") == path_a
    assert inputs.resolve_from_experiment_for("crs-b") == path_b


def test_resolve_returns_none_for_unknown_crs(tmp_path: Path) -> None:
    inputs = ExperimentPovInputs(
        from_experiment_by_crs={"crs-a": tmp_path / "a"},
    )
    assert inputs.resolve_from_experiment_for("crs-nonexistent") is None


def test_resolve_none_crs_with_map_returns_none(tmp_path: Path) -> None:
    inputs = ExperimentPovInputs(
        from_experiment_by_crs={"crs-a": tmp_path / "a"},
    )
    assert inputs.resolve_from_experiment_for(None) is None


def test_resolve_falls_back_to_single_path(tmp_path: Path) -> None:
    inputs = ExperimentPovInputs(from_experiment=tmp_path / "single")
    # Single path applies to every CRS.
    assert inputs.resolve_from_experiment_for("any-crs") == tmp_path / "single"
    assert inputs.resolve_from_experiment_for(None) == tmp_path / "single"


def test_resolve_returns_none_when_neither_set() -> None:
    inputs = ExperimentPovInputs()
    assert inputs.resolve_from_experiment_for("crs-a") is None
    assert inputs.resolve_from_experiment_for(None) is None


# ---------------------------------------------------------------------------
# Schema validation (mutual exclusion + task=bugfixing + resolve_path_fields)
# ---------------------------------------------------------------------------


def _minimal_config(tmp_path: Path, **overrides) -> dict:
    """Build a minimally valid ExperimentConfig dict; override pov-related fields in tests."""
    base = {
        "experiment": "exp-per-crs",
        "task": "bugfixing",
        "mode": "auto",
        "benchmarks": [{"afc-curl-delta-02": ["curl_fuzzer_ws"]}],
        "runtime": {
            "trials": 1,
            "max_total_time": 21600,
            "build_timeout": 1200,
            "run_timeout": 1200,
            "verify_timeout": 600,
        },
        "storage": {
            "experiment_filestore": str(tmp_path / "experiment-data"),
            "report_filestore": str(tmp_path / "report-data"),
        },
        "crs_compose": {
            "crs-claude-code": {
                "num_cores": 8,
            }
        },
    }
    # Merge overrides, but for runtime we want to merge rather than replace so
    # callers can add inputs/... without losing trials/max_total_time.
    runtime_override = overrides.pop("runtime", None)
    if runtime_override is not None:
        base["runtime"] = {**base["runtime"], **runtime_override}
    return {**base, **overrides}


def test_schema_rejects_both_single_and_per_crs(tmp_path: Path) -> None:
    from crsbench.validation.schemas import ExperimentConfig

    path_single = tmp_path / "single"
    path_single.mkdir()
    path_per_crs = tmp_path / "crs-a"
    path_per_crs.mkdir()

    raw = _minimal_config(
        tmp_path,
        runtime={
            "inputs": {
                "pov": {
                    "from_experiment": str(path_single),
                    "from_experiment_by_crs": {"crs-a": str(path_per_crs)},
                },
            },
        },
    )
    with pytest.raises(ValidationError, match="mutually exclusive"):
        ExperimentConfig(**raw)


def test_schema_rejects_empty_per_crs_map(tmp_path: Path) -> None:
    from crsbench.validation.schemas import ExperimentConfig

    raw = _minimal_config(
        tmp_path,
        runtime={
            "inputs": {
                "pov": {"from_experiment_by_crs": {}},
            },
        },
    )
    with pytest.raises(ValidationError, match="must not be empty"):
        ExperimentConfig(**raw)


def test_schema_rejects_per_crs_on_bugfinding(tmp_path: Path) -> None:
    from crsbench.validation.schemas import ExperimentConfig

    path_per_crs = tmp_path / "crs-a"
    path_per_crs.mkdir()

    raw = _minimal_config(
        tmp_path,
        task="bugfinding",
        runtime={
            "inputs": {
                "pov": {"from_experiment_by_crs": {"crs-a": str(path_per_crs)}},
            },
        },
    )
    with pytest.raises(ValidationError, match="only valid for task='bugfixing'"):
        ExperimentConfig(**raw)


def test_schema_resolves_relative_paths_in_per_crs_map(tmp_path: Path) -> None:
    from crsbench.validation.schemas import ExperimentConfig

    # Create a relative-looking path that actually exists after resolution.
    rel_dir = tmp_path / "relative-finding"
    rel_dir.mkdir()

    raw = _minimal_config(
        tmp_path,
        runtime={
            "inputs": {
                "pov": {
                    "from_experiment_by_crs": {"crs-claude-code": str(rel_dir)},
                },
            },
        },
    )
    config = ExperimentConfig(**raw)
    resolved = config.inputs.pov.from_experiment_by_crs["crs-claude-code"]
    assert resolved.is_absolute()
    assert resolved == rel_dir.resolve()


def test_schema_rejects_unknown_crs_keys_in_per_crs_map(tmp_path: Path) -> None:
    from crsbench.validation.schemas import ExperimentConfig

    path_per_crs = tmp_path / "crs-a"
    path_per_crs.mkdir()

    raw = _minimal_config(
        tmp_path,
        runtime={
            "inputs": {
                "pov": {
                    "from_experiment_by_crs": {"crs-unknown": str(path_per_crs)},
                },
            },
        },
    )
    with pytest.raises(ValidationError, match="must match fixing CRS names"):
        ExperimentConfig(**raw)


# ---------------------------------------------------------------------------
# Dict-level helper for legacy callers
# ---------------------------------------------------------------------------


def test_dict_resolver_prefers_map_when_set() -> None:
    pov_cfg = {
        "from_experiment": "/legacy/path",
        "from_experiment_by_crs": {"crs-a": "/per/crs/a"},
    }
    # Map wins even when from_experiment is also present in the raw dict.
    assert resolve_pov_from_experiment_for_crs(pov_cfg, "crs-a") == "/per/crs/a"


def test_dict_resolver_returns_none_for_missing_crs() -> None:
    pov_cfg = {"from_experiment_by_crs": {"crs-a": "/per/crs/a"}}
    assert resolve_pov_from_experiment_for_crs(pov_cfg, "crs-b") is None


def test_dict_resolver_falls_back_to_single() -> None:
    pov_cfg = {"from_experiment": "/legacy/path"}
    assert resolve_pov_from_experiment_for_crs(pov_cfg, "any-crs") == "/legacy/path"


# ---------------------------------------------------------------------------
# EffectiveInputSettings resolution with crs kwarg
# ---------------------------------------------------------------------------


def test_effective_inputs_picks_per_crs_path(tmp_path: Path) -> None:
    from crsbench.distributed.jobs import _resolve_effective_input_settings
    from crsbench.validation.schemas import ExperimentConfig

    path_a = tmp_path / "crs-a-source"
    path_b = tmp_path / "crs-b-source"
    path_a.mkdir()
    path_b.mkdir()

    raw = _minimal_config(
        tmp_path,
        crs_compose={
            "crs-claude-code": {"num_cores": 8},
            "crs-codex": {"num_cores": 8},
        },
        runtime={
            "inputs": {
                "pov": {
                    "from_experiment_by_crs": {
                        "crs-claude-code": str(path_a),
                        "crs-codex": str(path_b),
                    },
                },
            },
        },
    )
    config = ExperimentConfig(**raw)

    settings_a = _resolve_effective_input_settings(config, raw, crs="crs-claude-code")
    assert settings_a.pov_from_experiment == path_a

    settings_b = _resolve_effective_input_settings(config, raw, crs="crs-codex")
    assert settings_b.pov_from_experiment == path_b

    # Unknown CRS → no path
    settings_unknown = _resolve_effective_input_settings(config, raw, crs="crs-unknown")
    assert settings_unknown.pov_from_experiment is None


# ---------------------------------------------------------------------------
# generate_trial_matrix — missing per-CRS source directory
# ---------------------------------------------------------------------------


def test_trial_matrix_raises_when_any_per_crs_path_missing(tmp_path: Path) -> None:
    """All-or-nothing: a missing per-CRS path aborts the whole experiment.

    Rejecting partial runs prevents silently producing fewer trials than
    expected when phase 1 only completed for a subset of CRSes.
    """
    from crsbench.run_experiment import generate_trial_matrix
    from crsbench.validation.schemas import ExperimentConfig

    present_dir = tmp_path / "crs-present-source"
    present_dir.mkdir()
    missing_dir = tmp_path / "crs-missing-source"  # never created

    raw = _minimal_config(
        tmp_path,
        crs_compose={
            "crs-present": {"num_cores": 8},
            "crs-missing": {"num_cores": 8},
        },
        runtime={
            "inputs": {
                "pov": {
                    "from_experiment_by_crs": {
                        "crs-present": str(present_dir),
                        "crs-missing": str(missing_dir),
                    },
                },
            },
        },
    )
    config = ExperimentConfig(**raw)

    with pytest.raises(ValueError, match="crs-missing"):
        generate_trial_matrix(
            benchmark_harnesses=[],
            oss_crs_registry=["crs-present", "crs-missing"],
            config=config,
            registry_dir=tmp_path,
        )


def test_trial_matrix_reports_all_missing_paths_at_once(tmp_path: Path) -> None:
    """Pre-check must collect every missing path before raising, not stop at
    the first one — lets the operator fix them in one pass."""
    from crsbench.run_experiment import generate_trial_matrix
    from crsbench.validation.schemas import ExperimentConfig

    missing_a = tmp_path / "missing-a"
    missing_b = tmp_path / "missing-b"
    # neither created

    raw = _minimal_config(
        tmp_path,
        crs_compose={
            "crs-a": {"num_cores": 8},
            "crs-b": {"num_cores": 8},
        },
        runtime={
            "inputs": {
                "pov": {
                    "from_experiment_by_crs": {
                        "crs-a": str(missing_a),
                        "crs-b": str(missing_b),
                    },
                },
            },
        },
    )
    config = ExperimentConfig(**raw)

    with pytest.raises(ValueError) as excinfo:
        generate_trial_matrix(
            benchmark_harnesses=[],
            oss_crs_registry=["crs-a", "crs-b"],
            config=config,
            registry_dir=tmp_path,
        )

    message = str(excinfo.value)
    assert "crs-a" in message
    assert "crs-b" in message


def test_trial_matrix_pairs_fixing_trials_to_finding_trials(tmp_path: Path) -> None:
    """1:1 trial pairing: fixing trial-N is only scheduled when finding
    trial-N actually discovered the target CPV."""
    import json
    from unittest.mock import MagicMock, patch

    from crsbench.evaluation.verification.models import PovVerificationStatus
    from crsbench.run_experiment import generate_trial_matrix
    from crsbench.validation.schemas import (
        BenchmarkHarness,
        ExperimentConfig,
        HarnessFile,
    )

    # Build a fake finding-experiment tree with 3 trials. trial-1 finds
    # cpv_0, trial-2 finds nothing, trial-3 finds cpv_0 too.
    finding_root = tmp_path / "finding-source"
    bench, harness, mode, sanitizer = "bench-x", "fuzz_x", "delta", "address"

    def _seed_trial(trial_num: int, found_cpv: str | None) -> None:
        trial_povs = (
            finding_root
            / bench
            / harness
            / mode
            / sanitizer
            / f"trial-{trial_num}"
            / "povs"
        )
        trial_povs.mkdir(parents=True, exist_ok=True)
        if found_cpv is None:
            payload = {"povs": {}, "cpv_to_first_pov": {}}
        else:
            blob = trial_povs / "cpvs" / found_cpv / "blobs" / "abc.blob"
            blob.parent.mkdir(parents=True)
            blob.write_bytes(b"\x00")
            payload = {
                "povs": {
                    "abc": {
                        "hash": "abc",
                        "first_seen_ts": 1.0,
                        "file_mtime": None,
                        "file_size": 1,
                        "status": PovVerificationStatus.CPV.value,
                        "cpv_matched": [found_cpv],
                        "crash_log_path": None,
                        "crash_signature": "sig",
                        "verification_duration": 0.0,
                    }
                },
                "cpv_to_first_pov": {
                    found_cpv: {
                        "pov_hash": "abc",
                        "discovery_ts": 1.0,
                        "relative_time": 1.0,
                    }
                },
            }
        (trial_povs / "pov_store.json").write_text(json.dumps(payload))

    _seed_trial(1, "cpv_0")
    _seed_trial(2, None)
    _seed_trial(3, "cpv_0")

    raw = _minimal_config(
        tmp_path,
        runtime={
            "trials": 3,
            "inputs": {
                "pov": {"from_experiment": str(finding_root)},
            },
        },
    )
    config = ExperimentConfig(**raw)

    benchmark_path = tmp_path / "benchmark"
    benchmark_path.mkdir()
    benchmark_harness = BenchmarkHarness(
        name=bench,
        path=benchmark_path,
        harness=HarnessFile(name=harness, path="/fake/harness.cc"),
        target_cpvs=None,
    )

    fake_meta_harness = MagicMock()
    fake_vuln = MagicMock()
    fake_vuln.id = "cpv_0"
    fake_vuln.sanitizer = "address"
    fake_meta_harness.vulns = [fake_vuln]

    fake_adapter = MagicMock()
    fake_adapter.get_harness.return_value = fake_meta_harness

    with (
        patch(
            "crsbench.run_experiment.get_crs_type",
            return_value="bug-fixing",
        ),
        patch(
            "crsbench.run_experiment.get_available_modes_for_benchmark",
            return_value=["delta"],
        ),
        patch(
            "crsbench.run_experiment.MetaYamlAdapter.from_meta_yaml",
            return_value=fake_adapter,
        ),
        patch(
            "crsbench.run_experiment._filter_matched_cpvs",
            side_effect=lambda _harness, _sani, allowed: (
                {"cpv_0"} if (allowed is None or "cpv_0" in allowed) else set()
            ),
        ),
    ):
        trials = generate_trial_matrix(
            benchmark_harnesses=[benchmark_harness],
            oss_crs_registry=["crs-claude-code"],
            config=config,
            registry_dir=tmp_path,
        )

    scheduled_trial_nums = sorted(t.trial_num for t in trials)
    # finding trial-1 and trial-3 found cpv_0 → fixing trial-1 and trial-3
    # are scheduled. trial-2 found nothing → fixing trial-2 is NOT scheduled.
    assert scheduled_trial_nums == [1, 3]


def test_trial_matrix_raises_when_single_path_missing(tmp_path: Path) -> None:
    """Single-path mode also fails fast on a missing directory."""
    from crsbench.run_experiment import generate_trial_matrix
    from crsbench.validation.schemas import ExperimentConfig

    missing_dir = tmp_path / "missing-source"  # never created

    raw = _minimal_config(
        tmp_path,
        runtime={
            "inputs": {
                "pov": {"from_experiment": str(missing_dir)},
            },
        },
    )
    config = ExperimentConfig(**raw)

    with pytest.raises(ValueError, match="do not exist"):
        generate_trial_matrix(
            benchmark_harnesses=[],
            oss_crs_registry=["crs-claude-code"],
            config=config,
            registry_dir=tmp_path,
        )


# ---------------------------------------------------------------------------
# Metadata rewrite — per-CRS map
# ---------------------------------------------------------------------------


def test_rewrite_per_crs_map_translates_entries() -> None:
    config = yaml.safe_load(
        textwrap.dedent(
            """
            runtime:
              inputs:
                pov:
                  from_experiment_by_crs:
                    crs-a: /op/local/a
                    crs-b: /op/local/b
            """
        )
    )
    rewritten = _rewrite_from_experiment_by_crs(
        config,
        {
            "crs-a": "/var/lib/crsbench/from-experiment/exp-1/by-crs/crs-a",
            "crs-b": "/var/lib/crsbench/from-experiment/exp-1/by-crs/crs-b",
        },
    )
    assert rewritten["runtime"]["inputs"]["pov"]["from_experiment_by_crs"] == {
        "crs-a": "/var/lib/crsbench/from-experiment/exp-1/by-crs/crs-a",
        "crs-b": "/var/lib/crsbench/from-experiment/exp-1/by-crs/crs-b",
    }


def test_rewrite_per_crs_drops_unmapped_crs() -> None:
    config = yaml.safe_load(
        textwrap.dedent(
            """
            runtime:
              inputs:
                pov:
                  from_experiment_by_crs:
                    crs-a: /op/local/a
                    crs-unmapped: /op/local/unmapped
            """
        )
    )
    rewritten = _rewrite_from_experiment_by_crs(
        config,
        {"crs-a": "/var/lib/crsbench/from-experiment/exp-1/by-crs/crs-a"},
    )
    assert rewritten["runtime"]["inputs"]["pov"]["from_experiment_by_crs"] == {
        "crs-a": "/var/lib/crsbench/from-experiment/exp-1/by-crs/crs-a",
    }


def test_rewrite_per_crs_noop_when_field_missing() -> None:
    config = {"runtime": {"inputs": {"pov": {"enabled": True}}}}
    rewritten = _rewrite_from_experiment_by_crs(config, {"crs-a": "/remote/a"})
    assert "from_experiment_by_crs" not in rewritten["runtime"]["inputs"]["pov"]


def test_render_transported_config_applies_per_crs_map(tmp_path: Path) -> None:
    config_path = tmp_path / "experiment.yaml"
    config_path.write_text(
        textwrap.dedent(
            """
            experiment: exp-per-crs
            runtime:
              inputs:
                pov:
                  from_experiment_by_crs:
                    crs-a: .run/cc-finding/crs-a
                    crs-b: .run/cc-finding/crs-b
            """
        )
    )

    encoded = _render_transported_config_bytes(
        config_path,
        from_experiment_remote_by_crs={
            "crs-a": "/var/lib/crsbench/from-experiment/exp-per-crs/by-crs/crs-a",
            "crs-b": "/var/lib/crsbench/from-experiment/exp-per-crs/by-crs/crs-b",
        },
    )

    parsed = yaml.safe_load(encoded)
    assert parsed["runtime"]["inputs"]["pov"]["from_experiment_by_crs"] == {
        "crs-a": "/var/lib/crsbench/from-experiment/exp-per-crs/by-crs/crs-a",
        "crs-b": "/var/lib/crsbench/from-experiment/exp-per-crs/by-crs/crs-b",
    }


def test_build_orchestrator_metadata_rewrites_per_crs_map(
    tmp_path: Path, monkeypatch
) -> None:
    from crsbench.cloud.gce import metadata as metadata_module

    config_path = tmp_path / "experiment.yaml"
    config_path.write_text(
        textwrap.dedent(
            """
            experiment: exp-8
            runtime:
              inputs:
                pov:
                  from_experiment_by_crs:
                    crs-a: .run/cc-finding/crs-a
            """
        )
    )

    class _FakeOrchestrator:
        metadata: dict[str, str] = {}

    monkeypatch.setattr(metadata_module, "_apply_access_metadata", lambda **_: None)
    monkeypatch.setattr(metadata_module, "_apply_install_metadata", lambda **_: None)
    monkeypatch.setattr(
        metadata_module, "_apply_startup_script_metadata", lambda **_: None
    )

    built = metadata_module.build_orchestrator_metadata(
        experiment_name="exp-8",
        orchestrator=_FakeOrchestrator(),
        experiment_config_path=str(config_path),
        redis_password="irrelevant",
        startup_script="#!/bin/sh\n",
        from_experiment_remote_by_crs={
            "crs-a": "/var/lib/crsbench/from-experiment/exp-8/by-crs/crs-a",
        },
    )

    decoded = base64.b64decode(built[CRSBENCH_EXPERIMENT_CONFIG_B64_KEY])
    parsed = yaml.safe_load(decoded)
    assert parsed["runtime"]["inputs"]["pov"]["from_experiment_by_crs"] == {
        "crs-a": "/var/lib/crsbench/from-experiment/exp-8/by-crs/crs-a",
    }
