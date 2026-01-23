"""CI-level check jobs for DAG executor integration.

These jobs wrap BenchmarkValidator methods for parallel execution via
DAGExecutor. Each job stores its own validator reference and ignores
the JobContext (which is used by fine-grained jobs like BuildJob).

DAG structure for CI all:
    format_check (no deps, runs first as gate)
      -> pov_check (parallel)
      -> patch_rts_check (parallel, includes RTS)
      -> coverage_check (parallel)
"""

import shutil
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from crsbench.benchmark_ci.jobs.base import Job, JobContext, JobResult
from crsbench.benchmark_ci.models import CheckResult, CheckStatus
from crsbench.benchmark_ci.validator import BenchmarkValidator
from crsbench.evaluation.verification.models import UnitTestMode


@dataclass
class PovCheckJob(Job):
    """Run POV verification for a benchmark via DAG executor.

    Wraps validator.validate_povs() as a DAG node.
    """

    benchmark_path: Path
    validator: BenchmarkValidator
    use_inc_build: bool = True
    force_rebuild: bool = True
    _depends: list[str] = field(default_factory=list)

    @property
    def job_id(self) -> str:
        return f"pov-check:{self.benchmark_path.name}"

    @property
    def job_type(self) -> str:
        return "pov-check"

    @property
    def depends_on(self) -> list[str]:
        return self._depends

    def execute(self, context: JobContext) -> JobResult:  # noqa: ARG002
        """Execute POV check. Context unused — validator is stored internally."""
        started_at = datetime.now()
        try:
            result = self.validator.validate_povs(
                self.benchmark_path,
                force_rebuild=self.force_rebuild,
                use_inc_build=self.use_inc_build,
            )
            finished_at = datetime.now()
            return JobResult(
                job_id=self.job_id,
                job_type=self.job_type,
                success=result.status == CheckStatus.PASS,
                started_at=started_at,
                finished_at=finished_at,
                elapsed_seconds=result.time_seconds,
                error=result.error or None,
                details={"check_result": result},
            )
        except Exception as e:
            finished_at = datetime.now()
            return JobResult(
                job_id=self.job_id,
                job_type=self.job_type,
                success=False,
                started_at=started_at,
                finished_at=finished_at,
                elapsed_seconds=(finished_at - started_at).total_seconds(),
                error=str(e),
            )


@dataclass
class PatchRtsCheckJob(Job):
    """Run patch verification (FULL + RTS) for a benchmark via DAG executor.

    Builds patched variants once, then runs both FULL test.sh and RTS
    on the same build. RTS timing only reflects test execution.
    """

    benchmark_path: Path
    validator: BenchmarkValidator
    use_inc_build: bool = True
    force_rebuild: bool = True
    rts_mode: Optional[str] = None
    _depends: list[str] = field(default_factory=list)

    @property
    def job_id(self) -> str:
        return f"patch-rts-check:{self.benchmark_path.name}"

    @property
    def job_type(self) -> str:
        return "patch-rts-check"

    @property
    def depends_on(self) -> list[str]:
        return self._depends

    def execute(self, context: JobContext) -> JobResult:  # noqa: ARG002
        """Execute patch + RTS check. Context unused."""
        started_at = datetime.now()
        try:
            patch_work_dir = Path(tempfile.mkdtemp(prefix="ci-dag-patch-"))
            try:
                # Phase 1: Build patched variants
                build_start = time.time()
                try:
                    self.validator.validate_patches(
                        self.benchmark_path,
                        force_rebuild=self.force_rebuild,
                        build_only=True,
                        use_inc_build=self.use_inc_build,
                        work_dir=patch_work_dir,
                    )
                except Exception:
                    pass  # Build errors surface in verify call
                build_elapsed = time.time() - build_start

                # Phase 2: Verify with full test.sh
                patch_result = self.validator.validate_patches(
                    self.benchmark_path,
                    force_rebuild=False,
                    use_inc_build=self.use_inc_build,
                    work_dir=patch_work_dir,
                )
                # Add build phase timing to patch result
                patch_result = CheckResult(
                    status=patch_result.status,
                    time_seconds=patch_result.time_seconds,
                    build_time=build_elapsed,
                    verify_time=patch_result.verify_time or patch_result.time_seconds,
                    error=patch_result.error,
                    details=patch_result.details,
                    fallback_used=patch_result.fallback_used,
                )

                # Phase 3: RTS on same build (if supported)
                patch_rts_result = None
                if self.rts_mode:
                    try:
                        patch_rts_result = self.validator.validate_patches(
                            self.benchmark_path,
                            force_rebuild=False,
                            use_inc_build=self.use_inc_build,
                            test_mode=UnitTestMode.RTS,
                            work_dir=patch_work_dir,
                        )
                        # RTS reuses build, so build_time stays 0
                    except Exception as exc:
                        patch_rts_result = CheckResult.make_error(str(exc))
                else:
                    patch_rts_result = CheckResult.skip("No RTS mode configured")

            finally:
                shutil.rmtree(patch_work_dir, ignore_errors=True)

            finished_at = datetime.now()
            elapsed = (finished_at - started_at).total_seconds()

            success = patch_result.status == CheckStatus.PASS
            if patch_rts_result and patch_rts_result.status == CheckStatus.FAIL:
                success = False

            return JobResult(
                job_id=self.job_id,
                job_type=self.job_type,
                success=success,
                started_at=started_at,
                finished_at=finished_at,
                elapsed_seconds=elapsed,
                error=patch_result.error or None,
                details={
                    "patch_result": patch_result,
                    "patch_rts_result": patch_rts_result,
                },
            )

        except Exception as e:
            finished_at = datetime.now()
            return JobResult(
                job_id=self.job_id,
                job_type=self.job_type,
                success=False,
                started_at=started_at,
                finished_at=finished_at,
                elapsed_seconds=(finished_at - started_at).total_seconds(),
                error=str(e),
            )


@dataclass
class RtsCheckJob(Job):
    """Run standalone RTS check for a benchmark via DAG executor.

    Builds patched variants once, then runs RTS only (no full test.sh).
    Skips entirely if rts_mode is None.
    """

    benchmark_path: Path
    validator: BenchmarkValidator
    use_inc_build: bool = True
    force_rebuild: bool = False
    rts_mode: Optional[str] = None
    _depends: list[str] = field(default_factory=list)

    @property
    def job_id(self) -> str:
        return f"rts-check:{self.benchmark_path.name}"

    @property
    def job_type(self) -> str:
        return "rts-check"

    @property
    def depends_on(self) -> list[str]:
        return self._depends

    def execute(self, context: JobContext) -> JobResult:  # noqa: ARG002
        """Execute RTS-only check. Context unused."""
        started_at = datetime.now()

        if not self.rts_mode:
            finished_at = datetime.now()
            skip_result = CheckResult.skip("No RTS mode configured")
            return JobResult(
                job_id=self.job_id,
                job_type=self.job_type,
                success=True,
                started_at=started_at,
                finished_at=finished_at,
                elapsed_seconds=0.0,
                details={"check_result": skip_result},
            )

        try:
            patch_work_dir = Path(tempfile.mkdtemp(prefix="ci-dag-rts-"))
            try:
                # Phase 1: Build patched variants
                build_start = time.time()
                try:
                    self.validator.validate_patches(
                        self.benchmark_path,
                        force_rebuild=self.force_rebuild,
                        build_only=True,
                        use_inc_build=self.use_inc_build,
                        work_dir=patch_work_dir,
                    )
                except Exception:
                    pass  # Build errors surface in RTS call
                build_elapsed = time.time() - build_start

                # Phase 2: RTS only (no full test.sh)
                rts_result = self.validator.validate_patches(
                    self.benchmark_path,
                    force_rebuild=False,
                    use_inc_build=self.use_inc_build,
                    test_mode=UnitTestMode.RTS,
                    work_dir=patch_work_dir,
                )
                # Add build phase timing
                rts_result = CheckResult(
                    status=rts_result.status,
                    time_seconds=rts_result.time_seconds,
                    build_time=build_elapsed,
                    verify_time=rts_result.verify_time or rts_result.time_seconds,
                    error=rts_result.error,
                    details=rts_result.details,
                    fallback_used=rts_result.fallback_used,
                )
            finally:
                shutil.rmtree(patch_work_dir, ignore_errors=True)

            finished_at = datetime.now()
            return JobResult(
                job_id=self.job_id,
                job_type=self.job_type,
                success=rts_result.status == CheckStatus.PASS,
                started_at=started_at,
                finished_at=finished_at,
                elapsed_seconds=rts_result.time_seconds,
                error=rts_result.error or None,
                details={"check_result": rts_result},
            )

        except Exception as e:
            finished_at = datetime.now()
            return JobResult(
                job_id=self.job_id,
                job_type=self.job_type,
                success=False,
                started_at=started_at,
                finished_at=finished_at,
                elapsed_seconds=(finished_at - started_at).total_seconds(),
                error=str(e),
            )


@dataclass
class CoverageCheckJob(Job):
    """Run coverage collection for a benchmark via DAG executor.

    Wraps validator.validate_coverage() as a DAG node.
    """

    benchmark_path: Path
    validator: BenchmarkValidator
    use_inc_build: bool = True
    force_rebuild: bool = True
    _depends: list[str] = field(default_factory=list)

    @property
    def job_id(self) -> str:
        return f"coverage-check:{self.benchmark_path.name}"

    @property
    def job_type(self) -> str:
        return "coverage-check"

    @property
    def depends_on(self) -> list[str]:
        return self._depends

    def execute(self, context: JobContext) -> JobResult:  # noqa: ARG002
        """Execute coverage check. Context unused."""
        started_at = datetime.now()
        try:
            result = self.validator.validate_coverage(
                self.benchmark_path,
                force_rebuild=self.force_rebuild,
                use_inc_build=self.use_inc_build,
            )
            finished_at = datetime.now()
            return JobResult(
                job_id=self.job_id,
                job_type=self.job_type,
                success=result.status == CheckStatus.PASS,
                started_at=started_at,
                finished_at=finished_at,
                elapsed_seconds=result.time_seconds,
                error=result.error or None,
                details={"check_result": result},
            )
        except Exception as e:
            finished_at = datetime.now()
            return JobResult(
                job_id=self.job_id,
                job_type=self.job_type,
                success=False,
                started_at=started_at,
                finished_at=finished_at,
                elapsed_seconds=(finished_at - started_at).total_seconds(),
                error=str(e),
            )
