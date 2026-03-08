"""Targeted regression tests for benchmark CI result aggregation."""

from datetime import datetime, timedelta
from pathlib import Path

from crsbench.benchmark_ci.cli.result_aggregator import (
    aggregate_build_result,
    aggregate_patch_pov_results,
    aggregate_patch_unittest_results,
    aggregate_patch_var_results,
    aggregate_pov_var_results,
)
from crsbench.benchmark_ci.jobs.base import JobResult
from crsbench.benchmark_ci.models import (
    BenchmarkValidationResult,
    CheckResult,
    CheckStatus,
)
from crsbench.executor.types import ExecutorResult, JobStatus


def _job_result(
    job_id: str,
    *,
    success: bool,
    error: str | None = None,
    details: dict | None = None,
) -> JobResult:
    started_at = datetime(2026, 1, 1, 0, 0, 0)
    finished_at = started_at + timedelta(seconds=1)
    return JobResult(
        job_id=job_id,
        job_type="verify",
        success=success,
        started_at=started_at,
        finished_at=finished_at,
        elapsed_seconds=1.0,
        error=error,
        details=details or {},
    )


def test_failed_patch_pov_without_job_result_counts_fail() -> None:
    dag_results = {
        "test-patch-pov/bench/cpv_0/patch_0": ExecutorResult(
            job_id="test-patch-pov/bench/cpv_0/patch_0",
            status=JobStatus.FAILED,
            elapsed_seconds=2.0,
        )
    }

    result = aggregate_patch_pov_results(dag_results, "bench", [("cpv_0", "patch_0")])

    assert result.status == CheckStatus.FAIL
    assert "POV test failed" in result.error


def test_failed_patch_var_without_job_result_counts_fail() -> None:
    dag_results = {
        "test-patch-var/bench/cpv_0/patch_0": ExecutorResult(
            job_id="test-patch-var/bench/cpv_0/patch_0",
            status=JobStatus.FAILED,
            elapsed_seconds=2.0,
        )
    }

    result = aggregate_patch_var_results(dag_results, "bench", [("cpv_0", "patch_0")])

    assert result.status == CheckStatus.FAIL
    assert "variant test failed" in result.error


def test_failed_pov_var_without_job_result_counts_fail() -> None:
    dag_results = {
        "verify-cpv-var/bench/cpv_0": ExecutorResult(
            job_id="verify-cpv-var/bench/cpv_0",
            status=JobStatus.FAILED,
            elapsed_seconds=2.0,
        )
    }

    result = aggregate_pov_var_results(dag_results, "bench", ["cpv_0"])

    assert result.status == CheckStatus.FAIL
    assert "variant verification failed" in result.error


def test_infra_error_code_is_error_class_for_split_aggregator() -> None:
    job_id = "test-patch-unittest/bench/cpv_0/patch_0/FULL"
    dag_results = {
        job_id: ExecutorResult(
            job_id=job_id,
            status=JobStatus.FAILED,
            elapsed_seconds=1.0,
            error="infra_missing_build_context: missing build context",
            job_result=_job_result(
                job_id,
                success=False,
                error="infra_missing_build_context: missing build context",
                details={"error_code": "infra_missing_build_context"},
            ),
        )
    }

    result = aggregate_patch_unittest_results(
        dag_results,
        "bench",
        [("cpv_0", "patch_0")],
    )

    assert result.status == CheckStatus.ERROR
    assert "infra_missing_build_context" in result.error


def test_infra_error_code_is_error_class_for_build_aggregate() -> None:
    job_id = "build-variants/bench"
    dag_results = {
        job_id: ExecutorResult(
            job_id=job_id,
            status=JobStatus.FAILED,
            elapsed_seconds=3.0,
            error="infra_missing_build_context: build metadata missing",
            job_result=_job_result(
                job_id,
                success=False,
                error="infra_missing_build_context: build metadata missing",
                details={"error_code": "infra_missing_build_context"},
            ),
        )
    }

    result = aggregate_build_result(dag_results, "bench")

    assert result.status == CheckStatus.ERROR


def test_partial_split_falls_back_to_combined_check() -> None:
    result = BenchmarkValidationResult(
        benchmark="bench",
        benchmark_path=Path("/tmp/bench"),
        pov_check=CheckResult(status=CheckStatus.FAIL, time_seconds=10.0),
        pov_build_check=CheckResult(status=CheckStatus.PASS, time_seconds=1.0),
    )

    assert result.total_status == CheckStatus.FAIL
    assert result.total_time == 10.0


def test_total_status_prefers_error_over_fail() -> None:
    result = BenchmarkValidationResult(
        benchmark="bench",
        benchmark_path=Path("/tmp/bench"),
        format_check=CheckResult(status=CheckStatus.FAIL, time_seconds=1.0),
        patch_check=CheckResult(status=CheckStatus.ERROR, time_seconds=1.0),
    )

    assert result.total_status == CheckStatus.ERROR
