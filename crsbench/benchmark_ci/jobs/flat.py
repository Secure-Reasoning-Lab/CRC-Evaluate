"""Flat DAG jobs for per-CPV/per-patch atomic scheduling.

These jobs replace coarse validator wrappers (ci_checks.py) with fine-grained
nodes that call builder/infra directly. The DAGExecutor becomes the single
source of concurrency control via typed limits.

Job types:
- BuildVariantsJob: Build all variants for a benchmark (type="build")
- VerifyCpvPovJob: Verify POVs for a single CPV (type="verify")
- BuildPatchVariantJob: Build a patched variant (type="build")
- PatchVariantTestJob: Run POVs + unit tests on patch (type="verify")
- CollectCoverageJob: Collect coverage data (type="verify")
"""

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from crsbench.benchmark_ci.jobs.base import Job, JobContext, JobResult
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


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
    source_mode: str = "main_repo"

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
            adapter = engine._load_adapter(self.benchmark_path)
            if not adapter:
                raise ValueError(f"Failed to load adapter for {self.benchmark_path}")
            build_results = engine._get_or_build_results(
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
    """Verify POVs for a single CPV against built variants.

    Runs reproduce for each POV against each variant and resolves
    the verdict using the standard VerdictResolver.
    """

    benchmark_name: str
    cpv_id: str
    harness: str
    pov_paths: list[Path] = field(default_factory=list)
    build_job_id: str = ""
    source_mode: str = "main_repo"

    @property
    def job_id(self) -> str:
        return f"verify-cpv-pov:{self.benchmark_name}:{self.cpv_id}"

    @property
    def job_type(self) -> str:
        return "verify"

    @property
    def depends_on(self) -> list[str]:
        return [self.build_job_id] if self.build_job_id else []

    def execute(self, context: JobContext) -> JobResult:
        """Verify POVs for this CPV using pre-built variants."""
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
                    details={"cpv_id": self.cpv_id, "pov_count": 0},
                )
                self._write_job_log(context, result)
                return result

            from crsbench.evaluation.verification.pov import VerificationEngine
            from crsbench.utils.run_helper import get_oss_fuzz_root

            build_data = context.shared.get(self.build_job_id, {})
            build_results = build_data.get("build_results", {})
            adapter = build_data.get("adapter")

            if not build_results or not adapter:
                raise ValueError(f"No build data from {self.build_job_id}")

            oss_fuzz_path = Path(get_oss_fuzz_root())
            engine = VerificationEngine(
                oss_fuzz_path,
                source_mode=self.source_mode,
            )

            pov_harness_pairs = []
            for pov_path in self.pov_paths:
                pov_data = pov_path.read_bytes()
                pov_id = pov_path.stem
                pov_harness_pairs.append((pov_id, pov_data, self.harness))

            from crsbench.evaluation.verification.models import (
                PovVerificationStatus,
            )

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

            # Collect per-POV verdict info
            pov_verdicts = [
                {
                    "pov_id": r.pov_id or "unknown",
                    "status": r.status.value,
                    "cpv_matched": r.cpv_matched,
                }
                for r in results
            ]

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
                    "pov_count": len(pov_harness_pairs),
                    "variants_used": len(variants_used),
                    "variants": variants_used,
                    "verdicts": pov_verdicts,
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
    use_inc_build: bool = True
    force_rebuild: bool = False
    build_job_id: str = ""
    source_mode: str = "main_repo"

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
        """Build patched variant."""
        started_at = datetime.now()
        try:
            from crsbench.builder import OSSFuzzBuilder
            from crsbench.builder.types import BuildConfig, VariantType
            from crsbench.utils.run_helper import get_oss_fuzz_root

            oss_fuzz_path = Path(get_oss_fuzz_root())
            builder = OSSFuzzBuilder(
                oss_fuzz_path, max_workers=1, source_mode=self.source_mode
            )

            # Get adapter from parent build job's shared context
            build_data = context.shared.get(self.build_job_id, {})
            adapter = build_data.get("adapter")
            if not adapter:
                raise ValueError(f"No adapter from {self.build_job_id}")

            commit = adapter.get_ref_commit() or adapter.get_base_commit()
            build_config = BuildConfig(
                benchmark_name=self.benchmark_name,
                benchmark_path=self.benchmark_path,
                variant_type=VariantType.PATCHED,
                mode=adapter.get_mode(),
                sanitizer="address",
                language=adapter.lang,
                commit=commit,
                main_repo=adapter.main_repo,
                patch_id=self.patch_id,
                pov_id=self.cpv_id,
                use_inc_build=self.use_inc_build,
            )

            result = builder.build_single(
                build_config, force_rebuild=self.force_rebuild
            )

            variant_name = result.variant_name
            success = result.success

            context.shared[self.job_id] = {
                "variant_name": variant_name,
                "build_result": result,
            }

            finished_at = datetime.now()
            job_result = JobResult(
                job_id=self.job_id,
                job_type=self.job_type,
                success=success,
                started_at=started_at,
                finished_at=finished_at,
                elapsed_seconds=(finished_at - started_at).total_seconds(),
                error=None if success else (result.error or "Build failed"),
                details={
                    "variant_name": variant_name,
                    "cpv_id": self.cpv_id,
                    "patch_id": self.patch_id,
                },
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
class PatchVariantTestJob(Job):
    """Run POVs and unit tests against a patched build.

    Verifies that the patch fixes the vulnerability (POVs don't crash)
    and doesn't break functionality (tests pass).
    """

    benchmark_path: Path
    benchmark_name: str
    cpv_id: str
    patch_id: str
    harness: str
    pov_paths: list[Path] = field(default_factory=list)
    test_mode: str = "FULL"
    build_patch_job_id: str = ""

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

    def execute(self, context: JobContext) -> JobResult:
        """Run POVs and tests against patched variant."""
        started_at = datetime.now()
        try:
            build_data = context.shared.get(self.build_patch_job_id, {})
            variant_name = build_data.get("variant_name")

            if not variant_name:
                raise ValueError(f"No variant name from {self.build_patch_job_id}")

            failed_povs: list[str] = []
            passed_povs: list[str] = []

            if context.infra:
                for pov_path in self.pov_paths:
                    pov_data = pov_path.read_bytes()
                    pov_id = pov_path.stem
                    output = context.infra.reproduce(
                        project_name=variant_name,
                        harness=self.harness,
                        pov_data=pov_data,
                        timeout=context.timeout,
                        pov_id=pov_id,
                    )
                    if output.crashed:
                        failed_povs.append(pov_id)
                    else:
                        passed_povs.append(pov_id)

            # Run unit tests if infra supports it
            test_passed = True
            if context.infra and hasattr(context.infra, "run_tests"):
                rts_mode = self.test_mode == "RTS"
                passed, _stdout, _stderr = context.infra.run_tests(
                    variant_name,
                    self.benchmark_path,
                    rts_mode=rts_mode,
                )
                test_passed = passed

            success = len(failed_povs) == 0 and test_passed

            finished_at = datetime.now()
            result = JobResult(
                job_id=self.job_id,
                job_type=self.job_type,
                success=success,
                started_at=started_at,
                finished_at=finished_at,
                elapsed_seconds=(finished_at - started_at).total_seconds(),
                error=(
                    f"POVs still crash: {failed_povs}"
                    if failed_povs
                    else ("Tests failed" if not test_passed else None)
                ),
                details={
                    "cpv_id": self.cpv_id,
                    "patch_id": self.patch_id,
                    "test_mode": self.test_mode,
                    "total_povs": len(self.pov_paths),
                    "fixed": len(passed_povs),
                    "failed": failed_povs,
                    "test_passed": test_passed,
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
class FlatCollectCoverageJob(Job):
    """Collect coverage data for a benchmark.

    Runs after BuildVariantsJob completes. Uses the coverage variant
    from the build results.

    Note: CoverageEngine processes corpus files sequentially.
    Parallelism is controlled by DAGExecutor at the benchmark level.
    """

    benchmark_path: Path
    benchmark_name: str
    harness: str
    build_job_id: str = ""

    @property
    def job_id(self) -> str:
        return f"collect-coverage:{self.benchmark_name}"

    @property
    def job_type(self) -> str:
        return "verify"

    @property
    def depends_on(self) -> list[str]:
        return [self.build_job_id] if self.build_job_id else []

    def execute(self, context: JobContext) -> JobResult:
        """Collect coverage using pre-built variants."""
        started_at = datetime.now()
        temp_corpus_dir: Path | None = None
        try:
            import shutil
            import tempfile

            from crsbench.evaluation.coverage import CoverageEngine
            from crsbench.utils.run_helper import get_oss_fuzz_root

            oss_fuzz_path = Path(get_oss_fuzz_root())
            engine = CoverageEngine(oss_fuzz_path)

            # Use adapter from shared build context for corpus discovery
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

            success = bool(report.harness_name)

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
