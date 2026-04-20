"""Seed corpus preparer for runtime use.

Prepares seed corpus for CRS execution by filtering collected corpus
based on time constraints and copying to trial directory.
"""

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class PrepareResult:
    """Result of seed corpus preparation."""

    total_files: int
    copied_files: int
    output_dir: Path
    max_time_filter: Optional[int]


class SeedCorpusPreparer:
    """Prepares seed corpus for CRS execution.

    Reads collected seed corpus from benchmark's .aixcc/{harness}/corpus/,
    filters by relative time if configured, and copies to trial directory.
    """

    def __init__(self, benchmark_path: Path, harness_name: str):
        """Initialize preparer.

        Args:
            benchmark_path: Path to benchmark directory
            harness_name: Name of the harness
        """
        self.benchmark_path = benchmark_path
        self.harness_name = harness_name
        self.corpus_dir = benchmark_path / ".aixcc" / harness_name / "corpus"

    def has_seed_corpus(self) -> bool:
        """Check if seed corpus is available for this harness.

        Returns:
            True if seed corpus directory and manifest exist
        """
        manifest_path = self.corpus_dir / "manifest.json"
        return self.corpus_dir.exists() and manifest_path.exists()

    def prepare(
        self,
        output_dir: Path,
        *,
        max_time: Optional[int] = None,
        force: bool = False,
    ) -> PrepareResult:
        """Prepare seed corpus for CRS execution.

        Args:
            output_dir: Directory to copy seed corpus files to
            max_time: Maximum relative time in seconds (None for all files)
            force: If True, overwrite existing output directory

        Returns:
            PrepareResult with preparation statistics

        Raises:
            FileNotFoundError: If seed corpus not available
            FileExistsError: If output_dir exists and force=False
        """
        if not self.has_seed_corpus():
            raise FileNotFoundError(
                f"No seed corpus found at {self.corpus_dir}. "
                f"Run 'crsbench benchmark seed-import' first."
            )

        manifest_path = self.corpus_dir / "manifest.json"
        with manifest_path.open() as f:
            manifest = json.load(f)

        files_metadata = manifest.get("files", {})
        total_files = len(files_metadata)

        # Filter by max_time if specified
        if max_time is not None:
            selected_files = {
                file_hash: meta
                for file_hash, meta in files_metadata.items()
                if meta.get("relative_time", 0) <= max_time
            }
            logger.info(
                f"Filtered seed corpus: {len(selected_files)}/{total_files} files "
                f"(max_time={max_time}s)"
            )
        else:
            selected_files = files_metadata
            logger.info(f"Using all {total_files} seed corpus files")

        if not selected_files:
            logger.warning("No seed corpus files match the time filter")
            return PrepareResult(
                total_files=total_files,
                copied_files=0,
                output_dir=output_dir,
                max_time_filter=max_time,
            )

        # Prepare output directory
        if output_dir.exists():
            if not force:
                raise FileExistsError(f"Output directory exists: {output_dir}")
            shutil.rmtree(output_dir)

        output_dir.mkdir(parents=True)

        # Copy selected files
        copied_count = 0
        for file_hash in selected_files:
            src_path = self.corpus_dir / file_hash
            if src_path.exists():
                dst_path = output_dir / file_hash
                shutil.copy2(src_path, dst_path)
                copied_count += 1
            else:
                logger.warning(f"Seed corpus file not found: {src_path}")

        logger.info(f"Prepared {copied_count} seed corpus files in {output_dir}")

        return PrepareResult(
            total_files=total_files,
            copied_files=copied_count,
            output_dir=output_dir,
            max_time_filter=max_time,
        )
