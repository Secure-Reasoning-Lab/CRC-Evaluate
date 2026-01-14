"""Base class for job executors."""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

import yaml

from crsbench.benchmark_ci.models import (
    JobContext,
    JobResult,
    get_benchmarks_root,
)
from crsbench.builder import BuildResult, OSSFuzzBuilder, OSSFuzzInfrastructure
from crsbench.builder.types import BenchmarkMode, BuildConfig, VariantType
from crsbench.evaluation.coverage import CoverageEngine
from crsbench.evaluation.verification.patch import PatchVerificationEngine
from crsbench.evaluation.verification.pov import VerificationEngine
from crsbench.utils.logger import get_logger
from crsbench.utils.run_helper import get_oss_fuzz_root
from crsbench.validation.meta_adapter import MetaYamlAdapter
from crsbench.validation.schemas import ProjectConfig

logger = get_logger(__name__)


class JobExecutor(ABC):
    """Abstract base class for job executors.

    Follows the pattern from crsbench.evaluation.crs_executor.CRSExecutor.
    Each concrete executor implements a specific type of CI test job.
    """

    @abstractmethod
    def execute(
        self,
        job: JobContext,
        *,
        use_inc_build: bool = False,
    ) -> JobResult:
        """Execute the job.

        Args:
            job: Job context describing what to test
            use_inc_build: Use inc-build image for faster builds

        Returns:
            JobResult with success status and optional metrics
        """

    def _get_infra(self) -> OSSFuzzInfrastructure:
        """Get or create OSSFuzzInfrastructure instance.

        Returns:
            OSSFuzzInfrastructure instance
        """
        if not hasattr(self, "_infra"):
            self._infra = OSSFuzzInfrastructure(Path(get_oss_fuzz_root()))
        return self._infra

    def _get_builder(self) -> OSSFuzzBuilder:
        """Get or create OSSFuzzBuilder instance.

        Returns:
            OSSFuzzBuilder instance
        """
        if not hasattr(self, "_builder"):
            self._builder = OSSFuzzBuilder(Path(get_oss_fuzz_root()))
        return self._builder

    def _build_variant(
        self,
        benchmark: str,
        variant_type: VariantType,
        commit: str,
        sanitizer: str = "address",
        *,
        use_inc_build: bool = False,
        force_rebuild: bool = False,
    ) -> BuildResult:
        """Build a variant using OSSFuzzBuilder.

        This is the high-level build API that delegates to OSSFuzzBuilder,
        which handles inc-build image preparation, caching, and all build logic.

        Args:
            benchmark: Benchmark name
            variant_type: Type of variant to build (e.g., DELTA_BASE, DELTA_REF)
            commit: Git commit to checkout
            sanitizer: Sanitizer type (e.g., "address")
            use_inc_build: Use incremental build if available
            force_rebuild: Force rebuild even if cached

        Returns:
            BuildResult with success status and variant information
        """
        project_config = self._load_project_config(benchmark)
        benchmark_path = self._get_benchmark_path(benchmark)

        # Validate required config
        if not project_config.main_repo:
            raise RuntimeError(f"main_repo not configured for {benchmark}")

        # Determine mode from variant_type
        if variant_type in (VariantType.DELTA_BASE, VariantType.DELTA_REF):
            mode = BenchmarkMode.DELTA
        else:
            mode = BenchmarkMode.FULL

        config = BuildConfig(
            benchmark_name=benchmark,
            variant_type=variant_type,
            commit=commit,
            main_repo=project_config.main_repo,
            benchmark_path=benchmark_path,
            mode=mode,
            patches=[],
            language=project_config.language,
            repo_name=benchmark,
            sanitizer=sanitizer,
            use_inc_build=use_inc_build,
        )

        builder = self._get_builder()
        return builder.build_single(config, force_rebuild=force_rebuild)

    def _get_pov_engine(
        self,
        *,
        timeout: int = 120,
        build_workers: int = 4,
        verify_workers: int = 4,
    ) -> VerificationEngine:
        """Create a POV verification engine.

        Args:
            timeout: Timeout for POV verification in seconds
            build_workers: Number of parallel build workers
            verify_workers: Number of parallel verification workers

        Returns:
            Configured VerificationEngine instance
        """
        return VerificationEngine(
            oss_fuzz_path=Path(get_oss_fuzz_root()),
            timeout=timeout,
            build_workers=build_workers,
            verify_workers=verify_workers,
        )

    def _get_patch_engine(
        self,
        work_dir: Optional[Path] = None,
        *,
        timeout: int = 120,
        build_timeout: int = 1200,
        test_timeout: int = 1800,
        sanitizer: str = "address",
        use_inc_build: bool = True,
    ) -> PatchVerificationEngine:
        """Create a patch verification engine.

        Args:
            work_dir: Working directory for patch verification
            timeout: Timeout for POV verification in seconds
            build_timeout: Timeout for build in seconds
            test_timeout: Timeout for test execution in seconds
            sanitizer: Sanitizer to use
            use_inc_build: Use incremental build if available

        Returns:
            Configured PatchVerificationEngine instance
        """
        return PatchVerificationEngine(
            oss_fuzz_path=Path(get_oss_fuzz_root()),
            timeout=timeout,
            build_timeout=build_timeout,
            test_timeout=test_timeout,
            sanitizer=sanitizer,
            work_dir=work_dir,
            use_inc_build=use_inc_build,
        )

    def _get_coverage_engine(
        self,
        work_dir: Optional[Path] = None,
        *,
        build_workers: int = 4,
        verify_workers: int = 4,
    ) -> CoverageEngine:
        """Create a coverage engine.

        Args:
            work_dir: Working directory for coverage collection
            build_workers: Number of parallel build workers
            verify_workers: Number of parallel verification workers

        Returns:
            Configured CoverageEngine instance
        """
        return CoverageEngine(
            oss_fuzz_path=Path(get_oss_fuzz_root()),
            build_workers=build_workers,
            verify_workers=verify_workers,
            work_dir=work_dir,
        )

    def _get_benchmark_path(self, benchmark: str) -> Path:
        """Get the path to a benchmark directory."""
        return Path(get_benchmarks_root()) / benchmark

    def _load_project_config(self, benchmark: str) -> ProjectConfig:
        """Load project configuration from project.yaml."""
        benchmark_path = Path(get_benchmarks_root()) / benchmark
        project_yaml = benchmark_path / "project.yaml"

        if not project_yaml.exists():
            raise ValueError(f"project.yaml not found for {benchmark}")

        with project_yaml.open() as f:
            data = yaml.safe_load(f) or {}

        return ProjectConfig(**data)

    def _get_adapter(self, benchmark: str) -> MetaYamlAdapter:
        """Get MetaYamlAdapter for a benchmark.

        Args:
            benchmark: Benchmark name

        Returns:
            MetaYamlAdapter instance

        Raises:
            ValueError: If adapter cannot be loaded
        """
        benchmark_path = self._get_benchmark_path(benchmark)
        adapter = MetaYamlAdapter.from_benchmark_path(benchmark_path)
        if not adapter:
            raise ValueError(f"Failed to load MetaYamlAdapter for {benchmark}")
        return adapter
