"""Download CRSBench benchmarks from HuggingFace."""

import tempfile
from pathlib import Path
from typing import Optional

from crsbench.dataset.backends import download
from crsbench.dataset.bundle import ALL_ARCHIVES, unbundle_all
from crsbench.dataset.manifest import (
    REMOTE_INDEX_ALIAS_PATH,
    REMOTE_INDEX_PATH,
    BenchmarkManifestEntry,
    load_local_manifest,
    load_remote_index,
    write_local_manifest,
)
from crsbench.dataset.registry import (
    DatasetConfig,
    get_dataset,
    get_dataset_names,
    resolve_prefix,
)
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
    no_corpus: bool = False,
) -> Path:
    """Download a benchmark dataset and extract bundles.

    Downloads per-benchmark bundles from HuggingFace, then extracts them
    into the output directory.

    Args:
        dataset: Dataset short name (e.g., "crsbench")
        output_dir: Directory to place extracted benchmarks
        benchmarks: Optional list of specific benchmark names to download
        no_ground_truth: If True, skip downloading ground-truth.tar.gz
        no_corpus: If True, skip downloading corpus.tar.gz

    Returns:
        Path to the output directory
    """
    config = get_dataset(dataset)
    logger.info(f"Downloading dataset {dataset!r} from {config.location}")

    remote_manifest = _load_remote_manifest(config)
    local_manifest = load_local_manifest(output_dir)
    planned = _plan_download(
        requested=benchmarks,
        remote_manifest=remote_manifest,
        local_manifest=local_manifest,
        output_dir=output_dir,
        no_ground_truth=no_ground_truth,
        no_corpus=no_corpus,
    )
    if benchmarks:
        logger.info(f"Requested {len(benchmarks)} benchmarks")
    if planned.skipped:
        logger.info(f"Skipping {len(planned.skipped)} unchanged benchmarks")
    if planned.download and remote_manifest:
        logger.info(f"Downloading {len(planned.download)} changed/missing benchmarks")

    if remote_manifest and not planned.download:
        logger.info("All selected benchmarks are up-to-date locally")
        return output_dir

    # Build allow_patterns for selective download
    allow_patterns = _build_allow_patterns(
        planned.download if remote_manifest else benchmarks,
        no_ground_truth=no_ground_truth,
        no_corpus=no_corpus,
    )

    # Download bundles to a temp directory, then extract
    with tempfile.TemporaryDirectory(prefix="crsbench-download-") as tmpdir:
        staging_dir = Path(tmpdir)
        download(config, staging_dir, allow_patterns=allow_patterns)

        if _is_bundled_dataset(staging_dir):
            logger.info("Extracting bundles...")
            output_dir.mkdir(parents=True, exist_ok=True)
            bundle_names = _get_bundle_names(staging_dir)
            count = unbundle_all(staging_dir, output_dir)
            logger.info(f"Extracted {count} benchmarks to {output_dir}")
            _update_local_manifest(
                output_dir=output_dir,
                local_manifest=local_manifest,
                remote_manifest=remote_manifest,
                extracted_names=bundle_names,
            )
        else:
            # Legacy flat format — move files directly
            logger.info("Legacy format detected, copying files...")
            _copy_tree(staging_dir, output_dir)

    return output_dir


def _build_allow_patterns(
    benchmarks: Optional[list[str]],
    *,
    no_ground_truth: bool = False,
    no_corpus: bool = False,
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
        if not no_corpus:
            patterns.append(f"{prefix}/corpus.tar.gz")

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


def _get_bundle_names(staging_dir: Path) -> list[str]:
    """Return benchmark names present as bundle directories in staging."""
    names: list[str] = []
    for bundle_dir in sorted(staging_dir.iterdir()):
        if not bundle_dir.is_dir():
            continue
        if any((bundle_dir / name).exists() for name in ALL_ARCHIVES):
            names.append(bundle_dir.name)
    return names


class _DownloadPlan:
    """Computed download plan after manifest comparison."""

    def __init__(self, *, download: list[str], skipped: list[str]) -> None:
        self.download = download
        self.skipped = skipped


def _plan_download(
    *,
    requested: Optional[list[str]],
    remote_manifest: dict[str, BenchmarkManifestEntry],
    local_manifest: dict[str, BenchmarkManifestEntry],
    output_dir: Path,
    no_ground_truth: bool,
    no_corpus: bool,
) -> _DownloadPlan:
    """Determine which benchmarks need download based on local/remote manifests."""
    if not remote_manifest:
        # No remote index yet: keep legacy behavior.
        return _DownloadPlan(download=requested or [], skipped=[])

    targets = requested or sorted(remote_manifest.keys())
    to_download: list[str] = []
    skipped: list[str] = []
    for name in targets:
        remote_entry = remote_manifest.get(name)
        local_entry = local_manifest.get(name)
        benchmark_dir = output_dir / name

        if remote_entry is None:
            to_download.append(name)
            continue
        if not _is_local_entry_up_to_date(
            remote_entry=remote_entry,
            local_entry=local_entry,
            benchmark_dir=benchmark_dir,
            no_ground_truth=no_ground_truth,
            no_corpus=no_corpus,
        ):
            to_download.append(name)
            continue
        skipped.append(name)

    return _DownloadPlan(download=to_download, skipped=skipped)


def _is_local_entry_up_to_date(
    *,
    remote_entry: BenchmarkManifestEntry,
    local_entry: Optional[BenchmarkManifestEntry],
    benchmark_dir: Path,
    no_ground_truth: bool,
    no_corpus: bool,
) -> bool:
    """Check whether local benchmark install satisfies remote manifest entry."""
    if local_entry is None or not benchmark_dir.is_dir():
        return False

    if local_entry.benchmark_source_sha256 != remote_entry.benchmark_source_sha256:
        return False

    if not no_ground_truth:
        if (
            local_entry.ground_truth_source_sha256
            != remote_entry.ground_truth_source_sha256
        ):
            return False
        if (
            remote_entry.ground_truth_source_sha256
            and not (benchmark_dir / ".aixcc").is_dir()
        ):
            return False

    if not no_corpus and remote_entry.has_corpus:
        if local_entry.corpus_source_sha256 != remote_entry.corpus_source_sha256:
            return False

    return True


def _load_remote_manifest(config: DatasetConfig) -> dict[str, BenchmarkManifestEntry]:
    """Load remote dataset manifest index if present."""
    with tempfile.TemporaryDirectory(
        prefix="crsbench-download-remote-manifest-"
    ) as tmpdir:
        tmp_path = Path(tmpdir)
        for index_path in (REMOTE_INDEX_PATH, REMOTE_INDEX_ALIAS_PATH):
            try:
                download(config, tmp_path, allow_patterns=[index_path])
                loaded = load_remote_index(tmp_path / index_path)
                if loaded:
                    return loaded
            except Exception:
                continue
        logger.info("Remote manifest unavailable, using legacy download flow")
        return {}


def _benchmark_has_corpus_on_disk(benchmark_dir: Path) -> bool:
    """Return True when any ``.aixcc/<harness>/corpus/`` exists on disk."""
    aixcc = benchmark_dir / ".aixcc"
    if not aixcc.is_dir():
        return False
    for harness_dir in aixcc.iterdir():
        if not harness_dir.is_dir():
            continue
        if (harness_dir / "corpus").is_dir():
            return True
    return False


def _update_local_manifest(
    *,
    output_dir: Path,
    local_manifest: dict[str, BenchmarkManifestEntry],
    remote_manifest: dict[str, BenchmarkManifestEntry],
    extracted_names: list[str],
) -> None:
    """Update local manifest entries after successful extraction."""
    if not remote_manifest or not extracted_names:
        return

    for name in extracted_names:
        remote_entry = remote_manifest.get(name)
        if remote_entry is None:
            continue
        updated = BenchmarkManifestEntry.from_dict(remote_entry.to_dict())
        updated.has_ground_truth = (output_dir / name / ".aixcc").is_dir()
        updated.has_corpus = _benchmark_has_corpus_on_disk(output_dir / name)
        local_manifest[name] = updated

    write_local_manifest(output_dir, local_manifest)


def download_all(
    output_dir: Path,
    *,
    no_ground_truth: bool = False,
    no_corpus: bool = False,
) -> list[Path]:
    """Download all benchmark datasets.

    Args:
        output_dir: Directory to download benchmarks into
        no_ground_truth: If True, skip downloading ground-truth.tar.gz
        no_corpus: If True, skip downloading corpus.tar.gz

    Returns:
        List of paths to downloaded dataset directories
    """
    results = []
    for name in get_dataset_names():
        path = download_dataset(
            name,
            output_dir,
            no_ground_truth=no_ground_truth,
            no_corpus=no_corpus,
        )
        results.append(path)
    return results


def download_suite(
    suite_name: str,
    output_dir: Path,
    suites_root: Path,
    *,
    no_ground_truth: bool = False,
    no_corpus: bool = False,
) -> list[Path]:
    """Download benchmarks specified in a benchmark suite.

    Args:
        suite_name: Suite name (e.g., "afc-all", "sanity")
        output_dir: Directory to download benchmarks into
        suites_root: Root directory containing suite YAML files
        no_ground_truth: If True, skip downloading ground-truth.tar.gz
        no_corpus: If True, skip downloading corpus.tar.gz

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
            no_corpus=no_corpus,
        )
        results.append(path)
    return results
