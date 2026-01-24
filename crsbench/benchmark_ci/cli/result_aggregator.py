"""Result aggregation for flat DAG job outputs.

Merges per-CPV/per-patch ExecutorResults into CheckResult objects
compatible with the existing ValidationSummary output format.
"""

from crsbench.benchmark_ci.models import CheckResult, CheckStatus
from crsbench.executor.types import ExecutorResult, JobStatus
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


def _get_build_fallback(
    dag_results: dict[str, ExecutorResult],
    benchmark_name: str,
) -> bool:
    """Extract fallback_used from the build-variants job details."""
    build_job_id = f"build-variants:{benchmark_name}"
    build_result = dag_results.get(build_job_id)
    if build_result and build_result.job_result:
        return build_result.job_result.details.get("fallback_used", False)
    return False


def aggregate_pov_results(
    dag_results: dict[str, ExecutorResult],
    benchmark_name: str,
    cpv_ids: list[str],
) -> CheckResult:
    """Merge per-CPV POV results into a single CheckResult.

    PASS if all CPVs have their vulnerabilities detected.
    FAIL if any CPV is missing detection.
    ERROR if any job errored.
    """
    if not cpv_ids:
        return CheckResult.skip("No CPVs to verify")

    verify_time = 0.0
    failures: list[str] = []
    errors: list[str] = []

    for cpv_id in cpv_ids:
        job_id = f"verify-cpv-pov:{benchmark_name}:{cpv_id}"
        result = dag_results.get(job_id)

        if result is None:
            errors.append(f"{cpv_id}: job not found")
            continue

        verify_time += result.elapsed_seconds

        if result.status == JobStatus.DEP_FAILED:
            errors.append(f"{cpv_id}: dependency failed")
        elif result.status == JobStatus.FAILED:
            if result.job_result and result.job_result.error:
                failures.append(f"{cpv_id}: {result.job_result.error}")
            else:
                failures.append(f"{cpv_id}: verification failed")
        elif result.status == JobStatus.SUCCESS:
            if result.job_result and not result.job_result.success:
                failures.append(f"{cpv_id}: POV not detected")

    # POV uses only the shared build — don't attribute shared build time here
    fallback = _get_build_fallback(dag_results, benchmark_name)

    if errors:
        return CheckResult(
            status=CheckStatus.ERROR,
            time_seconds=verify_time,
            verify_time=verify_time,
            error="; ".join(errors),
            details={"failures": failures, "errors": errors},
            fallback_used=fallback,
        )

    if failures:
        return CheckResult(
            status=CheckStatus.FAIL,
            time_seconds=verify_time,
            verify_time=verify_time,
            error="; ".join(failures[:3]),
            details={"failures": failures},
            fallback_used=fallback,
        )

    return CheckResult(
        status=CheckStatus.PASS,
        time_seconds=verify_time,
        verify_time=verify_time,
        details={"cpv_count": len(cpv_ids)},
        fallback_used=fallback,
    )


def aggregate_patch_results(
    dag_results: dict[str, ExecutorResult],
    benchmark_name: str,
    patch_keys: list[tuple[str, str]],
    test_mode: str = "FULL",
) -> CheckResult:
    """Merge per-patch test results into a CheckResult.

    PASS if all patches are valid (POVs don't crash + tests pass).
    FAIL if any patch is invalid.
    """
    if not patch_keys:
        return CheckResult.skip("No patches to verify")

    failures: list[str] = []
    errors: list[str] = []
    build_time = 0.0
    verify_time = 0.0

    for cpv_id, patch_id in patch_keys:
        # Check build
        build_job_id = f"build-patch:{benchmark_name}:{cpv_id}:{patch_id}"
        build_result = dag_results.get(build_job_id)

        # Only attribute build time to FULL mode — RTS reuses the same build
        if build_result and test_mode == "FULL":
            build_time += build_result.elapsed_seconds

        if build_result and build_result.status != JobStatus.SUCCESS:
            error_msg = build_result.error or "build failed"
            errors.append(f"{cpv_id}/{patch_id}: {error_msg}")
            continue

        # Check test
        test_job_id = f"test-patch:{benchmark_name}:{cpv_id}:{patch_id}:{test_mode}"
        test_result = dag_results.get(test_job_id)

        if test_result is None:
            errors.append(f"{cpv_id}/{patch_id}: test job not found")
            continue

        verify_time += test_result.elapsed_seconds

        if test_result.status == JobStatus.DEP_FAILED:
            errors.append(f"{cpv_id}/{patch_id}: dependency failed")
        elif test_result.status == JobStatus.FAILED:
            error_msg = (
                test_result.job_result.error
                if test_result.job_result
                else "test failed"
            )
            failures.append(f"{cpv_id}/{patch_id}: {error_msg}")
        elif test_result.status == JobStatus.SUCCESS:
            if test_result.job_result and not test_result.job_result.success:
                error_msg = test_result.job_result.error or "test failed"
                failures.append(f"{cpv_id}/{patch_id}: {error_msg}")

    total_time = build_time + verify_time
    fallback = _get_build_fallback(dag_results, benchmark_name)

    if errors:
        return CheckResult(
            status=CheckStatus.ERROR,
            time_seconds=total_time,
            build_time=build_time,
            verify_time=verify_time,
            error="; ".join(errors),
            details={"failures": failures, "errors": errors},
            fallback_used=fallback,
        )

    if failures:
        return CheckResult(
            status=CheckStatus.FAIL,
            time_seconds=total_time,
            build_time=build_time,
            verify_time=verify_time,
            error="; ".join(failures[:3]),
            details={"failures": failures},
            fallback_used=fallback,
        )

    return CheckResult(
        status=CheckStatus.PASS,
        time_seconds=total_time,
        build_time=build_time,
        verify_time=verify_time,
        details={"patch_count": len(patch_keys)},
        fallback_used=fallback,
    )


def aggregate_coverage_result(
    dag_results: dict[str, ExecutorResult],
    benchmark_name: str,
) -> CheckResult:
    """Extract coverage result from DAG."""
    job_id = f"collect-coverage:{benchmark_name}"
    result = dag_results.get(job_id)

    if result is None:
        return CheckResult.make_error("Coverage job not found")

    if result.status == JobStatus.DEP_FAILED:
        return CheckResult.make_error("Coverage dependency failed")

    # Coverage uses only the shared build — don't attribute shared build time here
    verify_time = result.elapsed_seconds
    fallback = _get_build_fallback(dag_results, benchmark_name)

    if result.status == JobStatus.FAILED:
        error = result.error or "Coverage collection failed"
        return CheckResult(
            status=CheckStatus.FAIL,
            time_seconds=verify_time,
            verify_time=verify_time,
            error=error,
            fallback_used=fallback,
        )

    return CheckResult(
        status=CheckStatus.PASS,
        time_seconds=verify_time,
        verify_time=verify_time,
        fallback_used=fallback,
    )


def aggregate_build_result(
    dag_results: dict[str, ExecutorResult],
    benchmark_name: str,
) -> CheckResult:
    """Extract build result from DAG."""
    job_id = f"build-variants:{benchmark_name}"
    result = dag_results.get(job_id)

    if result is None:
        return CheckResult.make_error("Build job not found")

    if result.status == JobStatus.FAILED:
        error = result.error or "Build failed"
        return CheckResult(
            status=CheckStatus.FAIL,
            time_seconds=result.elapsed_seconds,
            error=error,
        )

    return CheckResult(
        status=CheckStatus.PASS,
        time_seconds=result.elapsed_seconds,
    )
