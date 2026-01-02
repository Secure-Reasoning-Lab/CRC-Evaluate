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

import hashlib
import json
import shutil
import tempfile
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from crsbench.builder.infrastructure import OSSFuzzInfrastructure
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

    def __init__(
        self,
        oss_fuzz_path: Path,
        project_name: str,
        language: str = "c",
        *,
        work_dir: Optional[Path] = None,
    ):
        """Initialize coverage strategy.

        Args:
            oss_fuzz_path: Path to OSS-Fuzz repository root.
            project_name: Name of the project.
            language: Programming language (default: "c").
            work_dir: Working directory for coverage build/dumps/reports.
                If None, uses oss_fuzz_path/build/out/project_name.
                Set this for per-trial isolation.
        """
        self.oss_fuzz_path = Path(oss_fuzz_path).resolve()
        self.project_name = project_name
        self.language = language.lower()
        self._helper_path = self.oss_fuzz_path / "infra" / "helper.py"

        # Work directory for coverage output (build, dumps, reports)
        if work_dir:
            self._work_dir = Path(work_dir).resolve()
        else:
            self._work_dir = self.oss_fuzz_path / "build" / "out" / project_name

        # Validate OSS-Fuzz path
        if not self._helper_path.exists():
            raise CoverageStrategyError(
                f"OSS-Fuzz helper.py not found at {self._helper_path}. "
                f"Please ensure oss_fuzz_path points to a valid OSS-Fuzz repository."
            )

        # Infrastructure for coverage collection
        self._infra = OSSFuzzInfrastructure(self.oss_fuzz_path)

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

    @abstractmethod
    def collect_single_coverage(
        self,
        harness_name: str,
        corpus_file: Path,
        *,
        output_dir: Optional[Path] = None,
    ) -> dict:
        """Collect coverage for a single corpus file.

        Runs coverage collection with a temp directory containing just this file.
        Slower due to Docker overhead per file, but enables true per-input
        coverage attribution.

        Args:
            harness_name: Name of the fuzz target.
            corpus_file: Path to the single corpus file to collect coverage for.
            output_dir: Directory to save detailed coverage files. If None, uses temp.

        Returns:
            Coverage data in format: {func_name: {"src": str, "lines": [int]}, ...}
            Returns empty dict if coverage collection fails.
        """

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

    def __init__(
        self,
        oss_fuzz_path: Path,
        project_name: str,
        language: str = "c",
        *,
        work_dir: Optional[Path] = None,
    ):
        """Initialize LLVM coverage strategy.

        Args:
            oss_fuzz_path: Path to OSS-Fuzz repository root.
            project_name: Name of the project.
            language: Programming language (default: "c").
            work_dir: Working directory for coverage output. If None, uses default.
        """
        super().__init__(oss_fuzz_path, project_name, language, work_dir=work_dir)
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

        Uses OSSFuzzInfrastructure.run_coverage() for unified coverage collection.
        This ensures consistent ownership fixes and output handling.

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

        # Use work_dir for batch coverage output
        batch_output_dir = self._work_dir / "batch_coverage"

        # Run coverage using unified infrastructure
        success, output_dir = self._infra.run_coverage(
            project_name=self.project_name,
            harness=target_name,
            corpus_dir=corpus_dir,
            output_dir=batch_output_dir,
            timeout=7200,  # 2 hours for coverage collection
        )

        if not success:
            raise CoverageStrategyError(f"Coverage collection failed for {target_name}")

        # Find summary.json in the custom output directory
        # With --coverage-output-dir: output_dir/report/linux/summary.json
        summary_path = output_dir / "report" / "linux" / "summary.json"

        if not summary_path.exists():
            raise CoverageStrategyError(
                f"Coverage summary not found. Expected at:\n"
                f"  {summary_path}\n"
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

    def collect_single_coverage(
        self,
        harness_name: str,
        corpus_file: Path,
        *,
        output_dir: Optional[Path] = None,
    ) -> dict:
        """Collect coverage for a single corpus file.

        Uses OSSFuzzInfrastructure.run_coverage() with --coverage-output-dir
        to output coverage results to a unique directory per corpus file.
        This allows using shared pre-built coverage binaries while maintaining
        per-corpus isolation for parallel execution.

        Args:
            harness_name: Name of the fuzz target.
            corpus_file: Path to the single corpus file.
            output_dir: Base directory for coverage output. If None, uses work_dir.
                        Actual output goes to: output_dir/dumps/<hash>/

        Returns:
            Coverage data: {func_name: {"src": str, "lines": [int]}, ...}
        """
        corpus_file = Path(corpus_file)
        if not corpus_file.exists():
            logger.warning(f"Corpus file not found: {corpus_file}")
            return {}

        # Use output_dir if provided, otherwise use work_dir
        # output_dir is typically trial-N/coverage/
        base_output = output_dir if output_dir else self._work_dir

        # Create unique output directory for this corpus file
        # Structure: trial-N/coverage/dumps/<hash>/
        corpus_hash = self._compute_corpus_hash(corpus_file)
        corpus_output_dir = base_output / "dumps" / corpus_hash

        try:
            # Create temp directory with single corpus file
            with tempfile.TemporaryDirectory() as tmp_dir:
                tmp_corpus = Path(tmp_dir) / corpus_file.name
                shutil.copy(corpus_file, tmp_corpus)

                # Run coverage using infrastructure
                success, cov_output_dir = self._infra.run_coverage(
                    project_name=self.project_name,
                    harness=harness_name,
                    corpus_dir=Path(tmp_dir),
                    output_dir=corpus_output_dir,
                    timeout=300,  # 5 minutes per corpus file
                )

                if not success:
                    logger.warning(f"Coverage failed for {corpus_file.name}")
                    return {}

            # Parse LLVM coverage from .covreport (more efficient than summary.json)
            # .covreport has line-level data while summary.json only has aggregates
            covreport_dir = cov_output_dir / "textcov_reports"
            if not covreport_dir.exists():
                logger.warning(f"Textcov reports not found: {covreport_dir}")
                return {}

            # Find the .covreport file (should be one per harness)
            covreport_files = list(covreport_dir.glob("*.covreport"))
            if not covreport_files:
                logger.warning(f"No .covreport files in {covreport_dir}")
                return {}

            return self._parse_covreport(covreport_files[0])

        except Exception as e:
            logger.error(f"Failed to collect single coverage: {e}")
            return {}

    def _compute_corpus_hash(self, corpus_file: Path) -> str:
        """Compute hash of corpus file for unique directory naming."""
        sha256 = hashlib.sha256()
        with corpus_file.open("rb") as f:
            while chunk := f.read(8192):
                sha256.update(chunk)
        return sha256.hexdigest()[:12]

    def _parse_covreport(self, covreport_path: Path) -> dict:
        """Parse llvm-cov show text output (.covreport) for line-level coverage.

        The .covreport format is:
            function_name:
                <line>|<exec_count>|<code>
            ...

        Lines with exec_count > 0 are covered.

        Args:
            covreport_path: Path to the .covreport file.

        Returns:
            Coverage data: {func_name: {"src": str, "lines": [int]}, ...}
        """
        result: dict = {}

        try:
            content = covreport_path.read_text()
        except Exception as e:
            logger.warning(f"Failed to read covreport: {e}")
            return {}

        current_func = None
        current_src = ""
        covered_lines: list[int] = []

        for line in content.split("\n"):
            # Check for function header (ends with :)
            stripped = line.strip()
            if stripped and stripped.endswith(":") and "|" not in stripped:
                # Save previous function if exists
                if current_func and covered_lines:
                    result[current_func] = {
                        "src": current_src,
                        "lines": sorted(set(covered_lines)),
                    }

                current_func = stripped[:-1]  # Remove trailing :
                current_src = ""
                covered_lines = []
                continue

            # Parse line coverage (e.g., "   14|      1|void func...")
            if "|" in line and current_func:
                parts = line.split("|")
                if len(parts) >= 2:
                    try:
                        line_num_str = parts[0].strip()
                        count_str = parts[1].strip()

                        # Skip branch/separator lines
                        if not line_num_str or line_num_str == "--":
                            continue

                        line_num = int(line_num_str)
                        count = int(count_str) if count_str else 0

                        if count > 0:
                            covered_lines.append(line_num)

                        # Extract source file from first covered line if not set
                        if not current_src and len(parts) >= 3:
                            # The source is typically in the path passed to llvm-cov
                            current_src = f"function:{current_func}"
                    except (ValueError, IndexError):
                        continue

        # Save last function
        if current_func and covered_lines:
            result[current_func] = {
                "src": current_src,
                "lines": sorted(set(covered_lines)),
            }

        return result

    def _run_fuzz_target_direct(
        self,
        harness_name: str,
        corpus_file: Path,
        profraw_output: Path,
    ) -> bool:
        """Run fuzz target directly via Docker to generate profraw.

        Args:
            harness_name: Name of the fuzz target.
            corpus_file: Path to the corpus file to run.
            profraw_output: Path where profraw file should be written.

        Returns:
            True if successful, False otherwise.
        """
        # Build output directory (where coverage binary lives)
        build_out = self.oss_fuzz_path / "build" / "out" / self.project_name

        # Docker image name
        image_name = f"gcr.io/oss-fuzz/{self.project_name}"

        # Ensure profraw output directory exists
        profraw_output.parent.mkdir(parents=True, exist_ok=True)

        # Docker command to run fuzz target with coverage
        # Mount: build output, corpus file, and profraw output directory
        cmd = [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{build_out}:/out:ro",
            "-v",
            f"{corpus_file.parent}:/corpus:ro",
            "-v",
            f"{profraw_output.parent}:/profraw",
            "-e",
            f"LLVM_PROFILE_FILE=/profraw/{profraw_output.name}",
            image_name,
            f"/out/{harness_name}",
            f"/corpus/{corpus_file.name}",
        ]

        logger.debug(f"Running fuzz target: {' '.join(cmd)}")

        stdout, stderr, returncode, timed_out = run_with_graceful_timeout(
            cmd,
            timeout=60,  # 1 minute per corpus file
            grace_period=10,
        )

        if timed_out:
            logger.warning(f"Fuzz target timed out for {corpus_file.name}")
            return False

        # Return code from fuzzer can be non-zero even on success
        # Check if profraw was generated
        if profraw_output.exists():
            logger.debug(f"Generated profraw: {profraw_output}")
            return True

        logger.warning(
            f"Fuzz target did not generate profraw. "
            f"returncode={returncode}, stderr={stderr[:500]}"
        )
        return False

    def _merge_profdata(self, profraw_file: Path, profdata_output: Path) -> bool:
        """Merge profraw file(s) into profdata.

        Args:
            profraw_file: Path to profraw file (or glob pattern).
            profdata_output: Path for merged profdata output.

        Returns:
            True if successful, False otherwise.
        """
        cmd = [
            "llvm-profdata",
            "merge",
            "-sparse",
            str(profraw_file),
            "-o",
            str(profdata_output),
        ]

        logger.debug(f"Merging profdata: {' '.join(cmd)}")

        stdout, stderr, returncode, timed_out = run_with_graceful_timeout(
            cmd,
            timeout=60,
            grace_period=10,
        )

        if timed_out or returncode != 0:
            logger.warning(
                f"llvm-profdata merge failed. "
                f"returncode={returncode}, stderr={stderr[:500]}"
            )
            return False

        if profdata_output.exists():
            logger.debug(f"Generated profdata: {profdata_output}")
            return True

        return False

    def _export_coverage_direct(
        self,
        harness_name: str,
        profdata_file: Path,
        *,
        output_dir: Optional[Path] = None,
    ) -> dict:
        """Export coverage data using llvm-cov with custom profdata location.

        Args:
            harness_name: Name of the fuzz target.
            profdata_file: Path to merged profdata file.
            output_dir: Directory for output files.

        Returns:
            Coverage data: {func_name: {"src": str, "lines": [int]}, ...}
        """
        # Build output directory (where coverage binary lives)
        build_out = self.oss_fuzz_path / "build" / "out" / self.project_name
        target_binary = build_out / harness_name

        if not target_binary.exists():
            logger.warning(f"Fuzz target binary not found: {target_binary}")
            return {}

        if not profdata_file.exists():
            logger.warning(f"Profdata file not found: {profdata_file}")
            return {}

        # Build llvm-cov export command
        cmd = [
            "llvm-cov",
            "export",
            "-instr-profile",
            str(profdata_file),
            str(target_binary),
            f"-path-equivalence=/,{build_out}",
            "-ignore-filename-regex=.*src/libfuzzer/.*",
        ]

        logger.debug(f"Exporting coverage: {' '.join(cmd)}")

        stdout, stderr, returncode, timed_out = run_with_graceful_timeout(
            cmd,
            timeout=120,
            grace_period=30,
        )

        if timed_out or returncode != 0:
            logger.warning(
                f"llvm-cov export failed. "
                f"returncode={returncode}, stderr={stderr[:500]}"
            )
            return {}

        # Save detailed coverage JSON if output_dir provided
        if output_dir:
            detailed_json = output_dir / f"coverage_{harness_name}_detailed.json"
            detailed_json.write_text(stdout)
            logger.info(f"Detailed coverage exported to: {detailed_json}")

        # Parse and return coverage data
        try:
            data = json.loads(stdout)
            return self._parse_llvm_detailed_coverage_data(data)
        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse llvm-cov output: {e}")
            return {}

    def _parse_llvm_detailed_coverage_data(self, data: dict) -> dict:
        """Parse LLVM cov export data (already loaded) to unified format.

        Args:
            data: Parsed llvm-cov export JSON data.

        Returns:
            Coverage data: {func_name: {"src": str, "lines": [int]}, ...}
        """
        result: dict = {}

        if "data" in data and data["data"]:
            for entry in data["data"]:
                functions = entry.get("functions", [])
                for func in functions:
                    func_name = func.get("name", "")
                    if not func_name:
                        continue

                    filenames = func.get("filenames", [])
                    filename = filenames[0] if filenames else ""

                    lines: list[int] = []
                    for region in func.get("regions", []):
                        if len(region) >= 5 and region[4] > 0:
                            start_line = region[0]
                            end_line = region[2]
                            lines.extend(range(start_line, end_line + 1))

                    if lines:
                        result[func_name] = {
                            "src": filename,
                            "lines": sorted(set(lines)),
                        }

        return result

    def _parse_llvm_detailed_coverage(self, detailed_path: Path) -> dict:
        """Parse LLVM cov export JSON to unified format.

        Args:
            detailed_path: Path to llvm-cov export JSON file.

        Returns:
            Coverage data: {func_name: {"src": str, "lines": [int]}, ...}
        """
        try:
            with detailed_path.open() as f:
                data = json.load(f)
        except Exception as e:
            logger.warning(f"Failed to parse LLVM coverage: {e}")
            return {}

        result: dict = {}

        # Parse llvm-cov export format
        # Structure: data[].functions[] has regions at entry level
        if "data" in data and data["data"]:
            for entry in data["data"]:
                functions = entry.get("functions", [])
                for func in functions:
                    func_name = func.get("name", "")
                    if not func_name:
                        continue

                    # Get filename from filenames array
                    filenames = func.get("filenames", [])
                    filename = filenames[0] if filenames else ""

                    # Extract covered line numbers from regions
                    lines: list[int] = []
                    for region in func.get("regions", []):
                        if len(region) >= 5 and region[4] > 0:
                            # region[0]=line_start, [2]=line_end, [4]=count
                            start_line = region[0]
                            end_line = region[2]
                            lines.extend(range(start_line, end_line + 1))

                    if lines:
                        result[func_name] = {
                            "src": filename,
                            "lines": sorted(set(lines)),
                        }

        return result


class JaCoCoLineStrategy(CoverageStrategy):
    """Coverage strategy for Java/JVM using JaCoCo.

    Uses OSS-Fuzz's helper.py to:
    1. Build fuzzers with --sanitizer=coverage and --fuzzing-language=jvm
    2. Run fuzz targets with JaCoCo agent to generate .exec files
    3. Generate reports via jacoco-cli.jar

    The coverage is converted to llvm-cov format using jacoco_report_converter.py.
    """

    def __init__(
        self,
        oss_fuzz_path: Path,
        project_name: str,
        language: str = "jvm",
        *,
        work_dir: Optional[Path] = None,
    ):
        """Initialize JaCoCo coverage strategy.

        Args:
            oss_fuzz_path: Path to OSS-Fuzz repository root.
            project_name: Name of the project.
            language: Programming language (default: "jvm").
            work_dir: Working directory for coverage output. If None, uses default.
        """
        super().__init__(oss_fuzz_path, project_name, language, work_dir=work_dir)
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

        Uses OSSFuzzInfrastructure.run_coverage() for unified coverage collection.
        For Java projects, this runs the fuzz target with JaCoCo agent.

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

        # Use work_dir for batch coverage output
        batch_output_dir = self._work_dir / "batch_coverage"

        # Run coverage using unified infrastructure
        success, output_dir = self._infra.run_coverage(
            project_name=self.project_name,
            harness=target_name,
            corpus_dir=corpus_dir,
            output_dir=batch_output_dir,
            timeout=7200,  # 2 hours for coverage collection
        )

        if not success:
            raise CoverageStrategyError(f"Coverage collection failed for {target_name}")

        # Find summary.json in the custom output directory
        # With --coverage-output-dir: output_dir/report/linux/summary.json
        summary_path = output_dir / "report" / "linux" / "summary.json"

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

    def export_detailed_coverage(
        self, harness_name: str, *, output_dir: Optional[Path] = None
    ) -> Optional[Path]:
        """Export detailed line-level coverage data from JaCoCo XML.

        Parses jacoco.xml to extract line-level coverage data since
        summary.json only has file-level summaries.

        Args:
            harness_name: Name of the fuzz target (e.g., "OssFuzz1").
            output_dir: Directory to write detailed coverage file to.
                        If None, uses a default temporary location.

        Returns:
            Path to detailed coverage JSON file, or None if export fails.
        """
        if not self._coverage_output_dir:
            logger.warning("Coverage not yet collected, cannot export detailed data")
            return None

        # Find jacoco.xml in the coverage output directory
        jacoco_xml_path = self._coverage_output_dir / "linux" / "jacoco.xml"
        if not jacoco_xml_path.exists():
            logger.warning(f"JaCoCo XML not found at {jacoco_xml_path}")
            return None

        # Output file for detailed coverage
        if output_dir:
            output_dir = Path(output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)
            detailed_json = output_dir / f"coverage_{harness_name}_detailed.json"
        else:
            import tempfile

            detailed_json = (
                Path(tempfile.gettempdir())
                / f"coverage_{self.project_name}_detailed.json"
            )

        try:
            cov_data = self._parse_jacoco_xml(jacoco_xml_path)
            if not cov_data:
                logger.warning("No coverage data extracted from JaCoCo XML")
                return None

            # Write as JSON in format compatible with collector's _parse_coverage_data
            # Format: {function_name: {"src": str, "lines": list[int]}, ...}
            # For Java, we use source file path as the "function name"
            detailed_json.write_text(json.dumps(cov_data, indent=2))
            logger.info(f"Detailed JaCoCo coverage exported to: {detailed_json}")
            return detailed_json
        except Exception as e:
            logger.error(f"Failed to export JaCoCo coverage: {e}")
            return None

    def _parse_jacoco_xml(self, jacoco_xml_path: Path) -> dict:
        """Parse JaCoCo XML to extract line-level coverage.

        JaCoCo XML structure:
            <report>
                <package name="com/example">
                    <sourcefile name="Foo.java">
                        <line nr="15" ci="6"/>  <!-- ci > 0 = covered -->
                        <line nr="16" ci="9"/>
                    </sourcefile>
                </package>
            </report>

        Args:
            jacoco_xml_path: Path to jacoco.xml file.

        Returns:
            Coverage data in format:
            {source_path: {"src": source_path, "lines": [covered_lines]}, ...}
        """
        result: dict = {}

        try:
            tree = ET.parse(jacoco_xml_path)
            root = tree.getroot()
        except ET.ParseError as e:
            logger.error(f"Failed to parse JaCoCo XML: {e}")
            return result

        # Iterate over packages
        for package in root.findall(".//package"):
            package_name = package.get("name", "")

            # Iterate over source files in package
            for sourcefile in package.findall("sourcefile"):
                source_name = sourcefile.get("name", "")
                if not source_name:
                    continue

                # Construct full source path
                if package_name:
                    source_path = f"{package_name.replace('/', '.')}/{source_name}"
                else:
                    source_path = source_name

                # Extract covered lines (ci > 0 means covered)
                covered_lines = []
                for line in sourcefile.findall("line"):
                    line_nr = line.get("nr")
                    ci = line.get("ci", "0")
                    if line_nr and int(ci) > 0:
                        covered_lines.append(int(line_nr))

                if covered_lines:
                    result[source_path] = {
                        "src": source_path,
                        "lines": sorted(covered_lines),
                    }

        logger.debug(f"Parsed JaCoCo XML: {len(result)} source files with coverage")
        return result

    def collect_single_coverage(
        self,
        harness_name: str,
        corpus_file: Path,
        *,
        output_dir: Optional[Path] = None,
    ) -> dict:
        """Collect coverage for a single corpus file.

        Uses OSSFuzzInfrastructure.run_coverage() with --coverage-output-dir
        to output coverage results to a unique directory per corpus file.
        Parses JaCoCo XML for detailed line coverage.

        Args:
            harness_name: Name of the fuzz target.
            corpus_file: Path to the single corpus file.
            output_dir: Base directory for coverage output. If None, uses work_dir.
                        Actual output goes to: output_dir/dumps/<hash>/

        Returns:
            Coverage data: {source_path: {"src": str, "lines": [int]}, ...}
        """
        corpus_file = Path(corpus_file)
        if not corpus_file.exists():
            logger.warning(f"Corpus file not found: {corpus_file}")
            return {}

        # Use output_dir if provided, otherwise use work_dir
        base_output = output_dir if output_dir else self._work_dir

        # Create unique output directory for this corpus file
        corpus_hash = self._compute_corpus_hash(corpus_file)
        corpus_output_dir = base_output / "dumps" / corpus_hash

        try:
            # Create temp directory with single corpus file
            with tempfile.TemporaryDirectory() as tmp_dir:
                tmp_corpus = Path(tmp_dir) / corpus_file.name
                shutil.copy(corpus_file, tmp_corpus)

                # Run coverage using infrastructure
                success, cov_output_dir = self._infra.run_coverage(
                    project_name=self.project_name,
                    harness=harness_name,
                    corpus_dir=Path(tmp_dir),
                    output_dir=corpus_output_dir,
                    timeout=300,
                )

                if not success:
                    logger.warning(f"Coverage failed for {corpus_file.name}")
                    return {}

            # Parse JaCoCo coverage from jacoco.xml
            jacoco_xml_path = cov_output_dir / "report" / "linux" / "jacoco.xml"
            if not jacoco_xml_path.exists():
                logger.warning(f"JaCoCo XML not found: {jacoco_xml_path}")
                return {}

            return self._parse_jacoco_xml(jacoco_xml_path)

        except Exception as e:
            logger.error(f"Failed to collect single coverage: {e}")
            return {}

    def _compute_corpus_hash(self, corpus_file: Path) -> str:
        """Compute hash of corpus file for unique directory naming."""
        sha256 = hashlib.sha256()
        with corpus_file.open("rb") as f:
            while chunk := f.read(8192):
                sha256.update(chunk)
        return sha256.hexdigest()[:12]


def create_coverage_strategy(
    oss_fuzz_path: Path,
    project_name: str,
    language: str,
    *,
    work_dir: Optional[Path] = None,
) -> CoverageStrategy:
    """Factory function to create appropriate coverage strategy.

    Args:
        oss_fuzz_path: Path to OSS-Fuzz repository root.
        project_name: Name of the project.
        language: Programming language (e.g., "c", "c++", "jvm", "java").
        work_dir: Working directory for coverage output. If None, uses default.
            Set this for per-trial isolation by copying the upfront-built
            coverage binary to a trial-specific directory.

    Returns:
        Appropriate CoverageStrategy instance.

    Raises:
        CoverageStrategyError: If language is not supported.
    """
    language = language.lower()

    if language in ("c", "c++", "cpp"):
        return LLVMCovLineStrategy(
            oss_fuzz_path, project_name, language, work_dir=work_dir
        )
    if language in ("jvm", "java"):
        return JaCoCoLineStrategy(
            oss_fuzz_path, project_name, language, work_dir=work_dir
        )

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
