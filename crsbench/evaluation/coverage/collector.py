"""Coverage collector orchestrating coverage collection using strategy pattern.

This module provides CoverageCollector, which orchestrates coverage collection
by delegating to a CoverageStrategy for actual coverage collection and using
CoverageStore for tracking corpus and coverage stats.

Per-Input Coverage:
    Coverage is collected per-corpus file, enabling true attribution of which
    lines each corpus file covers. Corpus files that contribute unique coverage
    are copied to corpus_unique/ and their coverage data saved to corpus_cov/.

Directory Structure:
    coverage/
    ├── corpus_unique/              # Only corpus that added unique lines
    │   ├── abc123def456            # Actual corpus file (copy)
    │   └── 789xyz...
    ├── corpus_cov/                 # Per-corpus coverage data
    │   ├── abc123def456.cov.json   # Coverage from this input
    │   └── 789xyz.cov.json
    └── ...

Usage:
    strategy = LLVMCovLineStrategy(oss_fuzz_path, project_name)
    store = CoverageStore(store_dir)
    collector = CoverageCollector(strategy, store, harness_name="fuzz_target")

    # Process new corpus files (per-input coverage)
    unique_count = collector.process_new_corpus(corpus_dir)

    # Get merged coverage from all corpus files
    merged_cov = collector.get_merged_coverage()
"""

import hashlib
import json
import shutil
import time
from pathlib import Path

from crsbench.evaluation.coverage.models import (
    CoverageSnapshot,
    CoverageSummary,
)
from crsbench.evaluation.coverage.store import CoverageStore
from crsbench.evaluation.coverage.strategy import (
    CoverageStrategy,
    CoverageStrategyError,
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
            output_dir: Directory to save coverage files (corpus_unique/, corpus_cov/).
                        If None, per-input coverage files won't be saved.
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

        # Global set of covered lines: (source_file, line_number) tuples
        self._covered_lines: set[tuple[str, int]] = set()

        # Create output directories for per-input coverage
        self._corpus_unique_dir: Path | None = None
        self._corpus_cov_dir: Path | None = None
        if self.output_dir:
            self._corpus_unique_dir = self.output_dir / "corpus_unique"
            self._corpus_cov_dir = self.output_dir / "corpus_cov"
            self._corpus_unique_dir.mkdir(parents=True, exist_ok=True)
            self._corpus_cov_dir.mkdir(parents=True, exist_ok=True)

    def set_run_start_time(self, start_time: float) -> None:
        """Set the CRS run start time for elapsed_time calculations.

        This should be called when the CRS run actually starts (after build),
        so elapsed_time reflects fuzzing time rather than build+fuzz time.

        Args:
            start_time: Unix timestamp when CRS run started
        """
        self.run_start_time = start_time

    def process_new_corpus(self, corpus_dir: Path) -> int:
        """Process new corpus files with per-input coverage.

        For each new corpus file:
        1. Run coverage collection for that single file
        2. Find which lines are newly covered (delta)
        3. If delta > 0: copy to corpus_unique/, save coverage to corpus_cov/
        4. Update global covered lines and store

        Args:
            corpus_dir: Directory containing corpus files.

        Returns:
            Count of corpus files that added unique coverage.
        """
        corpus_dir = Path(corpus_dir)
        if not corpus_dir.exists():
            logger.debug(f"Corpus directory not found: {corpus_dir}")
            return 0

        # Find all files in corpus directory (filter hidden files)
        corpus_files = [
            f
            for f in corpus_dir.iterdir()
            if f.is_file() and not f.name.startswith(".")
        ]
        if not corpus_files:
            logger.debug(f"No corpus files found in {corpus_dir}")
            return 0

        # Find new files (not already processed)
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

        unique_count = 0

        for corpus_file, file_hash in new_files_with_hash:
            try:
                # Collect coverage for this single file
                cov_data = self.strategy.collect_single_coverage(
                    self.harness_name, corpus_file
                )

                # Find new lines (not in global covered_lines)
                new_lines = self._find_new_lines(cov_data)
                contributes_unique = len(new_lines) > 0

                if contributes_unique:
                    # Copy to corpus_unique/
                    self._copy_to_unique(corpus_file, file_hash)
                    # Save coverage to corpus_cov/
                    self._save_corpus_cov(file_hash, cov_data)
                    # Update global covered lines
                    self._covered_lines.update(new_lines)
                    unique_count += 1
                    logger.debug(
                        f"Corpus {file_hash} contributes {len(new_lines)} new lines"
                    )

                # Register with store (metadata only)
                file_mtime = corpus_file.stat().st_mtime
                self.store.add_corpus(
                    corpus_path=corpus_file,
                    cov_data=cov_data,
                    timestamp=file_mtime,
                )

                # Mark as processed
                self._processed_hashes.add(file_hash)

            except CoverageStrategyError as e:
                logger.warning(f"Failed to collect coverage for {corpus_file}: {e}")
                continue
            except Exception as e:
                logger.warning(f"Error processing corpus {corpus_file}: {e}")
                continue

        logger.info(
            f"Processed {len(new_files_with_hash)} corpus files, "
            f"{unique_count} contributed unique coverage"
        )
        return unique_count

    def _find_new_lines(self, cov_data: dict) -> set[tuple[str, int]]:
        """Find lines covered by this input that aren't already in global set.

        Args:
            cov_data: Coverage data in format {func: {"src": str, "lines": [int]}}

        Returns:
            Set of (source_file, line_number) tuples not in _covered_lines
        """
        input_lines: set[tuple[str, int]] = set()
        for func_data in cov_data.values():
            if isinstance(func_data, dict):
                src = func_data.get("src", "")
                for line in func_data.get("lines", []):
                    input_lines.add((src, line))

        return input_lines - self._covered_lines

    def _copy_to_unique(self, corpus_file: Path, file_hash: str) -> None:
        """Copy corpus file to corpus_unique/ directory.

        Args:
            corpus_file: Path to original corpus file
            file_hash: 12-character hash of corpus content
        """
        if not self._corpus_unique_dir:
            return

        dst = self._corpus_unique_dir / file_hash
        shutil.copy2(corpus_file, dst)
        logger.debug(f"Copied unique corpus to {dst}")

    def _save_corpus_cov(self, file_hash: str, cov_data: dict) -> None:
        """Save coverage data to corpus_cov/ directory.

        Saves coverage in extended format with metadata:
        {
            "meta": {"corpus_hash": str, "collected_at": float, "language": str},
            "coverage": {func_name: {"src": str, "lines": [int]}, ...}
        }

        Args:
            file_hash: 12-character hash of corpus content
            cov_data: Coverage data dictionary
        """
        if not self._corpus_cov_dir:
            return

        cov_file = self._corpus_cov_dir / f"{file_hash}.cov.json"
        extended_data = {
            "meta": {
                "corpus_hash": file_hash,
                "collected_at": time.time(),
                "language": self.strategy.language,
            },
            "coverage": cov_data,
        }
        cov_file.write_text(json.dumps(extended_data, indent=2))
        logger.debug(f"Saved coverage to {cov_file}")

    def get_merged_coverage(self) -> dict:
        """Get union of all coverage data from corpus_cov/ files.

        Merges coverage from all processed corpus files into a single
        coverage dictionary with deduplicated lines per function.

        Returns:
            Merged coverage in format: {func_name: {"src": str, "lines": [int]}, ...}
        """
        if not self._corpus_cov_dir:
            return {}

        merged: dict[str, dict] = {}

        for cov_file in self._corpus_cov_dir.glob("*.cov.json"):
            try:
                data = json.loads(cov_file.read_text())
                # Handle extended format with "coverage" key
                cov_data = data.get("coverage", data)

                for func_name, func_data in cov_data.items():
                    if func_name == "meta":
                        continue
                    if func_name not in merged:
                        merged[func_name] = {
                            "src": func_data.get("src", ""),
                            "lines": set(),
                        }
                    merged[func_name]["lines"].update(func_data.get("lines", []))
            except Exception as e:
                logger.warning(f"Failed to parse {cov_file}: {e}")
                continue

        # Convert sets back to sorted lists
        for func_data in merged.values():
            func_data["lines"] = sorted(func_data["lines"])

        return merged

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
        """Parse coverage data from summary.json or detailed coverage JSON.

        Extracts coverage data from LLVM cov export or JaCoCo detailed format.

        Supported formats:
        1. LLVM-cov export: {"data": [{"functions": [...]}]}
        2. JaCoCo detailed: {"source_path": {"src": str, "lines": [int]}, ...}

        Args:
            summary_path: Path to coverage JSON file.

        Returns:
            Coverage data dictionary in format:
            {function_name: {"src": str, "lines": list[int]}, ...}
        """
        try:
            with summary_path.open() as f:
                data = json.load(f)
        except Exception as e:
            logger.warning(f"Failed to parse coverage data: {e}")
            return {}

        result: dict = {}

        # Check for JaCoCo detailed format (flat dict with src/lines)
        # Format: {"source_path": {"src": str, "lines": [int]}, ...}
        if self._is_jacoco_detailed_format(data):
            return data

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
                    lines: list[int] = []
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

    def _is_jacoco_detailed_format(self, data: dict) -> bool:
        """Check if data is in JaCoCo detailed coverage format.

        JaCoCo detailed format has flat structure:
        {"source_path": {"src": str, "lines": [int]}, ...}

        Args:
            data: Parsed JSON data.

        Returns:
            True if data matches JaCoCo detailed format.
        """
        if not isinstance(data, dict):
            return False

        # JaCoCo format shouldn't have "data" key (LLVM format uses that)
        if "data" in data:
            return False

        # Check if any top-level value has the expected structure
        for value in data.values():
            if isinstance(value, dict) and "src" in value and "lines" in value:
                return True

        return False
