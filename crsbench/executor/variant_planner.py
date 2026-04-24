"""VariantPlanner: centralized build job creation for all variant types.

Accepts a benchmark path and produces a complete list of BuildSingleVariantJob
instances covering all variant types (vulnerable, allpatched, per-CPV, patched).

Replaces duplicated build job creation logic in:
- crsbench/benchmark_ci/cli/commands/all_cmd.py
- crsbench/benchmark_ci/cli/commands/build_cmd.py
- crsbench/distributed/evaluator.py
- crsbench/evaluation/verification/pov/engine.py
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from crsbench.benchmark_ci.jobs.flat import BuildSingleVariantJob
from crsbench.builder.types import BenchmarkMode, VariantType
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from crsbench.validation.meta_adapter import MetaYamlAdapter


class VariantPlanner:
    """Converts benchmark metadata into BuildSingleVariantJob lists.

    This is the single source of truth for which build jobs a benchmark needs.
    All commands (ci build, ci all, run, evaluator) use this instead of
    duplicating build job creation logic.

    Args:
        oss_fuzz_path: Path to oss-fuzz directory
        source_mode: Source mode ("pkgs" or "main_repo")
    """

    def __init__(
        self,
        oss_fuzz_path: Path,
        source_mode: str = "pkgs",
    ) -> None:
        self._oss_fuzz_path = oss_fuzz_path
        self._source_mode = source_mode

    def plan_builds(
        self,
        benchmark_path: Path,
        *,
        use_inc_build: bool = True,
        force_rebuild: bool = False,
        skip_if_cached: bool = True,
        include_coverage: bool = False,
        include_patched: bool = False,
        project_image_prefix: str = "crsbench",
        inc_image_policy: str | None = None,
        inc_image_registry: str | None = None,
        inc_image_max_pull_bytes: int | None = None,
        inc_image_pull_timeout: int | None = None,
        local_image_prefix: str | None = None,
    ) -> list[BuildSingleVariantJob]:
        """Create build jobs for all variants of a single benchmark.

        Produces BuildSingleVariantJob instances for:
        - Vulnerable variant (DELTA_REF or FULL_BASE) per sanitizer
        - ALL_PATCHED variant per sanitizer
        - CPV variant per CPV (with CPV-specific sanitizer)
        - COVERAGE variant (optional, if include_coverage=True)
        - PATCHED variants (optional, if include_patched=True)

        All jobs are independent (no inter-job dependencies).

        Args:
            benchmark_path: Path to benchmark directory
            use_inc_build: Use incremental build (default True)
            force_rebuild: Force rebuild even if cached
            skip_if_cached: Skip build if cached (default True)
            include_coverage: Include coverage variant
            include_patched: Include patched variants for patch verification
            project_image_prefix: Docker image prefix

        Returns:
            List of BuildSingleVariantJob instances
        """
        supports_inc = self._check_inc_build_support(benchmark_path)
        effective_inc = use_inc_build and supports_inc
        jobs = list(
            self.iter_builds(
                benchmark_path,
                use_inc_build=use_inc_build,
                force_rebuild=force_rebuild,
                skip_if_cached=skip_if_cached,
                include_coverage=include_coverage,
                include_patched=include_patched,
                project_image_prefix=project_image_prefix,
                inc_image_policy=inc_image_policy,
                inc_image_registry=inc_image_registry,
                inc_image_max_pull_bytes=inc_image_max_pull_bytes,
                inc_image_pull_timeout=inc_image_pull_timeout,
                local_image_prefix=local_image_prefix,
            )
        )
        benchmark_name = benchmark_path.name
        logger.info(
            f"VariantPlanner: {benchmark_name} -> {len(jobs)} build jobs "
            f"(inc_build={effective_inc})"
        )
        return jobs

    def iter_builds(
        self,
        benchmark_path: Path,
        *,
        use_inc_build: bool = True,
        force_rebuild: bool = False,
        skip_if_cached: bool = True,
        include_coverage: bool = False,
        include_patched: bool = False,
        project_image_prefix: str = "crsbench",
        inc_image_policy: str | None = None,
        inc_image_registry: str | None = None,
        inc_image_max_pull_bytes: int | None = None,
        inc_image_pull_timeout: int | None = None,
        local_image_prefix: str | None = None,
    ) -> "Iterator[BuildSingleVariantJob]":
        """Yield build jobs lazily for a single benchmark."""
        benchmark_name = benchmark_path.name

        adapter = self._load_adapter(benchmark_path)
        if adapter is None:
            logger.warning(f"Failed to load adapter for {benchmark_name}")
            return

        mode, commit = self._resolve_mode_and_commit(adapter, benchmark_name)
        if mode is None or commit is None:
            return

        supports_inc = self._check_inc_build_support(benchmark_path)
        effective_inc = use_inc_build and supports_inc

        main_repo = adapter.main_repo
        language = adapter.lang
        repo_name = adapter.repo_name
        required_sanitizers = adapter.get_all_cpv_sanitizers()

        from crsbench.builder.infrastructure import OSSFuzzInfrastructure

        infra = OSSFuzzInfrastructure(self._oss_fuzz_path)
        all_patches = infra.get_all_patches(benchmark_path)

        vulnerable_type = (
            VariantType.DELTA_REF
            if mode == BenchmarkMode.DELTA
            else VariantType.FULL_BASE
        )

        for sanitizer in required_sanitizers:
            yield BuildSingleVariantJob(
                benchmark_path=benchmark_path,
                benchmark_name=benchmark_name,
                variant_type=vulnerable_type,
                commit=commit,
                main_repo=main_repo,
                mode=mode,
                language=language,
                use_inc_build=effective_inc,
                force_rebuild=force_rebuild,
                skip_if_cached=skip_if_cached,
                source_mode=self._source_mode,
                sanitizer=sanitizer,
                repo_name=repo_name,
                project_image_prefix=project_image_prefix,
                inc_image_policy=inc_image_policy,
                inc_image_registry=inc_image_registry,
                inc_image_max_pull_bytes=inc_image_max_pull_bytes,
                inc_image_pull_timeout=inc_image_pull_timeout,
                local_image_prefix=local_image_prefix,
            )

            yield BuildSingleVariantJob(
                benchmark_path=benchmark_path,
                benchmark_name=benchmark_name,
                variant_type=VariantType.ALL_PATCHED,
                commit=commit,
                main_repo=main_repo,
                mode=mode,
                language=language,
                patches=all_patches,
                use_inc_build=effective_inc,
                force_rebuild=force_rebuild,
                skip_if_cached=skip_if_cached,
                source_mode=self._source_mode,
                sanitizer=sanitizer,
                repo_name=repo_name,
                project_image_prefix=project_image_prefix,
                inc_image_policy=inc_image_policy,
                inc_image_registry=inc_image_registry,
                inc_image_max_pull_bytes=inc_image_max_pull_bytes,
                inc_image_pull_timeout=inc_image_pull_timeout,
                local_image_prefix=local_image_prefix,
            )

        if include_coverage:
            yield BuildSingleVariantJob(
                benchmark_path=benchmark_path,
                benchmark_name=benchmark_name,
                variant_type=VariantType.COVERAGE,
                commit=commit,
                main_repo=main_repo,
                mode=mode,
                language=language,
                use_inc_build=False,
                force_rebuild=force_rebuild,
                skip_if_cached=skip_if_cached,
                source_mode=self._source_mode,
                sanitizer="coverage",
                repo_name=repo_name,
                project_image_prefix=project_image_prefix,
                inc_image_policy=inc_image_policy,
                inc_image_registry=inc_image_registry,
                inc_image_max_pull_bytes=inc_image_max_pull_bytes,
                inc_image_pull_timeout=inc_image_pull_timeout,
                local_image_prefix=local_image_prefix,
            )

        yield from self._iter_cpv_variants(
            benchmark_path=benchmark_path,
            benchmark_name=benchmark_name,
            adapter=adapter,
            infra=infra,
            mode=mode,
            commit=commit,
            main_repo=main_repo,
            language=language,
            effective_inc=effective_inc,
            force_rebuild=force_rebuild,
            skip_if_cached=skip_if_cached,
            repo_name=repo_name,
            project_image_prefix=project_image_prefix,
            inc_image_policy=inc_image_policy,
            inc_image_registry=inc_image_registry,
            inc_image_max_pull_bytes=inc_image_max_pull_bytes,
            inc_image_pull_timeout=inc_image_pull_timeout,
            local_image_prefix=local_image_prefix,
        )

        if include_patched:
            yield from self._iter_patched_variants(
                benchmark_path=benchmark_path,
                benchmark_name=benchmark_name,
                adapter=adapter,
                mode=mode,
                commit=commit,
                main_repo=main_repo,
                language=language,
                effective_inc=effective_inc,
                force_rebuild=force_rebuild,
                skip_if_cached=skip_if_cached,
                repo_name=repo_name,
                project_image_prefix=project_image_prefix,
                inc_image_policy=inc_image_policy,
                inc_image_registry=inc_image_registry,
                inc_image_max_pull_bytes=inc_image_max_pull_bytes,
                inc_image_pull_timeout=inc_image_pull_timeout,
                local_image_prefix=local_image_prefix,
            )

    def plan_all_builds(
        self,
        benchmark_paths: list[Path],
        *,
        use_inc_build: bool = True,
        force_rebuild: bool = False,
        skip_if_cached: bool = True,
        include_coverage: bool = False,
        include_patched: bool = False,
        project_image_prefix: str = "crsbench",
        inc_image_policy: str | None = None,
        inc_image_registry: str | None = None,
        inc_image_max_pull_bytes: int | None = None,
        inc_image_pull_timeout: int | None = None,
        local_image_prefix: str | None = None,
    ) -> list[BuildSingleVariantJob]:
        """Create build jobs for multiple benchmarks.

        Args:
            benchmark_paths: List of benchmark directory paths
            use_inc_build: Use incremental build (default True)
            force_rebuild: Force rebuild even if cached
            skip_if_cached: Skip build if cached
            include_coverage: Include coverage variants
            include_patched: Include patched variants
            project_image_prefix: Docker image prefix

        Returns:
            List of BuildSingleVariantJob instances for all benchmarks
        """
        jobs: list[BuildSingleVariantJob] = []
        for path in benchmark_paths:
            jobs.extend(
                self.plan_builds(
                    path,
                    use_inc_build=use_inc_build,
                    force_rebuild=force_rebuild,
                    skip_if_cached=skip_if_cached,
                    include_coverage=include_coverage,
                    include_patched=include_patched,
                    project_image_prefix=project_image_prefix,
                    inc_image_policy=inc_image_policy,
                    inc_image_registry=inc_image_registry,
                    inc_image_max_pull_bytes=inc_image_max_pull_bytes,
                    inc_image_pull_timeout=inc_image_pull_timeout,
                    local_image_prefix=local_image_prefix,
                )
            )
        return jobs

    def _load_adapter(self, benchmark_path: Path) -> Optional[MetaYamlAdapter]:
        """Load benchmark adapter for extracting build configuration."""
        from crsbench.evaluation.verification.pov import VerificationEngine

        engine = VerificationEngine(self._oss_fuzz_path, source_mode=self._source_mode)
        return engine.load_adapter(benchmark_path)

    def _resolve_mode_and_commit(
        self, adapter: MetaYamlAdapter, benchmark_name: str
    ) -> tuple[Optional[BenchmarkMode], Optional[str]]:
        """Determine benchmark mode and commit from adapter."""
        ref_commit = adapter.get_ref_commit()  # type: ignore[attr-defined]
        base_commit = adapter.get_base_commit()  # type: ignore[attr-defined]

        if ref_commit:
            return BenchmarkMode.DELTA, ref_commit
        if base_commit:
            return BenchmarkMode.FULL, base_commit

        logger.warning(f"No commit found for {benchmark_name}, skipping")
        return None, None

    def _check_inc_build_support(self, benchmark_path: Path) -> bool:
        """Check if benchmark supports incremental builds."""
        from crsbench.benchmark_ci.validator import _load_project_capabilities

        supports_inc, _ = _load_project_capabilities(benchmark_path)
        return supports_inc

    def _iter_cpv_variants(
        self,
        benchmark_path: Path,
        benchmark_name: str,
        adapter: MetaYamlAdapter,
        infra: object,
        mode: BenchmarkMode,
        commit: str,
        main_repo: str,
        language: str,
        effective_inc: bool,
        force_rebuild: bool,
        skip_if_cached: bool,
        repo_name: Optional[str],
        project_image_prefix: str,
        inc_image_policy: str | None,
        inc_image_registry: str | None,
        inc_image_max_pull_bytes: int | None,
        inc_image_pull_timeout: int | None,
        local_image_prefix: str | None,
    ) -> "Iterator[BuildSingleVariantJob]":
        """Yield CPV variant build jobs lazily."""
        from crsbench.benchmark_ci.cli.benchmark_discovery import (
            discover_cpv_ids,
            discover_harness_names,
        )

        seen_cpvs: set[str] = set()

        harnesses = discover_harness_names(benchmark_path)
        for harness in harnesses:
            for cpv_id in discover_cpv_ids(benchmark_path, harness):
                if cpv_id in seen_cpvs:
                    continue
                seen_cpvs.add(cpv_id)

                cpv_num = int(cpv_id.split("_")[1])
                cpv_sanitizer = adapter.get_cpv_sanitizer(harness, cpv_id)  # type: ignore[attr-defined]
                cpv_patches = infra.get_patches_except(benchmark_path, cpv_num)  # type: ignore[attr-defined]

                yield BuildSingleVariantJob(
                    benchmark_path=benchmark_path,
                    benchmark_name=benchmark_name,
                    variant_type=VariantType.CPV,
                    commit=commit,
                    main_repo=main_repo,
                    mode=mode,
                    language=language,
                    cpv_num=cpv_num,
                    patches=cpv_patches,
                    use_inc_build=effective_inc,
                    force_rebuild=force_rebuild,
                    skip_if_cached=skip_if_cached,
                    source_mode=self._source_mode,
                    sanitizer=cpv_sanitizer,
                    repo_name=repo_name,
                    project_image_prefix=project_image_prefix,
                    inc_image_policy=inc_image_policy,
                    inc_image_registry=inc_image_registry,
                    inc_image_max_pull_bytes=inc_image_max_pull_bytes,
                    inc_image_pull_timeout=inc_image_pull_timeout,
                    local_image_prefix=local_image_prefix,
                )

    def _iter_patched_variants(
        self,
        benchmark_path: Path,
        benchmark_name: str,
        adapter: MetaYamlAdapter,
        mode: BenchmarkMode,
        commit: str,
        main_repo: str,
        language: str,
        effective_inc: bool,
        force_rebuild: bool,
        skip_if_cached: bool,
        repo_name: Optional[str],
        project_image_prefix: str,
        inc_image_policy: str | None,
        inc_image_registry: str | None,
        inc_image_max_pull_bytes: int | None,
        inc_image_pull_timeout: int | None,
        local_image_prefix: str | None,
    ) -> "Iterator[BuildSingleVariantJob]":
        """Yield patched variant build jobs for patch verification.

        Discovers all patches in the benchmark's .aixcc directory and creates
        one BuildSingleVariantJob per (cpv, patch) pair.
        """
        from crsbench.benchmark_ci.cli.benchmark_discovery import (
            discover_cpv_ids,
            discover_harness_names,
            discover_patch_paths,
        )

        seen: set[tuple[str, str]] = set()

        harnesses = discover_harness_names(benchmark_path)
        for harness in harnesses:
            for cpv_id in discover_cpv_ids(benchmark_path, harness):
                cpv_sanitizer = adapter.get_cpv_sanitizer(harness, cpv_id)  # type: ignore[attr-defined]

                patch_tuples = discover_patch_paths(benchmark_path, harness, cpv_id)
                for patch_id, patch_path in patch_tuples:
                    key = (cpv_id, patch_id)
                    if key in seen:
                        continue
                    seen.add(key)

                    yield BuildSingleVariantJob(
                        benchmark_path=benchmark_path,
                        benchmark_name=benchmark_name,
                        variant_type=VariantType.PATCHED,
                        commit=commit,
                        main_repo=main_repo,
                        mode=mode,
                        language=language,
                        cpv_num=int(cpv_id.split("_")[1]),
                        patch_id=patch_id,
                        pov_id=cpv_id,
                        patches=[patch_path],
                        use_inc_build=effective_inc,
                        force_rebuild=force_rebuild,
                        skip_if_cached=skip_if_cached,
                        source_mode=self._source_mode,
                        sanitizer=cpv_sanitizer,
                        repo_name=repo_name,
                        project_image_prefix=project_image_prefix,
                        inc_image_policy=inc_image_policy,
                        inc_image_registry=inc_image_registry,
                        inc_image_max_pull_bytes=inc_image_max_pull_bytes,
                        inc_image_pull_timeout=inc_image_pull_timeout,
                        local_image_prefix=local_image_prefix,
                    )
