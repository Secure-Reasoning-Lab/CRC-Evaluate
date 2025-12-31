"""OSS-Fuzz infrastructure utilities for the builder module.

This module provides OSSFuzzInfrastructure, which wraps OSS-Fuzz's helper.py
for building fuzzers and reproducing crashes.
"""

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from crsbench.builder.types import BuildConfig
from crsbench.utils.docker import fix_docker_ownership
from crsbench.utils.logger import get_logger
from crsbench.utils.repo_manager import clone_or_copy_cached_repo

logger = get_logger(__name__)

# Exit code constants from helper.py
EXIT_CODE_TIMEOUT = 124  # Subprocess timeout in helper.py


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
                stdin=subprocess.DEVNULL,  # Prevent terminal issues
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
        fix_docker_ownership(build_path)

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

    def reproduce(
        self,
        project_name: str,
        harness: str,
        pov_data: bytes,
        timeout: int = 120,
        request_id: Optional[int] = None,
    ) -> bool:
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
            timeout: Timeout for reproduce operation in seconds
            request_id: Optional request ID for logging

        Returns:
            True if the POV causes a crash, False otherwise
        """
        # Write POV data to temporary file
        with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
            f.write(pov_data)
            testcase_path = Path(f.name)

        req_prefix = f"[Request #{request_id}] " if request_id else ""

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
            result = subprocess.run(
                cmd,
                cwd=self.oss_fuzz_path,
                capture_output=True,
                text=True,
                timeout=timeout + 30,  # Grace period for helper.py
                stdin=subprocess.DEVNULL,  # Prevent terminal issues
            )

            # Handle exit codes explicitly
            if result.returncode == 0:
                logger.info(f"{req_prefix}{project_name}/{harness} did not crash")
                return False
            if result.returncode == EXIT_CODE_TIMEOUT:
                logger.info(
                    f"{req_prefix}{project_name}/{harness} timed out (exit code 124)"
                )
                return False
            logger.info(
                f"{req_prefix}{project_name}/{harness} crashed "
                f"(exit code {result.returncode})"
            )
            return True

        except subprocess.TimeoutExpired:
            # Our subprocess timeout (shouldn't happen with grace period)
            logger.warning(
                f"{req_prefix}Subprocess timeout for {project_name}/{harness}"
            )
            return False
        except Exception as e:
            logger.error(
                f"{req_prefix}Reproduce error for {project_name}/{harness}: {e}"
            )
            return False
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

            return True, output_dir

        except subprocess.TimeoutExpired:
            logger.warning(f"Coverage timeout for {project_name}/{harness}")
            return False, output_dir
        except Exception as e:
            logger.error(f"Coverage error for {project_name}/{harness}: {e}")
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

        # Check nested format and retag if found
        nested_image = self._get_nested_image_name(project_name, sanitizer)
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

    def _get_nested_image_name(
        self,
        project_name: str,
        sanitizer: str = "address",
    ) -> str:
        """Get nested format image name (legacy local builds).

        Some local builds use nested path format: aixcc-afc/aixcc/c/{project}:inc-{sanitizer}

        Args:
            project_name: OSS-Fuzz project name
            sanitizer: Sanitizer type

        Returns:
            Nested format image name
        """
        return f"aixcc-afc/aixcc/c/{project_name}:inc-{sanitizer}"

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
            logger.info(f"Inc-build image available locally: {inc_image}")
            return self._retag_for_ossfuzz(inc_image, ossfuzz_image)

        # Fallback: check nested format (legacy local builds)
        nested_image = self._get_nested_image_name(project_name, sanitizer)
        if self._docker_image_exists(nested_image):
            logger.info(f"Found nested format image locally: {nested_image}")
            return self._retag_for_ossfuzz(nested_image, ossfuzz_image)

        # Pull from registry
        logger.info(f"Pulling inc-build image: {inc_image}")
        try:
            result = subprocess.run(
                ["docker", "pull", inc_image],
                capture_output=True,
                text=True,
                timeout=600,  # 10 minutes for large images
                stdin=subprocess.DEVNULL,
            )

            if result.returncode == 0:
                logger.info(f"Successfully pulled: {inc_image}")
                return self._retag_for_ossfuzz(inc_image, ossfuzz_image)

            logger.error(f"Failed to pull {inc_image}: {result.stderr}")
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
                logger.info(f"Retagged {src_image} -> {dst_image}")
                return True
            logger.error(f"Failed to retag: {result.stderr}")
            return False
        except Exception as e:
            logger.error(f"Error retagging image: {e}")
            return False

    def build_with_inc_image(
        self,
        project_name: str,
        src_path: Path,
        repo_name: str,
        sanitizer: str = "address",
        timeout: int = 1200,
        registry: str = "ghcr.io/team-atlanta/crsbench",
    ) -> bool:
        """Build fuzzers using inc-build image for faster incremental builds.

        Args:
            project_name: OSS-Fuzz project name
            src_path: Path to source code
            repo_name: Repository name for volume mount
            sanitizer: Sanitizer type
            timeout: Build timeout in seconds
            registry: Docker registry

        Returns:
            True if build succeeded
        """
        image_name = self.get_inc_build_image_name(project_name, sanitizer, registry)

        # Prepare output and work directories
        out_dir = self.oss_fuzz_path / "build" / "out" / project_name
        work_dir = self.oss_fuzz_path / "build" / "work" / project_name
        out_dir.mkdir(parents=True, exist_ok=True)
        work_dir.mkdir(parents=True, exist_ok=True)

        # Run docker with inc-build image
        cmd = [
            "docker",
            "run",
            "--rm",
            "--privileged",
            "--shm-size=2g",
            "--platform",
            "linux/amd64",
            "-e",
            "FUZZING_ENGINE=libfuzzer",
            "-e",
            f"SANITIZER={sanitizer}",
            "-e",
            "ARCHITECTURE=x86_64",
            "-e",
            f"PROJECT_NAME={project_name}",
            "-e",
            "BUILD_UID=0",
            "-v",
            f"{src_path}:/src/{repo_name}:rw",
            "-v",
            f"{out_dir}:/out",
            "-v",
            f"{work_dir}:/work",
            image_name,
            "/bin/bash",
            "-c",
            "compile",
        ]

        logger.info(f"Building fuzzers with inc-build image: {image_name}")
        logger.debug(f"Command: {' '.join(cmd)}")

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                stdin=subprocess.DEVNULL,
            )

            if result.returncode == 0:
                logger.info(f"Successfully built fuzzers for {project_name}")
                fix_docker_ownership(out_dir)
                return True

            logger.error(f"Failed to build fuzzers: {result.stderr[:2000]}")
            return False

        except subprocess.TimeoutExpired:
            logger.error(f"Build timeout for {project_name}")
            return False
        except Exception as e:
            logger.error(f"Build error for {project_name}: {e}")
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

        logger.info(
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
                logger.info(f"Unit tests passed for {project_name}")
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
