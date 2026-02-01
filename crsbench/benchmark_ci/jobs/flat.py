"""Flat DAG jobs for per-CPV/per-patch atomic scheduling.

These jobs replace coarse validator wrappers (ci_checks.py) with fine-grained
nodes that call builder/infra directly. The DAGExecutor becomes the single
source of concurrency control via typed limits.

Job types:
- BuildSingleVariantJob: Build a single variant for a benchmark (type="build")
- BuildVariantsJob: Build all variants for a benchmark (type="build") [legacy]
- VerifyCpvPovJob: Verify POVs for a single CPV (type="verify")
- BuildPatchVariantJob: Build a patched variant (type="build")
- PatchVariantTestJob: Run POVs + unit tests on patch (type="verify")
- CollectCoverageJob: Collect coverage data (type="verify")
"""

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from crsbench.benchmark_ci.jobs.base import Job, JobContext, JobResult
from crsbench.benchmark_ci.storage import collect_benchmark_storage
from crsbench.builder.types import BenchmarkMode, VariantType
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


def _write_build_logs(
    context: JobContext, log_path: Path, stdout: str, stderr: str
) -> None:
    """Write separate .stdout and .stderr files for build logs."""
    if not context.output_dir or not log_path:
        return

    base_path = log_path.with_suffix("")  # Remove .log suffix

    # Write stdout log
    if stdout:
        stdout_path = base_path.parent / f"{base_path.name}.stdout"
        stdout_path.write_text(stdout)

    # Write stderr log
    if stderr:
        stderr_path = base_path.parent / f"{base_path.name}.stderr"
        stderr_path.write_text(stderr)


@dataclass
class BuildSingleVariantJob(Job):
    """Build a single variant for a benchmark.

    Creates a single BuildConfig and executes via OSSFuzzBuilder.build_single().
    Stores build result in context.shared for downstream jobs.

    This job enables DAGExecutor to parallelize builds across variants.
    """

    benchmark_path: Path
    benchmark_name: str
    variant_type: VariantType
    commit: str
    main_repo: str
    mode: BenchmarkMode
    language: str = "c"
    cpv_num: Optional[int] = None
    patch_id: Optional[str] = None
    pov_id: Optional[str] = None
    patches: list[Path] = field(default_factory=list)
    use_inc_build: bool = True
    force_rebuild: bool = False
    skip_if_cached: bool = True
    source_mode: str = "pkgs"
    sanitizer: str = "address"
    repo_name: Optional[str] = None
    project_image_prefix: str = "aixcc-afc"

    @property
    def job_id(self) -> str:
        """Compute job ID using BuildConfig naming logic."""
        from crsbench.builder.types import BuildConfig

        # Create a temporary config to compute variant_name
        config = BuildConfig(
            benchmark_name=self.benchmark_name,
            variant_type=self.variant_type,
            commit=self.commit,
            main_repo=self.main_repo,
            benchmark_path=self.benchmark_path,
            mode=self.mode,
            patches=self.patches,
            language=self.language,
            cpv_num=self.cpv_num,
            patch_id=self.patch_id,
            pov_id=self.pov_id,
            use_inc_build=self.use_inc_build,
            sanitizer=self.sanitizer,
            repo_name=self.repo_name,
        )
        return f"build-single:{self.benchmark_name}:{config.variant_name}"

    @property
    def job_type(self) -> str:
        return "build"

    def execute(self, context: JobContext) -> JobResult:
        """Build single variant via OSSFuzzBuilder."""
        started_at = datetime.now()
        try:
            from crsbench.builder import OSSFuzzBuilder
            from crsbench.builder.types import BuildConfig
            from crsbench.utils.run_helper import get_oss_fuzz_root

            oss_fuzz_path = Path(get_oss_fuzz_root())
            builder = OSSFuzzBuilder(
                oss_fuzz_path, max_workers=1, source_mode=self.source_mode
            )

            # Build config from job fields
            config = BuildConfig(
                benchmark_name=self.benchmark_name,
                variant_type=self.variant_type,
                commit=self.commit,
                main_repo=self.main_repo,
                benchmark_path=self.benchmark_path,
                mode=self.mode,
                patches=self.patches,
                language=self.language,
                cpv_num=self.cpv_num,
                patch_id=self.patch_id,
                pov_id=self.pov_id,
                use_inc_build=self.use_inc_build,
                sanitizer=self.sanitizer,
                repo_name=self.repo_name,
            )

            result = builder.build_single(
                config,
                force_rebuild=self.force_rebuild,
                skip_if_cached=self.skip_if_cached,
            )

            # Store in context.shared for downstream jobs
            context.shared[self.job_id] = {
                "build_result": result,
                "variant_name": result.variant_name,
            }

            # Collect storage metrics after build
            storage_metrics = collect_benchmark_storage(
                benchmark_name=self.benchmark_name,
                benchmark_path=self.benchmark_path,
                oss_fuzz_path=oss_fuzz_path,
                project_image_prefix=self.project_image_prefix,
            )
            storage_bytes = storage_metrics.total_bytes

            finished_at = datetime.now()
            elapsed = (finished_at - started_at).total_seconds()

            job_result = JobResult(
                job_id=self.job_id,
                job_type=self.job_type,
                success=result.success,
                started_at=started_at,
                finished_at=finished_at,
                elapsed_seconds=elapsed,
                error=None if result.success else (result.error or "Build failed"),
                details={
                    "variant_name": result.variant_name,
                    "variant_type": self.variant_type.value,
                    "cached": result.cached,
                    "fallback_used": result.fallback_used,
                    "storage_bytes": storage_bytes,
                },
            )
            self._write_job_log(context, job_result)

            # Write separate stdout/stderr files
            log_path = self._job_log_path(context)
            if log_path:
                _write_build_logs(context, log_path, result.stdout, result.stderr)

            return job_result
        except Exception as e:
            finished_at = datetime.now()
            job_result = JobResult(
                job_id=self.job_id,
                job_type=self.job_type,
                success=False,
                started_at=started_at,
                finished_at=finished_at,
                elapsed_seconds=(finished_at - started_at).total_seconds(),
                error=str(e),
            )
            self._write_job_log(context, job_result)
            return job_result


@dataclass
class BuildVariantsJob(Job):
    """Build all variants for a benchmark.

    Creates build plan via OSSFuzzBuilder and executes it. Stores
    build results in context.shared for downstream verify jobs.
    """

    benchmark_path: Path
    benchmark_name: str
    use_inc_build: bool = True
    force_rebuild: bool = False
    source_mode: str = "pkgs"
    project_image_prefix: str = "aixcc-afc"

    @property
    def job_id(self) -> str:
        return f"build-variants:{self.benchmark_name}"

    @property
    def job_type(self) -> str:
        return "build"

    def execute(self, context: JobContext) -> JobResult:
        """Build all variants via OSSFuzzBuilder."""
        started_at = datetime.now()
        try:
            from crsbench.evaluation.verification.pov import VerificationEngine
            from crsbench.utils.run_helper import get_oss_fuzz_root

            oss_fuzz_path = Path(get_oss_fuzz_root())
            engine = VerificationEngine(
                oss_fuzz_path,
                source_mode=self.source_mode,
            )
            adapter = engine.load_adapter(self.benchmark_path)
            if not adapter:
                raise ValueError(f"Failed to load adapter for {self.benchmark_path}")
            build_results = engine.get_or_build_results(
                adapter,
                force_rebuild=self.force_rebuild,
                use_inc_build=self.use_inc_build,
            )

            success = any(r.success for r in build_results.values())
            context.shared[self.job_id] = {
                "build_results": build_results,
                "adapter": adapter,
            }

            # Only consider inc-build target variants for fallback
            fallback_used = any(
                r.fallback_used
                for r in build_results.values()
                if r.config.variant_type.is_inc_build_target()
            )

            finished_at = datetime.now()
            elapsed = (finished_at - started_at).total_seconds()

            variants_info = [
                {
                    "name": name,
                    "variant_type": r.config.variant_type.value,
                    "success": r.success,
                    "fallback": r.fallback_used,
                    "cached": r.cached,
                    "elapsed": f"{r.elapsed_seconds:.1f}s",
                }
                for name, r in build_results.items()
            ]

            # Collect storage metrics after build
            storage_metrics = collect_benchmark_storage(
                benchmark_name=self.benchmark_name,
                benchmark_path=self.benchmark_path,
                oss_fuzz_path=oss_fuzz_path,
                project_image_prefix=self.project_image_prefix,
            )
            storage_bytes = storage_metrics.total_bytes

            result = JobResult(
                job_id=self.job_id,
                job_type=self.job_type,
                success=success,
                started_at=started_at,
                finished_at=finished_at,
                elapsed_seconds=elapsed,
                error=None if success else "No variants built successfully",
                details={
                    "variants_built": len(
                        [r for r in build_results.values() if r.success]
                    ),
                    "variants_total": len(build_results),
                    "fallback_used": fallback_used,
                    "variants": variants_info,
                    "storage_bytes": storage_bytes,
                },
            )
            self._write_job_log(context, result)
            return result
        except Exception as e:
            finished_at = datetime.now()
            result = JobResult(
                job_id=self.job_id,
                job_type=self.job_type,
                success=False,
                started_at=started_at,
                finished_at=finished_at,
                elapsed_seconds=(finished_at - started_at).total_seconds(),
                error=str(e),
            )
            self._write_job_log(context, result)
            return result


@dataclass
class VerifyCpvPovJob(Job):
    """Verify ground truth POV (pov_0) for a single CPV against built variants.

    Tests only pov_0 (ground truth) - variant POVs are handled by VerifyCpvVarJob.
    Runs in parallel with VerifyCpvVarJob after build completes.
    """

    benchmark_name: str
    cpv_id: str
    harness: str
    benchmark_path: Optional[Path] = None
    pov_path: Optional[Path] = None  # Single pov_0 path
    build_job_ids: list[str] = field(default_factory=list)
    source_mode: str = "pkgs"

    @property
    def job_id(self) -> str:
        return f"verify-cpv-pov:{self.benchmark_name}:{self.cpv_id}"

    @property
    def job_type(self) -> str:
        return "verify"

    @property
    def depends_on(self) -> list[str]:
        return self.build_job_ids

    def _write_verdict_logs(self, context: JobContext, results: list) -> None:
        """Write separate .stdout and .stderr files per variant for verdict logs."""
        if not context.output_dir:
            return

        log_path = self._job_log_path(context)
        if not log_path:
            return

        base_path = log_path.with_suffix("")  # Remove .log suffix

        for r in results:
            if not r.crash_info:
                continue

            pov_id = r.pov_id or "unknown"

            # Write stdout logs - one file per variant
            stdout_logs = r.crash_info.get("stdout", {})
            for variant_name, log in stdout_logs.items():
                stdout_path = (
                    base_path.parent
                    / f"{base_path.name}-{pov_id}-{variant_name}.stdout"
                )
                stdout_path.write_text(log)

            # Write stderr logs - one file per variant
            stderr_logs = r.crash_info.get("stderr", {})
            for variant_name, log in stderr_logs.items():
                stderr_path = (
                    base_path.parent
                    / f"{base_path.name}-{pov_id}-{variant_name}.stderr"
                )
                stderr_path.write_text(log)

    def execute(self, context: JobContext) -> JobResult:
        """Verify pov_0 for this CPV using pre-built variants."""
        started_at = datetime.now()
        try:
            if not self.pov_path:
                finished_at = datetime.now()
                result = JobResult(
                    job_id=self.job_id,
                    job_type=self.job_type,
                    success=True,
                    started_at=started_at,
                    finished_at=finished_at,
                    elapsed_seconds=(finished_at - started_at).total_seconds(),
                    details={"cpv_id": self.cpv_id, "pov_count": 0},
                )
                self._write_job_log(context, result)
                return result

            from crsbench.evaluation.verification.models import (
                PovVerificationStatus,
            )
            from crsbench.evaluation.verification.pov import VerificationEngine
            from crsbench.utils.run_helper import get_oss_fuzz_root

            oss_fuzz_path = Path(get_oss_fuzz_root())

            # Collect build results from build jobs
            build_results: dict = {}
            adapter = None

            for job_id in self.build_job_ids:
                build_data = context.shared.get(job_id, {})
                if build_data:
                    build_result = build_data.get("build_result")
                    if build_result:
                        build_results[build_result.variant_name] = build_result
                    if not adapter:
                        adapter = build_data.get("adapter")

            # If adapter not in context.shared, load via VerificationEngine
            if not adapter and self.benchmark_path:
                engine = VerificationEngine(
                    oss_fuzz_path,
                    source_mode=self.source_mode,
                )
                adapter = engine.load_adapter(self.benchmark_path)

            if not build_results or not adapter:
                raise ValueError(f"No build data from {self.build_job_ids}")

            engine = VerificationEngine(
                oss_fuzz_path,
                source_mode=self.source_mode,
            )

            # Test only pov_0
            pov_data = self.pov_path.read_bytes()
            pov_id = self.pov_path.stem
            pov_harness_pairs = [(pov_id, pov_data, self.harness)]

            results = engine.verify_povs_parallel(
                pov_harness_pairs, adapter, build_results
            )

            passed = all(r.status == PovVerificationStatus.CPV for r in results)

            # Collect variant info for structured logging
            variants_used = [
                {
                    "name": name,
                    "variant_type": r.config.variant_type.value,
                }
                for name, r in build_results.items()
                if r.success
            ]

            # Collect verdict info
            pov_verdicts = []
            for r in results:
                verdict_info = {
                    "pov_id": r.pov_id or "unknown",
                    "status": r.status.value,
                    "cpv_matched": r.cpv_matched,
                }
                pov_verdicts.append(verdict_info)

            finished_at = datetime.now()
            result = JobResult(
                job_id=self.job_id,
                job_type=self.job_type,
                success=passed,
                started_at=started_at,
                finished_at=finished_at,
                elapsed_seconds=(finished_at - started_at).total_seconds(),
                details={
                    "cpv_id": self.cpv_id,
                    "pov_count": 1,
                    "pov_0_passed": passed,
                    "variants_used": len(variants_used),
                    "variants": variants_used,
                    "verdicts": pov_verdicts,
                },
            )
            self._write_job_log(context, result)

            # Write separate stdout/stderr files for each verdict
            self._write_verdict_logs(context, results)

            return result
        except Exception as e:
            finished_at = datetime.now()
            result = JobResult(
                job_id=self.job_id,
                job_type=self.job_type,
                success=False,
                started_at=started_at,
                finished_at=finished_at,
                elapsed_seconds=(finished_at - started_at).total_seconds(),
                error=str(e),
            )
            self._write_job_log(context, result)
            return result


@dataclass
class VerifyCpvVarJob(Job):
    """Verify variant POVs (pov_1+) for a single CPV against built variants.

    Tests only variant POVs - ground truth (pov_0) is handled by VerifyCpvPovJob.
    Runs in parallel with VerifyCpvPovJob after build completes.
    """

    benchmark_name: str
    cpv_id: str
    harness: str
    benchmark_path: Optional[Path] = None
    pov_paths: list[Path] = field(default_factory=list)  # pov_1+ paths
    build_job_ids: list[str] = field(default_factory=list)
    source_mode: str = "pkgs"

    @property
    def job_id(self) -> str:
        return f"verify-cpv-var:{self.benchmark_name}:{self.cpv_id}"

    @property
    def job_type(self) -> str:
        return "verify"

    @property
    def depends_on(self) -> list[str]:
        return self.build_job_ids

    def _write_verdict_logs(self, context: JobContext, results: list) -> None:
        """Write separate .stdout and .stderr files per variant for verdict logs."""
        if not context.output_dir:
            return

        log_path = self._job_log_path(context)
        if not log_path:
            return

        base_path = log_path.with_suffix("")  # Remove .log suffix

        for r in results:
            if not r.crash_info:
                continue

            pov_id = r.pov_id or "unknown"

            stdout_logs = r.crash_info.get("stdout", {})
            for variant_name, log in stdout_logs.items():
                stdout_path = (
                    base_path.parent
                    / f"{base_path.name}-{pov_id}-{variant_name}.stdout"
                )
                stdout_path.write_text(log)

            stderr_logs = r.crash_info.get("stderr", {})
            for variant_name, log in stderr_logs.items():
                stderr_path = (
                    base_path.parent
                    / f"{base_path.name}-{pov_id}-{variant_name}.stderr"
                )
                stderr_path.write_text(log)

    def execute(self, context: JobContext) -> JobResult:
        """Verify variant POVs for this CPV using pre-built variants."""
        started_at = datetime.now()
        try:
            if not self.pov_paths:
                finished_at = datetime.now()
                result = JobResult(
                    job_id=self.job_id,
                    job_type=self.job_type,
                    success=True,
                    started_at=started_at,
                    finished_at=finished_at,
                    elapsed_seconds=(finished_at - started_at).total_seconds(),
                    details={
                        "cpv_id": self.cpv_id,
                        "var_passed": 0,
                        "var_total": 0,
                    },
                )
                self._write_job_log(context, result)
                return result

            from crsbench.evaluation.verification.models import (
                PovVerificationStatus,
            )
            from crsbench.evaluation.verification.pov import VerificationEngine
            from crsbench.utils.run_helper import get_oss_fuzz_root

            oss_fuzz_path = Path(get_oss_fuzz_root())

            # Collect build results from build jobs
            build_results: dict = {}
            adapter = None

            for job_id in self.build_job_ids:
                build_data = context.shared.get(job_id, {})
                if build_data:
                    build_result = build_data.get("build_result")
                    if build_result:
                        build_results[build_result.variant_name] = build_result
                    if not adapter:
                        adapter = build_data.get("adapter")

            # If adapter not in context.shared, load via VerificationEngine
            if not adapter and self.benchmark_path:
                engine = VerificationEngine(
                    oss_fuzz_path,
                    source_mode=self.source_mode,
                )
                adapter = engine.load_adapter(self.benchmark_path)

            if not build_results or not adapter:
                raise ValueError(f"No build data from {self.build_job_ids}")

            engine = VerificationEngine(
                oss_fuzz_path,
                source_mode=self.source_mode,
            )

            # Test variant POVs (pov_1+)
            pov_harness_pairs = []
            for pov_path in self.pov_paths:
                pov_data = pov_path.read_bytes()
                pov_id = pov_path.stem
                pov_harness_pairs.append((pov_id, pov_data, self.harness))

            results = engine.verify_povs_parallel(
                pov_harness_pairs, adapter, build_results
            )

            var_passed = sum(
                1 for r in results if r.status == PovVerificationStatus.CPV
            )
            var_total = len(results)
            passed = var_passed == var_total

            # Collect variant info for structured logging
            variants_used = [
                {
                    "name": name,
                    "variant_type": r.config.variant_type.value,
                }
                for name, r in build_results.items()
                if r.success
            ]

            # Collect verdict info
            pov_verdicts = []
            for r in results:
                verdict_info = {
                    "pov_id": r.pov_id or "unknown",
                    "status": r.status.value,
                    "cpv_matched": r.cpv_matched,
                }
                pov_verdicts.append(verdict_info)

            finished_at = datetime.now()
            result = JobResult(
                job_id=self.job_id,
                job_type=self.job_type,
                success=passed,
                started_at=started_at,
                finished_at=finished_at,
                elapsed_seconds=(finished_at - started_at).total_seconds(),
                details={
                    "cpv_id": self.cpv_id,
                    "var_passed": var_passed,
                    "var_total": var_total,
                    "variants_used": len(variants_used),
                    "variants": variants_used,
                    "verdicts": pov_verdicts,
                },
            )
            self._write_job_log(context, result)

            # Write separate stdout/stderr files for each verdict
            self._write_verdict_logs(context, results)

            return result
        except Exception as e:
            finished_at = datetime.now()
            result = JobResult(
                job_id=self.job_id,
                job_type=self.job_type,
                success=False,
                started_at=started_at,
                finished_at=finished_at,
                elapsed_seconds=(finished_at - started_at).total_seconds(),
                error=str(e),
            )
            self._write_job_log(context, result)
            return result


@dataclass
class BuildPatchVariantJob(Job):
    """Build a patched variant for a specific CPV.

    Applies a patch and rebuilds, storing the variant name in
    context.shared for downstream PatchVariantTestJob.
    """

    benchmark_path: Path
    benchmark_name: str
    cpv_id: str
    patch_id: str
    patch_path: Path
    harness: str = ""  # Harness name for per-harness sanitizer
    use_inc_build: bool = True
    force_rebuild: bool = False
    build_job_id: str = ""
    source_mode: str = "pkgs"

    @property
    def job_id(self) -> str:
        return f"build-patch:{self.benchmark_name}:{self.cpv_id}:{self.patch_id}"

    @property
    def job_type(self) -> str:
        return "build"

    @property
    def depends_on(self) -> list[str]:
        return [self.build_job_id] if self.build_job_id else []

    def execute(self, context: JobContext) -> JobResult:
        """Build patched variant using PatchVerificationEngine.

        Uses PatchVerificationEngine with build_only=True to ensure:
        1. Source is prepared in temp dir and patch is applied
        2. Build uses inc-build when available
        3. Build artifacts are cached for downstream PatchVariantTestJob
        """
        started_at = datetime.now()
        try:
            from crsbench.evaluation.verification.models import (
                PatchInfo,
                PatchVerificationStatus,
            )
            from crsbench.evaluation.verification.patch import PatchVerificationEngine
            from crsbench.evaluation.verification.pov import VerificationEngine
            from crsbench.utils.run_helper import get_oss_fuzz_root

            oss_fuzz_path = Path(get_oss_fuzz_root())

            # Load adapter to get sanitizer
            pov_engine = VerificationEngine(oss_fuzz_path, source_mode=self.source_mode)
            adapter = pov_engine.load_adapter(self.benchmark_path)

            if not adapter:
                raise ValueError(f"Failed to load adapter for {self.benchmark_path}")

            # Get sanitizer for this specific CPV
            if not self.harness or not self.cpv_id:
                raise ValueError(
                    f"BuildPatchVariantJob requires both harness and cpv_id: "
                    f"harness={self.harness}, cpv_id={self.cpv_id}"
                )

            sanitizer = adapter.get_cpv_sanitizer(self.harness, self.cpv_id)

            # Create PatchVerificationEngine with build_only=True
            # Source is prepared in temp dir - each job is self-contained
            engine = PatchVerificationEngine(
                oss_fuzz_path,
                sanitizer=sanitizer,
                use_inc_build=self.use_inc_build,
                force_rebuild=self.force_rebuild,
                source_mode=self.source_mode,
                build_only=True,  # Only build, skip verification
            )

            # Create PatchInfo for the engine
            patch_info = PatchInfo(
                patch_id=self.patch_id,
                pov_id=self.cpv_id,
                patch_path=self.patch_path,
            )

            # Build using the engine (verification is skipped due to build_only=True)
            result = engine.verify_patch(
                benchmark_path=self.benchmark_path,
                patch=patch_info,
                harness=self.harness,
                pov_path=self.patch_path.parent / "pov_0.blob",  # Placeholder, not used
            )

            # Determine variant_name from the result
            # PatchVerificationEngine uses same naming convention
            from crsbench.builder.types import BuildConfig, VariantType

            build_config = BuildConfig(
                benchmark_name=self.benchmark_name,
                benchmark_path=self.benchmark_path,
                variant_type=VariantType.PATCHED,
                mode=adapter.get_mode(),
                sanitizer=sanitizer,
                language=adapter.lang,
                commit=adapter.get_ref_commit() or adapter.get_base_commit(),
                main_repo=adapter.main_repo,
                patch_id=self.patch_id,
                pov_id=self.cpv_id,
            )
            variant_name = build_config.variant_name

            success = result.status == PatchVerificationStatus.VALID

            # Store in context.shared for downstream jobs
            context.shared[self.job_id] = {
                "variant_name": variant_name,
                "sanitizer": sanitizer,
                "fallback_used": result.fallback_used,
                "inc_build_available": result.inc_build_available,
            }

            finished_at = datetime.now()
            job_result = JobResult(
                job_id=self.job_id,
                job_type=self.job_type,
                success=success,
                started_at=started_at,
                finished_at=finished_at,
                elapsed_seconds=(finished_at - started_at).total_seconds(),
                error=None if success else (result.details or "Build failed"),
                details={
                    "variant_name": variant_name,
                    "cpv_id": self.cpv_id,
                    "patch_id": self.patch_id,
                    "fallback_used": result.fallback_used,
                    "inc_build_available": result.inc_build_available,
                    "build_time": result.build_time,
                },
            )
            self._write_job_log(context, job_result)

            # Write separate stdout/stderr files for build output
            log_path = self._job_log_path(context)
            if log_path:
                _write_build_logs(
                    context, log_path, result.build_stdout, result.build_stderr
                )

            return job_result
        except Exception as e:
            finished_at = datetime.now()
            job_result = JobResult(
                job_id=self.job_id,
                job_type=self.job_type,
                success=False,
                started_at=started_at,
                finished_at=finished_at,
                elapsed_seconds=(finished_at - started_at).total_seconds(),
                error=str(e),
            )
            self._write_job_log(context, job_result)
            return job_result


@dataclass
class PatchVariantTestJob(Job):
    """Run POVs and unit tests against a patched build.

    Uses PatchVerificationEngine to verify that the patch fixes the vulnerability
    (POVs don't crash) and doesn't break functionality (tests pass).

    The engine handles:
    - POV tests with proper variant testing
    - Unit tests with correct source path, sanitizer, and docker_image_tag
    """

    benchmark_path: Path
    benchmark_name: str
    cpv_id: str
    patch_id: str
    harness: str
    pov_paths: list[Path] = field(default_factory=list)
    test_mode: str = "FULL"
    build_patch_job_id: str = ""
    source_mode: str = "pkgs"

    @property
    def job_id(self) -> str:
        return (
            f"test-patch:{self.benchmark_name}:{self.cpv_id}"
            f":{self.patch_id}:{self.test_mode}"
        )

    @property
    def job_type(self) -> str:
        return "verify"

    @property
    def depends_on(self) -> list[str]:
        return [self.build_patch_job_id] if self.build_patch_job_id else []

    def _write_test_logs(
        self,
        context: JobContext,
        pov_stdout: dict[str, str],
        pov_stderr: dict[str, str],
        test_stdout: str,
        test_stderr: str,
    ) -> None:
        """Write separate .stdout and .stderr files per POV and for unit tests."""
        if not context.output_dir:
            return

        log_path = self._job_log_path(context)
        if not log_path:
            return

        base_path = log_path.with_suffix("")  # Remove .log suffix

        # Write POV stdout logs - one file per POV
        for pov_id, stdout in pov_stdout.items():
            stdout_path = base_path.parent / f"{base_path.name}-{pov_id}.stdout"
            stdout_path.write_text(stdout)

        # Write POV stderr logs - one file per POV
        for pov_id, stderr in pov_stderr.items():
            stderr_path = base_path.parent / f"{base_path.name}-{pov_id}.stderr"
            stderr_path.write_text(stderr)

        # Write unit test logs
        if test_stdout:
            stdout_path = base_path.parent / f"{base_path.name}-unit-tests.stdout"
            stdout_path.write_text(test_stdout)

        if test_stderr:
            stderr_path = base_path.parent / f"{base_path.name}-unit-tests.stderr"
            stderr_path.write_text(test_stderr)

    def execute(self, context: JobContext) -> JobResult:
        """Run POVs and tests against patched variant using PatchVerificationEngine.

        Uses PatchVerificationEngine with force_rebuild=False to leverage the
        cached build from BuildPatchVariantJob. The engine handles:
        1. POV tests (via reproduce)
        2. Unit tests (with proper source path, sanitizer, docker_image_tag)
        """
        from crsbench.evaluation.verification.models import (
            PatchInfo,
            PatchVerificationStatus,
            UnitTestMode,
        )
        from crsbench.evaluation.verification.patch import PatchVerificationEngine
        from crsbench.utils.run_helper import get_oss_fuzz_root

        started_at = datetime.now()
        try:
            # Get build data from upstream job
            build_data = context.shared.get(self.build_patch_job_id, {})
            variant_name = build_data.get("variant_name")
            sanitizer = build_data.get("sanitizer", "address")
            inc_build_available = build_data.get("inc_build_available", False)

            if not variant_name:
                raise ValueError(f"No variant name from {self.build_patch_job_id}")

            oss_fuzz_path = Path(get_oss_fuzz_root())

            # Create PatchVerificationEngine for verification
            # - force_rebuild=False: use cached build from BuildPatchVariantJob
            # - build_only=False: run full verification (POV + unit tests)
            # - verify_variants=True: test all POV variants for this CPV
            # use_inc_build must match what was used during build phase
            # (determined by inc_build_available from build job)
            test_mode = (
                UnitTestMode.RTS if self.test_mode == "RTS" else UnitTestMode.FULL
            )
            engine = PatchVerificationEngine(
                oss_fuzz_path,
                test_mode=test_mode,
                sanitizer=sanitizer,
                timeout=context.timeout,
                use_inc_build=inc_build_available,
                force_rebuild=False,  # Use cached build
                source_mode=self.source_mode,
                build_only=False,  # Run full verification
                verify_variants=True,  # Test all POV variants
            )

            # Get first POV path for the engine (it will discover all variants)
            pov_path = self.pov_paths[0] if self.pov_paths else None
            if not pov_path:
                raise ValueError(f"No POV paths provided for {self.cpv_id}")

            # Find patch path from benchmark structure
            patch_dir = (
                self.benchmark_path / ".aixcc" / self.harness / self.cpv_id / "patches"
            )
            patch_path = patch_dir / f"{self.patch_id}.diff"
            if not patch_path.exists():
                # Try alternative naming
                patch_path = patch_dir / "patch.diff"

            if not patch_path.exists():
                raise ValueError(f"Patch file not found: {patch_path}")

            # Create PatchInfo for the engine
            patch_info = PatchInfo(
                patch_id=self.patch_id,
                pov_id=self.cpv_id,
                patch_path=patch_path,
            )

            # Run verification using the engine
            result = engine.verify_patch(
                benchmark_path=self.benchmark_path,
                patch=patch_info,
                harness=self.harness,
                pov_path=pov_path,
            )

            # Map engine result to job result
            success = result.status == PatchVerificationStatus.VALID
            pov_test_passed = result.pov_test_passed
            unit_tests_passed = result.unit_tests_passed

            # Determine error message
            error_msg = None
            if result.status == PatchVerificationStatus.POV_STILL_TRIGGERS:
                error_msg = result.details or "POVs still crash"
            elif result.status == PatchVerificationStatus.TEST_FAILED:
                error_msg = result.details or "Unit tests failed"
            elif result.status == PatchVerificationStatus.BUILD_FAILED:
                error_msg = result.details or "Build failed"
            elif result.status == PatchVerificationStatus.ERROR:
                error_msg = result.details or "Verification error"

            # Build details dict
            details: dict = {
                "cpv_id": self.cpv_id,
                "patch_id": self.patch_id,
                "test_mode": self.test_mode,
                "total_povs": len(self.pov_paths),
                "pov_test_passed": pov_test_passed,
                "unit_tests_passed": unit_tests_passed,
                "security_verdict": result.security_verdict,
                "pov_test_time": result.pov_test_time,
                "unit_test_time": result.unit_test_time,
            }

            # Add CPV stats if available
            if result.cpv_stats:
                cpv_stats = result.cpv_stats.get(self.cpv_id)
                if cpv_stats:
                    details["variants_tested"] = cpv_stats.variants_tested
                    details["variants_matched"] = cpv_stats.variants_matched
                    details["cpv_status"] = cpv_stats.status

            finished_at = datetime.now()
            job_result = JobResult(
                job_id=self.job_id,
                job_type=self.job_type,
                success=success,
                started_at=started_at,
                finished_at=finished_at,
                elapsed_seconds=(finished_at - started_at).total_seconds(),
                error=error_msg,
                details=details,
            )
            self._write_job_log(context, job_result)

            # Write stdout/stderr logs
            # Note: The engine captures logs internally, but we write a summary
            pov_stdout: dict[str, str] = {}
            pov_stderr: dict[str, str] = {}
            if result.cpv_stats:
                cpv_stats = result.cpv_stats.get(self.cpv_id)
                if cpv_stats and cpv_stats.variant_results:
                    for variant_id, passed in cpv_stats.variant_results.items():
                        status = "PASS" if passed else "FAIL"
                        pov_stdout[variant_id] = f"Result: {status}"

            self._write_test_logs(
                context,
                pov_stdout,
                pov_stderr,
                "",  # Unit test stdout not captured in current result model
                "",  # Unit test stderr not captured in current result model
            )

            return job_result
        except Exception as e:
            finished_at = datetime.now()
            job_result = JobResult(
                job_id=self.job_id,
                job_type=self.job_type,
                success=False,
                started_at=started_at,
                finished_at=finished_at,
                elapsed_seconds=(finished_at - started_at).total_seconds(),
                error=str(e),
            )
            self._write_job_log(context, job_result)
            return job_result


@dataclass
class PatchPovTestJob(Job):
    """Run pov_0 (ground truth) test only against a patched build.

    Tests only pov_0 - variant POVs are handled by PatchVarTestJob.
    Runs in parallel with PatchVarTestJob and PatchUnitTestJob after build.
    """

    benchmark_path: Path
    benchmark_name: str
    cpv_id: str
    patch_id: str
    harness: str
    pov_path: Optional[Path] = None  # Single pov_0 path
    build_patch_job_id: str = ""
    source_mode: str = "pkgs"

    @property
    def job_id(self) -> str:
        return f"test-patch-pov:{self.benchmark_name}:{self.cpv_id}:{self.patch_id}"

    @property
    def job_type(self) -> str:
        return "verify"

    @property
    def depends_on(self) -> list[str]:
        return [self.build_patch_job_id] if self.build_patch_job_id else []

    def execute(self, context: JobContext) -> JobResult:
        """Run pov_0 test only using PatchVerificationEngine."""
        from crsbench.evaluation.verification.models import (
            PatchInfo,
            PatchVerificationStatus,
        )
        from crsbench.evaluation.verification.patch import PatchVerificationEngine
        from crsbench.utils.run_helper import get_oss_fuzz_root

        started_at = datetime.now()
        try:
            if not self.pov_path:
                finished_at = datetime.now()
                job_result = JobResult(
                    job_id=self.job_id,
                    job_type=self.job_type,
                    success=True,
                    started_at=started_at,
                    finished_at=finished_at,
                    elapsed_seconds=(finished_at - started_at).total_seconds(),
                    details={
                        "cpv_id": self.cpv_id,
                        "patch_id": self.patch_id,
                        "pov_0_passed": True,
                        "pov_count": 0,
                    },
                )
                self._write_job_log(context, job_result)
                return job_result

            # Get build data from upstream job
            build_data = context.shared.get(self.build_patch_job_id, {})
            variant_name = build_data.get("variant_name")
            sanitizer = build_data.get("sanitizer", "address")
            inc_build_available = build_data.get("inc_build_available", False)

            if not variant_name:
                raise ValueError(f"No variant name from {self.build_patch_job_id}")

            oss_fuzz_path = Path(get_oss_fuzz_root())

            # Create engine - only test pov_0, no variants
            engine = PatchVerificationEngine(
                oss_fuzz_path,
                sanitizer=sanitizer,
                timeout=context.timeout,
                use_inc_build=inc_build_available,
                force_rebuild=False,  # Use cached build
                source_mode=self.source_mode,
                build_only=False,
                verify_variants=False,  # Only test pov_0
                skip_unittest=True,
            )

            # Find patch path from benchmark structure
            patch_dir = (
                self.benchmark_path / ".aixcc" / self.harness / self.cpv_id / "patches"
            )
            patch_path = patch_dir / f"{self.patch_id}.diff"
            if not patch_path.exists():
                patch_path = patch_dir / "patch.diff"

            if not patch_path.exists():
                raise ValueError(f"Patch file not found: {patch_path}")

            patch_info = PatchInfo(
                patch_id=self.patch_id,
                pov_id=self.cpv_id,
                patch_path=patch_path,
            )

            result = engine.verify_patch(
                benchmark_path=self.benchmark_path,
                patch=patch_info,
                harness=self.harness,
                pov_path=self.pov_path,
            )

            # pov_0 passes if patch blocks the crash
            pov_0_passed = result.pov_test_passed is True
            success = pov_0_passed

            error_msg = None
            if not success:
                if result.status == PatchVerificationStatus.POV_STILL_TRIGGERS:
                    error_msg = result.details or "pov_0 still crashes"
                elif result.status == PatchVerificationStatus.ERROR:
                    error_msg = result.details or "POV verification error"

            details: dict = {
                "cpv_id": self.cpv_id,
                "patch_id": self.patch_id,
                "pov_0_passed": pov_0_passed,
                "pov_test_time": result.pov_test_time,
                "inc_build_available": inc_build_available,
            }

            finished_at = datetime.now()
            job_result = JobResult(
                job_id=self.job_id,
                job_type=self.job_type,
                success=success,
                started_at=started_at,
                finished_at=finished_at,
                elapsed_seconds=(finished_at - started_at).total_seconds(),
                error=error_msg,
                details=details,
            )
            self._write_job_log(context, job_result)
            return job_result

        except Exception as e:
            finished_at = datetime.now()
            job_result = JobResult(
                job_id=self.job_id,
                job_type=self.job_type,
                success=False,
                started_at=started_at,
                finished_at=finished_at,
                elapsed_seconds=(finished_at - started_at).total_seconds(),
                error=str(e),
            )
            self._write_job_log(context, job_result)
            return job_result


@dataclass
class PatchVarTestJob(Job):
    """Run variant POV tests (pov_1+) against a patched build.

    Tests only variant POVs - ground truth (pov_0) is handled by PatchPovTestJob.
    Runs in parallel with PatchPovTestJob and PatchUnitTestJob after build.
    """

    benchmark_path: Path
    benchmark_name: str
    cpv_id: str
    patch_id: str
    harness: str
    pov_paths: list[Path] = field(default_factory=list)  # pov_1+ paths
    build_patch_job_id: str = ""
    source_mode: str = "pkgs"

    @property
    def job_id(self) -> str:
        return f"test-patch-var:{self.benchmark_name}:{self.cpv_id}:{self.patch_id}"

    @property
    def job_type(self) -> str:
        return "verify"

    @property
    def depends_on(self) -> list[str]:
        return [self.build_patch_job_id] if self.build_patch_job_id else []

    def execute(self, context: JobContext) -> JobResult:
        """Run variant POV tests using PatchVerificationEngine."""
        from crsbench.evaluation.verification.models import (
            PatchInfo,
        )
        from crsbench.evaluation.verification.patch import PatchVerificationEngine
        from crsbench.utils.run_helper import get_oss_fuzz_root

        started_at = datetime.now()
        try:
            if not self.pov_paths:
                finished_at = datetime.now()
                job_result = JobResult(
                    job_id=self.job_id,
                    job_type=self.job_type,
                    success=True,
                    started_at=started_at,
                    finished_at=finished_at,
                    elapsed_seconds=(finished_at - started_at).total_seconds(),
                    details={
                        "cpv_id": self.cpv_id,
                        "patch_id": self.patch_id,
                        "var_passed": 0,
                        "var_total": 0,
                    },
                )
                self._write_job_log(context, job_result)
                return job_result

            # Get build data from upstream job
            build_data = context.shared.get(self.build_patch_job_id, {})
            variant_name = build_data.get("variant_name")
            sanitizer = build_data.get("sanitizer", "address")
            inc_build_available = build_data.get("inc_build_available", False)

            if not variant_name:
                raise ValueError(f"No variant name from {self.build_patch_job_id}")

            oss_fuzz_path = Path(get_oss_fuzz_root())

            # Find patch path from benchmark structure
            patch_dir = (
                self.benchmark_path / ".aixcc" / self.harness / self.cpv_id / "patches"
            )
            patch_path = patch_dir / f"{self.patch_id}.diff"
            if not patch_path.exists():
                patch_path = patch_dir / "patch.diff"

            if not patch_path.exists():
                raise ValueError(f"Patch file not found: {patch_path}")

            patch_info = PatchInfo(
                patch_id=self.patch_id,
                pov_id=self.cpv_id,
                patch_path=patch_path,
            )

            # Test each variant POV
            var_passed = 0
            var_total = len(self.pov_paths)
            pov_test_time = 0.0

            for pov_path in self.pov_paths:
                engine = PatchVerificationEngine(
                    oss_fuzz_path,
                    sanitizer=sanitizer,
                    timeout=context.timeout,
                    use_inc_build=inc_build_available,
                    force_rebuild=False,
                    source_mode=self.source_mode,
                    build_only=False,
                    verify_variants=False,  # Test one at a time
                    skip_unittest=True,
                )

                result = engine.verify_patch(
                    benchmark_path=self.benchmark_path,
                    patch=patch_info,
                    harness=self.harness,
                    pov_path=pov_path,
                )

                if result.pov_test_passed:
                    var_passed += 1
                pov_test_time += result.pov_test_time or 0.0

            success = var_passed == var_total

            error_msg = None
            if not success:
                error_msg = f"Partial fix: {var_passed}/{var_total} variants passed"

            details: dict = {
                "cpv_id": self.cpv_id,
                "patch_id": self.patch_id,
                "var_passed": var_passed,
                "var_total": var_total,
                "pov_test_time": pov_test_time,
                "inc_build_available": inc_build_available,
            }

            finished_at = datetime.now()
            job_result = JobResult(
                job_id=self.job_id,
                job_type=self.job_type,
                success=success,
                started_at=started_at,
                finished_at=finished_at,
                elapsed_seconds=(finished_at - started_at).total_seconds(),
                error=error_msg,
                details=details,
            )
            self._write_job_log(context, job_result)
            return job_result

        except Exception as e:
            finished_at = datetime.now()
            job_result = JobResult(
                job_id=self.job_id,
                job_type=self.job_type,
                success=False,
                started_at=started_at,
                finished_at=finished_at,
                elapsed_seconds=(finished_at - started_at).total_seconds(),
                error=str(e),
            )
            self._write_job_log(context, job_result)
            return job_result


@dataclass
class PatchUnitTestJob(Job):
    """Run unit tests only against a patched build.

    Uses PatchVerificationEngine with skip_pov=True to run only unit tests.
    This job depends on BuildPatchVariantJob and runs independently of POV tests.
    Unit tests verify functionality (no regressions), separate from security (POV).
    """

    benchmark_path: Path
    benchmark_name: str
    cpv_id: str
    patch_id: str
    harness: str
    test_mode: str = "FULL"  # FULL or RTS
    build_patch_job_id: str = ""  # Dependency on build job
    source_mode: str = "pkgs"

    @property
    def job_id(self) -> str:
        return (
            f"test-patch-unittest:{self.benchmark_name}:{self.cpv_id}"
            f":{self.patch_id}:{self.test_mode}"
        )

    @property
    def job_type(self) -> str:
        return "verify"

    @property
    def depends_on(self) -> list[str]:
        # Depend on build job directly (independent of POV test)
        return [self.build_patch_job_id] if self.build_patch_job_id else []

    def execute(self, context: JobContext) -> JobResult:
        """Run unit tests only using PatchVerificationEngine with skip_pov=True."""
        from crsbench.evaluation.verification.models import (
            PatchInfo,
            PatchVerificationStatus,
            UnitTestMode,
        )
        from crsbench.evaluation.verification.patch import PatchVerificationEngine
        from crsbench.utils.run_helper import get_oss_fuzz_root

        started_at = datetime.now()
        try:
            # Get build info directly from build job
            build_data = context.shared.get(self.build_patch_job_id, {})
            variant_name = build_data.get("variant_name")
            sanitizer = build_data.get("sanitizer", "address")
            inc_build_available = build_data.get("inc_build_available", False)

            if not variant_name:
                raise ValueError(f"No variant name from {self.build_patch_job_id}")

            oss_fuzz_path = Path(get_oss_fuzz_root())

            # Create engine with skip_pov=True - only run unit tests
            test_mode = (
                UnitTestMode.RTS if self.test_mode == "RTS" else UnitTestMode.FULL
            )
            engine = PatchVerificationEngine(
                oss_fuzz_path,
                test_mode=test_mode,
                sanitizer=sanitizer,
                timeout=context.timeout,
                use_inc_build=inc_build_available,
                force_rebuild=False,  # Use cached build
                source_mode=self.source_mode,
                build_only=False,
                verify_variants=False,  # Not needed for unit tests
                skip_pov=True,  # Skip POV, only run unit tests
            )

            # Find patch path from benchmark structure
            patch_dir = (
                self.benchmark_path / ".aixcc" / self.harness / self.cpv_id / "patches"
            )
            patch_path = patch_dir / f"{self.patch_id}.diff"
            if not patch_path.exists():
                patch_path = patch_dir / "patch.diff"

            if not patch_path.exists():
                raise ValueError(f"Patch file not found: {patch_path}")

            patch_info = PatchInfo(
                patch_id=self.patch_id,
                pov_id=self.cpv_id,
                patch_path=patch_path,
            )

            # We need a dummy POV path since verify_patch signature requires it
            # But skip_pov=True means it won't be used
            dummy_pov = (
                self.benchmark_path
                / ".aixcc"
                / self.harness
                / self.cpv_id
                / "blobs"
                / "pov_0.blob"
            )

            result = engine.verify_patch(
                benchmark_path=self.benchmark_path,
                patch=patch_info,
                harness=self.harness,
                pov_path=dummy_pov,
            )

            unit_tests_passed = result.unit_tests_passed
            success = unit_tests_passed is True

            error_msg = None
            if result.status == PatchVerificationStatus.TEST_FAILED:
                error_msg = result.details or "Unit tests failed"
            elif result.status == PatchVerificationStatus.ERROR:
                error_msg = result.details or "Unit test error"

            details: dict = {
                "cpv_id": self.cpv_id,
                "patch_id": self.patch_id,
                "test_mode": self.test_mode,
                "unit_tests_passed": unit_tests_passed,
                "unit_test_time": result.unit_test_time,
                "inc_build_available": inc_build_available,
            }

            finished_at = datetime.now()
            job_result = JobResult(
                job_id=self.job_id,
                job_type=self.job_type,
                success=success,
                started_at=started_at,
                finished_at=finished_at,
                elapsed_seconds=(finished_at - started_at).total_seconds(),
                error=error_msg,
                details=details,
            )
            self._write_job_log(context, job_result)
            return job_result

        except Exception as e:
            finished_at = datetime.now()
            job_result = JobResult(
                job_id=self.job_id,
                job_type=self.job_type,
                success=False,
                started_at=started_at,
                finished_at=finished_at,
                elapsed_seconds=(finished_at - started_at).total_seconds(),
                error=str(e),
            )
            self._write_job_log(context, job_result)
            return job_result


@dataclass
class FlatCollectCoverageJob(Job):
    """Collect coverage data for a benchmark.

    Runs after BuildVariantsJob completes. Uses the coverage variant
    from the build results.

    Note: CoverageEngine processes corpus files sequentially.
    Parallelism is controlled by DAGExecutor at the benchmark level.

    Supports both legacy build_job_id (single BuildVariantsJob) and
    new build_job_ids (list of BuildSingleVariantJob IDs).
    """

    benchmark_path: Path
    benchmark_name: str
    harness: str
    build_job_id: str = ""
    source_mode: str = "pkgs"
    build_job_ids: list[str] = field(default_factory=list)

    @property
    def job_id(self) -> str:
        return f"collect-coverage:{self.benchmark_name}"

    @property
    def job_type(self) -> str:
        return "verify"

    @property
    def depends_on(self) -> list[str]:
        # Support both legacy single build_job_id and new build_job_ids list
        if self.build_job_ids:
            return self.build_job_ids
        return [self.build_job_id] if self.build_job_id else []

    def execute(self, context: JobContext) -> JobResult:
        """Collect coverage using pre-built variants."""
        import shutil
        import tempfile

        from crsbench.evaluation.coverage import CoverageEngine

        started_at = datetime.now()
        temp_corpus_dir: Path | None = None
        try:
            from crsbench.utils.run_helper import get_oss_fuzz_root

            oss_fuzz_path = Path(get_oss_fuzz_root())
            engine = CoverageEngine(oss_fuzz_path, source_mode=self.source_mode)

            # Use adapter from shared build context for corpus discovery
            # Try build_job_ids first, then fall back to legacy build_job_id
            adapter = None
            if self.build_job_ids:
                for job_id in self.build_job_ids:
                    build_data = context.shared.get(job_id, {})
                    if build_data:
                        adapter = build_data.get("adapter")
                        if adapter:
                            break
            else:
                build_data = context.shared.get(self.build_job_id, {})
                adapter = build_data.get("adapter")

            corpus_dir = (
                adapter.get_corpus_dir(harness_name=self.harness) if adapter else None
            )

            # CI coverage uses artificial corpus to validate the pipeline
            if corpus_dir is None:
                temp_corpus_dir = Path(tempfile.mkdtemp(prefix="benchmark_ci_corpus_"))
                corpus_dir = temp_corpus_dir
                (temp_corpus_dir / "minimal_input").write_bytes(b"A")

            report = engine.collect_coverage(
                self.benchmark_path,
                corpus_dir,
                harness_filter=self.harness,
            )

            success = report.success

            finished_at = datetime.now()
            result = JobResult(
                job_id=self.job_id,
                job_type=self.job_type,
                success=success,
                started_at=started_at,
                finished_at=finished_at,
                elapsed_seconds=(finished_at - started_at).total_seconds(),
                details={
                    "benchmark_name": self.benchmark_name,
                    "harness": self.harness,
                },
            )
            self._write_job_log(context, result)
            return result
        except Exception as e:
            finished_at = datetime.now()
            result = JobResult(
                job_id=self.job_id,
                job_type=self.job_type,
                success=False,
                started_at=started_at,
                finished_at=finished_at,
                elapsed_seconds=(finished_at - started_at).total_seconds(),
                error=str(e),
            )
            self._write_job_log(context, result)
            return result
        finally:
            if temp_corpus_dir and temp_corpus_dir.exists():
                shutil.rmtree(temp_corpus_dir, ignore_errors=True)
