"""Corpus collector for seed import.

Collects corpus files from experiment output and stores them with metadata
for later use as seed corpus in benchmarks.
"""

import hashlib
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class CorpusFile:
    """Represents a corpus file with metadata."""

    path: Path
    content_hash: str
    mtime: float
    relative_time: float
    size: int


@dataclass
class CollectionResult:
    """Result of corpus collection operation."""

    benchmark_name: str
    harness_name: str
    total_files: int
    output_dir: Path
    warnings: list[str] = field(default_factory=list)


class CorpusCollector:
    """Collects corpus files from experiment output.

    Reads experiment output, extracts corpus files with their timestamps,
    and stores them in the benchmark's .aixcc directory with a manifest.

    Output structure:
        .aixcc/{harness_name}/corpus/
        ├── manifest.json     # Metadata for all files
        ├── {hash1}           # Actual corpus files (named by content hash)
        ├── {hash2}
        └── ...

    Manifest format:
        {
            "crs_run_start_time": 1234567890.0,
            "files": {
                "abc123def456": {
                    "relative_time": 500.0,
                    "original_name": "original_filename",
                    "size": 1234
                },
                ...
            }
        }
    """

    def __init__(self, experiment_dir: Path, benchmarks_path: Path):
        """Initialize collector.

        Args:
            experiment_dir: Path to experiment output directory
            benchmarks_path: Path to benchmarks directory (default: ./benchmarks/)
        """
        self.experiment_dir = experiment_dir.resolve()
        self.benchmarks_path = benchmarks_path.resolve()

    def collect(self, *, force: bool = False) -> CollectionResult:
        """Collect corpus files and store in benchmark directory.

        Args:
            force: If True, overwrite existing corpus directory

        Returns:
            CollectionResult with collection statistics

        Raises:
            FileNotFoundError: If experiment or benchmark not found
            ValueError: If required data is missing or invalid
        """
        warnings: list[str] = []

        # 1. Read experiment config to get benchmark name
        config = self._read_experiment_config()
        benchmark_name = config.get("benchmark_name") or config.get("benchmark")
        if not benchmark_name:
            raise ValueError(
                "Could not determine benchmark name from experiment config"
            )

        harness_name = config.get("harness_name") or config.get("harness")
        if not harness_name:
            raise ValueError("Could not determine harness name from experiment config")

        logger.info(
            f"Collecting corpus for benchmark: {benchmark_name}, harness: {harness_name}"
        )

        # 2. Find first trial directory
        trial_dir = self._find_trial_dir()
        logger.info(f"Using trial directory: {trial_dir}")

        # 3. Load crs_run_start_time from pov_store.json
        crs_start_time = self._load_crs_run_start_time(trial_dir)
        if crs_start_time is None:
            raise ValueError(
                f"Could not load crs_run_start_time from {trial_dir}/povs/pov_store.json"
            )
        logger.info(f"CRS run start time: {crs_start_time}")

        # 4. Find and collect corpus files
        corpus_dir = self._find_corpus_dir(trial_dir)
        logger.info(f"Found corpus directory: {corpus_dir}")

        corpus_files = self._collect_corpus_files(corpus_dir, crs_start_time)
        if not corpus_files:
            warnings.append("No corpus files found in trial output")
            logger.warning("No corpus files found")

        # Filter out files with negative relative time
        valid_files = []
        for cf in corpus_files:
            if cf.relative_time < 0:
                warnings.append(
                    f"Skipped file with negative relative time: {cf.path.name}"
                )
            else:
                valid_files.append(cf)

        logger.info(f"Collected {len(valid_files)} corpus files")

        # 5. Locate benchmark in benchmarks_path
        benchmark_dir = self.benchmarks_path / benchmark_name
        if not benchmark_dir.exists():
            raise FileNotFoundError(f"Benchmark not found: {benchmark_dir}")

        # 6. Write to benchmark/.aixcc/{harness}/corpus/
        output_dir = benchmark_dir / ".aixcc" / harness_name / "corpus"

        if output_dir.exists() and not force:
            raise FileExistsError(
                f"Corpus directory already exists: {output_dir}. Use --force to overwrite."
            )

        self._write_corpus(valid_files, output_dir, crs_start_time)

        return CollectionResult(
            benchmark_name=benchmark_name,
            harness_name=harness_name,
            total_files=len(valid_files),
            output_dir=output_dir,
            warnings=warnings,
        )

    def _read_experiment_config(self) -> dict:
        """Read experiment configuration.

        Looks for metadata.json or config.yaml in experiment_dir or trial directory.

        Returns:
            Configuration dictionary

        Raises:
            FileNotFoundError: If no config found
        """
        # Try trial directory's metadata.json first (actual experiment output format)
        trial_dir = self._find_trial_dir()
        metadata_path = trial_dir / "metadata.json"
        if metadata_path.exists():
            with metadata_path.open() as f:
                return json.load(f)

        # Try experiment_dir/config.yaml
        config_path = self.experiment_dir / "config.yaml"
        if config_path.exists():
            with config_path.open() as f:
                return yaml.safe_load(f) or {}

        # Try trial-1/config.yaml
        trial_config = trial_dir / "config.yaml"
        if trial_config.exists():
            with trial_config.open() as f:
                return yaml.safe_load(f) or {}

        raise FileNotFoundError(
            f"No experiment config found in {self.experiment_dir} or trial subdirectories"
        )

    def _find_trial_dir(self) -> Path:
        """Find the first trial directory.

        Handles multiple directory structures:
        1. Direct: experiment_dir/trial-1/
        2. Nested: experiment_dir/{benchmark}/{harness}/{mode}/{sanitizer}/trial-1/

        Returns:
            Path to trial-1 directory

        Raises:
            FileNotFoundError: If no trial directory found
        """
        # Look for trial-1 directly first
        trial_1 = self.experiment_dir / "trial-1"
        if trial_1.exists():
            return trial_1

        # Look for any trial-N directory directly
        for item in sorted(self.experiment_dir.iterdir()):
            if item.is_dir() and item.name.startswith("trial-"):
                return item

        # Search recursively for trial-N directories (nested structure)
        for trial_dir in sorted(self.experiment_dir.rglob("trial-*")):
            if trial_dir.is_dir():
                return trial_dir

        raise FileNotFoundError(f"No trial directory found in {self.experiment_dir}")

    def _find_corpus_dir(self, trial_dir: Path) -> Path:
        """Find the corpus directory within a trial.

        Handles multiple locations:
        1. trial_dir/output/corpus/
        2. trial_dir/crs-build/run/.../corpus/ (nested in run directory)

        Args:
            trial_dir: Path to trial directory

        Returns:
            Path to corpus directory

        Raises:
            FileNotFoundError: If corpus directory not found
        """
        # Try direct path first
        direct_corpus = trial_dir / "output" / "corpus"
        if direct_corpus.exists():
            return direct_corpus

        # Search in crs-build/run/ subdirectories
        crs_build_run = trial_dir / "crs-build" / "run"
        if crs_build_run.exists():
            for corpus_dir in crs_build_run.rglob("corpus"):
                if corpus_dir.is_dir():
                    # Verify it has files (not just an empty directory)
                    if any(corpus_dir.iterdir()):
                        return corpus_dir

        # As a last resort, search anywhere under trial_dir
        for corpus_dir in trial_dir.rglob("corpus"):
            if corpus_dir.is_dir() and any(corpus_dir.iterdir()):
                return corpus_dir

        raise FileNotFoundError(f"No corpus directory found in {trial_dir}")

    def _load_crs_run_start_time(self, trial_dir: Path) -> Optional[float]:
        """Load CRS run start time from pov_store.json.

        Args:
            trial_dir: Path to trial directory

        Returns:
            CRS run start timestamp or None if not found
        """
        pov_store_path = trial_dir / "povs" / "pov_store.json"
        if not pov_store_path.exists():
            return None

        try:
            with pov_store_path.open() as f:
                data = json.load(f)
            return data.get("crs_run_start_time")
        except (json.JSONDecodeError, KeyError):
            return None

    def _collect_corpus_files(
        self, corpus_dir: Path, crs_start_time: float
    ) -> list[CorpusFile]:
        """Collect corpus files with metadata.

        Skips hidden files (those starting with ".") which are typically
        metadata files like ".filename.metadata".

        Args:
            corpus_dir: Path to corpus directory
            crs_start_time: CRS run start timestamp

        Returns:
            List of CorpusFile objects
        """
        corpus_files = []

        for file_path in corpus_dir.iterdir():
            if not file_path.is_file():
                continue

            # Skip hidden files (metadata files like .filename.metadata)
            if file_path.name.startswith("."):
                continue

            # Get file stats
            stat = file_path.stat()
            mtime = stat.st_mtime
            relative_time = mtime - crs_start_time

            # Compute content hash
            content_hash = self._compute_hash(file_path)

            corpus_files.append(
                CorpusFile(
                    path=file_path,
                    content_hash=content_hash,
                    mtime=mtime,
                    relative_time=relative_time,
                    size=stat.st_size,
                )
            )

        return corpus_files

    def _compute_hash(self, file_path: Path) -> str:
        """Compute SHA256 hash of file content.

        Args:
            file_path: Path to file

        Returns:
            16-character hex hash
        """
        hasher = hashlib.sha256()
        with file_path.open("rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                hasher.update(chunk)
        return hasher.hexdigest()[:16]

    def _write_corpus(
        self, corpus_files: list[CorpusFile], output_dir: Path, crs_start_time: float
    ) -> None:
        """Write corpus files and manifest to output directory.

        Args:
            corpus_files: List of corpus files to write
            output_dir: Output directory path
            crs_start_time: CRS run start timestamp for manifest
        """
        # Remove existing directory if present
        if output_dir.exists():
            shutil.rmtree(output_dir)

        output_dir.mkdir(parents=True)

        # Build manifest and copy files
        manifest: dict = {
            "crs_run_start_time": crs_start_time,
            "files": {},
        }

        written_count = 0
        for cf in corpus_files:
            dest_path = output_dir / cf.content_hash
            if not dest_path.exists():
                shutil.copy2(cf.path, dest_path)
                written_count += 1

            # Add to manifest (use first occurrence if duplicate hash)
            if cf.content_hash not in manifest["files"]:
                manifest["files"][cf.content_hash] = {
                    "relative_time": cf.relative_time,
                    "original_name": cf.path.name,
                    "size": cf.size,
                }

        # Write manifest
        manifest_path = output_dir / "manifest.json"
        with manifest_path.open("w") as f:
            json.dump(manifest, f, indent=2)

        logger.info(f"Written {written_count} corpus files to {output_dir}")
        logger.info(f"Manifest: {manifest_path}")
