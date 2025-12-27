"""In-memory coverage store with JSON persistence.

This module provides CoverageStore, which tracks coverage data from
corpus files, supports deduplication based on unique line coverage,
and provides JSON persistence for saving/loading state.

Thread Safety:
    All mutations are protected by a threading.Lock to ensure safe
    concurrent access from multiple threads (e.g., snapshot thread
    and main thread).
"""

import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Optional

from crsbench.evaluation.coverage.models import (
    CorpusCoverage,
    CoverageSummary,
    FunctionCoverage,
)
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)

# Type alias for line location: (source_file, line_number)
type LineLocation = tuple[str, int]


class CoverageStore:
    """In-memory coverage store with JSON persistence.

    Tracks coverage data from corpus files, determines which corpus
    files contribute unique coverage, and provides aggregated statistics.

    Thread Safety:
        All mutations are protected by threading.Lock.

    Attributes:
        store_dir: Directory for storing coverage data files
        corpus: Hash-to-CorpusCoverage mapping
        covered_lines: Set of all covered (source, line) tuples
        line_to_first_corpus: Maps each (source, line) to the hash of
            the corpus that first covered it
    """

    def __init__(self, store_dir: Path):
        """Initialize coverage store.

        Args:
            store_dir: Directory for storing coverage data files.
                Will be created if it doesn't exist.
        """
        self.store_dir = store_dir
        self.corpus: dict[str, CorpusCoverage] = {}
        self.covered_lines: set[LineLocation] = set()
        self.line_to_first_corpus: dict[LineLocation, str] = {}
        self._lines_total: int = 0
        self._functions_total: int = 0
        self._lock = threading.Lock()

        # Create store directory if needed
        self.store_dir.mkdir(parents=True, exist_ok=True)

    def add_corpus(
        self,
        corpus_path: Path,
        cov_data: dict,
        *,
        timestamp: Optional[float] = None,
    ) -> tuple[str, bool]:
        """Add corpus file with its coverage data.

        Computes the corpus hash, extracts line coverage, and determines
        whether this corpus contributes unique coverage (lines not
        previously covered by any other corpus).

        Args:
            corpus_path: Path to the corpus file
            cov_data: Coverage data dictionary with format:
                {function_name: {"src": str, "lines": list[int]}, ...}
            timestamp: Optional timestamp for first_seen_ts (defaults to now)

        Returns:
            Tuple of (hash, contributes_unique) where:
                - hash: 12-character hex SHA256 hash of corpus content
                - contributes_unique: True if this corpus adds unique coverage
        """
        # Compute hash from corpus content
        corpus_hash = self._compute_hash(corpus_path)
        file_size = corpus_path.stat().st_size if corpus_path.exists() else 0
        ts = timestamp if timestamp is not None else time.time()

        # Extract line set from coverage data
        line_set = self._extract_lines(cov_data)

        # Convert coverage data to FunctionCoverage models
        coverage_dict: dict[str, FunctionCoverage] = {}
        for func_name, func_data in cov_data.items():
            if isinstance(func_data, dict):
                coverage_dict[func_name] = FunctionCoverage(
                    src=func_data.get("src", ""),
                    lines=func_data.get("lines", []),
                )

        with self._lock:
            # Check if corpus already exists
            if corpus_hash in self.corpus:
                existing = self.corpus[corpus_hash]
                # Keep earliest timestamp
                if ts < existing.first_seen_ts:
                    self.corpus[corpus_hash] = CorpusCoverage(
                        hash=corpus_hash,
                        first_seen_ts=ts,
                        file_size=file_size,
                        contributes_unique=existing.contributes_unique,
                        coverage=coverage_dict,
                    )
                return corpus_hash, existing.contributes_unique

            # Find new lines (not in covered_lines)
            new_lines = line_set - self.covered_lines
            contributes_unique = len(new_lines) > 0

            # Update covered lines and attribution
            for line_loc in new_lines:
                self.covered_lines.add(line_loc)
                self.line_to_first_corpus[line_loc] = corpus_hash

            # Store corpus coverage
            self.corpus[corpus_hash] = CorpusCoverage(
                hash=corpus_hash,
                first_seen_ts=ts,
                file_size=file_size,
                contributes_unique=contributes_unique,
                coverage=coverage_dict,
            )

            logger.debug(
                f"Added corpus {corpus_hash}: "
                f"contributes_unique={contributes_unique}, "
                f"new_lines={len(new_lines)}"
            )

            return corpus_hash, contributes_unique

    def get_contributing_corpus(self) -> list[str]:
        """Get hashes of corpus files that contribute unique coverage.

        Returns:
            List of corpus hashes that added at least one unique line
            of coverage when they were added.
        """
        with self._lock:
            return [h for h, c in self.corpus.items() if c.contributes_unique]

    def get_summary(self) -> CoverageSummary:
        """Get current coverage summary statistics.

        Returns:
            CoverageSummary with aggregated statistics including:
                - Total and contributing corpus counts
                - Lines covered and percentage
                - Functions covered (functions with at least one covered line)
        """
        with self._lock:
            corpus_total = len(self.corpus)
            corpus_contributing = sum(
                1 for c in self.corpus.values() if c.contributes_unique
            )
            lines_covered = len(self.covered_lines)
            lines_total = self._lines_total
            lines_percent = (
                (lines_covered / lines_total * 100.0) if lines_total > 0 else 0.0
            )

            # Count functions with at least one covered line
            functions_with_coverage: set[str] = set()
            for cov in self.corpus.values():
                for func_name, func_cov in cov.coverage.items():
                    if func_cov.lines:
                        functions_with_coverage.add(func_name)

            return CoverageSummary(
                metric="line",
                corpus_total=corpus_total,
                corpus_contributing=corpus_contributing,
                lines_covered=lines_covered,
                lines_total=lines_total,
                lines_percent=lines_percent,
                functions_covered=len(functions_with_coverage),
                functions_total=self._functions_total,
                saturation_detected=False,
            )

    def set_totals(self, lines_total: int, functions_total: int):
        """Set the total number of coverable lines and functions.

        This should be called when coverage baseline information is available
        (e.g., from LCOV info file or source analysis).

        Args:
            lines_total: Total number of coverable lines in the target
            functions_total: Total number of functions in the target
        """
        with self._lock:
            self._lines_total = lines_total
            self._functions_total = functions_total

    def save(self, path: Path):
        """Persist store state to JSON file.

        Args:
            path: Path to output JSON file
        """
        with self._lock:
            data = {
                "corpus": {h: c.model_dump() for h, c in self.corpus.items()},
                "covered_lines": [
                    {"src": src, "line": line}
                    for src, line in sorted(self.covered_lines)
                ],
                "line_to_first_corpus": {
                    f"{src}:{line}": corpus_hash
                    for (src, line), corpus_hash in self.line_to_first_corpus.items()
                },
                "lines_total": self._lines_total,
                "functions_total": self._functions_total,
            }

        # Write outside lock to minimize lock duration
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2))
        logger.debug(f"Saved coverage store to {path}")

    def load(self, path: Path):
        """Load store state from JSON file.

        Args:
            path: Path to input JSON file

        Raises:
            FileNotFoundError: If the file doesn't exist
            json.JSONDecodeError: If the file contains invalid JSON
        """
        data = json.loads(path.read_text())

        with self._lock:
            # Load corpus
            self.corpus = {}
            for h, c_data in data.get("corpus", {}).items():
                # Convert coverage dict back to FunctionCoverage models
                if "coverage" in c_data:
                    coverage_dict: dict[str, FunctionCoverage] = {}
                    for func_name, func_data in c_data["coverage"].items():
                        coverage_dict[func_name] = FunctionCoverage(**func_data)
                    c_data["coverage"] = coverage_dict
                self.corpus[h] = CorpusCoverage(**c_data)

            # Load covered lines
            self.covered_lines = set()
            for item in data.get("covered_lines", []):
                self.covered_lines.add((item["src"], item["line"]))

            # Load line attribution
            self.line_to_first_corpus = {}
            for key, corpus_hash in data.get("line_to_first_corpus", {}).items():
                src, line_str = key.rsplit(":", 1)
                self.line_to_first_corpus[(src, int(line_str))] = corpus_hash

            # Load totals
            self._lines_total = data.get("lines_total", 0)
            self._functions_total = data.get("functions_total", 0)

        logger.debug(f"Loaded coverage store from {path}")

    def _compute_hash(self, corpus_path: Path) -> str:
        """Compute SHA256 hash of corpus file content.

        Args:
            corpus_path: Path to corpus file

        Returns:
            First 12 hex characters of SHA256 hash
        """
        if not corpus_path.exists():
            # Return hash of empty content for non-existent files
            return hashlib.sha256(b"").hexdigest()[:12]

        sha256 = hashlib.sha256()
        with corpus_path.open("rb") as f:
            while chunk := f.read(8192):
                sha256.update(chunk)
        return sha256.hexdigest()[:12]

    def _extract_lines(self, cov_data: dict) -> set[LineLocation]:
        """Extract line set from coverage data.

        Args:
            cov_data: Coverage data dictionary with format:
                {function_name: {"src": str, "lines": list[int]}, ...}

        Returns:
            Set of (source_file, line_number) tuples
        """
        lines: set[LineLocation] = set()
        for func_data in cov_data.values():
            if isinstance(func_data, dict):
                src = func_data.get("src", "")
                for line in func_data.get("lines", []):
                    lines.add((src, line))
        return lines
