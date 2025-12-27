"""VerificationEngine for orchestrating POV validation.

This module provides the main orchestrator for POV verification:
1. Load benchmark configuration
2. Build all required variants
3. Run POVs against each variant
4. Collect crash results
5. Resolve verdicts
6. Deduplicate results
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional

from crsbench.utils.logger import get_logger
from crsbench.validation.variant.models import BuildTag, BuildVersion

if TYPE_CHECKING:
    from crsbench.validation.meta_adapter import MetaYamlAdapter
from crsbench.validation.verification.dedup import (
    get_dedup_strategy,
)
from crsbench.validation.verification.models import (
    VerificationRequest,
    VerificationResult,
    VerificationStatus,
)
from crsbench.validation.verification.reproducer import OSSFuzzReproducer
from crsbench.validation.verification.verdict import VerdictResolver

logger = get_logger(__name__)


class VerificationEngine:
    """Engine for orchestrating POV verification.

    Coordinates the entire verification workflow:
    1. Load and parse benchmark configuration
    2. Build all required variant projects
    3. Run POVs against each variant
    4. Collect and analyze crash results
    5. Determine final verdicts
    6. Apply deduplication

    Attributes:
        oss_fuzz_path: Path to oss-fuzz directory
        builder: VariantBuilder instance
        reproducer: OSSFuzzReproducer instance
        dedup_strategy: Deduplication strategy to use
        timeout: Timeout for reproduce operations
    """

    def __init__(
        self,
        oss_fuzz_path: Path,
        timeout: int = 120,
        dedup_strategy: str = "patch-based",
    ):
        """Initialize the verification engine.

        Args:
            oss_fuzz_path: Path to oss-fuzz directory
            timeout: Timeout for reproduce operations in seconds
            dedup_strategy: Name of deduplication strategy to use
        """
        # Lazy import to avoid circular dependencies
        from crsbench.validation.variant.builder import VariantBuilder

        self.oss_fuzz_path = Path(oss_fuzz_path)
        self.timeout = timeout
        self.builder = VariantBuilder(oss_fuzz_path)
        self.reproducer = OSSFuzzReproducer(oss_fuzz_path, timeout)
        self.dedup_strategy = get_dedup_strategy(dedup_strategy)
        self._built_versions: Dict[str, List[BuildVersion]] = {}

    def verify_pov(
        self,
        request: VerificationRequest,
        adapter: MetaYamlAdapter,
        versions: Optional[List[BuildVersion]] = None,
    ) -> VerificationResult:
        """Verify a single POV against all variants.

        Args:
            request: Verification request with POV data
            adapter: MetaYamlAdapter for benchmark config
            versions: Optional pre-built versions (for efficiency)

        Returns:
            VerificationResult with determined status
        """
        # Get or build variants
        if versions is None:
            versions = self._get_or_build_versions(adapter)

        if not versions:
            return VerificationResult(
                status=VerificationStatus.ERROR,
                benchmark=adapter.benchmark_name,
                pov_id=request.pov_id,
                details="No built versions available",
            )

        # Run reproduce for each version
        crash_results: Dict[BuildTag, bool] = {}
        cpv_crash_map: Dict[int, bool] = {}

        for version in versions:
            crashed = self.reproducer.reproduce(
                project_name=version.project_path,
                harness=request.harness,
                pov_data=request.pov_data,
                timeout=self.timeout,
            )

            if version.build_tag == BuildTag.CPV and version.cpv_num is not None:
                cpv_crash_map[version.cpv_num] = crashed
            else:
                crash_results[version.build_tag] = crashed

            logger.debug(
                f"{version.variant_project_name}: {'crashed' if crashed else 'ok'}"
            )

        # Resolve verdict
        return VerdictResolver.resolve(
            mode=adapter.get_mode(),
            crash_results=crash_results,
            cpv_crash_map=cpv_crash_map,
            benchmark_name=adapter.benchmark_name,
            pov_id=request.pov_id,
        )

    def verify_all_povs(
        self,
        adapter: MetaYamlAdapter,
        pov_dir: Path,
        harness_filter: Optional[str] = None,
        *,
        deduplicate: bool = True,
    ) -> List[VerificationResult]:
        """Verify all POVs in a directory against a benchmark.

        Args:
            adapter: MetaYamlAdapter for benchmark config
            pov_dir: Directory containing POV files
            harness_filter: Optional harness name to filter
            deduplicate: Whether to deduplicate results

        Returns:
            List of verification results
        """
        results = []

        # Build variants once
        versions = self._get_or_build_versions(adapter)
        if not versions:
            logger.error(f"Failed to build variants for {adapter.benchmark_name}")
            return []

        # Find all POV files
        pov_files = list(pov_dir.glob("*.bin")) + list(pov_dir.glob("*.pov"))
        if not pov_files:
            logger.warning(f"No POV files found in {pov_dir}")
            return []

        logger.info(f"Verifying {len(pov_files)} POVs against {adapter.benchmark_name}")

        # Get harness names to test
        harness_names = (
            [harness_filter] if harness_filter else adapter.get_harness_names()
        )

        # Progress tracking
        start_time = time.time()
        last_report_time = start_time
        total_items = len(pov_files) * len(harness_names)
        completed = 0
        report_interval = 300  # 5 minutes in seconds

        for pov_file in pov_files:
            pov_data = pov_file.read_bytes()

            for harness_name in harness_names:
                request = VerificationRequest(
                    pov_data=pov_data,
                    harness=harness_name,
                    benchmark=adapter.benchmark_name,
                    pov_id=pov_file.name,
                )

                result = self.verify_pov(request, adapter, versions)
                results.append(result)

                # Progress reporting every 5 minutes
                completed += 1
                current_time = time.time()
                if current_time - last_report_time >= report_interval:
                    elapsed_minutes = (current_time - start_time) / 60
                    logger.info(
                        f"Progress: {completed}/{total_items} POVs verified "
                        f"({elapsed_minutes:.1f} min elapsed)"
                    )
                    last_report_time = current_time

        # Deduplicate if requested
        if deduplicate:
            original_count = len(results)
            results = self.dedup_strategy.deduplicate(results)
            if len(results) < original_count:
                logger.info(
                    f"Deduplicated: {original_count} -> {len(results)} results "
                    f"(using {self.dedup_strategy.name})"
                )

        return results

    def verify_benchmark(
        self,
        benchmark_path: Path,
        pov_dir: Optional[Path] = None,
        harness_filter: Optional[str] = None,
        *,
        force_rebuild: bool = False,
        deduplicate: bool = True,
    ) -> List[VerificationResult]:
        """Verify POVs for a complete benchmark.

        Args:
            benchmark_path: Path to benchmark project directory
            pov_dir: Optional POV directory (overrides auto-discovery)
            harness_filter: Optional harness name to filter
            force_rebuild: Force rebuild of variants
            deduplicate: Whether to deduplicate results

        Returns:
            List of verification results
        """
        # Load benchmark configuration
        adapter = self._load_adapter(benchmark_path)
        if not adapter:
            return []

        # Build variants if needed
        if force_rebuild:
            self._built_versions.pop(adapter.benchmark_name, None)

        results = []
        versions = self._get_or_build_versions(adapter)
        if not versions:
            logger.error(f"Failed to build variants for {adapter.benchmark_name}")
            return []

        # Discover POVs using adapter (from meta.yaml)
        if pov_dir:
            # Use explicit POV directory (override)
            harness_names = (
                [harness_filter] if harness_filter else adapter.get_harness_names()
            )

            # Count total items for progress tracking
            pov_files = [f for f in pov_dir.glob("*") if f.is_file()]
            total_items = len(harness_names) * len(pov_files)
            completed = 0
            start_time = time.time()
            last_report_time = start_time
            report_interval = 300  # 5 minutes in seconds

            logger.info(
                f"Verifying {total_items} POVs against {adapter.benchmark_name}"
            )

            for harness_name in harness_names:
                for pov_file in pov_files:
                    pov_data = pov_file.read_bytes()
                    request = VerificationRequest(
                        pov_data=pov_data,
                        harness=harness_name,
                        benchmark=adapter.benchmark_name,
                        pov_id=pov_file.name,
                    )
                    result = self.verify_pov(request, adapter, versions)
                    results.append(result)

                    # Progress reporting every 5 minutes
                    completed += 1
                    current_time = time.time()
                    if current_time - last_report_time >= report_interval:
                        elapsed_minutes = (current_time - start_time) / 60
                        logger.info(
                            f"Progress: {completed}/{total_items} POVs verified "
                            f"({elapsed_minutes:.1f} min elapsed)"
                        )
                        last_report_time = current_time
        else:
            # Discover POVs from meta.yaml via adapter
            all_povs = adapter.get_all_pov_paths()

            # Filter by harness if specified
            if harness_filter:
                all_povs = [(h, v, p) for h, v, p in all_povs if h == harness_filter]

            if not all_povs:
                logger.warning("No POVs found in benchmark meta.yaml")
                return []

            logger.info(f"Verifying {len(all_povs)} POVs from meta.yaml")

            # Progress tracking
            total_items = len(all_povs)
            completed = 0
            start_time = time.time()
            last_report_time = start_time
            report_interval = 300  # 5 minutes in seconds

            for harness_name, _vuln_keyword, pov_path in all_povs:
                pov_data = pov_path.read_bytes()
                request = VerificationRequest(
                    pov_data=pov_data,
                    harness=harness_name,
                    benchmark=adapter.benchmark_name,
                    pov_id=pov_path.name,
                )
                result = self.verify_pov(request, adapter, versions)
                results.append(result)

                # Progress reporting every 5 minutes
                completed += 1
                current_time = time.time()
                if current_time - last_report_time >= report_interval:
                    elapsed_minutes = (current_time - start_time) / 60
                    logger.info(
                        f"Progress: {completed}/{total_items} POVs verified "
                        f"({elapsed_minutes:.1f} min elapsed)"
                    )
                    last_report_time = current_time

        # Deduplicate if requested
        if deduplicate and results:
            original_count = len(results)
            results = self.dedup_strategy.deduplicate(results)
            if len(results) < original_count:
                logger.info(
                    f"Deduplicated: {original_count} -> {len(results)} results "
                    f"(using {self.dedup_strategy.name})"
                )

        return results

    def _get_or_build_versions(
        self,
        adapter: MetaYamlAdapter,
    ) -> List[BuildVersion]:
        """Get cached versions or build new ones.

        Args:
            adapter: MetaYamlAdapter for benchmark config

        Returns:
            List of built versions
        """
        if adapter.benchmark_name in self._built_versions:
            return self._built_versions[adapter.benchmark_name]

        versions = self.builder.build_all_variants(adapter)
        self._built_versions[adapter.benchmark_name] = versions
        return versions

    def _load_adapter(self, benchmark_path: Path) -> Optional[MetaYamlAdapter]:
        """Load MetaYamlAdapter from benchmark path.

        Args:
            benchmark_path: Path to benchmark directory

        Returns:
            MetaYamlAdapter or None if loading fails
        """
        # Lazy import to avoid circular dependencies
        from crsbench.validation.meta_adapter import MetaYamlAdapter

        meta_yaml = benchmark_path / ".aixcc" / "meta.yaml"
        project_yaml = benchmark_path / "project.yaml"

        if not meta_yaml.exists():
            logger.error(f"meta.yaml not found: {meta_yaml}")
            return None

        # Extract benchmark name from path
        benchmark_name = benchmark_path.name

        # Get language, main_repo, and optional repo_name from project.yaml
        lang = "c"  # Default
        main_repo = ""
        repo_name = None
        if project_yaml.exists():
            import yaml

            with project_yaml.open() as f:
                project_data = yaml.safe_load(f)
            lang = project_data.get("language", "c")
            main_repo = project_data.get("main_repo", "")
            # Optional: explicit repo_name to use instead of deriving from URL
            repo_name = project_data.get("repo_name")

        try:
            return MetaYamlAdapter.from_meta_yaml(
                meta_yaml_path=meta_yaml,
                benchmark_name=benchmark_name,
                lang=lang,
                main_repo=main_repo,
                repo_name=repo_name,
            )
        except Exception as e:
            logger.error(f"Failed to load adapter: {e}")
            return None

    def cleanup(self, adapter: MetaYamlAdapter) -> None:
        """Clean up variant projects for a benchmark.

        Args:
            adapter: MetaYamlAdapter for benchmark config
        """
        self.builder.cleanup_variants(adapter)
        self._built_versions.pop(adapter.benchmark_name, None)
