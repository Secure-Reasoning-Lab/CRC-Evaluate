"""OSSFuzzBuilder - Unified builder for OSS-Fuzz project variants.

This module provides OSSFuzzBuilder, which builds all types of variants:
- Validation variants: deltabase, deltaref, allpatched, cpvN
- Coverage variants: coverage-instrumented builds

Features:
- Parallel builds using ThreadPoolExecutor
- Build caching with staleness detection
- Support for both FULL and DELTA benchmark modes
"""

import tempfile
import time
from pathlib import Path
from typing import Optional

from crsbench.builder.executor import ParallelExecutor
from crsbench.builder.infrastructure import OSSFuzzInfrastructure
from crsbench.builder.types import (
    BenchmarkMode,
    BuildConfig,
    BuildPlan,
    BuildResult,
    VariantType,
)
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


class OSSFuzzBuilder:
    """Unified builder for OSS-Fuzz project variants.

    Builds validation and coverage variants for benchmarks:
    - Validation variants (FULL_BASE, DELTA_BASE, DELTA_REF, ALL_PATCHED, CPV)
    - Coverage variant (COVERAGE)

    Supports parallel builds with configurable worker count.

    Attributes:
        oss_fuzz_path: Path to oss-fuzz directory
        max_workers: Maximum number of parallel workers
    """

    def __init__(
        self,
        oss_fuzz_path: Path,
        max_workers: int = 4,
    ):
        """Initialize the builder.

        Args:
            oss_fuzz_path: Path to oss-fuzz directory
            max_workers: Maximum number of parallel workers (default: 4)
        """
        self.oss_fuzz_path = Path(oss_fuzz_path).resolve()
        self.max_workers = max_workers
        self.infra = OSSFuzzInfrastructure(oss_fuzz_path)
        self.executor = ParallelExecutor(max_workers)

    def build_variants(
        self,
        configs: list[BuildConfig],
        *,
        force_rebuild: bool = False,
    ) -> dict[str, BuildResult]:
        """Build multiple variants in parallel.

        Args:
            configs: List of build configurations
            force_rebuild: Force rebuild even if cached

        Returns:
            Dict mapping variant names to their build results
        """
        if not configs:
            return {}

        # Filter out cached variants unless force_rebuild
        configs_to_build = []
        results: dict[str, BuildResult] = {}

        for config in configs:
            if not force_rebuild and self.infra.is_variant_built(config.variant_name):
                logger.info(f"Using cached build for {config.variant_name}")
                build_path = self.infra.get_build_output_path(config.variant_name)
                results[config.variant_name] = BuildResult.from_cache(
                    config, build_path
                )
            else:
                configs_to_build.append(config)

        # Build remaining variants in parallel
        if configs_to_build:
            build_results = self.executor.execute_builds(
                configs=configs_to_build,
                build_fn=self._build_single,
            )
            results.update(build_results)

        return results

    def build_single(
        self,
        config: BuildConfig,
        *,
        force_rebuild: bool = False,
    ) -> BuildResult:
        """Build a single variant.

        Args:
            config: Build configuration
            force_rebuild: Force rebuild even if cached

        Returns:
            Build result
        """
        # Check cache
        if not force_rebuild and self.infra.is_variant_built(config.variant_name):
            logger.info(f"Using cached build for {config.variant_name}")
            build_path = self.infra.get_build_output_path(config.variant_name)
            return BuildResult.from_cache(config, build_path)

        return self.executor.execute_single(
            config=config,
            build_fn=self._build_single,
        )

    def _build_single(self, config: BuildConfig) -> BuildResult:
        """Internal method to build a single variant.

        Args:
            config: Build configuration

        Returns:
            Build result
        """
        start_time = time.time()
        variant_name = config.variant_name

        # Create variant project directory
        variant_project_path = self.infra.create_variant_project(
            benchmark_path=config.benchmark_path,
            variant_name=variant_name,
        )
        if not variant_project_path:
            return BuildResult.from_error(
                config=config,
                error="Failed to create variant project directory",
                elapsed_seconds=time.time() - start_time,
            )

        # Clone and checkout source
        with tempfile.TemporaryDirectory() as temp_dir:
            try:
                repo_path = self.infra.clone_source(config, Path(temp_dir))
                if not repo_path:
                    return BuildResult.from_error(
                        config=config,
                        error="Failed to clone source repository",
                        elapsed_seconds=time.time() - start_time,
                    )

                # Apply patches for validation variants (not coverage)
                if config.variant_type.is_validation_variant():
                    self._apply_patches_for_variant(config, repo_path)

                # Build fuzzers
                if not self.infra.build_fuzzers(config, repo_path):
                    return BuildResult.from_error(
                        config=config,
                        error="Build failed",
                        elapsed_seconds=time.time() - start_time,
                    )

                # Success
                build_path = self.infra.get_build_output_path(variant_name)
                return BuildResult(
                    config=config,
                    success=True,
                    variant_name=variant_name,
                    build_path=build_path,
                    elapsed_seconds=time.time() - start_time,
                )

            except Exception as e:
                return BuildResult.from_error(
                    config=config,
                    error=str(e),
                    elapsed_seconds=time.time() - start_time,
                )

    def _apply_patches_for_variant(
        self,
        config: BuildConfig,
        repo_path: Path,
    ) -> None:
        """Apply patches from config.patches.

        The patches are pre-resolved in create_build_plan(), so this method
        simply iterates over config.patches and applies each one.

        Args:
            config: Build configuration (patches already resolved)
            repo_path: Path to cloned repository
        """
        if not config.patches:
            logger.debug(f"No patches to apply for {config.variant_name}")
            return

        logger.debug(
            f"Applying {len(config.patches)} patches for {config.variant_name}"
        )
        self.infra.apply_patches_from_list(repo_path, config.patches)

    def create_build_plan(
        self,
        benchmark_name: str,
        benchmark_path: Path,
        main_repo: str,
        mode: BenchmarkMode,
        base_commit: str,
        ref_commit: Optional[str],
        cpv_numbers: list[int],
        language: str = "c",
        repo_name: Optional[str] = None,
        *,
        include_coverage: bool = False,
    ) -> BuildPlan:
        """Create a build plan for a benchmark.

        Patches are resolved upfront and stored in each BuildConfig.
        This makes the builder logic simple: just apply config.patches.

        Args:
            benchmark_name: Name of the benchmark
            benchmark_path: Path to benchmark directory
            main_repo: Main repository URL
            mode: FULL or DELTA mode
            base_commit: Base commit hash
            ref_commit: Reference commit hash (delta mode only)
            cpv_numbers: List of CPV numbers
            language: Programming language
            repo_name: Optional repository name for caching
            include_coverage: Whether to include coverage variant

        Returns:
            BuildPlan with all required configurations
        """
        plan = BuildPlan(benchmark_name=benchmark_name)

        # Get all CPV patches upfront
        all_patches = self.infra.get_all_patches(benchmark_path)

        # Base version (no patches)
        base_type = (
            VariantType.FULL_BASE
            if mode == BenchmarkMode.FULL
            else VariantType.DELTA_BASE
        )
        plan.add_config(
            BuildConfig(
                benchmark_name=benchmark_name,
                variant_type=base_type,
                commit=base_commit,
                main_repo=main_repo,
                benchmark_path=benchmark_path,
                mode=mode,
                patches=[],  # Base: no patches
                language=language,
                repo_name=repo_name,
            )
        )

        # Reference version (delta mode only, no patches)
        if mode == BenchmarkMode.DELTA and ref_commit:
            plan.add_config(
                BuildConfig(
                    benchmark_name=benchmark_name,
                    variant_type=VariantType.DELTA_REF,
                    commit=ref_commit,
                    main_repo=main_repo,
                    benchmark_path=benchmark_path,
                    mode=mode,
                    patches=[],  # Ref: no patches
                    language=language,
                    repo_name=repo_name,
                )
            )

        # All-patched version (all patches applied)
        patched_commit = ref_commit if mode == BenchmarkMode.DELTA else base_commit
        if patched_commit:
            plan.add_config(
                BuildConfig(
                    benchmark_name=benchmark_name,
                    variant_type=VariantType.ALL_PATCHED,
                    commit=patched_commit,
                    main_repo=main_repo,
                    benchmark_path=benchmark_path,
                    mode=mode,
                    patches=all_patches,  # All patches
                    language=language,
                    repo_name=repo_name,
                )
            )

        # CPV variants (all patches except one)
        for cpv_num in cpv_numbers:
            cpv_patches = self.infra.get_patches_except(benchmark_path, cpv_num)
            plan.add_config(
                BuildConfig(
                    benchmark_name=benchmark_name,
                    variant_type=VariantType.CPV,
                    commit=patched_commit or base_commit,
                    main_repo=main_repo,
                    benchmark_path=benchmark_path,
                    mode=mode,
                    patches=cpv_patches,  # All patches except this CPV
                    language=language,
                    cpv_num=cpv_num,
                    repo_name=repo_name,
                )
            )

        # Coverage variant (no patches, different sanitizer)
        if include_coverage:
            plan.add_config(
                BuildConfig(
                    benchmark_name=benchmark_name,
                    variant_type=VariantType.COVERAGE,
                    commit=patched_commit or base_commit,
                    main_repo=main_repo,
                    benchmark_path=benchmark_path,
                    mode=mode,
                    patches=[],  # Coverage: no patches
                    language=language,
                    repo_name=repo_name,
                )
            )

        # Check which variants are already cached
        for config in plan.configs:
            if self.infra.is_variant_built(config.variant_name):
                plan.mark_cached(config.variant_name)

        logger.info(
            f"Build plan for {benchmark_name}: "
            f"{plan.total_count} variants, {plan.cached_count} cached, "
            f"{plan.build_count} to build"
        )

        return plan

    def execute_plan(
        self,
        plan: BuildPlan,
        *,
        force_rebuild: bool = False,
    ) -> dict[str, BuildResult]:
        """Execute a build plan.

        Args:
            plan: Build plan to execute
            force_rebuild: Force rebuild even if cached

        Returns:
            Dict mapping variant names to their build results
        """
        return self.build_variants(plan.configs, force_rebuild=force_rebuild)

    def cleanup_variants(self, benchmark_name: str) -> None:
        """Remove all variant projects for a benchmark.

        Args:
            benchmark_name: Name of the benchmark
        """
        prefix = f"{benchmark_name}-"
        for variant_dir in self.infra.projects_base.iterdir():
            if variant_dir.is_dir() and variant_dir.name.startswith(prefix):
                self.infra.cleanup_variant(variant_dir.name)

    def is_variant_built(self, variant_name: str) -> bool:
        """Check if a variant has been built.

        Args:
            variant_name: Variant name

        Returns:
            True if built
        """
        return self.infra.is_variant_built(variant_name)
