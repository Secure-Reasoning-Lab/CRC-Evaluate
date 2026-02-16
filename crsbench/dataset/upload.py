"""Upload CRSBench benchmarks to storage backends."""

from pathlib import Path

from crsbench.dataset.backends import upload
from crsbench.dataset.registry import DatasetConfig, get_dataset
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


def upload_dataset(
    dataset: str,
    benchmarks_dir: Path,
    *,
    dry_run: bool = False,
) -> None:
    """Upload benchmark directories to the configured backend.

    Args:
        dataset: Dataset short name (e.g., "team-atlanta", "afc")
        benchmarks_dir: Path to the benchmarks/ directory
        dry_run: If True, only list what would be uploaded
    """
    config = get_dataset(dataset)

    # Build allow_patterns from prefixes
    allow_patterns = [f"{prefix}*/**" for prefix in config.prefixes]

    # List matching directories for logging
    matching = []
    for prefix in config.prefixes:
        matching.extend(sorted(benchmarks_dir.glob(f"{prefix}*")))
    matching = [d for d in matching if d.is_dir()]

    logger.info(f"Dataset: {dataset} -> {config.location} ({config.backend})")
    logger.info(f"Matching benchmarks: {len(matching)}")

    if not matching:
        logger.warning("No matching benchmarks found — nothing to upload")
        return

    if dry_run:
        for d in matching:
            logger.info(f"  [dry-run] would upload: {d.name}")
        if config.cards_dir:
            _upload_card_files(config, Path(config.cards_dir), dry_run=True)
        return

    logger.info(f"Uploading {len(matching)} benchmarks to {config.location}...")
    upload(config, benchmarks_dir, allow_patterns=allow_patterns)
    logger.info(f"Upload complete: {config.location}")

    # Upload dataset card files (README.md, LICENSE-THIRD-PARTY.md, etc.)
    if config.cards_dir:
        _upload_card_files(config, Path(config.cards_dir))


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
