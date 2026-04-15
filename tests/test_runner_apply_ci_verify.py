"""Unit tests for BenchmarkRunner._apply_ci_verify_result."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from crsbench.evaluation.results import HarnessResult
from crsbench.evaluation.runner import BenchmarkRunner

if TYPE_CHECKING:
    from pathlib import Path


def _make_runner() -> BenchmarkRunner:
    adapter = MagicMock()
    adapter.mode = "bug-fixing"
    return BenchmarkRunner(adapter=adapter, snapshot_period=0)


def _write_verify_result(trial_dir: Path, crs_name: str, payload: dict) -> None:
    log_dir = trial_dir / "output" / "logs" / "crs" / crs_name / "log_dir"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "verify_patch_timing.json").write_text(json.dumps(payload))


def test_apply_ci_verify_result_populates_pass_status(tmp_path: Path) -> None:
    runner = _make_runner()
    harness_result = HarnessResult(name="fuzz_as", path="/p")
    _write_verify_result(
        tmp_path,
        "builder-sidecar-lite",
        {
            "status": "pass",
            "reason": None,
            "failed_step": None,
            "steps": {},
            "rebuild": 145.4,
            "test": 12.3,
        },
    )

    runner._apply_ci_verify_result(harness_result, tmp_path)

    assert harness_result.ci_verify_status == "pass"
    assert harness_result.ci_verify_failed_step is None


def test_apply_ci_verify_result_populates_fail_status(tmp_path: Path) -> None:
    runner = _make_runner()
    harness_result = HarnessResult(name="fuzz_as", path="/p")
    _write_verify_result(
        tmp_path,
        "builder-sidecar-lite",
        {
            "status": "fail",
            "reason": "Some POVs still crash after patch (1/1)",
            "failed_step": "run_povs_patched",
            "steps": {"run_povs_patched": {"status": "fail"}},
            "rebuild": 145.4,
            "test": 0.0,
        },
    )

    runner._apply_ci_verify_result(harness_result, tmp_path)

    assert harness_result.ci_verify_status == "fail"
    assert harness_result.ci_verify_reason == "Some POVs still crash after patch (1/1)"
    assert harness_result.ci_verify_failed_step == "run_povs_patched"


def test_apply_ci_verify_result_no_file_leaves_harness_unchanged(
    tmp_path: Path,
) -> None:
    runner = _make_runner()
    harness_result = HarnessResult(name="fuzz_as", path="/p")

    runner._apply_ci_verify_result(harness_result, tmp_path)

    assert harness_result.ci_verify_status is None
    assert harness_result.ci_verify_reason is None
    assert harness_result.ci_verify_failed_step is None


def test_apply_ci_verify_result_legacy_file_without_status_is_ignored(
    tmp_path: Path,
) -> None:
    """Legacy verify_patch_timing.json files with only rebuild/test keys
    should leave ci_verify_status unset so the lenient legacy path still
    applies for CRSes that predate the structured schema."""
    runner = _make_runner()
    harness_result = HarnessResult(name="fuzz_as", path="/p")
    _write_verify_result(
        tmp_path,
        "builder-sidecar-lite",
        {"rebuild": 70.4, "test": 481.0},
    )

    runner._apply_ci_verify_result(harness_result, tmp_path)

    assert harness_result.ci_verify_status is None


def test_apply_ci_verify_result_malformed_file_is_tolerated(tmp_path: Path) -> None:
    runner = _make_runner()
    harness_result = HarnessResult(name="fuzz_as", path="/p")
    log_dir = tmp_path / "output" / "logs" / "crs" / "builder-sidecar-lite" / "log_dir"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "verify_patch_timing.json").write_text("{ not valid json")

    # Must not raise.
    runner._apply_ci_verify_result(harness_result, tmp_path)

    assert harness_result.ci_verify_status is None
