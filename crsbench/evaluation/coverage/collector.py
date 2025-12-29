"""Coverage collector orchestrating coverage collection using strategy pattern.

This module provides CoverageCollector, which orchestrates coverage collection
by delegating to a CoverageStrategy for actual coverage collection and using
CoverageStore for deduplication and persistence.

Usage:
    strategy = LLVMCovLineStrategy(oss_fuzz_path, project_name)
    store = CoverageStore(store_dir)
    collector = CoverageCollector(strategy, store, harness_name="fuzz_target")

    # Process new corpus files
    new_count = collector.process_new_corpus(corpus_dir)

    # Collect snapshot for current state
    snapshot = collector.collect_snapshot(cycle=1, harness_name="fuzz_target", elapsed_time=60.0)

    # Export deduplicated corpus
    collector.export_deduped_corpus(output_dir)
"""

import hashlib
import shutil
import time
from pathlib import Path

from crsbench.evaluation.coverage.models import CoverageSnapshot, CoverageSummary
from crsbench.evaluation.coverage.store import CoverageStore
from crsbench.evaluation.coverage.strategy import (
    CoverageStrategy,
    CoverageStrategyError,
    parse_llvm_cov_summary,
)
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


class CoverageCollector:
    """Orchestrates coverage collection using strategy pattern.

    Coordinates between CoverageStrategy (for actual coverage collection)
    and CoverageStore (for deduplication and persistence).

    Attributes:
        strategy: CoverageStrategy instance for collecting coverage.
        store: CoverageStore instance for tracking and deduplicating corpus.
        harness_name: Name of the fuzz target/harness.
        output_dir: Directory to save coverage files to (e.g., trial-N/coverage/).
    """

    def __init__(
        self,
        strategy: CoverageStrategy,
        store: CoverageStore,
        harness_name: str,
        *,
        output_dir: Path | None = None,
        trial_start_time: float | None = None,
    ):
        """Initialize coverage collector.

        Args:
            strategy: CoverageStrategy instance for collecting coverage.
            store: CoverageStore instance for tracking and deduplicating corpus.
            harness_name: Name of the fuzz target/harness for coverage collection.
            output_dir: Directory to save coverage files (summary.json, detailed.json).
                        If None, files remain in their default locations.
            trial_start_time: Unix timestamp when trial started. Used to calculate
                elapsed_time for corpus files. If None, elapsed_time won't be available.
        """
        self.strategy = strategy
        self.store = store
        self.harness_name = harness_name
        self.output_dir = Path(output_dir) if output_dir else None
        # run_start_time is when the CRS fuzzing run started (after build).
        # Used to calculate elapsed_time for corpus files.
        # Can be set later via set_run_start_time() when the actual start is known.
        self.run_start_time = trial_start_time
        self._last_corpus_total = 0
        self._last_lines_covered = 0
        self._processed_hashes: set[str] = set()
        self._corpus_hash_to_path: dict[str, Path] = {}

    def set_run_start_time(self, start_time: float) -> None:
        """Set the CRS run start time for elapsed_time calculations.

        This should be called when the CRS run actually starts (after build),
        so elapsed_time reflects fuzzing time rather than build+fuzz time.

        Args:
            start_time: Unix timestamp when CRS run started
        """
        self.run_start_time = start_time

    def process_new_corpus(self, corpus_dir: Path) -> int:
        """Process new corpus files, update store.

        Finds all files in corpus_dir that haven't been processed yet,
        collects batch coverage, and registers them with the store.

        Args:
            corpus_dir: Directory containing corpus files.

        Returns:
            Count of corpus files that added unique coverage.
        """
        corpus_dir = Path(corpus_dir)
        if not corpus_dir.exists():
            # Debug level since corpus may not exist early in fuzzing run
            logger.debug(f"Corpus directory not found: {corpus_dir}")
            return 0

        # Find all files in corpus directory (filter hidden files like .gitkeep)
        corpus_files = [
            f
            for f in corpus_dir.iterdir()
            if f.is_file() and not f.name.startswith(".")
        ]
        if not corpus_files:
            logger.debug(f"No corpus files found in {corpus_dir}")
            return 0

        # Find new files (not already processed) and cache their hashes
        new_files_with_hash: list[tuple[Path, str]] = []
        for corpus_file in corpus_files:
            file_hash = self._compute_hash(corpus_file)
            if file_hash not in self._processed_hashes:
                new_files_with_hash.append((corpus_file, file_hash))
                self._corpus_hash_to_path[file_hash] = corpus_file

        if not new_files_with_hash:
            logger.debug("No new corpus files to process")
            return 0

        logger.info(
            f"Processing {len(new_files_with_hash)} new corpus files from {corpus_dir}"
        )

        # Collect batch coverage for the corpus directory
        try:
            summary_path = self.strategy.collect_batch_coverage(
                harness_path=Path(self.harness_name),
                corpus_dir=corpus_dir,
            )

            # Copy summary.json to output directory if specified
            if self.output_dir and summary_path.exists():
                self.output_dir.mkdir(parents=True, exist_ok=True)
                local_summary = self.output_dir / "summary.json"
                shutil.copy2(summary_path, local_summary)
                logger.debug(f"Copied summary.json to {local_summary}")

            # Try to get detailed coverage with line-level data
            cov_data = {}
            detailed_path = self.strategy.export_detailed_coverage(
                self.harness_name, output_dir=self.output_dir
            )
            if detailed_path:
                cov_data = self._parse_coverage_data(detailed_path)
                logger.debug(f"Parsed detailed coverage: {len(cov_data)} functions")

            # Fall back to summary if detailed export failed
            if not cov_data:
                cov_data = self._parse_coverage_data(summary_path)
        except CoverageStrategyError as e:
            logger.error(f"Failed to collect coverage: {e}")
            return 0
        except Exception as e:
            logger.error(f"Unexpected error collecting coverage: {e}")
            return 0

        # Register new corpus files with the store
        new_unique_count = 0

        for corpus_file, file_hash in new_files_with_hash:
            try:
                # Use file mtime (modification time) as the corpus discovery timestamp.
                # Rationale:
                # - mtime reflects when the fuzzer actually wrote the file content
                # - Using time.time() would give all batch-processed files the same
                #   timestamp, losing temporal granularity
                # - mtime is more accurate for analyzing fuzzer behavior over time
                # - Note: mtime may be preserved if files are copied, but for in-place
                #   fuzzing this accurately reflects corpus generation time
                file_mtime = corpus_file.stat().st_mtime

                # Use batch coverage data - all files in batch share same coverage
                # since we can't determine per-file breakdown
                _, contributes_unique = self.store.add_corpus(
                    corpus_path=corpus_file,
                    cov_data=cov_data,
                    timestamp=file_mtime,
                )
                # Mark as processed only after successful add
                self._processed_hashes.add(file_hash)
                if contributes_unique:
                    new_unique_count += 1
            except Exception as e:
                logger.warning(f"Failed to add corpus {corpus_file}: {e}")
                continue

        # Update store totals from summary
        try:
            summary_stats = parse_llvm_cov_summary(summary_path)
            self.store.set_totals(
                lines_total=int(summary_stats.get("lines_total", 0)),
                functions_total=int(summary_stats.get("functions_total", 0)),
            )
        except Exception as e:
            logger.warning(f"Failed to update totals from summary: {e}")

        logger.info(
            f"Processed {len(new_files_with_hash)} corpus files, "
            f"{new_unique_count} contributed unique coverage"
        )
        return new_unique_count

    def collect_snapshot(
        self,
        cycle: int,
        harness_name: str,
        elapsed_time: float,
    ) -> CoverageSnapshot:
        """Collect coverage snapshot for current state.

        Creates a snapshot of the current coverage state, tracking
        deltas since the last snapshot.

        Args:
            cycle: Snapshot cycle number (1-indexed).
            harness_name: Name of the harness being fuzzed.
            elapsed_time: Seconds elapsed since trial start.

        Returns:
            CoverageSnapshot with current coverage state.
        """
        summary = self.store.get_summary()

        # Calculate deltas since last snapshot
        new_corpus_count = summary.corpus_total - self._last_corpus_total
        new_lines_count = summary.lines_covered - self._last_lines_covered

        # Update tracking for next delta calculation
        self._last_corpus_total = summary.corpus_total
        self._last_lines_covered = summary.lines_covered

        snapshot = CoverageSnapshot(
            cycle=cycle,
            timestamp=time.time(),
            elapsed_time=elapsed_time,
            harness_name=harness_name,
            summary=summary,
            saturation_detected=summary.saturation_detected,
            new_corpus_count=max(0, new_corpus_count),
            new_lines_count=max(0, new_lines_count),
        )

        logger.debug(
            f"Collected snapshot cycle={cycle}: "
            f"corpus_total={summary.corpus_total}, "
            f"lines_covered={summary.lines_covered}, "
            f"new_corpus={new_corpus_count}, "
            f"new_lines={new_lines_count}"
        )

        return snapshot

    def export_unique_corpus(self, output_dir: Path):
        """Export only corpus that contributes unique coverage with coverage metadata.

        Copies only the corpus files that added unique coverage to the output
        directory and creates a `.{hash}.cov` file for each with coverage info.

        Output structure:
            corpus_unique/
            ├── {hash1}           # corpus file content
            ├── .{hash1}.cov      # JSON coverage metadata (elapsed_time, coverage info)
            ├── {hash2}
            ├── .{hash2}.cov
            └── ...

        The .cov file contains:
            - hash: SHA256 hash of corpus content (12 hex chars)
            - elapsed_time: Seconds since CRS run started when corpus was discovered
                (None if run_start_time not set)
            - file_size: Size of the corpus file in bytes
            - contributes_unique: Always True for exported corpus
            - coverage: Function-level coverage data

        Args:
            output_dir: Directory to export unique corpus to (corpus_unique/).
        """
        import json

        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        contributing_hashes = self.store.get_contributing_corpus()
        exported_count = 0

        for corpus_hash in contributing_hashes:
            # Find the original file path
            if corpus_hash not in self._corpus_hash_to_path:
                logger.debug(f"No path found for contributing corpus {corpus_hash}")
                continue

            src_path = self._corpus_hash_to_path[corpus_hash]
            if not src_path.exists():
                logger.debug(f"Source file not found: {src_path}")
                continue

            # Copy corpus file to output directory with hash as filename
            dst_path = output_dir / corpus_hash
            try:
                shutil.copy2(src_path, dst_path)
            except Exception as e:
                logger.warning(f"Failed to export corpus {corpus_hash}: {e}")
                continue

            # Get corpus coverage info from store
            corpus_cov = self.store.corpus.get(corpus_hash)
            if corpus_cov:
                # Calculate elapsed time from CRS run start (in seconds).
                # This reflects fuzzing time, not build+fuzz time.
                # Uses file mtime (when fuzzer wrote the file) minus run start time.
                elapsed_time = None
                if self.run_start_time is not None:
                    elapsed_time = corpus_cov.first_seen_ts - self.run_start_time

                # Write .{hash}.cov metadata file
                cov_path = output_dir / f".{corpus_hash}.cov"
                cov_data = {
                    "hash": corpus_cov.hash,
                    "elapsed_time": elapsed_time,
                    "file_size": corpus_cov.file_size,
                    "contributes_unique": corpus_cov.contributes_unique,
                    "coverage": {
                        func_name: {
                            "src": func_cov.src,
                            "lines": func_cov.lines,
                        }
                        for func_name, func_cov in corpus_cov.coverage.items()
                    },
                }
                try:
                    cov_path.write_text(json.dumps(cov_data, indent=2))
                except Exception as e:
                    logger.warning(
                        f"Failed to write coverage metadata for {corpus_hash}: {e}"
                    )

            exported_count += 1

        logger.info(
            f"Exported {exported_count}/{len(contributing_hashes)} "
            f"unique corpus files to {output_dir}"
        )

    def export_deduped_corpus(self, output_dir: Path):
        """Alias for export_unique_corpus for backwards compatibility."""
        self.export_unique_corpus(output_dir)

    def get_summary(self) -> CoverageSummary:
        """Get current coverage summary.

        Returns:
            Current CoverageSummary from the store.
        """
        return self.store.get_summary()

    def reset_snapshot_tracking(self):
        """Reset snapshot delta tracking.

        Call this when starting a new trial or when snapshot
        tracking should be reset.
        """
        self._last_corpus_total = 0
        self._last_lines_covered = 0

    def _compute_hash(self, corpus_path: Path) -> str:
        """Compute SHA256 hash of corpus file content.

        Args:
            corpus_path: Path to corpus file.

        Returns:
            First 12 hex characters of SHA256 hash.
        """
        if not corpus_path.exists():
            return hashlib.sha256(b"").hexdigest()[:12]

        sha256 = hashlib.sha256()
        with corpus_path.open("rb") as f:
            while chunk := f.read(8192):
                sha256.update(chunk)
        return sha256.hexdigest()[:12]

    def _parse_coverage_data(self, summary_path: Path) -> dict:
        """Parse coverage data from summary.json.

        Extracts function-level coverage data from the LLVM cov
        or JaCoCo summary.json file.

        Args:
            summary_path: Path to summary.json file.

        Returns:
            Coverage data dictionary in format:
            {function_name: {"src": str, "lines": list[int]}, ...}
        """
        try:
            import json

            with summary_path.open() as f:
                data = json.load(f)
        except Exception as e:
            logger.warning(f"Failed to parse coverage data: {e}")
            return {}

        result: dict = {}

        # Parse llvm-cov export format
        # Structure: data[].functions[] has regions at entry level
        # (not inside files[] - files[] has segments for file-level data)
        if "data" in data and data["data"]:
            for entry in data["data"]:
                # Functions are at entry level, not inside files
                functions = entry.get("functions", [])
                for func in functions:
                    func_name = func.get("name", "")
                    if not func_name:
                        continue

                    # Get filename from filenames array (first element)
                    filenames = func.get("filenames", [])
                    filename = filenames[0] if filenames else ""

                    # Extract covered line numbers from regions
                    lines = []
                    for region in func.get("regions", []):
                        if len(region) >= 5 and region[4] > 0:
                            # Region format: [line_start, col_start, line_end, col_end, count, ...]
                            start_line = region[0]
                            end_line = region[2]
                            lines.extend(range(start_line, end_line + 1))

                    if lines:  # Only add if we have covered lines
                        result[func_name] = {
                            "src": filename,
                            "lines": sorted(set(lines)),
                        }

        return result
