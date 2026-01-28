"""OSSFuzzBuilder - Unified builder for OSS-Fuzz project variants.

This module provides OSSFuzzBuilder, which builds all types of variants:
- Validation variants: deltabase, deltaref, allpatched, cpvN
- Coverage variants: coverage-instrumented builds
- Patch variants: CRS-generated patches for verification

Features:
- Parallel builds using ThreadPoolExecutor
- Build caching with staleness detection
- Support for both FULL and DELTA benchmark modes
- Incremental builds using pre-built images (for patch verification)
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
from crsbench.utils.repo_manager import clone_or_copy_cached_repo

logger = get_logger(__name__)


class OSSFuzzBuilder:
    """Unified builder for OSS-Fuzz project variants.

    Builds validation, coverage, and patch variants for benchmarks:
    - Validation variants (FULL_BASE, DELTA_REF, ALL_PATCHED, CPV)
    - Coverage variant (COVERAGE)
    - Patch variant (PATCHED) - CRS-generated patches for verification

    Supports parallel builds with configurable worker count.

    Attributes:
        oss_fuzz_path: Path to oss-fuzz directory
        max_workers: Maximum number of parallel workers
    """

    def __init__(
        self,
        oss_fuzz_path: Path,
        max_workers: int = 4,
        *,
        source_mode: str = "pkgs",
    ):
        """Initialize the builder.

        Args:
            oss_fuzz_path: Path to oss-fuzz directory
            max_workers: Maximum number of parallel workers (default: 4)
            source_mode: Source mode - "pkgs" (bundled, default) or "main_repo" (clone)
        """
        self.oss_fuzz_path = Path(oss_fuzz_path).resolve()
        self.max_workers = max_workers
        self.source_mode = source_mode
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

        # Clean up and filter cached variants
        configs_to_build = []
        results: dict[str, BuildResult] = {}

        for config in configs:
            if force_rebuild:
                # Clean up existing build outputs for force rebuild
                logger.debug(f"Force rebuild: cleaning {config.variant_name}")
                self.infra.cleanup_build_outputs(config.variant_name)
                self.infra.cleanup_source(config.variant_name)
                configs_to_build.append(config)
            elif self.infra.is_variant_built(
                config.variant_name,
                require_inc_build=config.use_inc_build,
            ):
                logger.debug(f"Using cached build for {config.variant_name}")
                build_path = self.infra.get_build_output_path(config.variant_name)
                results[config.variant_name] = BuildResult.from_cache(
                    config, build_path
                )
            else:
                configs_to_build.append(config)

        # Build remaining variants in parallel
        if configs_to_build:
            # Pre-cache repositories before parallel execution
            # This prevents multiple workers from cloning the same repo concurrently
            self._ensure_repos_cached(configs_to_build)

            build_results = self.executor.execute_builds(
                configs=configs_to_build,
                build_fn=self._build_single,
            )
            results.update(build_results)

        return results

    def _ensure_repos_cached(self, configs: list[BuildConfig]) -> None:
        """Ensure all unique repositories are cached before parallel builds.

        Args:
            configs: Build configurations to process
        """
        # Skip caching in pkgs mode - bundled source doesn't need git repos
        if self.source_mode == "pkgs":
            logger.debug("Using pkgs source mode, skipping repo cache")
            return

        # main_repo mode - cache all unique repos
        unique_repos: dict[str, BuildConfig] = {}
        for config in configs:
            cache_key = f"{config.main_repo}:{config.commit}"
            if cache_key not in unique_repos:
                unique_repos[cache_key] = config

        if not unique_repos:
            return

        logger.info(f"Pre-caching {len(unique_repos)} unique repository(s)")
        for config in unique_repos.values():
            with tempfile.TemporaryDirectory(prefix="repo-cache-") as temp_dir:
                target = str(Path(temp_dir) / "repo")
                clone_or_copy_cached_repo(
                    repo_url=config.main_repo,
                    commit=config.commit,
                    target_dir=target,
                    repo_name=config.repo_name,
                    verbose=True,
                )

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
        # Clean up existing build outputs if force rebuild
        # This ensures stale data (coverage dumps, etc.) is removed
        if force_rebuild:
            logger.info(f"Force rebuild: cleaning {config.variant_name}")
            self.infra.cleanup_build_outputs(config.variant_name)
            self.infra.cleanup_source(config.variant_name)

        # Check cache (skip if force_rebuild to ensure rebuild even if cleanup fails)
        if not force_rebuild and self.infra.is_variant_built(
            config.variant_name,
            require_inc_build=config.use_inc_build,
        ):
            logger.debug(f"Using cached build for {config.variant_name}")
            build_path = self.infra.get_build_output_path(config.variant_name)
            return BuildResult.from_cache(config, build_path)

        return self.executor.execute_single(
            config=config,
            build_fn=self._build_single,
        )

    def _build_single(self, config: BuildConfig) -> BuildResult:
        """Internal method to build a single variant.

        For variants with use_inc_build=True (and that support inc-build),
        uses incremental builds via pre-built Docker images for faster builds.

        Supported variant types for inc-build:
        - PATCHED: CRS-generated patches for verification
        - Validation variants: DELTA_REF, ALL_PATCHED, CPV, FULL_BASE
        - NOT supported: COVERAGE (requires different instrumentation)

        Falls back to standard build if inc-build image is not available.

        Args:
            config: Build configuration

        Returns:
            Build result
        """
        start_time = time.time()
        fallback_from_inc = False

        # Use inc-build path for supported variants when enabled and image available
        if config.variant_type.supports_inc_build() and config.use_inc_build:
            # Check if inc-build image is available (pull if needed)
            if self.infra.ensure_inc_image(config.benchmark_name, config.sanitizer):
                return self._build_with_inc_image(config, start_time)
            # Fall back to standard build if inc-build image not available
            # Mark as fallback so PASS-FB signals "prepare the inc-build image"
            msg = (
                f"Inc-build image not available for {config.benchmark_name}, "
                f"using standard build for {config.variant_name}"
            )
            if config.variant_type.is_inc_build_target():
                logger.warning(msg)
            else:
                logger.debug(msg)
            fallback_from_inc = True

        # Standard build path for coverage, non-inc variants, and fallback
        return self._build_standard(
            config, start_time, fallback_from_inc=fallback_from_inc
        )

    def _build_standard(
        self,
        config: BuildConfig,
        start_time: float,
        *,
        fallback_from_inc: bool = False,
    ) -> BuildResult:
        """Build a variant using standard OSS-Fuzz build process.

        Supports two source modes:
        1. Bundled source (pkgs/): Extract tarball, apply ref.diff if needed
        2. Git clone: Clone from main_repo and checkout commit

        Args:
            config: Build configuration
            start_time: Build start time
            fallback_from_inc: Whether this is a fallback from inc-build
                (inc-build was requested but image was unavailable)

        Returns:
            Build result
        """
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

        # Prepare source (from pkgs/ or git clone)
        with tempfile.TemporaryDirectory() as temp_dir:
            try:
                repo_path = self._prepare_source(config, Path(temp_dir))
                if not repo_path:
                    return BuildResult.from_error(
                        config=config,
                        error="Failed to prepare source",
                        elapsed_seconds=time.time() - start_time,
                    )

                # Apply patches for validation variants (not coverage)
                if config.variant_type.is_validation_variant():
                    self._apply_patches_for_variant(config, repo_path)

                # Apply patches for patch verification variants
                if config.variant_type.is_patch_variant():
                    self._apply_patches_for_variant(config, repo_path)

                # Build fuzzers
                build_result = self.infra.build_fuzzers(config, repo_path)
                if not build_result.success:
                    return BuildResult.from_error(
                        config=config,
                        error="Build failed",
                        elapsed_seconds=time.time() - start_time,
                    )

                # Success - write metadata and return
                build_path = self.infra.get_build_output_path(variant_name)
                self.infra.write_build_metadata(
                    variant_name,
                    inc_build=False,
                    sanitizer=config.sanitizer,
                )
                return BuildResult(
                    config=config,
                    success=True,
                    variant_name=variant_name,
                    build_path=build_path,
                    elapsed_seconds=time.time() - start_time,
                    fallback_used=fallback_from_inc,
                )

            except Exception as e:
                return BuildResult.from_error(
                    config=config,
                    error=str(e),
                    elapsed_seconds=time.time() - start_time,
                )

    def _prepare_source(self, config: BuildConfig, temp_dir: Path) -> Optional[Path]:
        """Prepare source for building - from pkgs/ or git clone.

        Source mode determines how source is obtained:
        - "main_repo": Clone from main_repo in project.yaml (default)
        - "pkgs": Use bundled tarballs from pkgs/ (requires pkgs/)

        Args:
            config: Build configuration
            temp_dir: Temporary directory for source extraction

        Returns:
            Path to prepared source, or None on failure

        Raises:
            RuntimeError: If source_mode is "pkgs" but no pkgs/ exists
        """
        from crsbench.benchmark.runtime import (
            has_bundled_source,
            prepare_source_from_bundle,
        )

        benchmark_path = config.benchmark_path

        # Handle main_repo mode - always clone from git
        if self.source_mode == "main_repo":
            logger.debug("Using main_repo source (cloning from git)")
            return self.infra.clone_source(config, temp_dir)

        # pkgs mode (default) - require bundled source
        if not has_bundled_source(benchmark_path):
            raise RuntimeError(
                f"No bundled source (pkgs/) found for {benchmark_path.name}. "
                "Run 'crsbench benchmark bundle' first, or use --source main_repo."
            )

        # Get source name from Dockerfile WORKDIR
        from crsbench.benchmark.packaging import get_expected_source_dir

        dockerfile = benchmark_path / "Dockerfile"
        source_name = get_expected_source_dir(dockerfile)
        if not source_name:
            # Fall back to repo_name or benchmark name
            source_name = config.repo_name or benchmark_path.name
            logger.debug(f"No WORKDIR found, using: {source_name}")

        # Bundled tarball already has correct commit structure (done at packaging):
        # - Both modes: 1 squashed commit at vulnerable state
        # - Delta mode provides ref.diff as hint (not via git history)
        # Just extract and use - no post-processing needed.
        logger.debug(f"Using bundled source: {source_name}.tar.gz")
        return prepare_source_from_bundle(
            benchmark_path,
            temp_dir,
            source_name,
        )

    def _build_with_inc_image(
        self, config: BuildConfig, start_time: float
    ) -> BuildResult:
        """Build a variant using incremental build image for faster builds.

        Uses pre-built Docker images that contain compiled dependencies,
        allowing faster incremental builds when only the source changes.

        Supports bundled source (pkgs/) and git clone.

        Args:
            config: Build configuration
            start_time: Build start time

        Returns:
            Build result
        """
        variant_name = config.variant_name

        # Prepare source (from pkgs/ or git clone)
        with tempfile.TemporaryDirectory() as temp_dir:
            try:
                repo_path = self._prepare_source(config, Path(temp_dir))
                if not repo_path:
                    return BuildResult.from_error(
                        config=config,
                        error="Failed to prepare source",
                        elapsed_seconds=time.time() - start_time,
                    )

                # Apply CRS-generated patches
                if config.patches:
                    self._apply_patches_for_variant(config, repo_path)

                # Create variant project for build
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

                # Prepare variant inc-build image (retag from project to variant)
                if not self.infra.prepare_inc_image_for_variant(
                    config.benchmark_name, variant_name, config.sanitizer
                ):
                    return BuildResult.from_error(
                        config=config,
                        error="Failed to prepare inc-build image for variant",
                        elapsed_seconds=time.time() - start_time,
                    )

                # Try inc-build first (uses pre-compiled objects from /built-src)
                build_result = self.infra.build_fuzzers(
                    config, repo_path, use_inc_image=True, inc_fallback=False
                )
                # If inc-build fails, retry with fallback (full recompile from /src)
                if not build_result.success:
                    logger.info(
                        f"Inc-build failed for {variant_name}, retrying with fallback"
                    )
                    build_result = self.infra.build_fuzzers(
                        config, repo_path, use_inc_image=True, inc_fallback=True
                    )

                if not build_result.success:
                    return BuildResult.from_error(
                        config=config,
                        error="Incremental build failed (both inc and fallback)",
                        elapsed_seconds=time.time() - start_time,
                    )

                # Success - write metadata and return
                build_path = self.infra.get_build_output_path(variant_name)
                self.infra.write_build_metadata(
                    variant_name,
                    inc_build=True,
                    sanitizer=config.sanitizer,
                    fallback_used=build_result.fallback_used,
                )
                return BuildResult(
                    config=config,
                    success=True,
                    variant_name=variant_name,
                    build_path=build_path,
                    elapsed_seconds=time.time() - start_time,
                    fallback_used=build_result.fallback_used,
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
        use_inc_build: bool = False,
        sanitizer: str = "address",
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
            use_inc_build: Use incremental builds if available (default: False).
                When True, validation variants use pre-built Docker images
                with cached dependencies for faster builds. Falls back to
                standard build if inc-build image is not available.
            sanitizer: Sanitizer type (default: "address"). Read from project.yaml.

        Returns:
            BuildPlan with all required configurations
        """
        # Create plan with global inc-build setting
        plan = BuildPlan(benchmark_name=benchmark_name, use_inc_build=use_inc_build)

        # Get all CPV patches upfront
        all_patches = self.infra.get_all_patches(benchmark_path)

        # Base/ref version (vulnerable version, no patches)
        # FULL mode: FULL_BASE at base_commit
        # DELTA mode: DELTA_REF at ref_commit
        if mode == BenchmarkMode.FULL:
            plan.add_variant(
                variant_type=VariantType.FULL_BASE,
                commit=base_commit,
                main_repo=main_repo,
                benchmark_path=benchmark_path,
                mode=mode,
                patches=[],
                language=language,
                repo_name=repo_name,
                sanitizer=sanitizer,
            )
        elif mode == BenchmarkMode.DELTA and ref_commit:
            plan.add_variant(
                variant_type=VariantType.DELTA_REF,
                commit=ref_commit,
                main_repo=main_repo,
                benchmark_path=benchmark_path,
                mode=mode,
                patches=[],
                language=language,
                repo_name=repo_name,
                sanitizer=sanitizer,
            )

        # All-patched version (all patches applied)
        patched_commit = ref_commit if mode == BenchmarkMode.DELTA else base_commit
        if patched_commit:
            plan.add_variant(
                variant_type=VariantType.ALL_PATCHED,
                commit=patched_commit,
                main_repo=main_repo,
                benchmark_path=benchmark_path,
                mode=mode,
                patches=all_patches,  # All patches
                language=language,
                repo_name=repo_name,
                sanitizer=sanitizer,
            )

        # CPV variants (all patches except one)
        for cpv_num in cpv_numbers:
            cpv_patches = self.infra.get_patches_except(benchmark_path, cpv_num)
            plan.add_variant(
                variant_type=VariantType.CPV,
                commit=patched_commit or base_commit,
                main_repo=main_repo,
                benchmark_path=benchmark_path,
                mode=mode,
                patches=cpv_patches,  # All patches except this CPV
                language=language,
                cpv_num=cpv_num,
                repo_name=repo_name,
                sanitizer=sanitizer,
            )

        # Coverage variant (no patches, different sanitizer)
        # Note: Coverage doesn't support inc-build (handled by add_variant)
        if include_coverage:
            plan.add_variant(
                variant_type=VariantType.COVERAGE,
                commit=patched_commit or base_commit,
                main_repo=main_repo,
                benchmark_path=benchmark_path,
                mode=mode,
                patches=[],  # Coverage: no patches
                language=language,
                repo_name=repo_name,
            )

        # Check which variants are already cached
        self._mark_cached_variants(plan)

        logger.debug(
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

    def is_variant_built(
        self,
        variant_name: str,
        *,
        require_inc_build: Optional[bool] = None,
    ) -> bool:
        """Check if a variant has been built.

        Args:
            variant_name: Variant name
            require_inc_build: If specified, also verify the cached build
                matches the requested inc-build mode. None means don't check.

        Returns:
            True if built (and matches required inc_build mode if specified)
        """
        return self.infra.is_variant_built(
            variant_name, require_inc_build=require_inc_build
        )

    def _mark_cached_variants(self, plan: BuildPlan) -> None:
        """Mark variants that are already built as cached in the plan.

        Args:
            plan: Build plan to update with cached variant information
        """
        for config in plan.configs:
            if self.infra.is_variant_built(
                config.variant_name,
                require_inc_build=config.use_inc_build,
            ):
                plan.mark_cached(config.variant_name)

    def create_patch_build_plan(
        self,
        benchmark_name: str,
        benchmark_path: Path,
        main_repo: str,
        commit: str,
        patches: list[tuple[str, str, Path]],
        mode: BenchmarkMode,
        language: str = "c",
        repo_name: Optional[str] = None,
        sanitizer: str = "address",
        *,
        use_inc_build: bool = False,
    ) -> BuildPlan:
        """Create a build plan for patch verification.

        Each patch gets its own isolated build to enable parallel builds
        without race conditions.

        Args:
            benchmark_name: Name of the benchmark
            benchmark_path: Path to benchmark directory
            main_repo: Main repository URL
            commit: Git commit hash to checkout
            patches: List of (pov_id, patch_id, patch_path) tuples
            mode: FULL or DELTA mode
            language: Programming language
            repo_name: Optional repository name for caching
            sanitizer: Sanitizer type (default: "address")
            use_inc_build: Use incremental builds if available (default: False)

        Returns:
            BuildPlan with configurations for each patch
        """
        # Create plan with global inc-build setting
        plan = BuildPlan(benchmark_name=benchmark_name, use_inc_build=use_inc_build)

        if not patches:
            logger.warning(f"No patches provided for {benchmark_name}")
            return plan

        for pov_id, patch_id, patch_path in patches:
            plan.add_variant(
                variant_type=VariantType.PATCHED,
                commit=commit,
                main_repo=main_repo,
                benchmark_path=benchmark_path,
                mode=mode,
                patches=[patch_path],  # Single CRS-generated patch
                language=language,
                pov_id=pov_id,
                patch_id=patch_id,
                sanitizer=sanitizer,
                repo_name=repo_name,
            )

        # Check which variants are already cached (respects inc_build mode)
        self._mark_cached_variants(plan)

        logger.debug(
            f"Patch build plan for {benchmark_name}: "
            f"{plan.total_count} patches, {plan.cached_count} cached, "
            f"{plan.build_count} to build"
        )

        return plan

    def has_inc_build_image(
        self,
        benchmark_name: str,
        sanitizer: str = "address",
        registry: str = "ghcr.io/team-atlanta/crsbench",
    ) -> bool:
        """Check if incremental build image exists for a benchmark.

        Args:
            benchmark_name: Name of the benchmark (e.g., "sanity-mock-c-delta-01")
            sanitizer: Sanitizer type
            registry: Docker registry

        Returns:
            True if inc-build image exists
        """
        # Extract project name from benchmark name (remove -delta-01 suffix)
        project_name = benchmark_name.rsplit("-", 2)[0]
        return self.infra.has_inc_build_image(project_name, sanitizer, registry)
