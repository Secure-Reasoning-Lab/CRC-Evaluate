"""OSS-Fuzz infrastructure utilities for the builder module.

This module provides OSSFuzzInfrastructure, which wraps OSS-Fuzz's helper.py
for building fuzzers and reproducing crashes.
"""

import json
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Optional

from crsbench.builder.types import (
    BUILD_METADATA_FILE,
    BuildConfig,
    BuildMetadata,
    FuzzerBuildResult,
    ReproduceOutput,
)
from crsbench.utils.docker import fix_docker_ownership
from crsbench.utils.logger import get_logger
from crsbench.utils.repo_manager import clone_or_copy_cached_repo

logger = get_logger(__name__)
# Separate logger for crash reproduction results
reproduce_logger = get_logger("reproduce")

# Exit code constants from helper.py
EXIT_CODE_TIMEOUT = 124  # Subprocess timeout in helper.py

# Track initialized OSS-Fuzz paths to avoid redundant setup
_initialized_oss_fuzz_paths: set[Path] = set()


def ensure_oss_fuzz_ready(oss_fuzz_path: Path) -> None:
    """Ensure OSS-Fuzz directory is ready for parallel builds.

    Creates the build directory upfront to avoid race condition in helper.py
    when multiple parallel builds try to create it simultaneously.

    This function is idempotent. Concurrent calls are safe because
    mkdir(exist_ok=True) handles races gracefully.

    Note: Path tracking is per-process. With multiprocessing, each process
    maintains its own cache, but this is harmless since mkdir is idempotent.

    Args:
        oss_fuzz_path: Path to oss-fuzz directory
    """
    path = Path(oss_fuzz_path).resolve()
    if path in _initialized_oss_fuzz_paths:
        return

    # Create build directory to prevent TOCTOU race in helper.py
    # where it checks existence then creates, causing FileExistsError
    (path / "build").mkdir(exist_ok=True)
    _initialized_oss_fuzz_paths.add(path)
    logger.debug(f"Initialized OSS-Fuzz build directory: {path / 'build'}")


class OSSFuzzInfrastructure:
    """Infrastructure for building OSS-Fuzz projects.

    Wraps OSS-Fuzz's helper.py to provide a unified interface for:
    - Building validation variants (with address sanitizer)
    - Building coverage variants (with coverage sanitizer)
    - Managing variant project directories
    - Caching and build detection

    Supports two modes:
    1. Shared mode (default): Uses oss_fuzz_path directly for everything
    2. Isolated mode: Builds output to work_dir, with symlinks from oss_fuzz_path
       so helper.py can find them

    Attributes:
        oss_fuzz_path: Path to oss-fuzz directory
        projects_base: Path to oss-fuzz/projects directory
        work_dir: Working directory for isolated builds (None = shared mode)
    """

    def __init__(self, oss_fuzz_path: Path, work_dir: Optional[Path] = None):
        """Initialize the OSS-Fuzz infrastructure.

        Args:
            oss_fuzz_path: Path to oss-fuzz directory
            work_dir: Optional working directory for isolated builds.
                If provided, builds output to work_dir/{variant}/ with symlinks
                from oss-fuzz/build/out/{variant} for helper.py compatibility.

        Raises:
            FileNotFoundError: If helper.py is not found
        """
        self.oss_fuzz_path = Path(oss_fuzz_path).resolve()
        self.work_dir = Path(work_dir).resolve() if work_dir else None
        self.projects_base = self.oss_fuzz_path / "projects"
        self._helper_script = self.oss_fuzz_path / "infra" / "helper.py"

        # Cache for inc-build image availability (project:sanitizer -> bool)
        # Prevents redundant docker pull attempts during parallel builds
        self._inc_image_cache: dict[str, bool] = {}
        self._inc_image_lock = threading.Lock()

        # Ensure OSS-Fuzz is ready for parallel builds
        ensure_oss_fuzz_ready(self.oss_fuzz_path)

        if not self._helper_script.exists():
            raise FileNotFoundError(
                f"OSS-Fuzz helper.py not found: {self._helper_script}"
            )

    def get_build_output_path(self, variant_name: str) -> Path:
        """Get the build output path for a variant (in oss-fuzz).

        This is the path helper.py expects to find builds.
        In isolated mode, this is a symlink target.

        Args:
            variant_name: Variant name (e.g., "benchmark-deltabase")

        Returns:
            Path to build output directory in oss-fuzz
        """
        return self.oss_fuzz_path / "build" / "out" / variant_name

    def get_isolated_build_path(self, variant_name: str) -> Path:
        """Get the actual build output path (isolated or shared).

        In isolated mode, returns path in work_dir.
        In shared mode, returns the oss-fuzz path.

        Args:
            variant_name: Variant name

        Returns:
            Path to actual build output directory
        """
        if self.work_dir:
            return self.work_dir / variant_name / "build" / "out"
        return self.get_build_output_path(variant_name)

    def get_isolated_work_path(self, variant_name: str) -> Path:
        """Get the work directory path (isolated or shared).

        Args:
            variant_name: Variant name

        Returns:
            Path to work directory
        """
        if self.work_dir:
            return self.work_dir / variant_name / "build" / "work"
        return self.oss_fuzz_path / "build" / "work" / variant_name

    def get_isolated_src_path(self, variant_name: str) -> Path:
        """Get the source directory path (isolated or shared).

        Args:
            variant_name: Variant name

        Returns:
            Path to source directory
        """
        if self.work_dir:
            return self.work_dir / variant_name / "src"
        return self.oss_fuzz_path / "build" / "src" / variant_name

    def is_isolated(self) -> bool:
        """Check if running in isolated mode.

        Returns:
            True if work_dir is set (isolated mode)
        """
        return self.work_dir is not None

    def setup_build_symlink(self, variant_name: str) -> bool:
        """Create symlink from oss-fuzz/build/out/{variant} to isolated build.

        This allows helper.py to find builds in isolated directories.
        Only needed in isolated mode.

        Args:
            variant_name: Variant name

        Returns:
            True if symlink created or not needed
        """
        if not self.work_dir:
            return True  # Not needed in shared mode

        isolated_path = self.get_isolated_build_path(variant_name)
        ossfuzz_path = self.get_build_output_path(variant_name)

        if not isolated_path.exists():
            logger.warning(f"Isolated build path not found: {isolated_path}")
            return False

        # Check if correct symlink already exists
        if ossfuzz_path.is_symlink():
            if ossfuzz_path.resolve() == isolated_path.resolve():
                logger.debug(f"Symlink already exists: {ossfuzz_path}")
                return True
            ossfuzz_path.unlink()
        elif ossfuzz_path.exists():
            logger.warning(f"Removing existing dir for symlink: {ossfuzz_path}")
            shutil.rmtree(ossfuzz_path)

        ossfuzz_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            ossfuzz_path.symlink_to(isolated_path)
            logger.debug(f"Created symlink: {ossfuzz_path} -> {isolated_path}")
            return True
        except FileExistsError:
            # Race condition: another worker created it
            if (
                ossfuzz_path.is_symlink()
                and ossfuzz_path.resolve() == isolated_path.resolve()
            ):
                logger.debug(f"Symlink created by another worker: {ossfuzz_path}")
                return True
            logger.error(f"Symlink exists with wrong target: {ossfuzz_path}")
            return False
        except Exception as e:
            logger.error(f"Failed to create symlink: {e}")
            return False

    def cleanup_isolated_build(self, variant_name: str) -> None:
        """Clean up isolated build for a variant.

        Removes:
        - Symlink from oss-fuzz/build/out/{variant}
        - Isolated build directory in work_dir/{variant}/

        Args:
            variant_name: Variant name
        """
        # Remove symlink from oss-fuzz
        ossfuzz_path = self.get_build_output_path(variant_name)
        if ossfuzz_path.is_symlink():
            ossfuzz_path.unlink()
            logger.debug(f"Removed symlink: {ossfuzz_path}")

        # Remove isolated directory
        if self.work_dir:
            isolated_dir = self.work_dir / variant_name
            if isolated_dir.exists():
                try:
                    shutil.rmtree(isolated_dir)
                    logger.debug(f"Removed isolated build: {isolated_dir}")
                except Exception as e:
                    logger.warning(f"Failed to remove isolated build: {e}")

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
                When True, also rejects builds that used fallback.

        Returns:
            True if built fuzzers exist (and match required inc_build mode if specified)
        """
        build_path = self.get_build_output_path(variant_name)
        project_path = self.projects_base / variant_name

        # Both project directory and build output must exist
        if not build_path.exists() or not project_path.exists():
            return False

        # Check that build output has actual files
        if not any(build_path.iterdir()):
            return False

        # If require_inc_build is specified, verify the cached build matches
        if require_inc_build is not None:
            metadata = self.read_build_metadata(variant_name)
            if metadata is None:
                # No metadata = legacy build, treat as standard (non-inc) build
                cached_is_inc = False
                fallback_used = False
            else:
                cached_is_inc = metadata.inc_build
                fallback_used = metadata.fallback_used

            if cached_is_inc != require_inc_build:
                logger.debug(
                    f"Cache mismatch for {variant_name}: "
                    f"cached inc_build={cached_is_inc}, required={require_inc_build}"
                )
                return False

            # If inc-build is required, reject builds that used fallback
            # A fallback build is not a true inc-build and shouldn't be used for CI validation
            if require_inc_build and fallback_used:
                logger.debug(
                    f"Cache mismatch for {variant_name}: "
                    f"cached build used fallback, but pure inc-build required"
                )
                return False

        return True

    def write_build_metadata(
        self,
        variant_name: str,
        *,
        inc_build: bool = False,
        sanitizer: str = "address",
        fallback_used: bool = False,
    ) -> None:
        """Write build metadata to the build output directory.

        Uses atomic write (write to temp file, then rename) to prevent
        race conditions when multiple workers might access the same file.

        Args:
            variant_name: Variant name
            inc_build: Whether this was an incremental build
            sanitizer: Sanitizer used for the build
            fallback_used: Whether fallback to clean build was used
        """
        from datetime import datetime

        build_path = self.get_build_output_path(variant_name)
        if not build_path.exists():
            logger.warning(f"Build path does not exist: {build_path}")
            return

        metadata = BuildMetadata(
            inc_build=inc_build,
            sanitizer=sanitizer,
            timestamp=datetime.now().isoformat(),
            fallback_used=fallback_used,
        )

        metadata_path = build_path / BUILD_METADATA_FILE
        temp_path = metadata_path.with_suffix(".tmp")
        try:
            # Atomic write: write to temp file, then rename
            with temp_path.open("w") as f:
                json.dump(metadata.to_dict(), f, indent=2)
            temp_path.replace(metadata_path)  # Atomic on POSIX
            logger.debug(f"Wrote build metadata: {metadata_path}")
        except Exception as e:
            logger.warning(f"Failed to write build metadata: {e}")
            # Clean up temp file if it exists
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)

    def read_build_metadata(self, variant_name: str) -> Optional[BuildMetadata]:
        """Read build metadata from the build output directory.

        Args:
            variant_name: Variant name

        Returns:
            BuildMetadata if found, None otherwise
        """
        build_path = self.get_build_output_path(variant_name)
        metadata_path = build_path / BUILD_METADATA_FILE

        if not metadata_path.exists():
            return None

        try:
            with metadata_path.open() as f:
                data = json.load(f)
            return BuildMetadata.from_dict(data)
        except Exception as e:
            logger.warning(f"Failed to read build metadata: {e}")
            return None

    def has_harness(self, variant_name: str, harness_name: str) -> bool:
        """Check if a specific harness exists in the build output.

        Args:
            variant_name: Variant name
            harness_name: Name of the harness executable

        Returns:
            True if the harness executable exists in build output
        """
        build_path = self.get_build_output_path(variant_name)
        if not build_path.exists():
            return False

        harness_path = build_path / harness_name
        return harness_path.exists() and harness_path.is_file()

    # =========================================================================
    # Cleanup methods (shared by POV, patch, coverage verification)
    # =========================================================================

    def cleanup_build_outputs(self, variant_name: str) -> None:
        """Clean up build outputs only (for force rebuild).

        Removes:
        - Build output directory (or symlink) at oss-fuzz/build/out/{variant}
        - Isolated build directory if work_dir is set
        - Work directory at oss-fuzz/build/work/{variant}

        Does NOT remove:
        - Project symlink (read-only, reusable)

        Args:
            variant_name: Variant name
        """
        # Remove build output (symlink or directory)
        build_path = self.get_build_output_path(variant_name)
        if build_path.is_symlink():
            build_path.unlink()
            logger.debug(f"Removed build symlink: {build_path}")
        elif build_path.exists():
            # Fix Docker ownership before removal (files may be owned by root)
            fix_docker_ownership(build_path)
            try:
                shutil.rmtree(build_path)
                logger.debug(f"Removed build output: {build_path}")
            except Exception as e:
                logger.warning(f"Failed to remove build output: {e}")

        # Remove isolated build directory if using work_dir
        if self.work_dir:
            isolated_build = self.get_isolated_build_path(variant_name)
            if isolated_build.exists():
                fix_docker_ownership(isolated_build)
                try:
                    shutil.rmtree(isolated_build)
                    logger.debug(f"Removed isolated build: {isolated_build}")
                except Exception as e:
                    logger.warning(f"Failed to remove isolated build: {e}")

        # Remove work directory
        work_path = self.get_isolated_work_path(variant_name)
        if work_path.exists():
            fix_docker_ownership(work_path)
            try:
                shutil.rmtree(work_path)
                logger.debug(f"Removed work dir: {work_path}")
            except Exception as e:
                logger.warning(f"Failed to remove work dir: {e}")

    def cleanup_project_symlink(self, variant_name: str) -> None:
        """Clean up project symlink only.

        Removes:
        - Project symlink at oss-fuzz/projects/{variant}

        Args:
            variant_name: Variant name
        """
        variant_path = self.projects_base / variant_name
        if variant_path.is_symlink():
            variant_path.unlink()
            logger.debug(f"Removed project symlink: {variant_path}")
        elif variant_path.exists():
            # Should not happen with symlink-based approach, but handle it
            try:
                shutil.rmtree(variant_path)
                logger.debug(f"Removed project dir: {variant_path}")
            except Exception as e:
                logger.warning(f"Failed to remove project dir: {e}")

    def cleanup_source(self, variant_name: str) -> None:
        """Clean up source directory only.

        Removes:
        - Source directory at work_dir/{variant}/src or build/src/{variant}

        Args:
            variant_name: Variant name
        """
        src_path = self.get_isolated_src_path(variant_name)
        if src_path.exists():
            # Fix Docker ownership before removal (files may be owned by root)
            fix_docker_ownership(src_path)
            try:
                shutil.rmtree(src_path)
                logger.debug(f"Removed source dir: {src_path}")
            except Exception as e:
                logger.warning(f"Failed to remove source dir: {e}")

    def create_variant_project(
        self,
        benchmark_path: Path,
        variant_name: str,
    ) -> Optional[Path]:
        """Create a variant project directory as symlink to benchmark.

        The project directory (Dockerfile, build.sh, etc.) is read-only during
        build/run, so we can safely symlink instead of copying.

        Thread-safe: handles race conditions when multiple workers try to
        create the same symlink simultaneously.

        Args:
            benchmark_path: Path to original benchmark directory
            variant_name: Name for the variant project

        Returns:
            Path to variant project symlink, or None on failure
        """
        variant_path = self.projects_base / variant_name
        target_path = benchmark_path.resolve()

        if variant_path.is_symlink():
            # Already exists - verify it points to the right target
            if variant_path.resolve() == target_path:
                logger.debug(f"Variant project already exists: {variant_path}")
                return variant_path
            # Wrong target - remove and recreate
            variant_path.unlink()
        elif variant_path.exists():
            logger.debug(f"Variant project already exists: {variant_path}")
            return variant_path

        if not benchmark_path.exists():
            logger.error(f"Benchmark path not found: {benchmark_path}")
            return None

        try:
            variant_path.parent.mkdir(parents=True, exist_ok=True)
            variant_path.symlink_to(target_path)
            logger.debug(f"Linked variant project: {variant_name} -> {benchmark_path}")
            return variant_path
        except FileExistsError:
            # Race condition: another worker created it first
            # Check if it's correct and return success
            if variant_path.is_symlink() and variant_path.resolve() == target_path:
                logger.debug(
                    f"Variant project created by another worker: {variant_path}"
                )
                return variant_path
            logger.error(f"Variant project exists with wrong target: {variant_path}")
            return None
        except Exception as e:
            logger.error(f"Failed to create variant project: {e}")
            return None

    def link_variant_project(
        self,
        source_project: str,
        variant_name: str,
    ) -> Optional[Path]:
        """Create a variant project as symlink to existing project.

        Use this when the variant shares the same project config (Dockerfile,
        build.sh, etc.) as an existing project. Avoids duplicate copies.

        Args:
            source_project: Name of existing project in oss-fuzz/projects/
            variant_name: Name for the variant project

        Returns:
            Path to variant project symlink, or None on failure
        """
        variant_path = self.projects_base / variant_name
        source_path = self.projects_base / source_project

        # Check if correct symlink already exists
        if variant_path.is_symlink():
            if variant_path.resolve() == source_path.resolve():
                logger.debug(f"Variant project already exists: {variant_path}")
                return variant_path
            # Wrong target - remove and recreate
            variant_path.unlink()
        elif variant_path.exists():
            logger.debug(f"Variant project already exists: {variant_path}")
            return variant_path

        if not source_path.exists():
            logger.error(f"Source project not found: {source_path}")
            return None

        try:
            variant_path.symlink_to(source_path)
            logger.debug(f"Linked variant project: {variant_name} -> {source_project}")
            return variant_path
        except FileExistsError:
            # Race condition: another worker created it
            if (
                variant_path.is_symlink()
                and variant_path.resolve() == source_path.resolve()
            ):
                logger.debug(
                    f"Variant project created by another worker: {variant_path}"
                )
                return variant_path
            logger.error(f"Variant project exists with wrong target: {variant_path}")
            return None
        except Exception as e:
            logger.error(f"Failed to link variant project: {e}")
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
        src_path: Optional[Path] = None,
        *,
        use_inc_image: bool = False,
        inc_fallback: bool = False,
    ) -> FuzzerBuildResult:
        """Build fuzzers for a variant.

        Args:
            config: Build configuration
            src_path: Path to source repository. If None, uses bundled source in Docker.
            use_inc_image: Use pre-built inc-build image for faster builds
            inc_fallback: Fallback to clean build using /src instead of /built-src.
                Use when incremental build fails due to incompatibility.

        Returns:
            FuzzerBuildResult with success status and fallback tracking.
            When inc_fallback=True is used, fallback_used will be True to indicate
            that the build MAY have used fallback (we can't detect if it was actually
            triggered, but we track the intent for correct cache metadata).
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
        ]

        # Use inc-build image for faster incremental builds
        if use_inc_image:
            cmd.extend(
                [
                    "--docker_image_tag",
                    f"inc-{config.sanitizer}",
                    "--no-build-image",
                ]
            )
            # Add inc-fallback flag if needed
            if inc_fallback:
                cmd.append("--inc-fallback")

        cmd.append(variant_name)
        # Only add source path if provided (not using bundled pkgs/)
        if src_path:
            cmd.append(str(src_path))

        logger.debug(f"Building {variant_name} with {config.sanitizer} sanitizer...")
        logger.debug(f"Build command: {' '.join(cmd)}")

        try:
            result = subprocess.run(
                cmd,
                cwd=self.oss_fuzz_path,
                capture_output=True,
                text=True,
                timeout=config.timeout,
                stdin=subprocess.DEVNULL,  # Prevent terminal issues
            )

            if result.returncode == 0:
                logger.debug(f"Build succeeded for {variant_name}")
                # Log build output for debugging
                if result.stdout:
                    logger.debug(f"Build stdout: {result.stdout[:1000]}")
                fix_docker_ownership(self.get_build_output_path(variant_name))
                # Track if fallback may have been used
                # Only relevant when inc-build was attempted (use_inc_image=True)
                # and fallback was enabled (inc_fallback=True)
                actual_fallback = use_inc_image and inc_fallback
                return FuzzerBuildResult(success=True, fallback_used=actual_fallback)

            logger.error(
                f"Build failed for {variant_name}. "
                f"Exit code: {result.returncode}\n"
                f"stdout: {result.stdout[:2000] if result.stdout else 'None'}...\n"
                f"stderr: {result.stderr[:2000] if result.stderr else 'None'}..."
            )
            return FuzzerBuildResult(success=False, fallback_used=False)

        except subprocess.TimeoutExpired:
            logger.error(f"Build timed out for {variant_name} ({config.timeout}s)")
            return FuzzerBuildResult(success=False, fallback_used=False)
        except Exception as e:
            logger.error(f"Build error for {variant_name}: {e}")
            return FuzzerBuildResult(success=False, fallback_used=False)

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

        Uses --whitespace=nowarn instead of --3way because bundled tarballs
        have fresh git init with different blob hashes than the original
        repository where patches were created. The --3way option requires
        matching index hashes which won't exist.

        Args:
            repo_path: Path to repository
            patch_file: Path to patch file

        Returns:
            True if successful
        """
        try:
            patch_file_abs = patch_file.resolve()
            result = subprocess.run(
                ["git", "apply", "--whitespace=nowarn", str(patch_file_abs)],
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
        """Remove all variant artifacts (full cleanup).

        Convenience method that removes everything:
        - Project symlink
        - Build outputs
        - Source directory (if isolated)

        For granular cleanup, use:
        - cleanup_project_symlink() - project symlink only
        - cleanup_build_outputs() - build outputs only
        - cleanup_source() - source directory only

        Args:
            variant_name: Variant name
        """
        self.cleanup_project_symlink(variant_name)
        self.cleanup_build_outputs(variant_name)
        self.cleanup_source(variant_name)

    def reproduce(
        self,
        project_name: str,
        harness: str,
        pov_data: bytes,
        timeout: int = 180,
        request_id: Optional[int] = None,
        pov_id: Optional[str] = None,
    ) -> "ReproduceOutput":
        """Reproduce a crash using OSS-Fuzz helper.py.

        Uses --propagate_exit_codes to get explicit exit codes:
        - 0: No crash
        - 77: ASAN crash
        - 124: Timeout (treated as no crash)
        - Other non-zero: Other crash types

        Args:
            project_name: Variant name (e.g., "benchmark-deltaref")
            harness: Harness name to run
            pov_data: Raw POV/testcase bytes
            timeout: Timeout for reproduce operation in seconds (default: 180s
                to handle large projects like wireshark)
            request_id: Optional request ID for logging
            pov_id: Optional POV identifier for logging

        Returns:
            ReproduceOutput with crashed status and stdout/stderr
        """
        from crsbench.builder.types import ReproduceOutput

        # Write POV data to temporary file
        with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
            f.write(pov_data)
            testcase_path = Path(f.name)

        req_prefix = f"[Request #{request_id}] " if request_id else ""
        pov_prefix = f"[{pov_id}] " if pov_id else ""

        try:
            cmd = [
                "python3",
                str(self._helper_script),
                "reproduce",
                "--propagate_exit_codes",
                "--timeout",
                str(timeout),
                project_name,
                harness,
                str(testcase_path),
                "--",
                "-detect_leaks=0",
            ]

            logger.debug(f"{req_prefix}Reproducing: {' '.join(cmd)}")

            # Use slightly longer subprocess timeout to let helper.py handle it
            # Use binary mode to handle fuzzer output that may contain non-UTF-8 bytes
            result = subprocess.run(
                cmd,
                cwd=self.oss_fuzz_path,
                capture_output=True,
                timeout=timeout + 30,  # Grace period for helper.py
                stdin=subprocess.DEVNULL,  # Prevent terminal issues
            )

            # Decode output with error handling for binary content
            stdout = result.stdout.decode("utf-8", errors="replace")
            stderr = result.stderr.decode("utf-8", errors="replace")

            # Handle exit codes explicitly
            if result.returncode == 0:
                reproduce_logger.debug(
                    f"{req_prefix}{pov_prefix}{project_name}/{harness} did not crash"
                )
                return ReproduceOutput(
                    crashed=False,
                    stdout=stdout,
                    stderr=stderr,
                    exit_code=0,
                )
            if result.returncode == EXIT_CODE_TIMEOUT:
                reproduce_logger.debug(
                    f"{req_prefix}{pov_prefix}{project_name}/{harness} "
                    "timed out (exit code 124)"
                )
                return ReproduceOutput(
                    crashed=False,
                    stdout=stdout,
                    stderr=stderr,
                    exit_code=124,
                )
            logger.debug(
                f"{req_prefix}{pov_prefix}{project_name}/{harness} crashed "
                f"(exit code {result.returncode})"
            )
            return ReproduceOutput(
                crashed=True,
                stdout=stdout,
                stderr=stderr,
                exit_code=result.returncode,
            )

        except subprocess.TimeoutExpired:
            # Our subprocess timeout (shouldn't happen with grace period)
            logger.warning(
                f"{req_prefix}Subprocess timeout for {project_name}/{harness}"
            )
            return ReproduceOutput(crashed=False)
        except Exception as e:
            logger.error(
                f"{req_prefix}Reproduce error for {project_name}/{harness}: {e}"
            )
            return ReproduceOutput(crashed=False)
        finally:
            # Clean up temporary file
            testcase_path.unlink(missing_ok=True)

    def run_coverage(
        self,
        project_name: str,
        harness: str,
        corpus_dir: Path,
        output_dir: Path,
        timeout: int = 300,
    ) -> tuple[bool, Path]:
        """Run coverage collection using OSS-Fuzz helper.py.

        Uses --coverage-output-dir to output results to a custom directory,
        enabling parallel coverage collection with shared coverage binaries.

        Args:
            project_name: Coverage variant name (e.g., "benchmark-coverage")
            harness: Harness/fuzz target name
            corpus_dir: Directory containing corpus files
            output_dir: Directory for coverage output (dumps, report, etc.)
            timeout: Timeout in seconds

        Returns:
            Tuple of (success: bool, output_dir: Path)
            The caller should parse the appropriate file based on language:
            - C/C++: output_dir/report/linux/summary.json
            - Java: output_dir/report/linux/jacoco.xml
        """
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        cmd = [
            "python3",
            str(self._helper_script),
            "coverage",
            "--coverage-output-dir",
            str(output_dir),
            "--corpus-dir",
            str(corpus_dir),
            "--fuzz-target",
            harness,
            "--no-serve",
            project_name,
        ]

        logger.debug(f"Running coverage: {' '.join(cmd)}")

        try:
            result = subprocess.run(
                cmd,
                cwd=self.oss_fuzz_path,
                capture_output=True,
                text=True,
                timeout=timeout,
                stdin=subprocess.DEVNULL,  # Prevent terminal issues
            )

            if result.returncode != 0:
                logger.warning(
                    f"Coverage failed for {project_name}/{harness}: "
                    f"{result.stderr[:500]}"
                )
                return False, output_dir

            # Verify output was generated (check report dir exists)
            report_dir = output_dir / "report" / "linux"
            if not report_dir.exists():
                logger.warning(f"Coverage report not found: {report_dir}")
                return False, output_dir

            # Fix ownership of coverage output (Docker creates root-owned files)
            # Note: build output ownership is already fixed at build time
            fix_docker_ownership(output_dir)

            return True, output_dir

        except subprocess.TimeoutExpired:
            logger.warning(f"Coverage timeout for {project_name}/{harness}")
            fix_docker_ownership(output_dir)
            return False, output_dir
        except Exception as e:
            logger.error(f"Coverage error for {project_name}/{harness}: {e}")
            fix_docker_ownership(output_dir)
            return False, output_dir

    # =========================================================================
    # Inc-build image support for patch verification
    # =========================================================================

    def get_inc_build_image_name(
        self,
        project_name: str,
        sanitizer: str = "address",
        registry: str = "ghcr.io/team-atlanta/crsbench",
    ) -> str:
        """Get inc-build Docker image name.

        Args:
            project_name: OSS-Fuzz project name
            sanitizer: Sanitizer type (address, undefined, memory)
            registry: Docker registry

        Returns:
            Full Docker image name (e.g., ghcr.io/team-atlanta/crsbench/proj:inc-address)
        """
        return f"{registry}/{project_name}:inc-{sanitizer}"

    def has_inc_build_image(
        self,
        project_name: str,
        sanitizer: str = "address",
        registry: str = "ghcr.io/team-atlanta/crsbench",
    ) -> bool:
        """Check if inc-build Docker image exists.

        Args:
            project_name: OSS-Fuzz project name
            sanitizer: Sanitizer type
            registry: Docker registry

        Returns:
            True if inc-build image exists
        """
        image_name = self.get_inc_build_image_name(project_name, sanitizer, registry)
        try:
            result = subprocess.run(
                ["docker", "image", "inspect", image_name],
                capture_output=True,
                check=False,
            )
            return result.returncode == 0
        except Exception:
            return False

    def get_ossfuzz_image_name(
        self,
        project_name: str,
        sanitizer: str = "address",
    ) -> str:
        """Get OSS-Fuzz compatible Docker image name.

        OSS-Fuzz helper.py expects images in format: aixcc-afc/{project}:{tag}

        Args:
            project_name: OSS-Fuzz project name
            sanitizer: Sanitizer type

        Returns:
            OSS-Fuzz compatible image name (e.g., aixcc-afc/proj:inc-address)
        """
        return f"aixcc-afc/{project_name}:inc-{sanitizer}"

    def is_inc_image_available(
        self,
        project_name: str,
        sanitizer: str = "address",
        registry: str = "ghcr.io/team-atlanta/crsbench",
    ) -> bool:
        """Check if inc-build image exists locally and ensure OSS-Fuzz format.

        If image exists in registry or nested format but not in OSS-Fuzz format,
        automatically retags it for OSS-Fuzz compatibility.

        Args:
            project_name: OSS-Fuzz project name
            sanitizer: Sanitizer type
            registry: Docker registry

        Returns:
            True if image exists locally (in OSS-Fuzz format)
        """
        ossfuzz_image = self.get_ossfuzz_image_name(project_name, sanitizer)

        # Check OSS-Fuzz compatible image first (already retagged)
        if self._docker_image_exists(ossfuzz_image):
            return True

        # Check registry image and retag if found
        inc_image = self.get_inc_build_image_name(project_name, sanitizer, registry)
        if self._docker_image_exists(inc_image):
            self._retag_for_ossfuzz(inc_image, ossfuzz_image)
            return True

        # Check nested formats and retag if found
        for nested_image in self._get_nested_image_names(project_name, sanitizer):
            if self._docker_image_exists(nested_image):
                self._retag_for_ossfuzz(nested_image, ossfuzz_image)
                return True

        return False

    def _docker_image_exists(self, image_name: str) -> bool:
        """Check if a Docker image exists locally."""
        try:
            result = subprocess.run(
                ["docker", "image", "inspect", image_name],
                capture_output=True,
                timeout=30,
                stdin=subprocess.DEVNULL,
            )
            return result.returncode == 0
        except Exception as e:
            logger.debug(f"Error checking image {image_name}: {e}")
            return False

    def _get_nested_image_names(
        self,
        project_name: str,
        sanitizer: str = "address",
    ) -> list[str]:
        """Get all possible nested format image names (legacy local builds).

        Some local builds use nested path format:
        - C/C++: aixcc-afc/aixcc/c/{project}:inc-{sanitizer}
        - JVM: aixcc-afc/aixcc/jvm/{project}:inc-{sanitizer}

        Args:
            project_name: OSS-Fuzz project name
            sanitizer: Sanitizer type

        Returns:
            List of possible nested format image names
        """
        return [
            f"aixcc-afc/aixcc/c/{project_name}:inc-{sanitizer}",
            f"aixcc-afc/aixcc/jvm/{project_name}:inc-{sanitizer}",
        ]

    def pull_inc_build_image(
        self,
        project_name: str,
        sanitizer: str = "address",
        registry: str = "ghcr.io/team-atlanta/crsbench",
    ) -> bool:
        """Pull inc-build image from registry and retag for OSS-Fuzz.

        Args:
            project_name: OSS-Fuzz project name
            sanitizer: Sanitizer type
            registry: Docker registry

        Returns:
            True if pull succeeded
        """
        ossfuzz_image = self.get_ossfuzz_image_name(project_name, sanitizer)

        # Check if already available
        if self._docker_image_exists(ossfuzz_image):
            logger.debug(f"OSS-Fuzz compatible image already exists: {ossfuzz_image}")
            return True

        inc_image = self.get_inc_build_image_name(project_name, sanitizer, registry)

        # Check if source image exists locally
        if self._docker_image_exists(inc_image):
            logger.debug(f"Inc-build image available locally: {inc_image}")
            return self._retag_for_ossfuzz(inc_image, ossfuzz_image)

        # Fallback: check nested formats (legacy local builds)
        for nested_image in self._get_nested_image_names(project_name, sanitizer):
            if self._docker_image_exists(nested_image):
                logger.debug(f"Found nested format image locally: {nested_image}")
                return self._retag_for_ossfuzz(nested_image, ossfuzz_image)

        # Pull from registry
        logger.debug(f"Pulling inc-build image: {inc_image}")
        try:
            result = subprocess.run(
                ["docker", "pull", inc_image],
                capture_output=True,
                text=True,
                timeout=600,  # 10 minutes for large images
                stdin=subprocess.DEVNULL,
            )

            if result.returncode == 0:
                logger.debug(f"Successfully pulled: {inc_image}")
                return self._retag_for_ossfuzz(inc_image, ossfuzz_image)

            logger.debug(f"Inc-build image not available: {inc_image}")
            return False

        except subprocess.TimeoutExpired:
            logger.error(f"Timeout pulling {inc_image}")
            return False
        except Exception as e:
            logger.error(f"Error pulling {inc_image}: {e}")
            return False

    def _retag_for_ossfuzz(self, src_image: str, dst_image: str) -> bool:
        """Retag an image for OSS-Fuzz compatibility."""
        try:
            result = subprocess.run(
                ["docker", "tag", src_image, dst_image],
                capture_output=True,
                text=True,
                timeout=30,
                stdin=subprocess.DEVNULL,
            )
            if result.returncode == 0:
                logger.debug(f"Retagged {src_image} -> {dst_image}")
                return True
            logger.error(f"Failed to retag: {result.stderr}")
            return False
        except Exception as e:
            logger.error(f"Error retagging image: {e}")
            return False

    def prepare_inc_image_for_variant(
        self,
        project_name: str,
        variant_name: str,
        sanitizer: str = "address",
    ) -> bool:
        """Prepare inc-build image for a variant by retagging.

        This allows parallel test execution with proper path isolation.
        The inc-build image is retagged from project name to variant name.

        Args:
            project_name: Original project name (source image)
            variant_name: Variant name (target image)
            sanitizer: Sanitizer type

        Returns:
            True if image is ready for use
        """
        # Source: inc-build image for the project
        src_image = self.get_ossfuzz_image_name(project_name, sanitizer)
        # Target: same image tagged with variant name
        dst_image = f"aixcc-afc/{variant_name}:inc-{sanitizer}"

        if self._docker_image_exists(dst_image):
            logger.debug(f"Variant inc-build image already exists: {dst_image}")
            return True

        if not self._docker_image_exists(src_image):
            logger.warning(f"Source inc-build image not found: {src_image}")
            return False

        return self._retag_for_ossfuzz(src_image, dst_image)

    def ensure_inc_image(
        self,
        project_name: str,
        sanitizer: str = "address",
        registry: str = "ghcr.io/team-atlanta/crsbench",
    ) -> bool:
        """Ensure inc-build image is available for use.

        This is a convenience method that:
        1. Checks if inc-build image exists locally (in OSS-Fuzz format)
        2. If not found locally, tries to pull from registry
        3. Returns True if image is available, False otherwise

        Use this method to prepare inc-build images before building variants.
        If this method returns False, callers should fall back to standard builds.

        Args:
            project_name: OSS-Fuzz project name
            sanitizer: Sanitizer type (default: "address")
            registry: Docker registry (default: ghcr.io/team-atlanta/crsbench)

        Returns:
            True if inc-build image is available and ready for use
        """
        cache_key = f"{project_name}:{sanitizer}"

        with self._inc_image_lock:
            if cache_key in self._inc_image_cache:
                return self._inc_image_cache[cache_key]

            # Check if already available locally
            if self.is_inc_image_available(project_name, sanitizer, registry):
                logger.debug(f"Inc-build image available for {project_name}")
                self._inc_image_cache[cache_key] = True
                return True

            # Try to pull from registry
            logger.debug(
                f"Inc-build image not found locally, trying to pull: {project_name}"
            )
            if self.pull_inc_build_image(project_name, sanitizer, registry):
                logger.debug(f"Successfully pulled inc-build image for {project_name}")
                self._inc_image_cache[cache_key] = True
                return True

            logger.warning(
                f"Inc-build image not available for {project_name}, "
                "will fall back to standard build"
            )
            self._inc_image_cache[cache_key] = False
            return False

    # =========================================================================
    # Unit test support for patch verification
    # =========================================================================

    def run_tests(
        self,
        project_name: str,
        src_path: Path,
        sanitizer: str = "address",
        timeout: int = 1800,
        *,
        rts_mode: bool = False,
        docker_image_tag: Optional[str] = None,
    ) -> tuple[bool, str, str]:
        """Run unit tests via helper.py run_test.

        Args:
            project_name: OSS-Fuzz project name
            src_path: Path to source code
            sanitizer: Sanitizer type
            timeout: Timeout in seconds
            rts_mode: If True, run only patch-affected tests (RTS)
            docker_image_tag: Optional Docker image tag (e.g., "inc-address")

        Returns:
            Tuple of (passed, stdout, stderr)
        """
        cmd = [
            "python3",
            str(self._helper_script),
            "run_test",
            "--sanitizer",
            sanitizer,
            project_name,
            str(src_path),
        ]

        if rts_mode:
            cmd.append("--rts")

        if docker_image_tag:
            cmd.extend(["--docker_image_tag", docker_image_tag])

        logger.debug(
            f"Running unit tests for {project_name} "
            f"(rts={rts_mode}, sanitizer={sanitizer})"
        )
        logger.debug(f"Command: {' '.join(cmd)}")

        try:
            result = subprocess.run(
                cmd,
                cwd=self.oss_fuzz_path,
                capture_output=True,
                text=True,
                timeout=timeout,
                stdin=subprocess.DEVNULL,
            )

            passed = result.returncode == 0
            if passed:
                logger.debug(f"Unit tests passed for {project_name}")
            else:
                logger.warning(
                    f"Unit tests failed for {project_name} "
                    f"(exit code: {result.returncode})"
                )

            return passed, result.stdout, result.stderr

        except subprocess.TimeoutExpired:
            logger.error(f"Test timeout for {project_name}")
            return False, "", "Test execution timed out"
        except Exception as e:
            logger.error(f"Test execution error for {project_name}: {e}")
            return False, "", str(e)

    def is_tests_available(self, project_name: str) -> bool:
        """Check if test.sh exists for the project.

        Args:
            project_name: OSS-Fuzz project name

        Returns:
            True if test.sh exists
        """
        test_script = self.projects_base / project_name / "test.sh"
        return test_script.exists()
