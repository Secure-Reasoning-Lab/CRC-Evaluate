"""Download CRSBench benchmarks from HuggingFace."""

import tempfile
from pathlib import Path
from typing import Optional

from crsbench.dataset.backends import download
from crsbench.dataset.bundle import ALL_ARCHIVES, unbundle_all
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


def _is_bundled_dataset(download_dir: Path) -> bool:
    """Check if a downloaded dataset uses the bundled format.

    A bundled dataset has per-benchmark directories containing
    benchmark.tar.gz, pkgs.tar.gz, and/or ground-truth.tar.gz.
    """
    for subdir in download_dir.iterdir():
        if not subdir.is_dir():
            continue
        if any((subdir / name).exists() for name in ALL_ARCHIVES):
            return True
    return False


def download_dataset(
    dataset: str,
    output_dir: Path,
    *,
    benchmarks: Optional[list[str]] = None,
    no_ground_truth: bool = False,
) -> Path:
    """Download a benchmark dataset and extract bundles.

    Downloads per-benchmark bundles from HuggingFace, then extracts them
    into the output directory.

    Args:
        dataset: Dataset short name (e.g., "crsbench")
        output_dir: Directory to place extracted benchmarks
        benchmarks: Optional list of specific benchmark names to download
        no_ground_truth: If True, skip downloading ground-truth.tar.gz

    Returns:
        Path to the output directory
    """
    config = get_dataset(dataset)
    logger.info(f"Downloading dataset {dataset!r} from {config.location}")

    # Build allow_patterns for selective download
    allow_patterns = _build_allow_patterns(benchmarks, no_ground_truth=no_ground_truth)

    if benchmarks:
        logger.info(f"Downloading {len(benchmarks)} benchmarks")

    # Download bundles to a temp directory, then extract
    with tempfile.TemporaryDirectory(prefix="crsbench-download-") as tmpdir:
        staging_dir = Path(tmpdir)
        download(config, staging_dir, allow_patterns=allow_patterns)

        if _is_bundled_dataset(staging_dir):
            logger.info("Extracting bundles...")
            output_dir.mkdir(parents=True, exist_ok=True)
            count = unbundle_all(staging_dir, output_dir)
            logger.info(f"Extracted {count} benchmarks to {output_dir}")
        else:
            # Legacy flat format — move files directly
            logger.info("Legacy format detected, copying files...")
            _copy_tree(staging_dir, output_dir)

    return output_dir


def _build_allow_patterns(
    benchmarks: Optional[list[str]],
    *,
    no_ground_truth: bool = False,
) -> list[str]:
    """Build HuggingFace allow_patterns for selective download."""
    patterns = []

    if benchmarks:
        prefixes = benchmarks
    else:
        prefixes = ["*"]

    for prefix in prefixes:
        patterns.append(f"{prefix}/benchmark.tar.gz")
        if not no_ground_truth:
            patterns.append(f"{prefix}/ground-truth.tar.gz")

    return patterns


def _copy_tree(src: Path, dst: Path) -> None:
    """Copy directory tree, handling the legacy flat download format."""
    import shutil

    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        target = dst / item.name
        if item.is_dir():
            shutil.copytree(str(item), str(target), dirs_exist_ok=True)
        else:
            shutil.copy2(str(item), str(target))


def download_all(
    output_dir: Path,
    *,
    no_ground_truth: bool = False,
) -> list[Path]:
    """Download all benchmark datasets.

    Args:
        output_dir: Directory to download benchmarks into
        no_ground_truth: If True, skip downloading ground-truth.tar.gz

    Returns:
        List of paths to downloaded dataset directories
    """
    results = []
    for name in get_dataset_names():
        path = download_dataset(name, output_dir, no_ground_truth=no_ground_truth)
        results.append(path)
    return results


def download_suite(
    suite_name: str,
    output_dir: Path,
    suites_root: Path,
    *,
    no_ground_truth: bool = False,
) -> list[Path]:
    """Download benchmarks specified in a benchmark suite.

    Args:
        suite_name: Suite name (e.g., "afc-all", "sanity")
        output_dir: Directory to download benchmarks into
        suites_root: Root directory containing suite YAML files
        no_ground_truth: If True, skip downloading ground-truth.tar.gz

    Returns:
        List of paths to downloaded dataset directories
    """
    benchmarks = _load_suite(suite_name, suites_root)
    logger.info(f"Suite {suite_name!r}: {len(benchmarks)} benchmarks")

    grouped = _group_by_dataset(benchmarks)

    results = []
    for dataset, names in sorted(grouped.items()):
        logger.info(f"Downloading {len(names)} benchmarks from {dataset}")
        path = download_dataset(
            dataset,
            output_dir,
            benchmarks=names,
            no_ground_truth=no_ground_truth,
        )
        results.append(path)
    return results
