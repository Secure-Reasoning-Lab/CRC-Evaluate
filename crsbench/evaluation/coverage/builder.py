"""CoverageBuilder for building coverage-instrumented variants.

This module provides CoverageBuilder, which creates and builds
coverage-instrumented variant projects for coverage collection.

Pattern:
- Creates `{project}-coverage` variant under oss-fuzz/projects/
- Builds with --sanitizer=coverage for LLVM source-based coverage
- Caches builds to avoid rebuilding
"""

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from crsbench.utils.logger import get_logger
from crsbench.utils.repo_manager import clone_or_copy_cached_repo

logger = get_logger(__name__)


@dataclass
class CoverageBuild:
    """Represents a coverage-instrumented build.

    Attributes:
        project_name: Original project name (e.g., "sanity-mock-c-delta-01")
        variant_name: Coverage variant name (e.g., "sanity-mock-c-delta-01-coverage")
        language: Programming language ("c", "cpp", "jvm")
        commit: Git commit hash used for this build
        build_path: Path to built artifacts
    """

    project_name: str
    variant_name: str
    language: str
    commit: str
    build_path: Path

    @property
    def harness_dir(self) -> Path:
        """Return the directory containing built fuzzers."""
        return self.build_path


class CoverageBuilder:
    """Builder for coverage-instrumented variants.

    Creates and builds coverage variants for projects:
    - Variant naming: `{project}-coverage`
    - Uses OSS-Fuzz's helper.py with --sanitizer=coverage
    - Supports both C/C++ (LLVM cov) and Java (JaCoCo)

    Attributes:
        oss_fuzz_path: Path to oss-fuzz directory
    """

    def __init__(self, oss_fuzz_path: Path):
        """Initialize the builder.

        Args:
            oss_fuzz_path: Path to oss-fuzz directory
        """
        self.oss_fuzz_path = Path(oss_fuzz_path).resolve()
        self.projects_base = self.oss_fuzz_path / "projects"
        self._helper_script = self.oss_fuzz_path / "infra" / "helper.py"

        if not self._helper_script.exists():
            raise FileNotFoundError(
                f"OSS-Fuzz helper.py not found: {self._helper_script}"
            )

    def get_coverage_variant_name(self, project_name: str) -> str:
        """Get the coverage variant name for a project.

        Args:
            project_name: Original project name

        Returns:
            Coverage variant name (e.g., "project-coverage")
        """
        return f"{project_name}-coverage"

    def get_build_output_path(self, variant_name: str) -> Path:
        """Get the build output path for a coverage variant.

        Args:
            variant_name: Coverage variant name

        Returns:
            Path to the build output directory
        """
        return self.oss_fuzz_path / "build" / "out" / variant_name

    def is_built(self, project_name: str) -> bool:
        """Check if a coverage variant has been built.

        Args:
            project_name: Original project name

        Returns:
            True if built fuzzers exist, False otherwise
        """
        variant_name = self.get_coverage_variant_name(project_name)
        build_path = self.get_build_output_path(variant_name)
        return build_path.exists() and any(build_path.iterdir())

    def build(
        self,
        project_name: str,
        benchmark_path: Path,
        main_repo: str,
        commit: str,
        language: str = "c",
        repo_name: Optional[str] = None,
        *,
        force_rebuild: bool = False,
        timeout: int = 3600,
    ) -> Optional[CoverageBuild]:
        """Build coverage-instrumented variant for a project.

        Args:
            project_name: Original project name
            benchmark_path: Path to benchmark directory
            main_repo: Main repository URL
            commit: Git commit hash to checkout
            language: Programming language ("c", "cpp", "jvm")
            repo_name: Optional repository name (for caching)
            force_rebuild: Force rebuild even if cached
            timeout: Build timeout in seconds

        Returns:
            CoverageBuild if successful, None otherwise
        """
        variant_name = self.get_coverage_variant_name(project_name)
        build_path = self.get_build_output_path(variant_name)

        # Check cache
        if not force_rebuild and self.is_built(project_name):
            logger.info(f"Using cached coverage build for {variant_name}")
            return CoverageBuild(
                project_name=project_name,
                variant_name=variant_name,
                language=language,
                commit=commit,
                build_path=build_path,
            )

        logger.info(f"Building coverage variant: {variant_name}")

        # Create variant project directory
        variant_project_path = self._create_variant_project(
            benchmark_path, variant_name
        )
        if not variant_project_path:
            return None

        # Clone and checkout source
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            try:
                repo_path = Path(temp_dir) / "repo"

                # Clone and checkout using cached repository if available
                repo_path_str = clone_or_copy_cached_repo(
                    repo_url=main_repo,
                    commit=commit,
                    target_dir=str(repo_path),
                    repo_name=repo_name,
                    verbose=True,
                )
                if not repo_path_str:
                    logger.error(f"Failed to clone repository: {main_repo}")
                    return None

                # Build with coverage instrumentation
                if not self._build_with_coverage(
                    variant_name, repo_path, language, timeout
                ):
                    logger.error(f"Failed to build coverage variant: {variant_name}")
                    return None

                return CoverageBuild(
                    project_name=project_name,
                    variant_name=variant_name,
                    language=language,
                    commit=commit,
                    build_path=build_path,
                )

            except Exception as e:
                logger.error(f"Build failed for {variant_name}: {e}")
                return None

    def _create_variant_project(
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
            # Create parent directories if needed
            variant_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(benchmark_path, variant_path)
            logger.info(f"Created coverage variant project: {variant_name}")
            return variant_path
        except Exception as e:
            logger.error(f"Failed to create variant project: {e}")
            return None

    def _build_with_coverage(
        self,
        variant_name: str,
        src_path: Path,
        language: str,
        timeout: int,
    ) -> bool:
        """Build project with coverage instrumentation.

        Args:
            variant_name: Coverage variant name
            src_path: Path to source repository
            language: Programming language
            timeout: Build timeout in seconds

        Returns:
            True if build succeeded, False otherwise
        """
        logger.debug(f"Building coverage for {variant_name} ({language})")
        uid = os.getuid()
        cmd = [
            "python3",
            str(self._helper_script),
            "build_fuzzers",
            "--sanitizer",
            "coverage",
            "-e",
            f"BUILD_UID={uid}",
            variant_name,
            str(src_path),
        ]

        logger.info(f"Building {variant_name} with coverage instrumentation...")
        logger.debug(f"Command: {' '.join(cmd)}")

        try:
            result = subprocess.run(
                cmd,
                cwd=self.oss_fuzz_path,
                capture_output=True,
                text=True,
                timeout=timeout,
            )

            if result.returncode == 0:
                logger.info(f"Coverage build succeeded for {variant_name}")
                return True

            logger.error(
                f"Coverage build failed for {variant_name}. "
                f"Exit code: {result.returncode}\n"
                f"stdout: {result.stdout[:2000]}...\n"
                f"stderr: {result.stderr[:2000]}..."
            )
            return False

        except subprocess.TimeoutExpired:
            logger.error(f"Coverage build timed out for {variant_name}")
            return False
        except Exception as e:
            logger.error(f"Coverage build error for {variant_name}: {e}")
            return False

    def cleanup(self, project_name: str) -> None:
        """Remove coverage variant project and build artifacts.

        Args:
            project_name: Original project name
        """
        variant_name = self.get_coverage_variant_name(project_name)

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
