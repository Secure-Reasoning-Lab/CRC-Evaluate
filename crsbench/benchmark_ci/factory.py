"""Job factory for benchmark CI.

Creates build and verify jobs from benchmark configuration.
"""

from collections import defaultdict
from pathlib import Path

from crsbench.benchmark_ci.jobs import BuildJob, Job, VerifyPatchJob, VerifyPovJob
from crsbench.builder.types import BenchmarkMode, BuildConfig, VariantType
from crsbench.utils.logger import get_logger
from crsbench.validation import MetaYamlAdapter

logger = get_logger(__name__)


def _parse_cpv_num(vuln_keyword: str) -> int:
    """Parse CPV number from vulnerability keyword.

    Args:
        vuln_keyword: Vulnerability keyword (e.g., "cpv_0", "cpv_1")

    Returns:
        CPV number as integer

    Raises:
        ValueError: If vuln_keyword is not in expected format
    """
    if not vuln_keyword.startswith("cpv_"):
        raise ValueError(f"Invalid vuln_keyword format: {vuln_keyword}")
    return int(vuln_keyword.split("_")[1])


class JobFactory:
    """Creates jobs for benchmark CI.

    Given a MetaYamlAdapter, generates all necessary build and verify jobs
    for validating benchmark data integrity.

    Attributes:
        adapter: MetaYamlAdapter for benchmark configuration
        sanitizer: Sanitizer to use for builds (default: "address")
        benchmark: Benchmark name
        mode: Benchmark mode (FULL or DELTA)
    """

    def __init__(self, adapter: MetaYamlAdapter, sanitizer: str = "address"):
        """Initialize the factory.

        Args:
            adapter: MetaYamlAdapter for the benchmark
            sanitizer: Sanitizer type (default: "address")
        """
        self.adapter = adapter
        self.sanitizer = sanitizer
        self.benchmark = adapter.benchmark_name
        self.mode = adapter.get_mode()

    def create_all_jobs(self) -> list[Job]:
        """Create all jobs for POV and patch verification.

        Returns:
            List of all jobs (build and verify) for the benchmark
        """
        jobs: list[Job] = []
        jobs.extend(self._create_build_jobs())
        jobs.extend(self._create_verify_pov_jobs())
        jobs.extend(self._create_verify_patch_jobs())
        return jobs

    def _get_all_povs_with_cpv(
        self,
    ) -> list[tuple[str, str, str, Path, int]]:
        """Get all POVs with their metadata.

        Returns:
            List of (harness, vuln_keyword, pov_id, pov_path, cpv_num) tuples
        """
        results = []
        for harness_name in self.adapter.get_harness_names():
            for vuln_keyword, pov in self.adapter.get_all_povs(harness_name):
                pov_path = self.adapter.get_pov_path(harness_name, vuln_keyword, pov.id)
                if not pov_path:
                    logger.warning(
                        f"POV path not found for {harness_name}/{vuln_keyword}/{pov.id}"
                    )
                    continue
                if not pov_path.exists():
                    logger.warning(f"POV file does not exist: {pov_path}")
                    continue
                cpv_num = _parse_cpv_num(vuln_keyword)
                results.append((harness_name, vuln_keyword, pov.id, pov_path, cpv_num))
        return results

    def _create_build_jobs(self) -> list[BuildJob]:
        """Create build jobs for all required variants.

        For POV verification:
        - DELTA mode: deltabase, deltaref, allpatched, cpv{N}
        - FULL mode: fullbase, allpatched, cpv{N}

        For patch verification:
        - patched-cpv{N} for each CPV

        Returns:
            List of BuildJob instances
        """
        jobs = []
        cpv_nums = self.adapter.get_cpv_numbers()

        # Base variants based on mode
        if self.mode == BenchmarkMode.DELTA:
            jobs.append(self._make_build_job("deltabase", VariantType.DELTA_BASE))
            jobs.append(self._make_build_job("deltaref", VariantType.DELTA_REF))
        else:
            jobs.append(self._make_build_job("fullbase", VariantType.FULL_BASE))

        # Allpatched variant (all CPV patches applied)
        jobs.append(self._make_build_job("allpatched", VariantType.ALL_PATCHED))

        # CPV variants (all patches except one)
        for cpv_num in cpv_nums:
            jobs.append(
                self._make_build_job(f"cpv{cpv_num}", VariantType.CPV, cpv_num=cpv_num)
            )

        # Patched variants for patch verification
        for cpv_num in cpv_nums:
            jobs.append(
                self._make_build_job(
                    f"patched-cpv{cpv_num}",
                    VariantType.PATCHED,
                    cpv_num=cpv_num,
                    use_inc_build=True,
                )
            )

        return jobs

    def _make_build_job(
        self,
        variant_type_str: str,
        variant_type: VariantType,
        cpv_num: int | None = None,
        *,
        use_inc_build: bool = False,
    ) -> BuildJob:
        """Create a BuildJob for a variant.

        Args:
            variant_type_str: Variant type as string for job ID
            variant_type: VariantType enum value
            cpv_num: CPV number if applicable
            use_inc_build: Whether to use incremental build

        Returns:
            Configured BuildJob instance
        """
        # Determine commit based on variant type
        if variant_type == VariantType.DELTA_BASE:
            commit = self.adapter.get_base_commit()
        elif variant_type == VariantType.DELTA_REF:
            ref_commit = self.adapter.get_ref_commit()
            if ref_commit is None:
                raise ValueError("DELTA mode requires ref_commit")
            commit = ref_commit
        elif variant_type == VariantType.FULL_BASE:
            commit = self.adapter.get_base_commit()
        else:
            # For allpatched, cpv, patched variants, use ref_commit (DELTA) or base (FULL)
            if self.mode == BenchmarkMode.DELTA:
                ref_commit = self.adapter.get_ref_commit()
                if ref_commit is None:
                    raise ValueError("DELTA mode requires ref_commit")
                commit = ref_commit
            else:
                commit = self.adapter.get_base_commit()

        # Collect patches for this variant
        patches = self._get_patches_for_variant(variant_type, cpv_num)

        # Build patch_id and pov_id for PATCHED variants
        patch_id = None
        pov_id = None
        if variant_type == VariantType.PATCHED and cpv_num is not None:
            patch_id = f"patch_{cpv_num}"
            pov_id = f"cpv_{cpv_num}"

        config = BuildConfig(
            benchmark_name=self.benchmark,
            variant_type=variant_type,
            commit=commit,
            main_repo=self.adapter.main_repo,
            benchmark_path=self.adapter.benchmark_path or Path.cwd(),
            mode=self.mode,
            patches=patches,
            language=self.adapter.lang,
            cpv_num=cpv_num,
            patch_id=patch_id,
            pov_id=pov_id,
            use_inc_build=use_inc_build,
            sanitizer=self.sanitizer,
            repo_name=self.adapter.repo_name,
        )

        return BuildJob(
            benchmark=self.benchmark,
            sanitizer=self.sanitizer,
            variant_type=variant_type_str,
            config=config,
        )

    def _get_patches_for_variant(
        self, variant_type: VariantType, cpv_num: int | None = None
    ) -> list[Path]:
        """Get patches to apply for a variant.

        Args:
            variant_type: Type of variant
            cpv_num: CPV number if applicable

        Returns:
            List of patch file paths to apply
        """
        all_cpvs = self.adapter.get_cpv_numbers()
        patches: list[Path] = []

        if variant_type in (
            VariantType.DELTA_BASE,
            VariantType.DELTA_REF,
            VariantType.FULL_BASE,
        ):
            # No patches for base/ref variants
            return []

        if variant_type == VariantType.ALL_PATCHED:
            # Apply all patches
            for cpv in all_cpvs:
                patch = self._get_patch_for_cpv(cpv)
                if patch:
                    patches.append(patch)

        elif variant_type == VariantType.CPV:
            # Apply all patches except the one for this CPV
            for cpv in all_cpvs:
                if cpv != cpv_num:
                    patch = self._get_patch_for_cpv(cpv)
                    if patch:
                        patches.append(patch)

        elif variant_type == VariantType.PATCHED:
            # Apply only the patch for this CPV
            if cpv_num is not None:
                patch = self._get_patch_for_cpv(cpv_num)
                if patch:
                    patches.append(patch)

        return patches

    def _get_patch_for_cpv(self, cpv_num: int) -> Path | None:
        """Get patch file path for a CPV.

        Args:
            cpv_num: CPV number

        Returns:
            Path to patch file if found, None otherwise
        """
        vuln_keyword = f"cpv_{cpv_num}"
        # Find the harness that has this CPV
        for harness_name in self.adapter.get_harness_names():
            patch_path = self.adapter.get_patch_path(harness_name, vuln_keyword)
            if patch_path and patch_path.exists():
                return patch_path
        return None

    def _create_verify_pov_jobs(self) -> list[VerifyPovJob]:
        """Create verify jobs for all POV × variant combinations.

        Each POV is tested against:
        - deltabase/fullbase: should NOT crash
        - deltaref: should crash (DELTA mode only)
        - allpatched: should NOT crash
        - cpv{N}: should crash only if N matches POV's CPV

        Returns:
            List of VerifyPovJob instances
        """
        jobs = []
        povs = self._get_all_povs_with_cpv()
        cpv_nums = self.adapter.get_cpv_numbers()

        # Define base variants and expected crash behavior
        if self.mode == BenchmarkMode.DELTA:
            base_variants = [
                ("deltabase", False),  # Should NOT crash (pre-vulnerability)
                ("deltaref", True),  # Should crash (vulnerable)
            ]
        else:
            base_variants = [
                ("fullbase", True),  # Should crash (vulnerable)
            ]

        base_variants.append(("allpatched", False))  # Should NOT crash

        for harness, _vuln_keyword, pov_id, pov_path, pov_cpv_num in povs:
            # Test against base variants
            for variant_type, expected_crash in base_variants:
                jobs.append(
                    VerifyPovJob(
                        benchmark=self.benchmark,
                        sanitizer=self.sanitizer,
                        variant_type=variant_type,
                        pov_id=pov_id,
                        pov_path=pov_path,
                        harness=harness,
                        expected_crash=expected_crash,
                    )
                )

            # Test against CPV variants
            for cpv_num in cpv_nums:
                # POV should crash only on its own CPV variant
                # (where that CPV's patch is NOT applied)
                expected_crash = cpv_num == pov_cpv_num
                jobs.append(
                    VerifyPovJob(
                        benchmark=self.benchmark,
                        sanitizer=self.sanitizer,
                        variant_type=f"cpv{cpv_num}",
                        pov_id=pov_id,
                        pov_path=pov_path,
                        harness=harness,
                        expected_crash=expected_crash,
                    )
                )

        return jobs

    def _create_verify_patch_jobs(self) -> list[VerifyPatchJob]:
        """Create patch verification jobs.

        A patch for CPV_N must prevent ALL POVs targeting that CPV from crashing.

        Returns:
            List of VerifyPatchJob instances
        """
        jobs = []
        povs = self._get_all_povs_with_cpv()

        # Group POVs by CPV number
        cpv_povs: dict[int, list[tuple[str, Path, str]]] = defaultdict(list)
        for harness, _vuln_keyword, pov_id, pov_path, cpv_num in povs:
            cpv_povs[cpv_num].append((pov_id, pov_path, harness))

        for cpv_num, pov_list in cpv_povs.items():
            # Get patch path for this CPV
            patch_path = self._get_patch_for_cpv(cpv_num)
            if not patch_path:
                continue

            # Extract (pov_id, pov_path) tuples and determine harness
            povs_for_cpv = [(pov_id, pov_path) for pov_id, pov_path, _ in pov_list]
            harness = pov_list[0][2]  # Use first POV's harness

            jobs.append(
                VerifyPatchJob(
                    benchmark=self.benchmark,
                    sanitizer=self.sanitizer,
                    cpv_num=cpv_num,
                    patch_path=patch_path,
                    povs_for_cpv=povs_for_cpv,
                    harness=harness,
                )
            )

        return jobs


def create_jobs_for_benchmark(
    benchmark_path: Path,
    sanitizer: str = "address",
) -> list[Job]:
    """Convenience function to create all jobs for a benchmark.

    Args:
        benchmark_path: Path to benchmark directory
        sanitizer: Sanitizer to use (default: "address")

    Returns:
        List of all jobs for the benchmark

    Raises:
        ValueError: If benchmark cannot be loaded
    """
    adapter = MetaYamlAdapter.from_benchmark_path(benchmark_path)
    if adapter is None:
        raise ValueError(f"Failed to load benchmark from {benchmark_path}")

    factory = JobFactory(adapter, sanitizer=sanitizer)
    return factory.create_all_jobs()
