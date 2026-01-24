"""Benchmark CPV/POV/patch discovery utilities for flat DAG job construction.

Discovers CPV IDs, POV paths, and patch paths from the .aixcc directory
structure without loading the full MetaYamlAdapter.
"""

from pathlib import Path

from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


def discover_harness_names(benchmark_path: Path) -> list[str]:
    """Discover harness names from the .aixcc directory.

    A directory is considered a harness if it contains cpv_* subdirectories.
    Utility directories (tests/, etc.) are excluded.
    """
    aixcc_dir = benchmark_path / ".aixcc"
    if not aixcc_dir.exists():
        return []

    harnesses = []
    for entry in sorted(aixcc_dir.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name.startswith("."):
            continue
        # Only directories with cpv_* subdirectories are harnesses
        has_cpvs = any(
            sub.is_dir() and sub.name.startswith("cpv_") for sub in entry.iterdir()
        )
        if has_cpvs:
            harnesses.append(entry.name)
    return harnesses


def discover_cpv_ids(benchmark_path: Path, harness: str) -> list[str]:
    """Discover CPV IDs for a harness from the .aixcc directory.

    Structure: .aixcc/{harness}/cpv_*/
    """
    harness_dir = benchmark_path / ".aixcc" / harness
    if not harness_dir.exists():
        return []

    cpv_ids = []
    for entry in sorted(harness_dir.iterdir()):
        if entry.is_dir() and entry.name.startswith("cpv_"):
            cpv_ids.append(entry.name)
    return cpv_ids


def discover_pov_paths(benchmark_path: Path, harness: str, cpv_id: str) -> list[Path]:
    """Discover POV blob paths for a specific CPV.

    Structure: .aixcc/{harness}/{cpv_id}/blobs/pov_*.blob
    """
    blobs_dir = benchmark_path / ".aixcc" / harness / cpv_id / "blobs"
    if not blobs_dir.exists():
        return []

    pov_files = list(blobs_dir.glob("pov_*.blob"))

    def extract_pov_num(path: Path) -> int:
        try:
            return int(path.stem.split("_")[1])
        except (IndexError, ValueError):
            return 999

    pov_files.sort(key=extract_pov_num)
    return pov_files


def discover_patch_paths(
    benchmark_path: Path, harness: str, cpv_id: str
) -> list[tuple[str, Path]]:
    """Discover patch files for a specific CPV.

    Structure: .aixcc/{harness}/{cpv_id}/patches/patch_*.diff

    Returns list of (patch_id, patch_path) tuples.
    """
    patches_dir = benchmark_path / ".aixcc" / harness / cpv_id / "patches"
    if not patches_dir.exists():
        return []

    patch_files = list(patches_dir.glob("patch_*.diff"))

    def extract_patch_num(path: Path) -> int:
        try:
            return int(path.stem.split("_")[1])
        except (IndexError, ValueError):
            return 999

    patch_files.sort(key=extract_patch_num)
    return [(p.stem, p) for p in patch_files]
