"""Upload CRSBench benchmarks to storage backends."""

import tempfile
from pathlib import Path

from crsbench.dataset.backends import upload
from crsbench.dataset.bundle import bundle_all_benchmarks
from crsbench.dataset.registry import DatasetConfig, get_dataset
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


def upload_dataset(
    dataset: str,
    benchmarks_dir: Path,
    *,
    benchmarks: list[str] | None = None,
    dry_run: bool = False,
) -> None:
    """Bundle and upload benchmark directories to the configured backend.

    Each benchmark is split into 2 tarballs (benchmark.tar.gz,
    ground-truth.tar.gz) before upload to minimize HuggingFace API calls.

    Args:
        dataset: Dataset short name (e.g., "crsbench")
        benchmarks_dir: Path to the benchmarks/ directory
        benchmarks: Specific benchmark names to upload (None = all matching)
        dry_run: If True, only list what would be uploaded
    """
    config = get_dataset(dataset)

    # List matching directories for logging
    matching = _find_matching_benchmarks(benchmarks_dir, config.prefixes)

    # Filter to specific benchmarks if requested
    if benchmarks:
        names = set(benchmarks)
        matching = [d for d in matching if d.name in names]

    logger.info(f"Dataset: {dataset} -> {config.location} ({config.backend})")
    logger.info(f"Matching benchmarks: {len(matching)}")

    if not matching:
        logger.warning("No matching benchmarks found — nothing to upload")
        return

    if dry_run:
        for d in matching:
            logger.info(f"  [dry-run] would bundle and upload: {d.name}")
        if config.cards_dir:
            _upload_card_files(config, Path(config.cards_dir), dry_run=True)
        return

    # Bundle into a temp staging directory, then upload
    with tempfile.TemporaryDirectory(prefix="crsbench-upload-") as tmpdir:
        staging_dir = Path(tmpdir)
        logger.info(f"Bundling {len(matching)} benchmarks...")
        count = bundle_all_benchmarks(
            benchmarks_dir, staging_dir, prefixes=config.prefixes
        )
        logger.info(f"Bundled {count} benchmarks, uploading to {config.location}...")

        upload(config, staging_dir)

    logger.info(f"Upload complete: {config.location}")

    # Upload dataset card files (README.md, LICENSE-THIRD-PARTY.md, etc.)
    if config.cards_dir:
        _upload_card_files(config, Path(config.cards_dir))


def _find_matching_benchmarks(benchmarks_dir: Path, prefixes: list[str]) -> list[Path]:
    """Find benchmark directories matching any of the given prefixes."""
    matching = []
    for prefix in prefixes:
        matching.extend(sorted(benchmarks_dir.glob(f"{prefix}*")))
    return [d for d in matching if d.is_dir()]


def _upload_card_files(
    config: DatasetConfig,
    cards_dir: Path,
    *,
    dry_run: bool = False,
) -> None:
    """Upload dataset card files (README.md, LICENSE, etc.) to repo root."""
    if not cards_dir.is_dir():
        logger.warning(f"Cards directory not found: {cards_dir}")
        return

    card_files = [f for f in cards_dir.iterdir() if f.is_file()]
    if not card_files:
        return

    if dry_run:
        for f in card_files:
            logger.info(f"  [dry-run] would upload card: {f.name}")
        return

    try:
        from huggingface_hub import HfApi
    except ImportError:
        raise ImportError(
            "huggingface_hub is required for HuggingFace uploads. "
            "Install it with: pip install 'crsbench[dataset]'"
        ) from None

    api = HfApi()
    for f in card_files:
        logger.info(f"Uploading card file: {f.name}")
        api.upload_file(
            path_or_fileobj=str(f),
            path_in_repo=f.name,
            repo_id=config.location,
            repo_type=config.repo_type or "dataset",
        )
