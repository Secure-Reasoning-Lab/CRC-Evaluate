"""Upload CRSBench benchmarks to storage backends."""

import tempfile
from datetime import UTC, datetime
from pathlib import Path

from crsbench.dataset.backends import delete, download, upload
from crsbench.dataset.bundle import (
    BENCHMARK_ARCHIVE,
    CORPUS_ARCHIVE,
    GROUND_TRUTH_ARCHIVE,
    bundle_benchmark,
)
from crsbench.dataset.manifest import (
    REMOTE_INDEX_ALIAS_PATH,
    REMOTE_INDEX_PATH,
    BenchmarkManifestEntry,
    compute_file_sha256,
    compute_source_fingerprints,
    load_remote_index,
    write_remote_index,
)
from crsbench.dataset.registry import DatasetConfig, get_dataset
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)
REPO_ROOT = Path(__file__).resolve().parents[2]
HF_CARD_FILES: list[tuple[Path, str]] = [
    (REPO_ROOT / "README_HF.md", "README.md"),
    (REPO_ROOT / "LICENSE", "LICENSE"),
    (REPO_ROOT / "LICENSE-THIRD-PARTY.md", "LICENSE-THIRD-PARTY.md"),
]


def upload_dataset(
    dataset: str,
    benchmarks_dir: Path,
    *,
    benchmarks: list[str] | None = None,
    dry_run: bool = False,
    prune: bool = False,
) -> None:
    """Bundle and upload benchmark directories to the configured backend.

    Each benchmark is split into 2 tarballs (benchmark.tar.gz,
    ground-truth.tar.gz) before upload to minimize HuggingFace API calls.

    Args:
        dataset: Dataset short name (e.g., "crsbench")
        benchmarks_dir: Path to the benchmarks/ directory
        benchmarks: Specific benchmark names to upload (None = all matching)
        dry_run: If True, only list what would be uploaded/pruned
        prune: If True, delete remote benchmark folders that are no longer
            present locally (full set for the dataset prefixes). Incompatible
            with ``benchmarks`` because a subset upload does not represent the
            intended full state.
    """
    config = get_dataset(dataset)

    # Full local set ignores the ``benchmarks`` filter and is the reference
    # used for prune comparison against the remote manifest.
    full_local = _find_matching_benchmarks(benchmarks_dir, config.prefixes)

    if prune and benchmarks:
        raise ValueError(
            "prune=True is incompatible with a benchmarks filter: pruning "
            "requires the full local benchmark set for the dataset"
        )

    if benchmarks:
        names = set(benchmarks)
        matching = [d for d in full_local if d.name in names]
    else:
        matching = list(full_local)

    logger.info(f"Dataset: {dataset} -> {config.location} ({config.backend})")
    logger.info(f"Matching benchmarks: {len(matching)}")

    remote_manifest = _load_remote_manifest(config)
    remote_files = _load_remote_files(config)
    manifest_updates, changed = _plan_upload(matching, remote_manifest, remote_files)
    changed_names = {p.name for p in changed}

    stale_names: set[str] = set()
    if prune:
        full_local_names = {d.name for d in full_local}
        stale_names = set(remote_manifest.keys()) - full_local_names

    if not matching and not stale_names:
        logger.warning("No matching benchmarks found and nothing to prune")
        return

    logger.info(
        f"Upload plan: {len(changed)} changed, {len(matching) - len(changed)} unchanged"
    )
    if prune:
        logger.info(f"Prune plan: {len(stale_names)} stale remote benchmarks")

    if dry_run:
        for benchmark_dir in changed:
            logger.info(f"  [dry-run] would bundle and upload: {benchmark_dir.name}")
        for benchmark_dir in matching:
            if benchmark_dir.name in changed_names:
                continue
            logger.info(f"  [dry-run] unchanged, skip upload: {benchmark_dir.name}")
        for name in sorted(stale_names):
            logger.info(f"  [dry-run] would prune remote benchmark: {name}")
        _upload_card_files(config, dry_run=True)
        return

    # Delete stale folders first: if a later step fails, the remote is still
    # closer to the intended state than before.
    if stale_names:
        logger.info(
            f"Pruning {len(stale_names)} stale benchmarks from {config.location}..."
        )
        delete(config, sorted(stale_names))
        logger.info(f"Pruned {len(stale_names)} stale benchmarks")

    if changed:
        # Bundle changed benchmarks and upload with updated index.
        with tempfile.TemporaryDirectory(prefix="crsbench-upload-") as tmpdir:
            staging_dir = Path(tmpdir)
            logger.info(f"Bundling {len(changed)} changed benchmarks...")
            for benchmark_dir in changed:
                output = staging_dir / benchmark_dir.name
                archives = bundle_benchmark(benchmark_dir, output)
                logger.info(f"Bundled {benchmark_dir.name}: {len(archives)} archives")
                _update_archive_metadata(manifest_updates[benchmark_dir.name], output)

            merged_manifest = _compose_manifest(
                remote_manifest, manifest_updates, stale_names
            )
            write_remote_index(staging_dir / REMOTE_INDEX_PATH, merged_manifest)
            write_remote_index(staging_dir / REMOTE_INDEX_ALIAS_PATH, merged_manifest)

            logger.info(
                f"Uploading {len(changed)} benchmarks + manifest to {config.location}..."
            )
            upload(config, staging_dir)
            logger.info(f"Upload complete: {config.location}")
    elif manifest_updates or stale_names:
        # No changed bundles, but the remote manifest needs a refresh — either
        # because unchanged entries are being re-synced or because prune
        # dropped entries.
        with tempfile.TemporaryDirectory(prefix="crsbench-upload-index-") as tmpdir:
            staging_dir = Path(tmpdir)
            merged_manifest = _compose_manifest(
                remote_manifest, manifest_updates, stale_names
            )
            write_remote_index(staging_dir / REMOTE_INDEX_PATH, merged_manifest)
            write_remote_index(staging_dir / REMOTE_INDEX_ALIAS_PATH, merged_manifest)
            upload(
                config,
                staging_dir,
                allow_patterns=[REMOTE_INDEX_PATH, REMOTE_INDEX_ALIAS_PATH],
            )
            logger.info("Uploaded updated manifest index only")
    else:
        logger.info("All selected benchmarks are unchanged; skipping benchmark upload")

    # Upload dataset card files (README.md, LICENSE, etc.) from repo root.
    _upload_card_files(config)


def _compose_manifest(
    remote: dict[str, BenchmarkManifestEntry],
    updates: dict[str, BenchmarkManifestEntry],
    stale: set[str],
) -> dict[str, BenchmarkManifestEntry]:
    """Compose final manifest from remote + local updates, dropping pruned."""
    merged = dict(remote)
    merged.update(updates)
    for name in stale:
        merged.pop(name, None)
    return merged


def _find_matching_benchmarks(benchmarks_dir: Path, prefixes: list[str]) -> list[Path]:
    """Find benchmark directories matching any of the given prefixes."""
    matching = []
    for prefix in prefixes:
        matching.extend(sorted(benchmarks_dir.glob(f"{prefix}*")))
    return [d for d in matching if d.is_dir()]


def _upload_card_files(
    config: DatasetConfig,
    *,
    dry_run: bool = False,
) -> None:
    """Upload canonical dataset card files to the HF dataset repo root."""
    card_files = [(src, dst) for src, dst in HF_CARD_FILES if src.is_file()]
    if not card_files:
        logger.warning("No dataset card files found; skipping card upload")
        return

    if dry_run:
        for src, dst in card_files:
            logger.info(f"  [dry-run] would upload card: {src} -> {dst}")
        return

    from huggingface_hub import HfApi

    api = HfApi()
    for src, dst in card_files:
        logger.info(f"Uploading card file: {src} -> {dst}")
        api.upload_file(
            path_or_fileobj=str(src),
            path_in_repo=dst,
            repo_id=config.location,
            repo_type=config.repo_type,
        )


def _load_remote_manifest(config: DatasetConfig) -> dict[str, BenchmarkManifestEntry]:
    """Load existing remote manifest index from dataset storage backend."""
    with tempfile.TemporaryDirectory(
        prefix="crsbench-upload-remote-manifest-"
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
        logger.info("Remote manifest not available yet, starting fresh")
        return {}


def _plan_upload(
    matching: list[Path],
    remote_manifest: dict[str, BenchmarkManifestEntry],
    remote_files: set[str],
) -> tuple[dict[str, BenchmarkManifestEntry], list[Path]]:
    """Plan changed benchmarks by comparing source fingerprints."""
    updates: dict[str, BenchmarkManifestEntry] = {}
    changed: list[Path] = []
    source_commit = _get_git_commit()
    updated_at = datetime.now(UTC).isoformat()

    for benchmark_dir in matching:
        fingerprints = compute_source_fingerprints(benchmark_dir)
        has_ground_truth = (benchmark_dir / ".aixcc").is_dir()
        has_corpus = _benchmark_has_corpus(benchmark_dir)
        entry = BenchmarkManifestEntry(
            benchmark=benchmark_dir.name,
            benchmark_source_sha256=fingerprints.benchmark,
            ground_truth_source_sha256=fingerprints.ground_truth,
            corpus_source_sha256=fingerprints.corpus,
            source_commit=source_commit,
            updated_at=updated_at,
            has_ground_truth=has_ground_truth,
            has_corpus=has_corpus,
        )
        updates[benchmark_dir.name] = entry
        existing = remote_manifest.get(benchmark_dir.name)
        if existing is None:
            changed.append(benchmark_dir)
            continue
        if (
            existing.benchmark_source_sha256 != fingerprints.benchmark
            or existing.ground_truth_source_sha256 != fingerprints.ground_truth
            or existing.corpus_source_sha256 != fingerprints.corpus
        ):
            changed.append(benchmark_dir)
            continue
        if not _remote_archives_exist(
            benchmark_dir.name, has_ground_truth, has_corpus, remote_files
        ):
            changed.append(benchmark_dir)
            continue

        # Preserve full existing metadata for unchanged entries.
        updates[benchmark_dir.name] = existing

    return updates, changed


def _benchmark_has_corpus(benchmark_dir: Path) -> bool:
    """Return True when any .aixcc/<harness>/corpus/ directory is present."""
    aixcc = benchmark_dir / ".aixcc"
    if not aixcc.is_dir():
        return False
    for harness_dir in aixcc.iterdir():
        if not harness_dir.is_dir():
            continue
        if (harness_dir / "corpus").is_dir():
            return True
    return False


def _remote_archives_exist(
    benchmark_name: str,
    has_ground_truth: bool,
    has_corpus: bool,
    remote_files: set[str],
) -> bool:
    """Return True when expected remote archives are present for benchmark."""
    benchmark_archive = f"{benchmark_name}/{BENCHMARK_ARCHIVE}"
    if benchmark_archive not in remote_files:
        return False

    ground_truth_archive = f"{benchmark_name}/{GROUND_TRUTH_ARCHIVE}"
    if has_ground_truth and ground_truth_archive not in remote_files:
        return False

    corpus_archive = f"{benchmark_name}/{CORPUS_ARCHIVE}"
    if has_corpus and corpus_archive not in remote_files:
        return False

    return True


def _update_archive_metadata(entry: BenchmarkManifestEntry, bundle_dir: Path) -> None:
    """Attach archive hash/size metadata for uploaded bundles."""
    benchmark_archive = bundle_dir / BENCHMARK_ARCHIVE
    if benchmark_archive.is_file():
        entry.benchmark_archive_sha256 = compute_file_sha256(benchmark_archive)
        entry.benchmark_archive_size = benchmark_archive.stat().st_size

    ground_truth_archive = bundle_dir / GROUND_TRUTH_ARCHIVE
    if ground_truth_archive.is_file():
        entry.ground_truth_archive_sha256 = compute_file_sha256(ground_truth_archive)
        entry.ground_truth_archive_size = ground_truth_archive.stat().st_size

    corpus_archive = bundle_dir / CORPUS_ARCHIVE
    if corpus_archive.is_file():
        entry.corpus_archive_sha256 = compute_file_sha256(corpus_archive)
        entry.corpus_archive_size = corpus_archive.stat().st_size


def _get_git_commit() -> str:
    """Return current git commit hash if available."""
    import subprocess

    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            errors="replace",
        )
        return result.stdout.strip()
    except Exception:
        return ""


def _load_remote_files(config: DatasetConfig) -> set[str]:
    """Load remote file paths for archive existence checks."""
    from huggingface_hub import HfApi

    try:
        files = HfApi().list_repo_files(config.location, repo_type=config.repo_type)
    except Exception as e:
        logger.info(f"Could not list remote files, treating as empty remote: {e}")
        return set()
    return set(files)
