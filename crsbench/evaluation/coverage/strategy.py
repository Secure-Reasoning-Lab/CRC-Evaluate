"""Atlantis-backed coverage strategies.

Coverage analysis in CRSBench uses the Atlantis/libCRS warm-runner backend for
both native and JVM targets. This module keeps only the language-specific
coverage parsing and `UniAFLCoverageSession` wiring needed by that backend.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from crsbench.evaluation.coverage.backend import CoverageSession, UniAFLCoverageSession
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


class CoverageStrategyError(Exception):
    """Exception raised for coverage strategy errors."""


class CoverageStrategy(ABC):
    """Base Atlantis-backed coverage strategy."""

    def __init__(
        self,
        oss_fuzz_path: Path,
        project_name: str,
        language: str = "c",
        *,
        benchmark_path: Optional[Path] = None,
        work_dir: Optional[Path] = None,
    ):
        self.oss_fuzz_path = Path(oss_fuzz_path).resolve()
        self.project_name = project_name
        self.language = language.lower()
        self.benchmark_path = Path(benchmark_path).resolve() if benchmark_path else None
        if work_dir:
            self._work_dir = Path(work_dir).resolve()
        else:
            self._work_dir = self.oss_fuzz_path / "build" / "out" / project_name

    @property
    def build_output_dir(self) -> Path:
        return self.oss_fuzz_path / "build" / "out" / self.project_name

    @abstractmethod
    def open_session(
        self,
        harness_name: str,
        *,
        output_dir: Optional[Path] = None,
        cpu_set: Optional[str] = None,
        session_label: Optional[str] = None,
    ) -> CoverageSession:
        """Open a warm coverage session."""

    def collect_single_coverage(
        self,
        harness_name: str,
        corpus_file: Path,
        *,
        output_dir: Optional[Path] = None,
    ) -> dict:
        """Collect coverage for one input using a short-lived warm session."""
        session_output = output_dir if output_dir else self._work_dir
        with self.open_session(harness_name, output_dir=session_output) as session:
            return session.collect_single(corpus_file).coverage_data

    def export_detailed_coverage(
        self,
        harness_name: str,  # noqa: ARG002
        *,
        output_dir: Optional[Path] = None,  # noqa: ARG002
    ) -> Optional[Path]:
        """Optional detailed export hook retained for compatibility."""
        return None


class LLVMCovLineStrategy(CoverageStrategy):
    """Atlantis-backed native coverage strategy."""

    def open_session(
        self,
        harness_name: str,
        *,
        output_dir: Optional[Path] = None,
        cpu_set: Optional[str] = None,
        session_label: Optional[str] = None,
    ) -> CoverageSession:
        session_output = output_dir if output_dir else self._work_dir
        return UniAFLCoverageSession(
            project_name=self.project_name,
            harness_name=harness_name,
            language=self.language,
            benchmark_path=self.benchmark_path or Path(),
            source_repo_dir=self.build_output_dir / ".crsbench-repo",
            build_output_dir=self.build_output_dir,
            output_dir=session_output,
            parse_single_output=self._parse_llvm_detailed_coverage_data,
            parse_textcov_output=self._parse_covreport,
            parse_summary=parse_llvm_cov_summary,
            cpu_set=cpu_set,
            session_label=session_label,
        )

    def _parse_covreport(self, covreport_path: Path) -> dict:
        """Parse llvm-cov show text output (.covreport) for line-level coverage."""
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
            stripped = line.strip()
            if stripped and stripped.endswith(":") and "|" not in stripped:
                if current_func and covered_lines:
                    result[current_func] = {
                        "src": current_src,
                        "lines": sorted(set(covered_lines)),
                    }

                current_func = stripped[:-1]
                current_src = ""
                covered_lines = []
                continue

            if "|" in line and current_func:
                parts = line.split("|")
                if len(parts) >= 2:
                    try:
                        line_num_str = parts[0].strip()
                        count_str = parts[1].strip()

                        if not line_num_str or line_num_str == "--":
                            continue

                        line_num = int(line_num_str)
                        count = int(count_str) if count_str else 0

                        if count > 0:
                            covered_lines.append(line_num)

                        if not current_src and len(parts) >= 3:
                            current_src = f"function:{current_func}"
                    except (ValueError, IndexError):
                        continue

        if current_func and covered_lines:
            result[current_func] = {
                "src": current_src,
                "lines": sorted(set(covered_lines)),
            }

        return result

    def _parse_llvm_detailed_coverage_data(self, data: dict) -> dict:
        """Parse LLVM cov export data to unified coverage format."""
        result: dict = {}

        if "data" not in data or not data["data"]:
            return result

        for entry in data["data"]:
            for func in entry.get("functions", []):
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


class JaCoCoLineStrategy(CoverageStrategy):
    """Atlantis-backed JVM coverage strategy."""

    def open_session(
        self,
        harness_name: str,
        *,
        output_dir: Optional[Path] = None,
        cpu_set: Optional[str] = None,
        session_label: Optional[str] = None,
    ) -> CoverageSession:
        session_output = output_dir if output_dir else self._work_dir
        return UniAFLCoverageSession(
            project_name=self.project_name,
            harness_name=harness_name,
            language=self.language,
            benchmark_path=self.benchmark_path or Path(),
            source_repo_dir=self.build_output_dir / ".crsbench-repo",
            build_output_dir=self.build_output_dir,
            output_dir=session_output,
            parse_single_output=self._parse_jacoco_xml,
            parse_textcov_output=None,
            parse_summary=parse_jacoco_summary,
            cpu_set=cpu_set,
            session_label=session_label,
        )

    def _parse_jacoco_xml(self, jacoco_xml_path: Path) -> dict:
        """Parse JaCoCo XML to extract line-level coverage."""
        result: dict = {}

        try:
            tree = ET.parse(jacoco_xml_path)
            root = tree.getroot()
        except ET.ParseError as e:
            logger.error(f"Failed to parse JaCoCo XML: {e}")
            return result

        for package in root.findall(".//package"):
            package_name = package.get("name", "")
            for sourcefile in package.findall("sourcefile"):
                source_name = sourcefile.get("name", "")
                if not source_name:
                    continue

                if package_name:
                    source_path = f"{package_name.replace('/', '.')}/{source_name}"
                else:
                    source_path = source_name

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

    def export_detailed_coverage(
        self, harness_name: str, *, output_dir: Optional[Path] = None
    ) -> Optional[Path]:
        """Export detailed coverage JSON from an existing jacoco.xml report."""
        coverage_root = getattr(self, "_coverage_output_dir", self._work_dir)
        jacoco_xml_path = Path(coverage_root) / "linux" / "jacoco.xml"
        if not jacoco_xml_path.exists():
            logger.warning(f"JaCoCo XML not found at {jacoco_xml_path}")
            return None

        target_dir = Path(output_dir) if output_dir else self._work_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        detailed_json = target_dir / f"coverage_{harness_name}_detailed.json"

        cov_data = self._parse_jacoco_xml(jacoco_xml_path)
        if not cov_data:
            return None
        detailed_json.write_text(json.dumps(cov_data, indent=2))
        return detailed_json


def create_coverage_strategy(
    oss_fuzz_path: Path,
    project_name: str,
    language: str,
    *,
    benchmark_path: Optional[Path] = None,
    work_dir: Optional[Path] = None,
) -> CoverageStrategy:
    """Create the Atlantis-backed coverage strategy for the benchmark language."""
    normalized = language.lower()

    if normalized in ("c", "c++", "cpp"):
        return LLVMCovLineStrategy(
            oss_fuzz_path,
            project_name,
            normalized,
            benchmark_path=benchmark_path,
            work_dir=work_dir,
        )
    if normalized in ("jvm", "java"):
        return JaCoCoLineStrategy(
            oss_fuzz_path,
            project_name,
            normalized,
            benchmark_path=benchmark_path,
            work_dir=work_dir,
        )

    raise CoverageStrategyError(
        f"Unsupported language for coverage: {language}. "
        f"Supported languages: c, c++, jvm, java"
    )


def parse_llvm_cov_summary(summary_path: Path | str) -> dict[str, int | float]:
    """Parse LLVM summary.json to extract coverage totals."""
    summary_path = Path(summary_path)
    try:
        with summary_path.open() as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise CoverageStrategyError(f"Failed to parse summary.json: {e}") from e
    except FileNotFoundError as e:
        raise CoverageStrategyError(f"Summary file not found: {summary_path}") from e

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

    return result


def parse_jacoco_summary(summary_path: Path | str) -> dict[str, int | float]:
    """Parse JaCoCo XML counters into CRSBench summary totals."""
    summary_path = Path(summary_path)
    try:
        tree = ET.parse(summary_path)
        root = tree.getroot()
    except ET.ParseError as e:
        raise CoverageStrategyError(f"Failed to parse JaCoCo XML: {e}") from e
    except FileNotFoundError as e:
        raise CoverageStrategyError(f"Summary file not found: {summary_path}") from e

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

    counters: dict[str, ET.Element] = {
        counter.get("type", ""): counter for counter in root.findall("counter")
    }

    line_counter = counters.get("LINE")
    if line_counter is not None:
        missed = int(line_counter.get("missed", "0"))
        covered = int(line_counter.get("covered", "0"))
        total = missed + covered
        result["lines_covered"] = covered
        result["lines_total"] = total
        result["lines_percent"] = (covered / total * 100.0) if total else 0.0

    method_counter = counters.get("METHOD")
    if method_counter is not None:
        missed = int(method_counter.get("missed", "0"))
        covered = int(method_counter.get("covered", "0"))
        total = missed + covered
        result["functions_covered"] = covered
        result["functions_total"] = total
        result["functions_percent"] = (covered / total * 100.0) if total else 0.0

    return result


def parse_coverage_summary(summary_path: Path | str) -> dict[str, int | float]:
    """Parse either LLVM summary.json or JaCoCo XML summary data."""
    summary_path = Path(summary_path)
    if summary_path.suffix == ".xml":
        return parse_jacoco_summary(summary_path)
    return parse_llvm_cov_summary(summary_path)
