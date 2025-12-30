"""OSS-Fuzz infrastructure utilities for the builder module.

This module provides OSSFuzzInfrastructure, which wraps OSS-Fuzz's helper.py
for building fuzzers with different configurations.
"""

import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from crsbench.builder.types import BuildConfig
from crsbench.utils.logger import get_logger
from crsbench.utils.repo_manager import clone_or_copy_cached_repo

logger = get_logger(__name__)


class OSSFuzzInfrastructure:
    """Infrastructure for building OSS-Fuzz projects.

    Wraps OSS-Fuzz's helper.py to provide a unified interface for:
    - Building validation variants (with address sanitizer)
    - Building coverage variants (with coverage sanitizer)
    - Managing variant project directories
    - Caching and build detection

    Attributes:
        oss_fuzz_path: Path to oss-fuzz directory
        projects_base: Path to oss-fuzz/projects directory
    """

    def __init__(self, oss_fuzz_path: Path):
        """Initialize the OSS-Fuzz infrastructure.

        Args:
            oss_fuzz_path: Path to oss-fuzz directory

        Raises:
            FileNotFoundError: If helper.py is not found
        """
        self.oss_fuzz_path = Path(oss_fuzz_path).resolve()
        self.projects_base = self.oss_fuzz_path / "projects"
        self._helper_script = self.oss_fuzz_path / "infra" / "helper.py"

        if not self._helper_script.exists():
            raise FileNotFoundError(
                f"OSS-Fuzz helper.py not found: {self._helper_script}"
            )

    def get_build_output_path(self, variant_name: str) -> Path:
        """Get the build output path for a variant.

        Args:
            variant_name: Variant name (e.g., "benchmark-deltabase")

        Returns:
            Path to build output directory
        """
        return self.oss_fuzz_path / "build" / "out" / variant_name

    def is_variant_built(self, variant_name: str) -> bool:
        """Check if a variant has been built.

        Args:
            variant_name: Variant name

        Returns:
            True if built fuzzers exist
        """
        build_path = self.get_build_output_path(variant_name)
        project_path = self.projects_base / variant_name

        # Both project directory and build output must exist
        if not build_path.exists() or not project_path.exists():
            return False

        # Check that build output has actual files
        return any(build_path.iterdir())

    def create_variant_project(
        self,
        benchmark_path: Path,
        variant_name: str,
    ) -> Optional[Path]:
        """Create a variant project directory by copying the original.

        Args:
            benchmark_path: Path to original benchmark directory
            variant_name: Name for the variant project

        Returns:
            Path to variant project, or None on failure
        """
        variant_path = self.projects_base / variant_name

        if variant_path.exists():
            logger.debug(f"Variant project already exists: {variant_path}")
            return variant_path

        if not benchmark_path.exists():
            logger.error(f"Benchmark path not found: {benchmark_path}")
            return None

        try:
            variant_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(benchmark_path, variant_path)
            logger.info(f"Created variant project: {variant_name}")
            return variant_path
        except Exception as e:
            logger.error(f"Failed to create variant project: {e}")
            return None

    def clone_source(
        self,
        config: BuildConfig,
        dest_dir: Path,
    ) -> Optional[Path]:
        """Clone and checkout source repository.

        Args:
            config: Build configuration
            dest_dir: Destination directory for clone

        Returns:
            Path to cloned repository, or None on failure
        """
        repo_path = dest_dir / "repo"

        result = clone_or_copy_cached_repo(
            repo_url=config.main_repo,
            commit=config.commit,
            target_dir=str(repo_path),
            repo_name=config.repo_name,
            verbose=True,
        )

        if not result:
            logger.error(f"Failed to clone repository: {config.main_repo}")
            return None

        return repo_path

    def build_fuzzers(
        self,
        config: BuildConfig,
        src_path: Path,
    ) -> bool:
        """Build fuzzers for a variant.

        Args:
            config: Build configuration
            src_path: Path to source repository

        Returns:
            True if build succeeded
        """
        variant_name = config.variant_name
        logger.debug(f"Building {variant_name} ({config.language})")

        # Map language to OSS-Fuzz FUZZING_LANGUAGE format
        fuzzing_language = config.language if config.language != "c" else "c++"

        # Build command
        cmd = [
            "python3",
            str(self._helper_script),
            "build_fuzzers",
            "--sanitizer",
            config.sanitizer,
            "-e",
            f"FUZZING_LANGUAGE={fuzzing_language}",
            variant_name,
            str(src_path),
        ]

        logger.info(f"Building {variant_name} with {config.sanitizer} sanitizer...")
        logger.debug(f"Command: {' '.join(cmd)}")

        try:
            result = subprocess.run(
                cmd,
                cwd=self.oss_fuzz_path,
                capture_output=True,
                text=True,
                timeout=config.timeout,
            )

            if result.returncode == 0:
                logger.info(f"Build succeeded for {variant_name}")
                self._fix_build_ownership(variant_name)
                return True

            logger.error(
                f"Build failed for {variant_name}. "
                f"Exit code: {result.returncode}\n"
                f"stdout: {result.stdout[:2000]}...\n"
                f"stderr: {result.stderr[:2000]}..."
            )
            return False

        except subprocess.TimeoutExpired:
            logger.error(f"Build timed out for {variant_name} ({config.timeout}s)")
            return False
        except Exception as e:
            logger.error(f"Build error for {variant_name}: {e}")
            return False

    def _fix_build_ownership(self, variant_name: str) -> None:
        """Fix ownership of build output files.

        Docker builds run as root, so we chown the output to current user.

        Args:
            variant_name: Variant name
        """
        build_path = self.get_build_output_path(variant_name)
        if not build_path.exists():
            return

        try:
            uid = os.getuid()
            gid = os.getgid()
            subprocess.run(
                ["sudo", "chown", "-R", f"{uid}:{gid}", str(build_path)],
                capture_output=True,
                timeout=30,
            )
            logger.debug(f"Fixed ownership of {build_path}")
        except Exception as e:
            logger.debug(f"Could not fix ownership of {build_path}: {e}")

    def get_cpv_patches(self, benchmark_path: Path) -> dict[int, list[Path]]:
        """Get all CPV patches from a benchmark's .aixcc directory.

        CRSBench structure: .aixcc/{harness}/cpv_{N}/patches/patch_*.diff

        Args:
            benchmark_path: Path to benchmark directory

        Returns:
            Dict mapping CPV number to list of patch files
        """
        aixcc_dir = benchmark_path / ".aixcc"
        if not aixcc_dir.exists():
            logger.warning(f".aixcc directory not found: {aixcc_dir}")
            return {}

        cpv_patches: dict[int, list[Path]] = {}

        for harness_dir in aixcc_dir.iterdir():
            if not harness_dir.is_dir() or harness_dir.name in ("tests", "povs"):
                continue

            for cpv_dir in harness_dir.glob("cpv_*"):
                if not cpv_dir.is_dir():
                    continue

                try:
                    cpv_num = int(cpv_dir.name.split("_")[1])
                except (IndexError, ValueError):
                    continue

                patches_dir = cpv_dir / "patches"
                if patches_dir.exists():
                    for patch_file in sorted(patches_dir.glob("*.diff")):
                        if cpv_num not in cpv_patches:
                            cpv_patches[cpv_num] = []
                        cpv_patches[cpv_num].append(patch_file)

        return cpv_patches

    def get_all_patches(self, benchmark_path: Path) -> list[Path]:
        """Get all CPV patches as a flat list.

        Args:
            benchmark_path: Path to benchmark directory

        Returns:
            Sorted list of all patch files
        """
        cpv_patches = self.get_cpv_patches(benchmark_path)
        all_patches = []
        for cpv_num in sorted(cpv_patches.keys()):
            all_patches.extend(cpv_patches[cpv_num])
        return all_patches

    def get_patches_except(self, benchmark_path: Path, exclude_cpv: int) -> list[Path]:
        """Get all CPV patches except for a specific CPV.

        Args:
            benchmark_path: Path to benchmark directory
            exclude_cpv: CPV number to exclude

        Returns:
            List of patch files excluding the specified CPV
        """
        cpv_patches = self.get_cpv_patches(benchmark_path)
        patches = []
        for cpv_num in sorted(cpv_patches.keys()):
            if cpv_num != exclude_cpv:
                patches.extend(cpv_patches[cpv_num])
        return patches

    def apply_patch(self, repo_path: Path, patch_file: Path) -> bool:
        """Apply a single patch file to a repository.

        Args:
            repo_path: Path to repository
            patch_file: Path to patch file

        Returns:
            True if patch applied successfully
        """
        return self._apply_single_patch(repo_path, patch_file)

    def apply_patches_from_list(self, repo_path: Path, patches: list[Path]) -> bool:
        """Apply a list of patches to a repository.

        Args:
            repo_path: Path to repository
            patches: List of patch files to apply

        Returns:
            True if all patches applied successfully
        """
        success = True
        for patch_file in patches:
            if not self._apply_single_patch(repo_path, patch_file):
                logger.warning(f"Failed to apply patch: {patch_file}")
                success = False
            else:
                logger.debug(f"Applied patch: {patch_file.name}")
        return success

    def apply_patches(
        self,
        repo_path: Path,
        aixcc_dir: Path,
        exclude_cpv: Optional[int] = None,
    ) -> None:
        """Apply CPV patches from CRSBench structure.

        DEPRECATED: Use apply_patches_from_list() with get_all_patches() or
        get_patches_except() instead.

        CRSBench structure: .aixcc/{harness}/cpv_{N}/patches/patch_*.diff

        Args:
            repo_path: Path to the repository
            aixcc_dir: Path to .aixcc directory
            exclude_cpv: CPV number to exclude (skip its patches)
        """
        if not aixcc_dir.exists():
            logger.warning(f".aixcc directory not found: {aixcc_dir}")
            return

        # Find all cpv_* directories across all harnesses
        cpv_patches: dict[int, list[Path]] = {}

        for harness_dir in aixcc_dir.iterdir():
            if not harness_dir.is_dir() or harness_dir.name in ("tests", "povs"):
                continue

            for cpv_dir in harness_dir.glob("cpv_*"):
                if not cpv_dir.is_dir():
                    continue

                try:
                    cpv_num = int(cpv_dir.name.split("_")[1])
                except (IndexError, ValueError):
                    continue

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
                if not self._apply_single_patch(repo_path, patch_file):
                    logger.warning(f"Failed to apply patch: {patch_file}")
                else:
                    logger.debug(f"Applied patch: {patch_file.name} (cpv_{cpv_num})")

    def _apply_single_patch(self, repo_path: Path, patch_file: Path) -> bool:
        """Apply a single patch file.

        Args:
            repo_path: Path to repository
            patch_file: Path to patch file

        Returns:
            True if successful
        """
        try:
            patch_file_abs = patch_file.resolve()
            result = subprocess.run(
                ["git", "apply", "--3way", str(patch_file_abs)],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=60,
                stdin=subprocess.DEVNULL,  # Prevent interactive terminal issues
            )
            if result.returncode == 0:
                return True
            logger.warning(f"Patch apply failed: {result.stderr}")
            return False
        except Exception as e:
            logger.error(f"Patch error: {e}")
            return False

    def cleanup_variant(self, variant_name: str) -> None:
        """Remove variant project and build artifacts.

        Args:
            variant_name: Variant name
        """
        # Remove project directory
        variant_path = self.projects_base / variant_name
        if variant_path.exists():
            try:
                shutil.rmtree(variant_path)
                logger.info(f"Removed variant project: {variant_path}")
            except Exception as e:
                logger.error(f"Failed to remove variant project: {e}")

        # Remove build output
        build_path = self.get_build_output_path(variant_name)
        if build_path.exists():
            try:
                shutil.rmtree(build_path)
                logger.info(f"Removed build output: {build_path}")
            except Exception as e:
                logger.error(f"Failed to remove build output: {e}")
