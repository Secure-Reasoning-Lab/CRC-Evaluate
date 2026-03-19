"""Storage metrics collection and formatting for benchmark CI.

Measures disk usage across build artifacts, Docker images, and git clones,
with human-readable formatting using binary units (MiB/GiB).
"""

import subprocess
from dataclasses import dataclass
from pathlib import Path

import humanfriendly

from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class StorageMetrics:
    """Storage metrics for a single benchmark."""

    build_artifacts_bytes: int = 0
    docker_image_bytes: int = 0
    git_bytes: int = 0

    @property
    def total_bytes(self) -> int:
        """Total storage in bytes."""
        return self.build_artifacts_bytes + self.docker_image_bytes + self.git_bytes

    def format_total(self) -> str:
        """Format total storage as human-readable string."""
        return format_storage_size(self.total_bytes)


def format_storage_size(size_bytes: int) -> str:
    """Format bytes as human-readable string using binary units.

    Uses binary=True to match `du -h` output (KiB, MiB, GiB).

    Args:
        size_bytes: Size in bytes

    Returns:
        Human-readable string like "1.5 GiB" or "450 MiB"
    """
    if size_bytes <= 0:
        return "0 B"
    return humanfriendly.format_size(size_bytes, binary=True)


def measure_directory_size(path: Path) -> int:
    """Measure directory size in bytes using du.

    Args:
        path: Directory path to measure

    Returns:
        Size in bytes, or 0 if directory doesn't exist or measurement fails
    """
    if not path.exists():
        return 0

    try:
        result = subprocess.run(
            ["du", "-s", "-B1", str(path)],
            capture_output=True,
            text=True,
            errors="replace",
            check=True,
            timeout=30,
        )
        size_str = result.stdout.split()[0]
        return int(size_str)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError) as e:
        logger.debug(f"Failed to measure directory {path}: {e}")
        return 0


def measure_build_artifacts_size(oss_fuzz_path: Path, benchmark_name: str) -> int:
    """Measure build artifacts size for a benchmark.

    Measures all matching directories in build/out/ and build/work/ that
    start with the benchmark name.

    Args:
        oss_fuzz_path: Path to oss-fuzz directory
        benchmark_name: Benchmark name (e.g., "curl")

    Returns:
        Total size in bytes across all matching directories
    """
    total_size = 0

    for subdir in ["out", "work"]:
        build_path = oss_fuzz_path / "build" / subdir
        if not build_path.exists():
            continue

        # Find all directories starting with benchmark name
        for entry in build_path.iterdir():
            if entry.is_dir() and entry.name.startswith(benchmark_name):
                total_size += measure_directory_size(entry)

    return total_size


def measure_docker_image_size(image_name: str) -> int:
    """Measure Docker image size in bytes.

    Args:
        image_name: Full Docker image name with tag

    Returns:
        Size in bytes, or 0 if image doesn't exist or query fails
    """
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", image_name, "--format", "{{.Size}}"],
            capture_output=True,
            text=True,
            errors="replace",
            check=True,
            timeout=10,
        )
        return int(result.stdout.strip())
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError) as e:
        logger.debug(f"Failed to measure Docker image {image_name}: {e}")
        return 0


def measure_benchmark_docker_size(benchmark_name: str, prefix: str) -> int:
    """Measure total Docker image size for a benchmark by unique image IDs.

    Multiple tags may point to the same image (same ID). This function finds
    all images matching the benchmark name and sums sizes by unique ID to
    avoid double-counting shared images.

    Args:
        benchmark_name: Benchmark name to match
        prefix: Docker image prefix (e.g., "crsbench")

    Returns:
        Total size in bytes for unique images
    """
    try:
        # Get all images with ID and size
        result = subprocess.run(
            ["docker", "images", "--format", "{{.ID}}\t{{.Size}}\t{{.Repository}}"],
            capture_output=True,
            text=True,
            errors="replace",
            check=True,
            timeout=30,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        logger.debug(f"Failed to list Docker images: {e}")
        return 0

    # Track unique image IDs and their sizes
    unique_images: dict[str, int] = {}

    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue

        image_id, size_str, repo = parts[0], parts[1], parts[2]

        # Check if this image belongs to our benchmark
        if not repo.startswith(f"{prefix}/{benchmark_name}"):
            continue

        # Skip if we've already counted this image ID
        if image_id in unique_images:
            continue

        # Parse size (e.g., "1.95GB", "450MB")
        try:
            size_bytes = humanfriendly.parse_size(size_str)
            unique_images[image_id] = size_bytes
        except humanfriendly.InvalidSize:
            logger.debug(f"Failed to parse Docker image size: {size_str}")

    return sum(unique_images.values())


def measure_git_size(benchmark_path: Path) -> int:
    """Measure git repository size.

    Args:
        benchmark_path: Path to benchmark directory containing .git

    Returns:
        Size of .git directory in bytes
    """
    git_path = benchmark_path / ".git"
    return measure_directory_size(git_path)


def collect_benchmark_storage(
    benchmark_name: str,
    benchmark_path: Path,
    oss_fuzz_path: Path,
    *,
    project_image_prefix: str = "crsbench",
) -> StorageMetrics:
    """Collect all storage metrics for a benchmark.

    Measures:
    - Build artifacts in build/{out,work}/{benchmark}*/ directories
    - Docker images matching the benchmark (unique IDs only)
    - Git repository size (or .aixcc source size for bundled benchmarks)

    Args:
        benchmark_name: Name of the benchmark
        benchmark_path: Path to benchmark directory
        oss_fuzz_path: Path to oss-fuzz directory
        project_image_prefix: Docker image prefix (default: "crsbench")

    Returns:
        StorageMetrics with all measurements
    """
    # Measure all build artifacts for this benchmark (out + work)
    build_artifacts = measure_build_artifacts_size(oss_fuzz_path, benchmark_name)

    # Measure git size, or .aixcc source size for bundled benchmarks
    git_size = measure_git_size(benchmark_path)
    if git_size == 0:
        # For bundled source benchmarks, measure .aixcc directory instead
        aixcc_path = benchmark_path / ".aixcc"
        if aixcc_path.exists():
            git_size = measure_directory_size(aixcc_path)

    # Measure Docker images (unique IDs only to avoid double-counting)
    docker_size = measure_benchmark_docker_size(benchmark_name, project_image_prefix)

    return StorageMetrics(
        build_artifacts_bytes=build_artifacts,
        docker_image_bytes=docker_size,
        git_bytes=git_size,
    )
