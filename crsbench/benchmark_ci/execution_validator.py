"""Execution validation for benchmark builds, POVs, and tests."""

from pathlib import Path
from typing import Optional

from crsbench.utils.logger import get_logger
from crsbench.benchmark_ci.utils import JobContext, ExecJobType, TaskMode
from crsbench.benchmark_ci.file_validator import get_harnesses

logger = get_logger(__name__)
from crsbench.benchmark_ci.run_helper import (
    build_benchmark,
    reproduce_pov,
    run_test_sh,
    apply_patch,
    revert_patch,
)


def check_benchmark_execution(job: JobContext, output_dir: Optional[str] = None, enable_check_build: bool = False, force_rebuild: bool = False) -> None:
    """Execute a benchmark test job.

    Args:
        job: Job context describing what to test
        output_dir: Directory to save test artifacts
        enable_check_build: Enable check_build validation after building
        force_rebuild: Force rebuild Docker images even if they already exist

    Raises:
        Exception: If any test fails
    """
    # Create job-specific output directory
    job_output_dir = None
    if output_dir:
        job_output_dir = Path(output_dir) / job.benchmark / f"{job.job_type.value}-{job.engine}-{job.sanitizer}"
        job_output_dir.mkdir(parents=True, exist_ok=True)

    if job.job_type == ExecJobType.DELTA_BASE_CHECK:
        _delta_base_check(job, job_output_dir, enable_check_build)
    elif job.job_type == ExecJobType.DELTA_REF_CHECK:
        _delta_ref_check(job, job_output_dir, enable_check_build)
    elif job.job_type == ExecJobType.FULL_BASE_CHECK:
        _full_base_check(job, job_output_dir, enable_check_build)
    elif job.job_type == ExecJobType.PATCH_CHECK:
        _patch_check(job, job_output_dir, enable_check_build)
    elif job.job_type == ExecJobType.TEST_SH_CHECK:
        _test_sh_check(job, job_output_dir, enable_check_build, force_rebuild)
    else:
        raise Exception(f"[Error] Unknown job type: {job.job_type}")


def _delta_base_check(job: JobContext, output_dir: Optional[Path] = None, enable_check_build: bool = False) -> None:
    """Check delta mode base commit: build and verify POVs do NOT crash.

    In delta mode, base_commit is the clean version before bug-inducing diff.
    We only verify POVs do not crash; test.sh is only checked at ref_commit.

    Test steps:
    1. Build at base commit (clean version)
    2. Verify all POVs do NOT trigger crashes (no vulnerability yet)

    Args:
        job: Job context
        output_dir: Directory to save artifacts
        enable_check_build: Enable check_build validation
    """
    logger.info(f"=== DELTA BASE CHECK ===")
    logger.info(f"Step 1/2: Building at base commit (clean version)")

    # Build at base commit (clean version)
    base_commit = job.task.base_commit
    logger.info(f"Using base_commit: {base_commit[:8]}")
    build_benchmark(job.benchmark, job.engine, job.sanitizer, clean=True, enable_check_build=enable_check_build, commit=base_commit)

    # Get all harnesses
    harnesses = get_harnesses(job.benchmark)

    logger.info(f"Step 2/2: Verifying POVs do NOT crash (clean version)")
    # Test all POVs - they should NOT crash at base commit (clean)
    pov_count = 0
    for harness in harnesses:
        for vuln in harness.vulnerabilities:
            # Skip if not in context (for delta mode filtering)
            if job.vulns_in_context and vuln.id not in job.vulns_in_context:
                continue

            for pov in vuln.povs:
                # Only test if sanitizer matches
                if pov.sanitizer == job.sanitizer and job.engine == "libfuzzer":
                    pov_count += 1
                    logger.info(f"  Testing POV {pov.id} (should NOT crash)")
                    reproduce_pov(
                        job.benchmark,
                        harness.name,
                        pov.blob_path,
                        pov.error_token,
                        expect_crash=False,  # Should NOT crash at base commit (clean)
                        output_dir=output_dir
                    )

    logger.info(f"✓ Verified {pov_count} POVs do NOT crash (clean version)")
    logger.info(f"✓ Base commit check complete (test.sh will be checked at ref_commit)")


def _delta_ref_check(job: JobContext, output_dir: Optional[Path] = None, enable_check_build: bool = False) -> None:
    """Check delta mode ref commit: build and verify POVs crash.

    In delta mode, ref_commit is the vulnerable version after bug-inducing diff.

    Test steps:
    1. Build at ref commit (vulnerable version)
    2. Verify all POVs trigger crashes (vulnerability present)
    3. Verify test.sh passes (unit tests should pass despite vulnerability)

    Args:
        job: Job context
        output_dir: Directory to save artifacts
        enable_check_build: Enable check_build validation
    """
    logger.info(f"=== DELTA REF CHECK ===")

    # Get ref_commit from task
    ref_commit = job.task.ref_commit
    if not ref_commit:
        raise Exception(f"[Error] No ref_commit found for delta mode task")

    logger.info(f"Step 1/3: Building at ref commit (vulnerable version)")
    logger.info(f"Using ref_commit: {ref_commit[:8]}")

    # Build at ref commit (vulnerable version)
    build_benchmark(job.benchmark, job.engine, job.sanitizer, clean=True, enable_check_build=enable_check_build, commit=ref_commit)

    # Get all harnesses
    harnesses = get_harnesses(job.benchmark)

    logger.info(f"Step 2/3: Verifying POVs trigger crashes (vulnerable)")
    # Test all POVs - they should crash at ref commit (vulnerable)
    pov_count = 0
    for harness in harnesses:
        for vuln in harness.vulnerabilities:
            # Skip if not in context
            if job.vulns_in_context and vuln.id not in job.vulns_in_context:
                continue

            for pov in vuln.povs:
                # Only test if sanitizer matches
                if pov.sanitizer == job.sanitizer and job.engine == "libfuzzer":
                    pov_count += 1
                    logger.info(f"  Testing POV {pov.id} (should crash)")
                    reproduce_pov(
                        job.benchmark,
                        harness.name,
                        pov.blob_path,
                        pov.error_token,
                        expect_crash=True,  # Should crash at ref commit (vulnerable)
                        output_dir=output_dir
                    )

    logger.info(f"✓ Verified {pov_count} POVs trigger crashes (vulnerable version)")

    # Test test.sh at ref commit - should pass even with vulnerability
    logger.info(f"Step 3/3: Running test.sh (should pass)")
    run_test_sh(job.benchmark, expect_success=True, output_dir=output_dir, commit=ref_commit)
    logger.info(f"✓ test.sh passes at ref commit")


def _full_base_check(job: JobContext, output_dir: Optional[Path] = None, enable_check_build: bool = False) -> None:
    """Check full mode base commit: build and verify POVs crash.

    Test steps:
    1. Build at base commit (vulnerable version)
    2. Verify all POVs trigger crashes
    3. Verify test.sh passes (unit tests should pass despite vulnerability)

    Args:
        job: Job context
        output_dir: Directory to save artifacts
        enable_check_build: Enable check_build validation
    """
    logger.info(f"=== FULL BASE CHECK ===")
    logger.info(f"Step 1/3: Building at base commit (vulnerable version)")

    # Build at base commit (vulnerable version)
    base_commit = job.task.base_commit
    logger.info(f"Using base_commit: {base_commit[:8]}")
    build_benchmark(job.benchmark, job.engine, job.sanitizer, clean=True, enable_check_build=enable_check_build, commit=base_commit)

    # Get all harnesses
    harnesses = get_harnesses(job.benchmark)

    logger.info(f"Step 2/3: Verifying POVs trigger crashes")
    # Test all POVs - they should crash
    pov_count = 0
    for harness in harnesses:
        for vuln in harness.vulnerabilities:
            for pov in vuln.povs:
                # Only test if sanitizer matches
                if pov.sanitizer == job.sanitizer and job.engine == "libfuzzer":
                    pov_count += 1
                    logger.info(f"  Testing POV {pov.id} (should crash)")
                    reproduce_pov(
                        job.benchmark,
                        harness.name,
                        pov.blob_path,
                        pov.error_token,
                        expect_crash=True  # Should crash
                    )

    logger.info(f"✓ Verified {pov_count} POVs trigger crashes")

    # Test test.sh
    logger.info(f"Step 3/3: Running test.sh (should pass)")
    run_test_sh(job.benchmark, expect_success=True, output_dir=output_dir, commit=base_commit)
    logger.info(f"✓ test.sh passes at base commit")


def _patch_check(job: JobContext, output_dir: Optional[Path] = None, enable_check_build: bool = False) -> None:
    """Check that patch fixes the vulnerability.

    For FULL mode: Uses base_commit (vulnerable)
    For DELTA mode: Uses ref_commit (vulnerable)

    Tests:
    1. Build at vulnerable commit
    2. Apply patch
    3. Rebuild with patch
    4. After patch: POV should NOT crash
    5. test.sh should pass
    6. Revert patch

    Args:
        job: Job context with specific vulnerability and POV
        output_dir: Directory to save artifacts
        enable_check_build: Enable check_build validation
    """
    if not job.vulnerability or not job.pov:
        raise Exception("[Error] PATCH_CHECK requires vulnerability and POV")

    if not job.vulnerability.patch_path:
        logger.warning(f"No patch file for {job.vulnerability.id}, skipping")
        return

    logger.info(f"=== PATCH CHECK ===")
    logger.info(f"Verifying patch fixes {job.vulnerability.id}")

    # Determine which commit to use based on mode
    from crsbench.benchmark_ci.utils import TaskMode
    if job.task.mode == TaskMode.DELTA:
        # Delta mode: ref_commit is vulnerable
        target_commit = job.task.ref_commit
        if not target_commit:
            raise Exception(f"[Error] No ref_commit found for delta mode")
        logger.info(f"Using ref_commit (vulnerable): {target_commit[:8]}")
    else:
        # Full mode: base_commit is vulnerable
        target_commit = job.task.base_commit
        logger.info(f"Using base_commit (vulnerable): {target_commit[:8]}")

    # Step 1: Build at vulnerable commit
    logger.info(f"Step 1/5: Building at vulnerable commit")
    build_benchmark(job.benchmark, job.engine, job.sanitizer, clean=True, enable_check_build=enable_check_build, commit=target_commit)

    # Step 2: Apply patch
    logger.info(f"Step 2/5: Applying patch {job.vulnerability.patch_path}")
    apply_patch(job.benchmark, job.vulnerability.patch_path)

    # Step 3: Rebuild with patch
    logger.info(f"Step 3/5: Rebuilding with patch applied")
    build_benchmark(job.benchmark, job.engine, job.sanitizer, clean=False, enable_check_build=enable_check_build, commit=target_commit)

    # Step 4: Verify POV does NOT crash after patch
    logger.info(f"Step 4/5: Verifying POV does NOT crash after patch")
    reproduce_pov(
        job.benchmark,
        job.harness.name,
        job.pov.blob_path,
        job.pov.error_token,
        expect_crash=False  # Should NOT crash after patch
    )
    logger.info(f"✓ Patch successfully fixes the vulnerability")

    # Step 5: Verify test.sh passes with patch
    logger.info(f"Step 5/5: Running test.sh with patch applied")
    run_test_sh(job.benchmark, expect_success=True, output_dir=output_dir, commit=target_commit)
    logger.info(f"✓ test.sh passes with patch applied")

    # Cleanup: Revert patch
    logger.info(f"Reverting patch for cleanup...")
    revert_patch(job.benchmark, job.vulnerability.patch_path)

    logger.info(f"✓ Patch verification complete for {job.vulnerability.id}")


def _test_sh_check(job: JobContext, output_dir: Optional[Path] = None, enable_check_build: bool = False, force_rebuild: bool = False) -> None:
    """Check that test.sh passes.

    This tests that the benchmark's unit tests work correctly.
    For delta mode: uses ref_commit (vulnerable but test.sh should pass)
    For full mode: uses base_commit (vulnerable)

    Args:
        job: Job context
        output_dir: Directory to save artifacts
        enable_check_build: Enable check_build validation
        force_rebuild: Force rebuild Docker images even if they already exist
    """
    from crsbench.utils.run_helper import docker_image_exists

    logger.info(f"TEST.SH CHECK: Running test.sh for {job.benchmark}")

    # Determine which commit to use based on mode
    from crsbench.benchmark_ci.utils import TaskMode
    if job.task:
        if job.task.mode == TaskMode.DELTA:
            # Delta mode: use ref_commit (vulnerable version where test.sh should pass)
            target_commit = job.task.ref_commit
            if not target_commit:
                raise Exception(f"[Error] No ref_commit found for delta mode")
            logger.info(f"Using ref_commit (vulnerable): {target_commit[:8]}")
        else:
            # Full mode: use base_commit (vulnerable version)
            target_commit = job.task.base_commit
            logger.info(f"Using base_commit (vulnerable): {target_commit[:8]}")
    else:
        target_commit = None
        logger.info(f"No task provided, using base_commit from meta.yaml")

    # Check if Docker image exists (skip build if it does, unless force_rebuild)
    image_tag = f"aixcc-afc/{job.benchmark}"
    if not force_rebuild and docker_image_exists(image_tag):
        logger.info(f"Docker image '{image_tag}' already exists, skipping build")
    else:
        if force_rebuild:
            logger.info(f"Force rebuild requested, building benchmark...")
        else:
            logger.info(f"Docker image '{image_tag}' not found, building benchmark...")
        build_benchmark(job.benchmark, job.engine, job.sanitizer, clean=True, enable_check_build=enable_check_build, commit=target_commit)

    # Run test.sh
    run_test_sh(job.benchmark, expect_success=True, output_dir=output_dir, commit=target_commit)

    logger.info(f"✓ test.sh check complete")
