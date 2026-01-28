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
    patches: list[Path] = field(default_factory=list)
    use_inc_build: bool = True
    force_rebuild: bool = False
    source_mode: str = "main_repo"
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
                use_inc_build=self.use_inc_build,
                sanitizer=self.sanitizer,
                repo_name=self.repo_name,
            )

            result = builder.build_single(config, force_rebuild=self.force_rebuild)

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
    source_mode: str = "main_repo"
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
    """Verify POVs for a single CPV against built variants.

    Runs reproduce for each POV against each variant and resolves
    the verdict using the standard VerdictResolver.

    Supports both legacy build_job_id (single BuildVariantsJob) and
    new build_job_ids (list of BuildSingleVariantJob IDs).
    """

    benchmark_name: str
    cpv_id: str
    harness: str
    benchmark_path: Optional[Path] = None
    pov_paths: list[Path] = field(default_factory=list)
    build_job_id: str = ""
    build_job_ids: list[str] = field(default_factory=list)
    source_mode: str = "main_repo"

    @property
    def job_id(self) -> str:
        return f"verify-cpv-pov:{self.benchmark_name}:{self.cpv_id}"

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

            oss_fuzz_path = Path(get_oss_fuzz_root())

            # Collect build results from multiple build jobs or single legacy job
            build_results: dict = {}
            adapter = None

            if self.build_job_ids:
                # New mode: collect from multiple BuildSingleVariantJob results
                for job_id in self.build_job_ids:
                    build_data = context.shared.get(job_id, {})
                    if build_data:
                        build_result = build_data.get("build_result")
                        if build_result:
                            build_results[build_result.variant_name] = build_result
                        if not adapter:
                            adapter = build_data.get("adapter")
            else:
                # Legacy mode: single BuildVariantsJob
                build_data = context.shared.get(self.build_job_id, {})
                build_results = build_data.get("build_results", {})
                adapter = build_data.get("adapter")

            # If adapter not in context.shared, load via VerificationEngine
            if not adapter and self.benchmark_path:
                engine = VerificationEngine(
                    oss_fuzz_path,
                    source_mode=self.source_mode,
                )
                adapter = engine._load_adapter(self.benchmark_path)

            if not build_results or not adapter:
                deps = self.build_job_ids or [self.build_job_id]
                raise ValueError(f"No build data from {deps}")

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
    harness: str = ""  # Harness name for per-harness sanitizer
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

            # Get adapter from parent build job's shared context, or load directly
            build_data = context.shared.get(self.build_job_id, {})
            adapter = build_data.get("adapter")

            # If adapter not in context.shared (BuildSingleVariantJob case), load directly
            if not adapter:
                from crsbench.evaluation.verification.pov import VerificationEngine

                engine = VerificationEngine(oss_fuzz_path, source_mode=self.source_mode)
                adapter = engine._load_adapter(self.benchmark_path)

            if not adapter:
                raise ValueError(f"Failed to load adapter for {self.benchmark_path}")

            commit = adapter.get_ref_commit() or adapter.get_base_commit()

            # Get sanitizer for this specific CPV (supports mixed sanitizers within harness)
            if not self.harness or not self.cpv_id:
                raise ValueError(
                    f"VerifyCpvPovJob requires both harness and cpv_id: "
                    f"harness={self.harness}, cpv_id={self.cpv_id}"
                )

            sanitizer = adapter.get_cpv_sanitizer(self.harness, self.cpv_id)

            # PATCHED variants can use inc-build:
            # - Base inc-build image (benchmark_name) is retagged to variant_name
            # - Patches are applied to the inc-build source
            build_config = BuildConfig(
                benchmark_name=self.benchmark_name,
                benchmark_path=self.benchmark_path,
                variant_type=VariantType.PATCHED,
                mode=adapter.get_mode(),
                sanitizer=sanitizer,
                language=adapter.lang,
                commit=commit,
                main_repo=adapter.main_repo,
                patch_id=self.patch_id,
                pov_id=self.cpv_id,
                patches=[self.patch_path],
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

    Supports both legacy build_job_id (single BuildVariantsJob) and
    new build_job_ids (list of BuildSingleVariantJob IDs).
    """

    benchmark_path: Path
    benchmark_name: str
    harness: str
    build_job_id: str = ""
    source_mode: str = "main_repo"
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
