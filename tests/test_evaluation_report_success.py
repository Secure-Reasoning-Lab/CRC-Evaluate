"""Unit tests for ``EvaluationReport.success`` semantics.

These cover the two code paths in
``crsbench.evaluation.results.EvaluationReport.success``:

1. Legacy lenient behavior: success iff any harness produced a result.
2. CI-verify-aware behavior: when any ``HarnessResult.ci_verify_status``
   is set, success iff *every* reporting harness is ``"pass"``.
"""

from __future__ import annotations

from datetime import datetime

from crsbench.evaluation.results import EvaluationReport, HarnessResult


def _base_report(harness_results: list[HarnessResult]) -> EvaluationReport:
    return EvaluationReport(
        benchmark_path="/tmp/bench",
        evaluation_mode="delta",
        start_time=datetime(2026, 1, 1),
        end_time=datetime(2026, 1, 1),
        total_execution_time=0.0,
        harness_results=harness_results,
        total_povs=0,
        povs_found=0,
        povs_missed=0,
        povs_error=0,
    )


def test_success_false_when_no_harness_results():
    report = _base_report(harness_results=[])
    assert report.success is False


def test_success_true_lenient_when_no_ci_verify_status():
    # Legacy behavior: a harness with run_successful=False (non-zero CRS
    # exit code, e.g. fuzzer killed by timeout) is still considered a
    # successful evaluation because artifacts may have been produced.
    hr = HarnessResult(
        name="harness_a",
        path="path",
        run_successful=False,
    )
    report = _base_report(harness_results=[hr])
    assert report.success is True


def test_success_true_when_ci_verify_pass():
    hr = HarnessResult(
        name="harness_a",
        path="path",
        run_successful=True,
        ci_verify_status="pass",
    )
    report = _base_report(harness_results=[hr])
    assert report.success is True


def test_success_false_when_ci_verify_fail():
    hr = HarnessResult(
        name="harness_a",
        path="path",
        run_successful=True,  # even if CRS exit code was 0
        ci_verify_status="fail",
        ci_verify_reason="Some POVs still crash after patch",
        ci_verify_failed_step="run_povs_patched",
    )
    report = _base_report(harness_results=[hr])
    assert report.success is False


def test_success_false_when_ci_verify_error():
    hr = HarnessResult(
        name="harness_a",
        path="path",
        ci_verify_status="error",
        ci_verify_reason="verify_patch exited unexpectedly",
    )
    report = _base_report(harness_results=[hr])
    assert report.success is False


def test_success_mixed_reporting_and_legacy_harnesses_requires_all_pass():
    # If even one harness reports a structured status, that signal takes
    # precedence — the other harness's absence of status should not let a
    # failed verification count as success.
    hr_pass = HarnessResult(
        name="harness_a",
        path="path",
        ci_verify_status="pass",
    )
    hr_legacy = HarnessResult(
        name="harness_b",
        path="path",
        ci_verify_status=None,  # legacy / non-reporting
    )
    report = _base_report(harness_results=[hr_pass, hr_legacy])
    assert report.success is True

    hr_fail = HarnessResult(
        name="harness_c",
        path="path",
        ci_verify_status="fail",
    )
    report_with_fail = _base_report(harness_results=[hr_pass, hr_fail, hr_legacy])
    assert report_with_fail.success is False
