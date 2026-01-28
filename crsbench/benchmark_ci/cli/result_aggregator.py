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


def _get_shared_build_time(
    dag_results: dict[str, ExecutorResult],
    benchmark_name: str,
) -> float:
    """Get elapsed time from the shared build-variants job."""
    build_job_id = f"build-variants:{benchmark_name}"
    build_result = dag_results.get(build_job_id)
    if build_result:
        return build_result.elapsed_seconds
    return 0.0


def aggregate_pov_build_results(
    dag_results: dict[str, ExecutorResult],
    benchmark_name: str,
) -> CheckResult:
    """Aggregate build results for POV variants (BuildSingleVariantJob).

    PASS if all variant builds succeeded.
    FAIL if any variant build failed.
    """
    build_time = 0.0
    failures: list[str] = []
    errors: list[str] = []
    fallback_used = False

    # Find all build-single jobs for this benchmark
    for job_id, result in dag_results.items():
        if not job_id.startswith(f"build-single:{benchmark_name}:"):
            continue

        build_time += result.elapsed_seconds

        if result.status == JobStatus.FAILED:
            error_msg = result.error or "build failed"
            failures.append(f"{job_id}: {error_msg}")
        elif result.status == JobStatus.SUCCESS:
            if result.job_result:
                if result.job_result.details.get("fallback_used", False):
                    fallback_used = True

    if not build_time:
        return CheckResult.skip("No variant builds found")

    if errors:
        return CheckResult(
            status=CheckStatus.ERROR,
            time_seconds=build_time,
            build_time=build_time,
            error="; ".join(errors),
            details={"failures": failures, "errors": errors},
            fallback_used=fallback_used,
        )

    if failures:
        return CheckResult(
            status=CheckStatus.FAIL,
            time_seconds=build_time,
            build_time=build_time,
            error="; ".join(failures[:3]),
            details={"failures": failures},
            fallback_used=fallback_used,
        )

    return CheckResult(
        status=CheckStatus.PASS,
        time_seconds=build_time,
        build_time=build_time,
        fallback_used=fallback_used,
    )


def aggregate_pov_pov_results(
    dag_results: dict[str, ExecutorResult],
    benchmark_name: str,
    cpv_ids: list[str],
) -> CheckResult:
    """Aggregate POV verification results for pov_0 (ground truth) only.

    PASS if all CPVs have their pov_0 vulnerability detected.
    FAIL if any CPV pov_0 is missing detection.
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
            # Check pov_0_passed specifically (ground truth)
            if result.job_result:
                pov_0_passed = result.job_result.details.get("pov_0_passed", True)
                if not pov_0_passed:
                    failures.append(f"{cpv_id}: pov_0 not detected")

    if errors:
        return CheckResult(
            status=CheckStatus.ERROR,
            time_seconds=verify_time,
            verify_time=verify_time,
            error="; ".join(errors),
            details={"failures": failures, "errors": errors},
        )

    if failures:
        return CheckResult(
            status=CheckStatus.FAIL,
            time_seconds=verify_time,
            verify_time=verify_time,
            error="; ".join(failures[:3]),
            details={"failures": failures},
        )

    return CheckResult(
        status=CheckStatus.PASS,
        time_seconds=verify_time,
        verify_time=verify_time,
        details={"cpv_count": len(cpv_ids)},
    )


def aggregate_pov_var_results(
    dag_results: dict[str, ExecutorResult],
    benchmark_name: str,
    cpv_ids: list[str],
) -> CheckResult:
    """Aggregate POV verification results for variants (pov_1+) only.

    Returns a result with var_passed/var_total in details.
    SKIP if no variants exist.
    """
    if not cpv_ids:
        return CheckResult.skip("No CPVs to verify")

    var_passed = 0
    var_total = 0
    failures: list[str] = []

    for cpv_id in cpv_ids:
        job_id = f"verify-cpv-pov:{benchmark_name}:{cpv_id}"
        result = dag_results.get(job_id)

        if result is None:
            continue  # Already reported in pov_pov_results

        if result.status != JobStatus.SUCCESS:
            continue  # Already reported in pov_pov_results

        # Extract variant counts from job details
        if result.job_result:
            cpv_var_passed = result.job_result.details.get("var_passed", 0)
            cpv_var_total = result.job_result.details.get("var_total", 0)
            var_passed += cpv_var_passed
            var_total += cpv_var_total
            if cpv_var_total > 0 and cpv_var_passed < cpv_var_total:
                failures.append(f"{cpv_id}: {cpv_var_passed}/{cpv_var_total} variants")

    # No variants found
    if var_total == 0:
        return CheckResult.skip("No variants")

    if failures:
        return CheckResult(
            status=CheckStatus.FAIL,
            time_seconds=0.0,
            error="; ".join(failures[:3]),
            details={
                "var_passed": var_passed,
                "var_total": var_total,
                "failures": failures,
            },
        )

    return CheckResult(
        status=CheckStatus.PASS,
        time_seconds=0.0,
        details={"var_passed": var_passed, "var_total": var_total},
    )


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

    # Include shared build time for display
    build_time = _get_shared_build_time(dag_results, benchmark_name)
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

    Supports both old job structure (test-patch:FULL/RTS) and new structure
    (test-patch-pov + test-patch-unittest).
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

        # Try new job structure first (separate POV and unittest jobs)
        pov_job_id = f"test-patch-pov:{benchmark_name}:{cpv_id}:{patch_id}"
        pov_result = dag_results.get(pov_job_id)

        unittest_job_id = (
            f"test-patch-unittest:{benchmark_name}:{cpv_id}:{patch_id}:{test_mode}"
        )
        unittest_result = dag_results.get(unittest_job_id)

        # New structure: check POV and unit test separately
        if pov_result is not None:
            verify_time += pov_result.elapsed_seconds

            if pov_result.status == JobStatus.DEP_FAILED:
                errors.append(f"{cpv_id}/{patch_id}: POV dependency failed")
                continue
            if pov_result.status == JobStatus.FAILED:
                error_msg = (
                    pov_result.job_result.error
                    if pov_result.job_result
                    else "POV test failed"
                )
                failures.append(f"{cpv_id}/{patch_id}: {error_msg}")
                continue
            if pov_result.status == JobStatus.SUCCESS:
                if pov_result.job_result and not pov_result.job_result.success:
                    error_msg = pov_result.job_result.error or "POV test failed"
                    failures.append(f"{cpv_id}/{patch_id}: {error_msg}")
                    continue

            # POV passed, check unit test
            if unittest_result is not None:
                verify_time += unittest_result.elapsed_seconds

                if unittest_result.status == JobStatus.DEP_FAILED:
                    # Expected if POV failed - don't treat as error
                    pass
                elif unittest_result.status == JobStatus.FAILED:
                    error_msg = (
                        unittest_result.job_result.error
                        if unittest_result.job_result
                        else "unit test failed"
                    )
                    failures.append(f"{cpv_id}/{patch_id}: {error_msg}")
                elif unittest_result.status == JobStatus.SUCCESS:
                    if (
                        unittest_result.job_result
                        and not unittest_result.job_result.success
                    ):
                        error_msg = (
                            unittest_result.job_result.error or "unit test failed"
                        )
                        failures.append(f"{cpv_id}/{patch_id}: {error_msg}")
            continue

        # Fallback to old job structure (combined test-patch job)
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


def aggregate_patch_build_results(
    dag_results: dict[str, ExecutorResult],
    benchmark_name: str,
    patch_keys: list[tuple[str, str]],
) -> CheckResult:
    """Aggregate build-patch job results only (no verification).

    PASS if all patch builds succeeded.
    FAIL if any patch build failed.
    """
    if not patch_keys:
        return CheckResult.skip("No patches to build")

    failures: list[str] = []
    errors: list[str] = []
    build_time = 0.0

    for cpv_id, patch_id in patch_keys:
        build_job_id = f"build-patch:{benchmark_name}:{cpv_id}:{patch_id}"
        build_result = dag_results.get(build_job_id)

        if build_result is None:
            errors.append(f"{cpv_id}/{patch_id}: build job not found")
            continue

        build_time += build_result.elapsed_seconds

        if build_result.status == JobStatus.DEP_FAILED:
            errors.append(f"{cpv_id}/{patch_id}: dependency failed")
        elif build_result.status != JobStatus.SUCCESS:
            error_msg = build_result.error or "build failed"
            failures.append(f"{cpv_id}/{patch_id}: {error_msg}")

    if errors:
        return CheckResult(
            status=CheckStatus.ERROR,
            time_seconds=build_time,
            build_time=build_time,
            error="; ".join(errors),
            details={"failures": failures, "errors": errors},
        )

    if failures:
        return CheckResult(
            status=CheckStatus.FAIL,
            time_seconds=build_time,
            build_time=build_time,
            error="; ".join(failures[:3]),
            details={"failures": failures},
        )

    return CheckResult(
        status=CheckStatus.PASS,
        time_seconds=build_time,
        build_time=build_time,
        details={"patch_count": len(patch_keys)},
    )


def aggregate_patch_pov_results(
    dag_results: dict[str, ExecutorResult],
    benchmark_name: str,
    patch_keys: list[tuple[str, str]],
) -> CheckResult:
    """Aggregate POV test results for pov_0 (ground truth) from test-patch-pov jobs.

    PASS if all patches pass pov_0 tests (vulnerability fixed).
    FAIL if any patch fails pov_0 tests (vulnerability not fixed).
    """
    if not patch_keys:
        return CheckResult.skip("No patches to verify")

    failures: list[str] = []
    errors: list[str] = []
    verify_time = 0.0

    for cpv_id, patch_id in patch_keys:
        # Try new job ID format first (test-patch-pov)
        test_job_id = f"test-patch-pov:{benchmark_name}:{cpv_id}:{patch_id}"
        test_result = dag_results.get(test_job_id)

        # Fallback to old job ID format (test-patch:FULL) for compatibility
        if test_result is None:
            test_job_id = f"test-patch:{benchmark_name}:{cpv_id}:{patch_id}:FULL"
            test_result = dag_results.get(test_job_id)

        if test_result is None:
            errors.append(f"{cpv_id}/{patch_id}: test job not found")
            continue

        if test_result.status == JobStatus.DEP_FAILED:
            errors.append(f"{cpv_id}/{patch_id}: dependency failed")
            continue

        # Extract pov_0_passed from job details
        if test_result.job_result:
            pov_0_passed = test_result.job_result.details.get("pov_0_passed", True)
            # Use specific pov_test_time if available, otherwise elapsed
            pov_test_time = test_result.job_result.details.get("pov_test_time", 0.0)
            verify_time += (
                pov_test_time if pov_test_time > 0 else test_result.elapsed_seconds
            )
            if not pov_0_passed:
                failures.append(f"{cpv_id}/{patch_id}: pov_0 still triggers")
        else:
            verify_time += test_result.elapsed_seconds

    if errors:
        return CheckResult(
            status=CheckStatus.ERROR,
            time_seconds=verify_time,
            verify_time=verify_time,
            error="; ".join(errors),
            details={"failures": failures, "errors": errors},
        )

    if failures:
        return CheckResult(
            status=CheckStatus.FAIL,
            time_seconds=verify_time,
            verify_time=verify_time,
            error="; ".join(failures[:3]),
            details={"failures": failures},
        )

    return CheckResult(
        status=CheckStatus.PASS,
        time_seconds=verify_time,
        verify_time=verify_time,
        details={"patch_count": len(patch_keys)},
    )


def aggregate_patch_var_results(
    dag_results: dict[str, ExecutorResult],
    benchmark_name: str,
    patch_keys: list[tuple[str, str]],
) -> CheckResult:
    """Aggregate variant POV test results (pov_1+) from test-patch-pov jobs.

    Returns a result with var_passed/var_total in details.
    SKIP if no variants exist.
    """
    if not patch_keys:
        return CheckResult.skip("No patches to verify")

    verify_time = 0.0
    var_passed = 0
    var_total = 0
    failures: list[str] = []

    for cpv_id, patch_id in patch_keys:
        # Try new job ID format first (test-patch-pov)
        test_job_id = f"test-patch-pov:{benchmark_name}:{cpv_id}:{patch_id}"
        test_result = dag_results.get(test_job_id)

        # Fallback to old job ID format (test-patch:FULL) for compatibility
        if test_result is None:
            test_job_id = f"test-patch:{benchmark_name}:{cpv_id}:{patch_id}:FULL"
            test_result = dag_results.get(test_job_id)

        if test_result is None or test_result.status != JobStatus.SUCCESS:
            continue

        # Extract variant counts from job details
        if test_result.job_result:
            patch_var_passed = test_result.job_result.details.get("var_passed", 0)
            patch_var_total = test_result.job_result.details.get("var_total", 0)
            var_passed += patch_var_passed
            var_total += patch_var_total
            if patch_var_total > 0 and patch_var_passed < patch_var_total:
                failures.append(
                    f"{cpv_id}/{patch_id}: {patch_var_passed}/{patch_var_total} variants"
                )

    # No variants found
    if var_total == 0:
        return CheckResult.skip("No variants")

    if failures:
        return CheckResult(
            status=CheckStatus.FAIL,
            time_seconds=verify_time,
            verify_time=verify_time,
            error="; ".join(failures[:3]),
            details={
                "var_passed": var_passed,
                "var_total": var_total,
                "failures": failures,
            },
        )

    return CheckResult(
        status=CheckStatus.PASS,
        time_seconds=verify_time,
        verify_time=verify_time,
        details={"var_passed": var_passed, "var_total": var_total},
    )


def aggregate_patch_unittest_results(
    dag_results: dict[str, ExecutorResult],
    benchmark_name: str,
    patch_keys: list[tuple[str, str]],
    test_mode: str = "FULL",
) -> CheckResult:
    """Aggregate unit test results from test-patch-unittest jobs.

    PASS if all patches pass unit tests.
    FAIL if any patch fails unit tests.
    """
    if not patch_keys:
        return CheckResult.skip("No patches to verify")

    failures: list[str] = []
    errors: list[str] = []
    verify_time = 0.0
    has_results = False

    for cpv_id, patch_id in patch_keys:
        # Try new job ID format first (test-patch-unittest)
        test_job_id = (
            f"test-patch-unittest:{benchmark_name}:{cpv_id}:{patch_id}:{test_mode}"
        )
        test_result = dag_results.get(test_job_id)

        # Fallback to old job ID format (test-patch) for compatibility
        if test_result is None:
            test_job_id = f"test-patch:{benchmark_name}:{cpv_id}:{patch_id}:{test_mode}"
            test_result = dag_results.get(test_job_id)

        if test_result is None:
            errors.append(f"{cpv_id}/{patch_id}: test job not found")
            continue

        if test_result.status == JobStatus.DEP_FAILED:
            errors.append(f"{cpv_id}/{patch_id}: dependency failed")
            continue

        # Extract unit_tests_passed and unit_test_time from job details
        if test_result.job_result:
            unittest_value = test_result.job_result.details.get("unit_tests_passed")
            # Skip if unit tests weren't run (POV failed first)
            if unittest_value is None:
                continue
            has_results = True
            unittest_passed = unittest_value
            # Use specific unit_test_time if available, otherwise elapsed
            unit_test_time = test_result.job_result.details.get("unit_test_time", 0.0)
            verify_time += (
                unit_test_time if unit_test_time > 0 else test_result.elapsed_seconds
            )
            if not unittest_passed:
                failures.append(f"{cpv_id}/{patch_id}: unit tests failed")

    if errors:
        return CheckResult(
            status=CheckStatus.ERROR,
            time_seconds=verify_time,
            verify_time=verify_time,
            error="; ".join(errors),
            details={"failures": failures, "errors": errors},
        )

    if failures:
        return CheckResult(
            status=CheckStatus.FAIL,
            time_seconds=verify_time,
            verify_time=verify_time,
            error="; ".join(failures[:3]),
            details={"failures": failures},
        )

    # If no unit tests were run (all POV tests failed), return SKIP
    if not has_results:
        return CheckResult.skip("POV failed, unit tests skipped")

    return CheckResult(
        status=CheckStatus.PASS,
        time_seconds=verify_time,
        verify_time=verify_time,
        details={"patch_count": len(patch_keys)},
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

    # Include shared build time for display
    build_time = _get_shared_build_time(dag_results, benchmark_name)
    verify_time = result.elapsed_seconds
    total_time = build_time + verify_time
    fallback = _get_build_fallback(dag_results, benchmark_name)

    if result.status == JobStatus.FAILED:
        error = result.error or "Coverage collection failed"
        return CheckResult(
            status=CheckStatus.FAIL,
            time_seconds=total_time,
            build_time=build_time,
            verify_time=verify_time,
            error=error,
            fallback_used=fallback,
        )

    return CheckResult(
        status=CheckStatus.PASS,
        time_seconds=total_time,
        build_time=build_time,
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
