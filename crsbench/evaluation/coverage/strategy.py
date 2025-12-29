"""Coverage collection strategies for CRSBench.

This module provides abstract base class and concrete implementations
for collecting code coverage from corpus files during CRS evaluation.

Strategies:
    - LLVMCovLineStrategy: For C/C++ projects using LLVM source-based coverage
    - JaCoCoLineStrategy: For Java/JVM projects using JaCoCo coverage

Usage:
    strategy = LLVMCovLineStrategy(oss_fuzz_path, project_name)
    if strategy.build_with_coverage():
        summary_path = strategy.collect_batch_coverage(harness_path, corpus_dir)
"""

import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from crsbench.evaluation.process_utils import run_with_graceful_timeout
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


class CoverageStrategyError(Exception):
    """Exception raised for coverage strategy errors."""


class CoverageStrategy(ABC):
    """Base class for coverage collection strategies.

    This abstract class defines the interface for collecting code coverage
    from corpus files. Concrete implementations handle specific coverage
    tools (LLVM cov for C/C++, JaCoCo for Java).

    Attributes:
        oss_fuzz_path: Path to OSS-Fuzz repository root.
        project_name: Name of the project (e.g., "curl-delta-02").
        language: Programming language ("c", "c++", "jvm", etc.).
    """

    def __init__(self, oss_fuzz_path: Path, project_name: str, language: str = "c"):
        """Initialize coverage strategy.

        Args:
            oss_fuzz_path: Path to OSS-Fuzz repository root.
            project_name: Name of the project.
            language: Programming language (default: "c").
        """
        self.oss_fuzz_path = Path(oss_fuzz_path).resolve()
        self.project_name = project_name
        self.language = language.lower()
        self._helper_path = self.oss_fuzz_path / "infra" / "helper.py"

        # Validate OSS-Fuzz path
        if not self._helper_path.exists():
            raise CoverageStrategyError(
                f"OSS-Fuzz helper.py not found at {self._helper_path}. "
                f"Please ensure oss_fuzz_path points to a valid OSS-Fuzz repository."
            )

    @abstractmethod
    def collect_batch_coverage(self, harness_path: Path, corpus_dir: Path) -> Path:
        """Collect coverage for all corpus files.

        Runs the fuzz target against all corpus files and generates
        a coverage report. Returns the path to summary.json.

        Args:
            harness_path: Path to the fuzz target executable (relative to /out).
            corpus_dir: Directory containing corpus files.

        Returns:
            Path to the generated summary.json file.

        Raises:
            CoverageStrategyError: If coverage collection fails.
        """

    @abstractmethod
    def build_with_coverage(self) -> bool:
        """Build project with coverage instrumentation.

        Builds the project using OSS-Fuzz's build_fuzzers with
        sanitizer=coverage to enable source-based code coverage.

        Returns:
            True if build succeeded, False otherwise.
        """

    def export_detailed_coverage(
        self,
        harness_name: str,  # noqa: ARG002
        *,
        output_dir: Optional[Path] = None,  # noqa: ARG002
    ) -> Optional[Path]:
        """Export detailed line-level coverage data.

        Optional method that subclasses can override to provide
        detailed coverage with line-level information.

        Args:
            harness_name: Name of the fuzz target.
            output_dir: Directory to write detailed coverage file to.
                        If None, uses a default temporary location.

        Returns:
            Path to detailed coverage JSON file, or None if not supported.
        """
        return None

    def _run_helper_command(
        self,
        args: list[str],
        timeout: int = 3600,
        grace_period: int = 60,
    ) -> tuple[str, str, int, bool]:
        """Run OSS-Fuzz helper.py command.

        Args:
            args: Arguments to pass to helper.py.
            timeout: Command timeout in seconds (default: 1 hour).
            grace_period: Grace period for graceful shutdown (default: 60s).

        Returns:
            Tuple of (stdout, stderr, returncode, timed_out).
        """
        cmd = ["python3", str(self._helper_path)] + args
        logger.debug(f"Running helper command: {' '.join(cmd)}")

        return run_with_graceful_timeout(
            cmd,
            timeout=timeout,
            grace_period=grace_period,
            cwd=self.oss_fuzz_path,
        )


class LLVMCovLineStrategy(CoverageStrategy):
    """Coverage strategy for C/C++ using LLVM source-based coverage.

    Uses OSS-Fuzz's helper.py to:
    1. Build fuzzers with --sanitizer=coverage
    2. Run fuzz targets against corpus to generate .profraw files
    3. Merge profiles and generate coverage reports

    The coverage report is generated as summary.json in the output directory.
    """

    def __init__(self, oss_fuzz_path: Path, project_name: str, language: str = "c"):
        """Initialize LLVM coverage strategy.

        Args:
            oss_fuzz_path: Path to OSS-Fuzz repository root.
            project_name: Name of the project.
            language: Programming language (default: "c").
        """
        super().__init__(oss_fuzz_path, project_name, language)
        self._coverage_output_dir: Optional[Path] = None

    def build_with_coverage(self) -> bool:
        """Build project with coverage instrumentation.

        Runs: python3 infra/helper.py build_fuzzers --sanitizer=coverage <project>

        Returns:
            True if build succeeded, False otherwise.
        """
        logger.info(f"Building {self.project_name} with coverage instrumentation...")

        args = [
            "build_fuzzers",
            "--sanitizer",
            "coverage",
            "--engine",
            "libfuzzer",
            "--architecture",
            "x86_64",
            self.project_name,
        ]

        stdout, stderr, returncode, timed_out = self._run_helper_command(
            args, timeout=3600
        )

        if timed_out:
            logger.error(f"Coverage build timed out for {self.project_name}")
            return False

        if returncode != 0:
            logger.error(
                f"Coverage build failed for {self.project_name}. "
                f"Exit code: {returncode}\n"
                f"stdout: {stdout[:2000]}...\n"
                f"stderr: {stderr[:2000]}..."
            )
            return False

        logger.info(f"Coverage build succeeded for {self.project_name}")
        return True

    def collect_batch_coverage(self, harness_path: Path, corpus_dir: Path) -> Path:
        """Collect coverage for all corpus files.

        Runs: python3 infra/helper.py coverage --corpus-dir=<dir>
              --fuzz-target=<target> --no-serve <project>

        Args:
            harness_path: Path to the fuzz target (name only, e.g., "fuzz_target").
            corpus_dir: Directory containing corpus files.

        Returns:
            Path to the generated summary.json file.

        Raises:
            CoverageStrategyError: If coverage collection fails.
        """
        # Extract target name from path (may be full path or just name)
        target_name = (
            harness_path.name
            if isinstance(harness_path, Path)
            else Path(harness_path).name
        )

        corpus_dir = Path(corpus_dir)
        if not corpus_dir.exists():
            raise CoverageStrategyError(f"Corpus directory not found: {corpus_dir}")

        if not any(corpus_dir.iterdir()):
            raise CoverageStrategyError(f"Corpus directory is empty: {corpus_dir}")

        logger.info(
            f"Collecting coverage for {target_name} with corpus from {corpus_dir}..."
        )

        args = [
            "coverage",
            "--corpus-dir",
            str(corpus_dir.absolute()),
            "--fuzz-target",
            target_name,
            "--no-serve",
            self.project_name,
        ]

        stdout, stderr, returncode, timed_out = self._run_helper_command(
            args,
            timeout=7200,  # 2 hours for coverage collection
        )

        if timed_out:
            raise CoverageStrategyError(
                f"Coverage collection timed out for {target_name}"
            )

        if returncode != 0:
            raise CoverageStrategyError(
                f"Coverage collection failed for {target_name}. "
                f"Exit code: {returncode}\n"
                f"stdout: {stdout[:2000]}...\n"
                f"stderr: {stderr[:2000]}..."
            )

        # Find summary.json in the output directory
        # OSS-Fuzz puts it in: build/out/<project>/report/linux/summary.json
        summary_path = (
            self.oss_fuzz_path
            / "build"
            / "out"
            / self.project_name
            / "report"
            / "linux"
            / "summary.json"
        )

        if not summary_path.exists():
            # Try alternative location for per-target reports
            summary_path = (
                self.oss_fuzz_path
                / "build"
                / "out"
                / self.project_name
                / "report_target"
                / target_name
                / "linux"
                / "summary.json"
            )

        if not summary_path.exists():
            raise CoverageStrategyError(
                f"Coverage summary not found. Expected at:\n"
                f"  {self.oss_fuzz_path / 'build' / 'out' / self.project_name / 'report' / 'linux' / 'summary.json'}\n"
                f"Check helper.py coverage output for errors."
            )

        logger.info(f"Coverage summary generated at: {summary_path}")
        self._coverage_output_dir = summary_path.parent.parent

        return summary_path

    def get_coverage_output_dir(self) -> Optional[Path]:
        """Get the coverage output directory.

        Returns:
            Path to the coverage output directory, or None if not yet collected.
        """
        return self._coverage_output_dir

    def export_detailed_coverage(
        self, harness_name: str, *, output_dir: Optional[Path] = None
    ) -> Optional[Path]:
        """Export detailed line-level coverage data.

        Runs llvm-cov export WITHOUT -summary-only to get full coverage data
        including line execution counts and regions.

        Args:
            harness_name: Name of the fuzz target (e.g., "fuzz_target").
            output_dir: Directory to write detailed coverage file to.
                        If None, uses a default temporary location.

        Returns:
            Path to detailed coverage JSON file, or None if export fails.
        """
        if not self._coverage_output_dir:
            logger.warning("Coverage not yet collected, cannot export detailed data")
            return None

        # Find the profdata file
        dumps_dir = self.oss_fuzz_path / "build" / "out" / self.project_name / "dumps"
        profdata_file = dumps_dir / "merged.profdata"

        if not profdata_file.exists():
            # Try per-target profdata
            profdata_file = dumps_dir / f"{harness_name}.profdata"

        if not profdata_file.exists():
            logger.warning(f"Profdata file not found: {profdata_file}")
            return None

        # Find the fuzz target binary
        out_dir = self.oss_fuzz_path / "build" / "out" / self.project_name
        target_binary = out_dir / harness_name

        if not target_binary.exists():
            logger.warning(f"Fuzz target binary not found: {target_binary}")
            return None

        # Output file for detailed coverage
        if output_dir:
            output_dir = Path(output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)
            detailed_json = output_dir / f"coverage_{harness_name}_detailed.json"
        else:
            # Fallback to /tmp if no output_dir provided
            import tempfile

            detailed_json = (
                Path(tempfile.gettempdir())
                / f"coverage_{self.project_name}_detailed.json"
            )

        # Build llvm-cov export command WITHOUT -summary-only
        cmd = [
            "llvm-cov",
            "export",
            "-instr-profile",
            str(profdata_file),
            str(target_binary),
            f"-path-equivalence=/,{out_dir}",
            "-ignore-filename-regex=.*src/libfuzzer/.*",
        ]

        logger.debug(f"Running llvm-cov export for detailed coverage: {' '.join(cmd)}")

        stdout, stderr, returncode, timed_out = run_with_graceful_timeout(
            cmd,
            timeout=300,
            grace_period=30,
            cwd=self.oss_fuzz_path,
        )

        if timed_out:
            logger.error("llvm-cov export timed out")
            return None

        if returncode != 0:
            logger.error(
                f"llvm-cov export failed. Exit code: {returncode}\n"
                f"stderr: {stderr[:500]}"
            )
            return None

        # Write the detailed JSON
        detailed_json.write_text(stdout)

        logger.info(f"Detailed coverage exported to: {detailed_json}")
        return detailed_json


class JaCoCoLineStrategy(CoverageStrategy):
    """Coverage strategy for Java/JVM using JaCoCo.

    Uses OSS-Fuzz's helper.py to:
    1. Build fuzzers with --sanitizer=coverage and --fuzzing-language=jvm
    2. Run fuzz targets with JaCoCo agent to generate .exec files
    3. Generate reports via jacoco-cli.jar

    The coverage is converted to llvm-cov format using jacoco_report_converter.py.
    """

    def __init__(self, oss_fuzz_path: Path, project_name: str, language: str = "jvm"):
        """Initialize JaCoCo coverage strategy.

        Args:
            oss_fuzz_path: Path to OSS-Fuzz repository root.
            project_name: Name of the project.
            language: Programming language (default: "jvm").
        """
        super().__init__(oss_fuzz_path, project_name, language)
        self._coverage_output_dir: Optional[Path] = None

    def build_with_coverage(self) -> bool:
        """Build Java project with coverage instrumentation.

        Runs: python3 infra/helper.py build_fuzzers --sanitizer=coverage <project>

        For Java, the coverage is collected via JaCoCo agent at runtime,
        so the build step just prepares the fuzzers.

        Returns:
            True if build succeeded, False otherwise.
        """
        logger.info(f"Building {self.project_name} (Java) with coverage support...")

        args = [
            "build_fuzzers",
            "--sanitizer",
            "coverage",
            "--engine",
            "libfuzzer",
            "--architecture",
            "x86_64",
            self.project_name,
        ]

        stdout, stderr, returncode, timed_out = self._run_helper_command(
            args, timeout=3600
        )

        if timed_out:
            logger.error(f"Coverage build timed out for {self.project_name}")
            return False

        if returncode != 0:
            logger.error(
                f"Coverage build failed for {self.project_name}. "
                f"Exit code: {returncode}\n"
                f"stdout: {stdout[:2000]}...\n"
                f"stderr: {stderr[:2000]}..."
            )
            return False

        logger.info(f"Coverage build succeeded for {self.project_name}")
        return True

    def collect_batch_coverage(self, harness_path: Path, corpus_dir: Path) -> Path:
        """Collect coverage for all corpus files.

        For Java projects, runs the fuzz target with JaCoCo agent,
        generates .exec files, and converts to summary.json format.

        Args:
            harness_path: Path to the fuzz target (name only, e.g., "FuzzTarget").
            corpus_dir: Directory containing corpus files.

        Returns:
            Path to the generated summary.json file.

        Raises:
            CoverageStrategyError: If coverage collection fails.
        """
        target_name = (
            harness_path.name
            if isinstance(harness_path, Path)
            else Path(harness_path).name
        )

        corpus_dir = Path(corpus_dir)
        if not corpus_dir.exists():
            raise CoverageStrategyError(f"Corpus directory not found: {corpus_dir}")

        if not any(corpus_dir.iterdir()):
            raise CoverageStrategyError(f"Corpus directory is empty: {corpus_dir}")

        logger.info(
            f"Collecting JaCoCo coverage for {target_name} "
            f"with corpus from {corpus_dir}..."
        )

        args = [
            "coverage",
            "--corpus-dir",
            str(corpus_dir.absolute()),
            "--fuzz-target",
            target_name,
            "--no-serve",
            self.project_name,
        ]

        stdout, stderr, returncode, timed_out = self._run_helper_command(
            args,
            timeout=7200,  # 2 hours for coverage collection
        )

        if timed_out:
            raise CoverageStrategyError(
                f"Coverage collection timed out for {target_name}"
            )

        if returncode != 0:
            raise CoverageStrategyError(
                f"Coverage collection failed for {target_name}. "
                f"Exit code: {returncode}\n"
                f"stdout: {stdout[:2000]}...\n"
                f"stderr: {stderr[:2000]}..."
            )

        # Find summary.json in the output directory
        # For Java, OSS-Fuzz puts it in: build/out/<project>/report/linux/summary.json
        summary_path = (
            self.oss_fuzz_path
            / "build"
            / "out"
            / self.project_name
            / "report"
            / "linux"
            / "summary.json"
        )

        if not summary_path.exists():
            raise CoverageStrategyError(
                f"Coverage summary not found at {summary_path}. "
                f"Check helper.py coverage output for errors."
            )

        logger.info(f"Coverage summary generated at: {summary_path}")
        self._coverage_output_dir = summary_path.parent.parent

        return summary_path

    def get_coverage_output_dir(self) -> Optional[Path]:
        """Get the coverage output directory.

        Returns:
            Path to the coverage output directory, or None if not yet collected.
        """
        return self._coverage_output_dir


def create_coverage_strategy(
    oss_fuzz_path: Path,
    project_name: str,
    language: str,
) -> CoverageStrategy:
    """Factory function to create appropriate coverage strategy.

    Args:
        oss_fuzz_path: Path to OSS-Fuzz repository root.
        project_name: Name of the project.
        language: Programming language (e.g., "c", "c++", "jvm", "java").

    Returns:
        Appropriate CoverageStrategy instance.

    Raises:
        CoverageStrategyError: If language is not supported.
    """
    language = language.lower()

    if language in ("c", "c++", "cpp"):
        return LLVMCovLineStrategy(oss_fuzz_path, project_name, language)
    if language in ("jvm", "java"):
        return JaCoCoLineStrategy(oss_fuzz_path, project_name, language)

    raise CoverageStrategyError(
        f"Unsupported language for coverage: {language}. "
        f"Supported languages: c, c++, jvm, java"
    )


def parse_llvm_cov_summary(summary_path: Path | str) -> dict[str, int | float]:
    """Parse LLVM cov summary.json to extract coverage data.

    Args:
        summary_path: Path to summary.json file.

    Returns:
        Dictionary with coverage statistics:
        {
            "lines_covered": int,
            "lines_total": int,
            "lines_percent": float,
            "functions_covered": int,
            "functions_total": int,
            "functions_percent": float,
            "regions_covered": int,
            "regions_total": int,
            "regions_percent": float,
        }

    Raises:
        CoverageStrategyError: If parsing fails.
    """
    summary_path = Path(summary_path)
    try:
        with summary_path.open() as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise CoverageStrategyError(f"Failed to parse summary.json: {e}") from e
    except FileNotFoundError as e:
        raise CoverageStrategyError(f"Summary file not found: {summary_path}") from e

    # Handle both llvm-cov and JaCoCo converter formats
    result: dict[str, int | float] = {
        "lines_covered": 0,
        "lines_total": 0,
        "lines_percent": 0.0,
        "functions_covered": 0,
        "functions_total": 0,
        "functions_percent": 0.0,
        "regions_covered": 0,
        "regions_total": 0,
        "regions_percent": 0.0,
    }

    try:
        # Standard llvm-cov export format
        if "data" in data and data["data"]:
            totals = data["data"][0].get("totals", {})

            lines = totals.get("lines", {})
            result["lines_covered"] = lines.get("covered", 0)
            result["lines_total"] = lines.get("count", 0)
            result["lines_percent"] = lines.get("percent", 0.0)

            functions = totals.get("functions", {})
            result["functions_covered"] = functions.get("covered", 0)
            result["functions_total"] = functions.get("count", 0)
            result["functions_percent"] = functions.get("percent", 0.0)

            regions = totals.get("regions", {})
            result["regions_covered"] = regions.get("covered", 0)
            result["regions_total"] = regions.get("count", 0)
            result["regions_percent"] = regions.get("percent", 0.0)

        # Alternative format (direct totals)
        elif "totals" in data:
            totals = data["totals"]

            lines = totals.get("lines", {})
            result["lines_covered"] = lines.get("covered", 0)
            result["lines_total"] = lines.get("count", 0)
            result["lines_percent"] = lines.get("percent", 0.0)

            functions = totals.get("functions", {})
            result["functions_covered"] = functions.get("covered", 0)
            result["functions_total"] = functions.get("count", 0)
            result["functions_percent"] = functions.get("percent", 0.0)

    except (KeyError, TypeError) as e:
        logger.warning(f"Unexpected summary.json format: {e}")
        # Return empty result rather than failing

    return result
