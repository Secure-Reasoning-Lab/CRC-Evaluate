"""VariantBuilder for creating and building benchmark variants.

This module handles:
1. Creating variant project directories (copy approach)
2. Applying patches for each variant
3. Building fuzzers via OSS-Fuzz helper.py
4. Managing build cache
"""

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

from crsbench.utils.logger import get_logger
from crsbench.utils.repo_manager import clone_or_copy_cached_repo, run_git
from crsbench.validation.meta_adapter import MetaYamlAdapter
from crsbench.validation.variant.models import BenchmarkMode, BuildTag, BuildVersion
from crsbench.validation.verification.reproducer import OSSFuzzReproducer

logger = get_logger(__name__)


class VariantBuilder:
    """Builder for benchmark variants.

    Creates and builds all required variants for POV validation:
    - FULL_BASE / DELTA_BASE: Base commit without patches
    - DELTA_REF: Reference commit without patches (delta mode only)
    - ALL_PATCHED: All patches applied (should not crash)
    - CPV_N: All patches except CPV N (should crash if POV targets CPV N)

    Attributes:
        oss_fuzz_path: Path to oss-fuzz directory
        reproducer: OSSFuzzReproducer instance for building
    """

    def __init__(self, oss_fuzz_path: Path):
        """Initialize the builder.

        Args:
            oss_fuzz_path: Path to oss-fuzz directory
        """
        self.oss_fuzz_path = Path(oss_fuzz_path)
        self.projects_base = self.oss_fuzz_path / "projects"
        self.reproducer = OSSFuzzReproducer(oss_fuzz_path)

    def build_all_variants(
        self,
        adapter: MetaYamlAdapter,
        *,
        force_rebuild: bool = False,
    ) -> List[BuildVersion]:
        """Build all required variants for a benchmark.

        Args:
            adapter: MetaYamlAdapter with benchmark configuration
            force_rebuild: If True, rebuild even if cached

        Returns:
            List of successfully built BuildVersion instances
        """
        versions = []
        mode = adapter.get_mode()

        # Build base version
        base_version = self._build_base_version(adapter, force_rebuild)
        if base_version:
            versions.append(base_version)

        # Build ref version (delta mode only)
        if mode == BenchmarkMode.DELTA:
            ref_version = self._build_ref_version(adapter, force_rebuild)
            if ref_version:
                versions.append(ref_version)

        # Build all-patched version
        allpatched_version = self._build_allpatched_version(adapter, force_rebuild)
        if allpatched_version:
            versions.append(allpatched_version)

        # Build CPV variants
        cpv_versions = self._build_cpv_versions(adapter, force_rebuild)
        versions.extend(cpv_versions)

        logger.info(f"Built {len(versions)} variants for {adapter.benchmark_name}")
        return versions

    def _build_base_version(
        self,
        adapter: MetaYamlAdapter,
        force_rebuild: bool,
    ) -> Optional[BuildVersion]:
        """Build the base version (no patches applied)."""
        mode = adapter.get_mode()
        build_tag = (
            BuildTag.FULL_BASE if mode == BenchmarkMode.FULL else BuildTag.DELTA_BASE
        )

        return self._build_version(
            adapter=adapter,
            commit=adapter.get_base_commit(),
            build_tag=build_tag,
            apply_patches=False,
            force_rebuild=force_rebuild,
        )

    def _build_ref_version(
        self,
        adapter: MetaYamlAdapter,
        force_rebuild: bool,
    ) -> Optional[BuildVersion]:
        """Build the reference version (delta mode, no patches)."""
        ref_commit = adapter.get_ref_commit()
        if not ref_commit:
            return None

        return self._build_version(
            adapter=adapter,
            commit=ref_commit,
            build_tag=BuildTag.DELTA_REF,
            apply_patches=False,
            force_rebuild=force_rebuild,
        )

    def _build_allpatched_version(
        self,
        adapter: MetaYamlAdapter,
        force_rebuild: bool,
    ) -> Optional[BuildVersion]:
        """Build the all-patched version (all CPV patches applied)."""
        mode = adapter.get_mode()
        commit = (
            adapter.get_ref_commit()
            if mode == BenchmarkMode.DELTA
            else adapter.get_base_commit()
        )

        if not commit:
            logger.error("Cannot build all-patched version: commit is None")
            return None

        return self._build_version(
            adapter=adapter,
            commit=commit,
            build_tag=BuildTag.ALL_PATCHED,
            apply_patches=True,
            exclude_cpv=None,  # Apply ALL patches
            force_rebuild=force_rebuild,
        )

    def _build_cpv_versions(
        self,
        adapter: MetaYamlAdapter,
        force_rebuild: bool,
    ) -> List[BuildVersion]:
        """Build CPV-specific versions (all patches except one)."""
        versions = []
        mode = adapter.get_mode()
        commit = (
            adapter.get_ref_commit()
            if mode == BenchmarkMode.DELTA
            else adapter.get_base_commit()
        )

        if not commit:
            logger.error("Cannot build CPV versions: commit is None")
            return versions

        for cpv_num in adapter.get_cpv_numbers():
            version = self._build_version(
                adapter=adapter,
                commit=commit,
                build_tag=BuildTag.CPV,
                apply_patches=True,
                exclude_cpv=cpv_num,  # Exclude this CPV's patch
                cpv_num=cpv_num,
                force_rebuild=force_rebuild,
            )
            if version:
                versions.append(version)

        return versions

    def _build_version(
        self,
        adapter: MetaYamlAdapter,
        commit: str,
        build_tag: BuildTag,
        apply_patches: bool,
        exclude_cpv: Optional[int] = None,
        cpv_num: Optional[int] = None,
        *,
        force_rebuild: bool = False,
    ) -> Optional[BuildVersion]:
        """Build a specific variant version.

        Args:
            adapter: MetaYamlAdapter with config
            commit: Git commit to checkout
            build_tag: Type of variant
            apply_patches: Whether to apply patches
            exclude_cpv: CPV number to exclude from patching
            cpv_num: CPV number for CPV variants
            force_rebuild: Force rebuild even if cached

        Returns:
            BuildVersion if successful, None otherwise
        """
        variant_name = adapter.get_variant_name(build_tag, cpv_num)
        # Project path for OSS-Fuzz - use benchmark name directly without aixcc prefix
        project_path = variant_name

        # Check cache
        if not force_rebuild and self.reproducer.is_variant_built(project_path):
            logger.info(f"Using cached build for {variant_name}")
            return self._create_build_version(
                adapter, build_tag, commit, variant_name, cpv_num
            )

        logger.info(f"Building variant {variant_name}")

        # Create variant project directory
        variant_project_path = self._create_variant_project(adapter, variant_name)
        if not variant_project_path:
            return None

        # Clone and checkout source
        with tempfile.TemporaryDirectory() as temp_dir:
            try:
                repo_path = Path(temp_dir) / "repo"

                # Clone and checkout using cached repository if available
                repo_path_str = clone_or_copy_cached_repo(
                    repo_url=adapter.main_repo,
                    commit=commit,
                    target_dir=str(repo_path),
                    repo_name=adapter.repo_name,
                    verbose=True,
                )
                if not repo_path_str:
                    return None

                # Apply patches if needed
                if apply_patches:
                    # Use benchmark_path from adapter for patches location
                    # CRSBench structure: .aixcc/{harness}/cpv_{N}/patches/
                    if adapter.benchmark_path and adapter.benchmark_path.exists():
                        aixcc_dir = adapter.benchmark_path / ".aixcc"
                    else:
                        aixcc_dir = (
                            self.projects_base / adapter.benchmark_name / ".aixcc"
                        )

                    self._apply_cpv_patches(
                        repo_path,
                        aixcc_dir,
                        exclude_cpv,
                    )

                # Build fuzzers
                if not self.reproducer.build_fuzzers(project_path, repo_path):
                    logger.error(f"Failed to build fuzzers for {variant_name}")
                    return None

                return self._create_build_version(
                    adapter, build_tag, commit, variant_name, cpv_num
                )

            except Exception as e:
                logger.error(f"Build failed for {variant_name}: {e}")
                return None

    def _create_variant_project(
        self,
        adapter: MetaYamlAdapter,
        variant_name: str,
    ) -> Optional[Path]:
        """Create a variant project directory by copying the original.

        Args:
            adapter: MetaYamlAdapter with config
            variant_name: Name for the variant project

        Returns:
            Path to variant project, or None on failure
        """
        # Use benchmark_path from adapter if available
        if adapter.benchmark_path and adapter.benchmark_path.exists():
            original_path = adapter.benchmark_path
        else:
            original_path = self.projects_base / adapter.benchmark_name

        # Variant projects go directly under oss-fuzz/projects/
        variant_path = self.projects_base / variant_name

        if variant_path.exists():
            logger.debug(f"Variant project already exists: {variant_path}")
            return variant_path

        if not original_path.exists():
            logger.error(f"Original project not found: {original_path}")
            return None

        try:
            # Create parent directories if needed
            variant_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(original_path, variant_path)
            logger.info(f"Created variant project: {variant_name}")
            return variant_path
        except Exception as e:
            logger.error(f"Failed to create variant project: {e}")
            return None

    # TODO: unused now
    def _clone_and_checkout(
        self,
        repo_url: str,
        commit: str,
        dest_path: Path,
    ) -> bool:
        """Clone a repository and checkout a specific commit.

        Args:
            repo_url: Repository URL
            commit: Commit hash to checkout
            dest_path: Destination path for clone

        Returns:
            True if successful, False otherwise
        """
        try:
            # Clone
            result = run_git(
                ["clone", "--depth", "1", repo_url, str(dest_path)],
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
            )
            if result.returncode != 0:
                # Try full clone if shallow fails
                result = run_git(
                    ["clone", repo_url, str(dest_path)],
                    capture_output=True,
                    text=True,
                    timeout=600,
                    check=False,
                )
                if result.returncode != 0:
                    logger.error(f"Failed to clone {repo_url}: {result.stderr}")
                    return False

            # Fetch the specific commit
            run_git(
                ["-C", str(dest_path), "fetch", "origin", commit],
                capture_output=True,
                timeout=120,
                check=False,
            )

            # Checkout
            result = run_git(
                ["-C", str(dest_path), "checkout", commit],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            if result.returncode != 0:
                logger.error(f"Failed to checkout {commit}: {result.stderr}")
                return False

            return True

        except subprocess.TimeoutExpired:
            logger.error(f"Timeout cloning {repo_url}")
            return False
        except Exception as e:
            logger.error(f"Clone error: {e}")
            return False

    def _apply_cpv_patches(
        self,
        repo_path: Path,
        aixcc_dir: Path,
        exclude_cpv: Optional[int],
    ) -> None:
        """Apply CPV patches from CRSBench structure.

        CRSBench structure: .aixcc/{harness}/cpv_{N}/patches/patch_*.diff

        Args:
            repo_path: Path to the repository
            aixcc_dir: Path to .aixcc directory
            exclude_cpv: CPV number to exclude (skip its patch)
        """
        if not aixcc_dir.exists():
            logger.warning(f".aixcc directory not found: {aixcc_dir}")
            return

        # Find all cpv_* directories across all harnesses
        cpv_patches: Dict[int, List[Path]] = {}

        for harness_dir in aixcc_dir.iterdir():
            if not harness_dir.is_dir() or harness_dir.name in ("tests", "povs"):
                continue

            # Look for cpv_* directories in each harness
            for cpv_dir in harness_dir.glob("cpv_*"):
                if not cpv_dir.is_dir():
                    continue

                try:
                    cpv_num = int(cpv_dir.name.split("_")[1])
                except (IndexError, ValueError):
                    continue

                # Get patches from this CPV
                patches_dir = cpv_dir / "patches"
                if patches_dir.exists():
                    for patch_file in patches_dir.glob("*.diff"):
                        if cpv_num not in cpv_patches:
                            cpv_patches[cpv_num] = []
                        cpv_patches[cpv_num].append(patch_file)

        # Apply patches, excluding the specified CPV
        for cpv_num, patch_files in sorted(cpv_patches.items()):
            if exclude_cpv is not None and cpv_num == exclude_cpv:
                logger.debug(f"Skipping patches for cpv_{cpv_num}")
                continue

            for patch_file in patch_files:
                if not self._apply_patch(repo_path, patch_file):
                    logger.warning(f"Failed to apply patch: {patch_file}")
                else:
                    logger.debug(f"Applied patch: {patch_file.name} (cpv_{cpv_num})")

    def _apply_patches(
        self,
        repo_path: Path,
        patches_dir: Path,
        exclude_cpv: Optional[int],
    ) -> None:
        """Apply patches to a repository (legacy format).

        Args:
            repo_path: Path to the repository
            patches_dir: Path to patches directory
            exclude_cpv: CPV number to exclude (skip its patch)
        """
        if not patches_dir.exists():
            logger.warning(f"Patches directory not found: {patches_dir}")
            return

        # Get all patch files sorted by CPV number
        patch_files = self._get_patch_files(patches_dir)

        for cpv_num, patch_file in sorted(patch_files.items()):
            if exclude_cpv is not None and cpv_num == exclude_cpv:
                logger.debug(f"Skipping patch for cpv_{cpv_num}")
                continue

            if not self._apply_patch(repo_path, patch_file):
                logger.warning(f"Failed to apply patch: {patch_file.name}")

    def _get_patch_files(self, patches_dir: Path) -> Dict[int, Path]:
        """Get patch files mapped by CPV number.

        Args:
            patches_dir: Path to patches directory

        Returns:
            Dict mapping CPV number to patch file path
        """
        patches = {}
        for patch_file in patches_dir.glob("cpv_*.patch"):
            try:
                # Extract CPV number from filename (e.g., cpv_0.patch)
                cpv_num = int(patch_file.stem.split("_")[1])
                patches[cpv_num] = patch_file
            except (IndexError, ValueError):
                logger.warning(f"Invalid patch filename: {patch_file.name}")
        return patches

    def _apply_patch(self, repo_path: Path, patch_file: Path) -> bool:
        """Apply a single patch file.

        Args:
            repo_path: Path to repository
            patch_file: Path to patch file

        Returns:
            True if successful, False otherwise
        """
        try:
            # Resolve to absolute path since git apply runs from repo_path
            patch_file_abs = patch_file.resolve()
            result = subprocess.run(
                ["git", "apply", "--3way", str(patch_file_abs)],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode == 0:
                logger.debug(f"Applied patch: {patch_file.name}")
                return True
            logger.warning(f"Patch apply failed: {result.stderr}")
            return False
        except Exception as e:
            logger.error(f"Patch error: {e}")
            return False

    def _create_build_version(
        self,
        adapter: MetaYamlAdapter,
        build_tag: BuildTag,
        commit: str,
        variant_name: str,
        cpv_num: Optional[int],
    ) -> BuildVersion:
        """Create a BuildVersion instance."""
        return BuildVersion(
            benchmark_name=adapter.benchmark_name,
            lang=adapter.lang,
            mode=adapter.get_mode(),
            build_tag=build_tag,
            commit=commit,
            variant_project_name=variant_name,
            cpv_num=cpv_num,
        )

    def cleanup_variants(self, adapter: MetaYamlAdapter) -> None:
        """Remove all variant projects for a benchmark.

        Args:
            adapter: MetaYamlAdapter with config
        """
        if not self.projects_base.exists():
            return

        prefix = f"{adapter.benchmark_name}-"
        for variant_dir in self.projects_base.iterdir():
            if variant_dir.is_dir() and variant_dir.name.startswith(prefix):
                try:
                    shutil.rmtree(variant_dir)
                    logger.info(f"Removed variant: {variant_dir.name}")
                except Exception as e:
                    logger.error(f"Failed to remove {variant_dir.name}: {e}")
