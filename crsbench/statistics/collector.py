"""Benchmark information collection functions."""

from pathlib import Path

import yaml
from pydantic import ValidationError

from crsbench.statistics.models import BenchmarkInfo, VulnEntry
from crsbench.utils.logger import get_logger
from crsbench.validation.schemas import ProjectConfig

logger = get_logger(__name__)


# --- Utility functions ---


def find_project_root() -> Path:
    """Find the CRSBench project root directory.

    Returns:
        Path to project root
    """
    from crsbench.utils.paths import get_crsbench_root

    return get_crsbench_root()


def get_default_benchmarks_dir() -> Path:
    """Get the default benchmarks directory.

    Returns:
        Path to benchmarks directory
    """
    return find_project_root() / "benchmarks"


def detect_source(benchmark_name: str) -> str:
    """Detect benchmark source from naming convention.

    Args:
        benchmark_name: Name of the benchmark

    Returns:
        Source identifier (Team-Atlanta, AFC, ASC, Sanity, etc.)
    """
    name_lower = benchmark_name.lower()

    if name_lower.startswith("atlanta-"):
        return "Team-Atlanta"
    if name_lower.startswith("afc-"):
        return "AFC"
    if name_lower.startswith("asc-"):
        return "ASC"
    if name_lower.startswith("sanity-"):
        return "Sanity"
    return "Unknown"


def detect_mode(meta_data: dict) -> str:
    """Detect benchmark mode from meta.yaml data.

    Args:
        meta_data: Parsed meta.yaml content

    Returns:
        Mode string (delta, full, or unknown)
    """
    if meta_data.get("delta_mode"):
        return "delta"
    if meta_data.get("full_mode"):
        return "full"
    return "unknown"


def count_files_in_dir(directory: Path, pattern: str) -> int:
    """Count files matching pattern in a directory.

    Args:
        directory: Directory to search
        pattern: Glob pattern (e.g., "*.blob", "*.diff")

    Returns:
        Number of matching files
    """
    if not directory.exists():
        return 0
    return len(list(directory.glob(pattern)))


# --- Collection functions ---


def collect_benchmark_info(benchmark_dir: Path) -> BenchmarkInfo | None:
    """Collect information from a single benchmark directory.

    Args:
        benchmark_dir: Path to benchmark directory

    Returns:
        BenchmarkInfo object or None if not a valid benchmark
    """
    benchmark_name = benchmark_dir.name
    info = BenchmarkInfo(name=benchmark_name, path=benchmark_dir)

    # Detect source from name
    info.source = detect_source(benchmark_name)

    # Check for test.sh
    info.has_test_sh = (benchmark_dir / "test.sh").exists()

    # Read project.yaml for repo URL and language
    project_yaml = benchmark_dir / "project.yaml"
    if project_yaml.exists():
        try:
            with project_yaml.open() as f:
                project_data = yaml.safe_load(f) or {}

            # Use ProjectConfig for validation and parsing
            project_config = ProjectConfig(**project_data)

            info.repo_url = project_config.main_repo or ""
            info.language = project_config.get_normalized_language()
            info.rts_mode = (
                project_config.rts_mode.value if project_config.rts_mode else None
            )
            info.inc_build = project_config.inc_build

        except ValidationError as e:
            info.errors.append(f"Invalid project.yaml: {e}")
        except Exception as e:
            info.errors.append(f"Error reading project.yaml: {e}")
    else:
        info.errors.append("Missing project.yaml")

    # Read meta.yaml for mode and vulnerability info
    meta_yaml = benchmark_dir / ".aixcc" / "meta.yaml"
    if not meta_yaml.exists():
        info.errors.append("Missing .aixcc/meta.yaml")
        return info

    try:
        with meta_yaml.open() as f:
            meta_data = yaml.safe_load(f) or {}

        # Detect mode
        info.mode = detect_mode(meta_data)

        # Extract harness and vulnerability information
        harness_files = meta_data.get("harness_files", [])
        for harness in harness_files:
            harness_name = harness.get("name", "unknown")
            info.harnesses.append(harness_name)

            # Get vulnerabilities for this harness
            vulns = harness.get("vulns", [])
            for vuln in vulns:
                vuln_id = vuln.get("vuln_keyword", "unknown")

                # CPV directory path: .aixcc/{harness_name}/{vuln_id}/
                cpv_dir = benchmark_dir / ".aixcc" / harness_name / vuln_id

                # Count actual POV blobs
                blobs_dir = cpv_dir / "blobs"
                num_povs = count_files_in_dir(blobs_dir, "*.blob")

                # Count actual patches
                patches_dir = cpv_dir / "patches"
                num_patches = count_files_in_dir(patches_dir, "*.diff")

                # Check vuln.yaml existence and read CWEs
                vuln_yaml_path = cpv_dir / "vuln.yaml"
                has_vuln_yaml = vuln_yaml_path.exists()
                cwes: list[str] = []
                if has_vuln_yaml:
                    try:
                        with vuln_yaml_path.open() as vf:
                            vuln_data = yaml.safe_load(vf) or {}
                        cwes = vuln_data.get("cwes", [])
                    except Exception:
                        pass  # Ignore errors reading vuln.yaml for CWEs

                vuln_entry = VulnEntry(
                    benchmark_name=benchmark_name,
                    source=info.source,
                    repo_url=info.repo_url,
                    mode=info.mode,
                    language=info.language,
                    harness_name=harness_name,
                    vuln_id=vuln_id,
                    num_povs=num_povs,
                    num_patches=num_patches,
                    has_test_sh=info.has_test_sh,
                    has_vuln_yaml=has_vuln_yaml,
                    cwes=cwes,
                )
                info.vulns.append(vuln_entry)

    except Exception as e:
        info.errors.append(f"Error reading meta.yaml: {e}")

    return info


def collect_benchmark_stats(
    benchmarks_dir: Path, specific_benchmarks: list[str] | None = None
) -> list[BenchmarkInfo]:
    """Collect statistics from all benchmarks.

    Args:
        benchmarks_dir: Path to benchmarks directory
        specific_benchmarks: Optional list of specific benchmark names to process

    Returns:
        List of BenchmarkInfo objects
    """
    if not benchmarks_dir.exists():
        logger.error(f"Benchmarks directory does not exist: {benchmarks_dir}")
        return []

    results = []

    for item in sorted(benchmarks_dir.iterdir()):
        if not item.is_dir():
            continue

        # Skip if specific benchmarks are requested and this isn't one
        if specific_benchmarks and item.name not in specific_benchmarks:
            continue

        # Check if it's a valid benchmark (has .aixcc directory)
        if not (item / ".aixcc").exists():
            logger.debug(f"Skipping {item.name}: no .aixcc directory")
            continue

        info = collect_benchmark_info(item)
        if info:
            results.append(info)
            logger.debug(
                f"Collected {item.name}: {len(info.vulns)} vulns, "
                f"{len(info.harnesses)} harnesses, "
                f"lang={info.language or 'Unknown'}, mode={info.mode}"
            )

    return results
