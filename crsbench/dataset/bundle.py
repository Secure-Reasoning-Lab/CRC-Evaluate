"""Bundle and unbundle benchmark directories for efficient storage/transfer.

Each benchmark is split into two tarballs:
- benchmark.tar.gz: all project files (Dockerfile, build.sh, pkgs/, etc.)
- ground-truth.tar.gz: .aixcc/ ground truth (vuln info, patches, hints, POVs)

This reduces HuggingFace API calls from ~20+ per benchmark to 2.
"""

import tarfile
from pathlib import Path

from crsbench.utils.logger import get_logger

logger = get_logger(__name__)

BENCHMARK_ARCHIVE = "benchmark.tar.gz"
GROUND_TRUTH_ARCHIVE = "ground-truth.tar.gz"

ALL_ARCHIVES = [BENCHMARK_ARCHIVE, GROUND_TRUTH_ARCHIVE]

# Directory split into its own archive
_GROUND_TRUTH_DIR = ".aixcc"


def bundle_benchmark(benchmark_dir: Path, output_dir: Path) -> list[Path]:
    """Bundle a single benchmark into two tarballs.

    Args:
        benchmark_dir: Path to the benchmark directory
        output_dir: Directory to write tarballs into (created if needed)

    Returns:
        List of created tarball paths
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    created = []

    # Collect everything except .aixcc/
    benchmark_files = []
    for item in sorted(benchmark_dir.iterdir()):
        if item.name == _GROUND_TRUTH_DIR:
            continue
        benchmark_files.append(item)

    # benchmark.tar.gz — all project files (including pkgs/)
    if benchmark_files:
        archive_path = output_dir / BENCHMARK_ARCHIVE
        _create_tarball(archive_path, benchmark_dir, benchmark_files)
        created.append(archive_path)

    # ground-truth.tar.gz — .aixcc/ ground truth
    aixcc_dir = benchmark_dir / _GROUND_TRUTH_DIR
    if aixcc_dir.is_dir():
        archive_path = output_dir / GROUND_TRUTH_ARCHIVE
        _create_tarball(archive_path, benchmark_dir, [aixcc_dir])
        created.append(archive_path)

    return created


def _create_tarball(
    archive_path: Path,
    base_dir: Path,
    items: list[Path],
) -> None:
    """Create a gzip-compressed tarball from a list of files/directories.

    Paths inside the tarball are relative to base_dir.
    """
    with tarfile.open(archive_path, "w:gz") as tar:
        for item in items:
            arcname = str(item.relative_to(base_dir))
            tar.add(str(item), arcname=arcname)


def unbundle_benchmark(bundle_dir: Path, output_dir: Path) -> None:
    """Extract bundled tarballs into a benchmark directory.

    Args:
        bundle_dir: Directory containing the 3 tarballs
        output_dir: Target benchmark directory to extract into
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    for archive_name in ALL_ARCHIVES:
        archive_path = bundle_dir / archive_name
        if not archive_path.exists():
            logger.debug(f"Archive not found, skipping: {archive_path}")
            continue

        with tarfile.open(archive_path, "r:gz") as tar:
            tar.extractall(path=str(output_dir))

        logger.debug(f"Extracted {archive_name} -> {output_dir}")


def bundle_all_benchmarks(
    benchmarks_dir: Path,
    staging_dir: Path,
    *,
    prefixes: list[str],
) -> int:
    """Bundle all matching benchmarks into a staging directory.

    Args:
        benchmarks_dir: Root benchmarks/ directory
        staging_dir: Staging directory for bundles
        prefixes: List of benchmark name prefixes to include

    Returns:
        Number of benchmarks bundled
    """
    count = 0
    for benchmark_dir in sorted(benchmarks_dir.iterdir()):
        if not benchmark_dir.is_dir():
            continue
        if not any(benchmark_dir.name.startswith(p) for p in prefixes):
            continue

        output = staging_dir / benchmark_dir.name
        archives = bundle_benchmark(benchmark_dir, output)
        count += 1
        logger.info(f"Bundled {benchmark_dir.name}: {len(archives)} archives")

    return count


def unbundle_all(
    staging_dir: Path,
    output_dir: Path,
) -> int:
    """Extract all bundled benchmarks from staging into output directory.

    Args:
        staging_dir: Directory containing per-benchmark bundle directories
        output_dir: Target benchmarks/ directory

    Returns:
        Number of benchmarks extracted
    """
    count = 0
    for bundle_dir in sorted(staging_dir.iterdir()):
        if not bundle_dir.is_dir():
            continue
        # Check if it contains any of our archive files
        if not any((bundle_dir / name).exists() for name in ALL_ARCHIVES):
            continue

        target = output_dir / bundle_dir.name
        unbundle_benchmark(bundle_dir, target)
        count += 1
        logger.debug(f"Unbundled {bundle_dir.name} -> {target}")

    return count
