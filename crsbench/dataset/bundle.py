"""Bundle and unbundle benchmark directories for efficient storage/transfer.

Each benchmark is split into up to three tarballs:

- ``benchmark.tar.gz``: all project files (``Dockerfile``, ``build.sh``,
  ``pkgs/``, etc.) — everything outside ``.aixcc/``.
- ``ground-truth.tar.gz``: the ``.aixcc/`` ground truth directory, excluding
  per-harness ``corpus/`` subdirs (those are a separate archive).
- ``corpus.tar.gz``: collected seed corpora at ``.aixcc/*/corpus/``.

Splitting corpus off lets consumers skip the (often large) corpus without
losing ground truth, and reduces churn on the other archives when only
the corpus changes.
"""

from __future__ import annotations

import tarfile
from typing import TYPE_CHECKING

from crsbench.utils.logger import get_logger

if TYPE_CHECKING:
    from pathlib import Path

logger = get_logger(__name__)

BENCHMARK_ARCHIVE = "benchmark.tar.gz"
GROUND_TRUTH_ARCHIVE = "ground-truth.tar.gz"
CORPUS_ARCHIVE = "corpus.tar.gz"

ALL_ARCHIVES = [BENCHMARK_ARCHIVE, GROUND_TRUTH_ARCHIVE, CORPUS_ARCHIVE]

_GROUND_TRUTH_DIR = ".aixcc"
_CORPUS_SUBDIR = "corpus"


def bundle_benchmark(benchmark_dir: Path, output_dir: Path) -> list[Path]:
    """Bundle a single benchmark into up to three tarballs.

    Args:
        benchmark_dir: Path to the benchmark directory
        output_dir: Directory to write tarballs into (created if needed)

    Returns:
        List of created tarball paths
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []

    # benchmark.tar.gz — everything except .aixcc/
    benchmark_files = [
        item
        for item in sorted(benchmark_dir.iterdir())
        if item.name != _GROUND_TRUTH_DIR
    ]
    if benchmark_files:
        archive_path = output_dir / BENCHMARK_ARCHIVE
        _create_tarball(archive_path, benchmark_dir, benchmark_files)
        created.append(archive_path)

    # ground-truth.tar.gz and corpus.tar.gz — split from .aixcc/
    aixcc_dir = benchmark_dir / _GROUND_TRUTH_DIR
    if aixcc_dir.is_dir():
        corpus_dirs = _find_corpus_dirs(aixcc_dir)

        gt_path = output_dir / GROUND_TRUTH_ARCHIVE
        _create_tarball_filtered(
            gt_path,
            benchmark_dir,
            [aixcc_dir],
            exclude_paths=set(corpus_dirs),
        )
        created.append(gt_path)

        if corpus_dirs:
            corpus_path = output_dir / CORPUS_ARCHIVE
            _create_tarball(corpus_path, benchmark_dir, sorted(corpus_dirs))
            created.append(corpus_path)

    return created


def _find_corpus_dirs(aixcc_dir: Path) -> list[Path]:
    """Return every ``.aixcc/*/corpus/`` directory present on disk."""
    corpus_dirs: list[Path] = []
    for harness_dir in sorted(aixcc_dir.iterdir()):
        if not harness_dir.is_dir():
            continue
        candidate = harness_dir / _CORPUS_SUBDIR
        if candidate.is_dir():
            corpus_dirs.append(candidate)
    return corpus_dirs


def _create_tarball(
    archive_path: Path,
    base_dir: Path,
    items: list[Path],
) -> None:
    """Create a gzip-compressed tarball of ``items`` relative to ``base_dir``."""
    with tarfile.open(archive_path, "w:gz") as tar:
        for item in items:
            arcname = str(item.relative_to(base_dir))
            tar.add(str(item), arcname=arcname)


def _create_tarball_filtered(
    archive_path: Path,
    base_dir: Path,
    items: list[Path],
    *,
    exclude_paths: set[Path],
) -> None:
    """Create a tarball, skipping entries whose resolved path is in ``exclude_paths``.

    ``tarfile.TarFile.add`` receives a ``filter`` callable invoked for every
    file and directory it considers adding (including the top-level dir).
    Returning ``None`` from the filter drops the entry and, for directories,
    also drops everything underneath.
    """
    exclude_resolved = {p.resolve() for p in exclude_paths}

    def _filter(tarinfo: tarfile.TarInfo) -> tarfile.TarInfo | None:
        # tarinfo.name is the archive-internal path (relative to base_dir).
        actual = (base_dir / tarinfo.name).resolve()
        if actual in exclude_resolved:
            return None
        return tarinfo

    with tarfile.open(archive_path, "w:gz") as tar:
        for item in items:
            arcname = str(item.relative_to(base_dir))
            tar.add(str(item), arcname=arcname, filter=_filter)


def unbundle_benchmark(bundle_dir: Path, output_dir: Path) -> None:
    """Extract the known tarballs in ``bundle_dir`` into ``output_dir``."""
    output_dir.mkdir(parents=True, exist_ok=True)

    for archive_name in ALL_ARCHIVES:
        archive_path = bundle_dir / archive_name
        if not archive_path.exists():
            logger.debug(f"Archive not found, skipping: {archive_path}")
            continue

        with tarfile.open(archive_path, "r:gz") as tar:
            tar.extractall(path=str(output_dir), filter="data")

        logger.debug(f"Extracted {archive_name} -> {output_dir}")


def bundle_all_benchmarks(
    benchmarks_dir: Path,
    staging_dir: Path,
    *,
    prefixes: list[str],
) -> int:
    """Bundle every matching benchmark into ``staging_dir``.

    Returns the number of benchmarks bundled.
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
    """Extract every bundled benchmark in ``staging_dir`` into ``output_dir``."""
    count = 0
    for bundle_dir in sorted(staging_dir.iterdir()):
        if not bundle_dir.is_dir():
            continue
        if not any((bundle_dir / name).exists() for name in ALL_ARCHIVES):
            continue

        target = output_dir / bundle_dir.name
        unbundle_benchmark(bundle_dir, target)
        count += 1
        logger.debug(f"Unbundled {bundle_dir.name} -> {target}")

    return count
