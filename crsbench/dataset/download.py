"""Download CRSBench benchmarks from HuggingFace."""

from pathlib import Path
from typing import Optional

from crsbench.dataset.backends import download
from crsbench.dataset.registry import get_dataset, get_dataset_names, resolve_prefix
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


def _load_suite(suite_name: str, suites_root: Path) -> list[str]:
    """Load benchmark names from a suite YAML file.

    Args:
        suite_name: Suite name (e.g., "afc-all", "sanity")
        suites_root: Root directory containing suite YAML files

    Returns:
        List of benchmark names from the suite

    Raises:
        FileNotFoundError: If suite file doesn't exist
        ValueError: If suite file is invalid
    """
    import yaml

    suite_path = suites_root / f"{suite_name}.yaml"
    if not suite_path.exists():
        available = [p.stem for p in sorted(suites_root.glob("*.yaml"))]
        raise FileNotFoundError(
            f"Suite file not found: {suite_path}\n"
            f"Available suites: {', '.join(available)}"
        )

    with suite_path.open() as f:
        data = yaml.safe_load(f)

    benchmarks = data.get("benchmark_list", [])
    if not benchmarks:
        raise ValueError(f"Suite {suite_name!r} has no benchmarks")

    return benchmarks


def _group_by_dataset(benchmarks: list[str]) -> dict[str, list[str]]:
    """Group benchmark names by their dataset.

    Args:
        benchmarks: List of benchmark names

    Returns:
        Dict mapping dataset name to list of benchmark names
    """
    grouped: dict[str, list[str]] = {}
    for name in benchmarks:
        dataset = resolve_prefix(name)
        if dataset is None:
            logger.warning(f"Unknown prefix for benchmark {name!r}, skipping")
            continue
        grouped.setdefault(dataset, []).append(name)
    return grouped


def download_dataset(
    dataset: str,
    output_dir: Path,
    *,
    benchmarks: Optional[list[str]] = None,
) -> Path:
    """Download a benchmark dataset.

    Args:
        dataset: Dataset short name (e.g., "team-atlanta", "afc")
        output_dir: Directory to download benchmarks into
        benchmarks: Optional list of specific benchmark names to download

    Returns:
        Path to the downloaded dataset directory
    """
    config = get_dataset(dataset)
    logger.info(f"Downloading dataset {dataset!r} from {config.location}")

    allow_patterns = None
    if benchmarks:
        allow_patterns = [f"{name}/**" for name in benchmarks]
        logger.info(f"Downloading {len(benchmarks)} benchmarks")

    output_dir.mkdir(parents=True, exist_ok=True)

    result = download(config, output_dir, allow_patterns=allow_patterns)
    logger.info(f"Downloaded to: {result}")
    return result


def download_all(output_dir: Path) -> list[Path]:
    """Download all benchmark datasets.

    Args:
        output_dir: Directory to download benchmarks into

    Returns:
        List of paths to downloaded dataset directories
    """
    results = []
    for name in get_dataset_names():
        path = download_dataset(name, output_dir)
        results.append(path)
    return results


def download_suite(
    suite_name: str,
    output_dir: Path,
    suites_root: Path,
) -> list[Path]:
    """Download benchmarks specified in a benchmark suite.

    Args:
        suite_name: Suite name (e.g., "afc-all", "sanity")
        output_dir: Directory to download benchmarks into
        suites_root: Root directory containing suite YAML files

    Returns:
        List of paths to downloaded dataset directories
    """
    benchmarks = _load_suite(suite_name, suites_root)
    logger.info(f"Suite {suite_name!r}: {len(benchmarks)} benchmarks")

    grouped = _group_by_dataset(benchmarks)

    results = []
    for dataset, names in sorted(grouped.items()):
        logger.info(f"Downloading {len(names)} benchmarks from {dataset}")
        path = download_dataset(dataset, output_dir, benchmarks=names)
        results.append(path)
    return results
