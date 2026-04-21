"""Corpus collector: import seed corpus from experiment output.

Imports seed corpus files from one or more trial directories and stores them
deduplicated in the benchmark's ``.aixcc/{harness}/corpus/`` directory.

Supports three shapes of ``experiment_dir``:

1. A single trial directory (``experiment_dir/trial-N/`` or the trial itself).
2. A tree containing multiple trials for one ``(benchmark, harness)`` pair
   (aggregated by content hash).
3. A tree containing multiple ``(benchmark, harness)`` pairs, e.g.
   ``<root>/<project>/<harness>/<mode>/<sanitizer>/trial-N/output/seeds/``.
   Enable this shape with ``all_mode=True``.

Merge is the default: existing files in ``.aixcc/{harness}/corpus/`` are kept
and new files are added. ``force=True`` replaces the directory instead.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Optional

import yaml

from crsbench.utils.logger import get_logger

logger = get_logger(__name__)

CORPUS_DIR_NAME = "corpus"


@dataclass
class CorpusFile:
    """One corpus file staged for import."""

    path: Path
    content_hash: str
    size: int
    relative_time: Optional[float] = None


@dataclass
class TrialSource:
    """A single trial contributing seed files to a benchmark/harness pair."""

    trial_dir: Path
    seeds_dir: Path
    benchmark: str
    harness: str
    crs_start_time: Optional[float]


@dataclass
class CollectionResult:
    """Outcome of importing one ``(benchmark, harness)`` group."""

    benchmark_name: str
    harness_name: str
    total_files: int
    new_files: int
    source_trials: int
    output_dir: Path
    warnings: list[str] = field(default_factory=list)


class CorpusCollector:
    """Import seed corpus from experiment output into benchmark directories.

    Output per ``(benchmark, harness)``::

        .aixcc/{harness}/corpus/
        ├── manifest.json
        ├── {hash1}
        ├── {hash2}
        └── ...

    Files are named by truncated SHA256 (16 hex chars) of file contents, so
    duplicates collapse to a single entry.
    """

    def __init__(self, experiment_dir: Path, benchmarks_path: Path) -> None:
        self.experiment_dir = experiment_dir.resolve()
        self.benchmarks_path = benchmarks_path.resolve()

    # ------------------------------------------------------------------ API

    def collect(
        self,
        *,
        force: bool = False,
        all_mode: bool = False,
        benchmark_filter: Optional[str] = None,
        harness_filter: Optional[str] = None,
        dry_run: bool = False,
    ) -> list[CollectionResult]:
        """Import seeds from discovered trials into benchmark corpus dirs.

        Args:
            force: If True, replace existing corpus instead of merging.
            all_mode: If True, allow multiple ``(benchmark, harness)`` groups
                found under ``experiment_dir`` to be imported in one call.
                Without ``all_mode``, multiple groups raise ``ValueError``.
            benchmark_filter: Only import trials whose benchmark matches.
            harness_filter: Only import trials whose harness matches.
            dry_run: If True, compute results without touching the filesystem.
                No corpus directory is created, no files are copied, and no
                manifest is written. The returned ``CollectionResult.new_files``
                counts still reflect what *would* be written.

        Returns:
            One ``CollectionResult`` per ``(benchmark, harness)`` group.
        """
        trials = self._discover_trials()
        if benchmark_filter:
            trials = [t for t in trials if t.benchmark == benchmark_filter]
        if harness_filter:
            trials = [t for t in trials if t.harness == harness_filter]

        if not trials:
            raise FileNotFoundError(
                f"No trial-*/output/seeds directories found under {self.experiment_dir}"
            )

        groups: dict[tuple[str, str], list[TrialSource]] = {}
        for trial in trials:
            groups.setdefault((trial.benchmark, trial.harness), []).append(trial)

        if not all_mode and len(groups) > 1:
            summary = ", ".join(f"{b}/{h}" for (b, h) in sorted(groups))
            raise ValueError(
                f"Found multiple benchmark/harness pairs: {summary}. "
                f"Pass --all to import every pair, or use --benchmark/--harness to filter."
            )

        total_groups = len(groups)
        unique_benchmarks = len({b for (b, _) in groups})
        logger.info(
            f"Importing {total_groups} (benchmark, harness) group(s) "
            f"across {unique_benchmarks} unique benchmark(s)"
        )

        results: list[CollectionResult] = []
        for index, ((benchmark, harness), trial_list) in enumerate(
            sorted(groups.items()), start=1
        ):
            logger.info(
                f"[{index}/{total_groups}] {benchmark}/{harness}: "
                f"processing {len(trial_list)} trial(s)..."
            )
            result = self._import_group(
                benchmark, harness, trial_list, force=force, dry_run=dry_run
            )
            results.append(result)
        return results

    # -------------------------------------------------------- Discovery

    def _discover_trials(self) -> list[TrialSource]:
        """Find all importable trials under ``experiment_dir``.

        Uses a bounded ``os.scandir`` walk that *stops descending* once it
        enters a ``trial-*`` directory. Without this pruning, a naive
        ``rglob("trial-*")`` on a combined experiment tree would walk every
        seed file under ``trial-*/output/seeds/`` — potentially millions of
        stat calls — just to find the trial directories themselves.
        """
        logger.info(f"Scanning for trial directories under {self.experiment_dir}...")
        trial_dirs = self._find_trial_dirs()
        logger.info(f"Found {len(trial_dirs)} candidate trial director(ies)")

        trials: list[TrialSource] = []
        skipped_no_seeds: list[Path] = []
        skipped_no_identity: list[Path] = []
        for trial_dir in trial_dirs:
            seeds_dir = self._find_seeds_dir(trial_dir)
            if seeds_dir is None:
                skipped_no_seeds.append(trial_dir)
                continue

            benchmark, harness = self._identify_benchmark_and_harness(trial_dir)
            if benchmark is None or harness is None:
                skipped_no_identity.append(trial_dir)
                continue

            trials.append(
                TrialSource(
                    trial_dir=trial_dir,
                    seeds_dir=seeds_dir,
                    benchmark=benchmark,
                    harness=harness,
                    crs_start_time=self._load_crs_run_start_time(trial_dir),
                )
            )

        logger.info(f"Matched {len(trials)} trial(s) with seeds to import")
        if skipped_no_seeds:
            logger.warning(
                f"Skipped {len(skipped_no_seeds)} trial(s) without output/seeds or "
                f"output/corpus dir (example: {skipped_no_seeds[0]})"
            )
        if skipped_no_identity:
            logger.warning(
                f"Skipped {len(skipped_no_identity)} trial(s) whose benchmark/harness "
                f"could not be determined from metadata.json, config.yaml, or the "
                f"path layout <proj>/<harness>/<mode>/<san>/trial-* "
                f"(example: {skipped_no_identity[0]})"
            )
        return trials

    def _find_trial_dirs(self, *, max_depth: int = 6) -> list[Path]:
        """Return every ``trial-*`` directory reachable from ``experiment_dir``.

        Bounded DFS that prunes at ``trial-*`` so we never walk into
        ``output/seeds/`` during discovery.
        """
        if self.experiment_dir.name.startswith("trial-"):
            return [self.experiment_dir]

        found: list[Path] = []
        self._scan_for_trials(
            self.experiment_dir, depth=0, max_depth=max_depth, out=found
        )
        return sorted(found)

    def _scan_for_trials(
        self, directory: Path, *, depth: int, max_depth: int, out: list[Path]
    ) -> None:
        if depth > max_depth:
            return
        try:
            iterator = os.scandir(directory)
        except OSError:
            return
        with iterator as entries:
            for entry in entries:
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                if not is_dir:
                    continue
                child = Path(entry.path)
                if entry.name.startswith("trial-"):
                    out.append(child)
                    # Do not descend — trials hold the potentially huge
                    # output/seeds/ tree we explicitly want to skip.
                    continue
                self._scan_for_trials(
                    child, depth=depth + 1, max_depth=max_depth, out=out
                )

    def _find_seeds_dir(self, trial_dir: Path) -> Optional[Path]:
        """Return the per-trial seeds directory or None if absent."""
        for candidate in (
            trial_dir / "output" / "seeds",
            trial_dir / "output" / CORPUS_DIR_NAME,
        ):
            if candidate.is_dir():
                return candidate
        return None

    def _identify_benchmark_and_harness(
        self, trial_dir: Path
    ) -> tuple[Optional[str], Optional[str]]:
        """Resolve (benchmark, harness) using metadata.json, config.yaml, or path."""
        metadata_path = trial_dir / "metadata.json"
        if metadata_path.is_file():
            try:
                with metadata_path.open() as f:
                    data = json.load(f)
                benchmark = data.get("benchmark_name") or data.get("benchmark")
                harness = data.get("harness_name") or data.get("harness")
                if benchmark and harness:
                    return str(benchmark), str(harness)
            except (json.JSONDecodeError, OSError):
                pass

        config_path = trial_dir / "config.yaml"
        if config_path.is_file():
            try:
                with config_path.open() as f:
                    data = yaml.safe_load(f) or {}
                benchmark = data.get("benchmark_name") or data.get("benchmark")
                harness = data.get("harness_name") or data.get("harness")
                if benchmark and harness:
                    return str(benchmark), str(harness)
            except (yaml.YAMLError, OSError):
                pass

        # Fall back to inferring from the path shape
        # <root>/<project>/<harness>/<mode>/<sanitizer>/trial-N/
        try:
            rel = trial_dir.relative_to(self.experiment_dir)
        except ValueError:
            return None, None

        parts = rel.parts
        if len(parts) >= 5 and parts[-1].startswith("trial-"):
            return parts[-5], parts[-4]
        return None, None

    @staticmethod
    def _load_crs_run_start_time(trial_dir: Path) -> Optional[float]:
        pov_store_path = trial_dir / "povs" / "pov_store.json"
        if not pov_store_path.is_file():
            return None
        try:
            with pov_store_path.open() as f:
                data = json.load(f)
            value = data.get("crs_run_start_time")
            return float(value) if value is not None else None
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            return None

    # ---------------------------------------------------------- Import

    def _import_group(
        self,
        benchmark: str,
        harness: str,
        trials: list[TrialSource],
        *,
        force: bool,
        dry_run: bool = False,
    ) -> CollectionResult:
        benchmark_dir = self.benchmarks_path / benchmark
        if not benchmark_dir.is_dir():
            raise FileNotFoundError(f"Benchmark not found: {benchmark_dir}")

        output_dir = benchmark_dir / ".aixcc" / harness / CORPUS_DIR_NAME
        warnings: list[str] = []

        # Seed state from the existing corpus when merging; skip when replacing.
        if force:
            files_map: dict[str, dict] = {}
            source_trials: list[dict] = []
        else:
            existing_manifest = _load_manifest(output_dir)
            files_map = dict(existing_manifest.get("files", {}))
            source_trials = list(existing_manifest.get("source_trials", []))

        # Track which hashes are already present so "new" is computed in-memory
        # and the dry-run path does not depend on output_dir existing.
        present_hashes: set[str] = set(files_map.keys())
        if not force and output_dir.exists():
            for entry in output_dir.iterdir():
                if entry.is_file() and entry.name != "manifest.json":
                    present_hashes.add(entry.name)

        if not dry_run:
            if output_dir.exists() and force:
                shutil.rmtree(output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)

        new_files = 0
        sorted_trials = sorted(trials, key=lambda t: str(t.trial_dir))
        total_trials = len(sorted_trials)
        for trial_index, trial in enumerate(sorted_trials, start=1):
            logger.info(
                f"  trial {trial_index}/{total_trials}: hashing seeds from "
                f"{self._trial_identifier(trial)}"
            )
            collected = self._collect_trial_files(trial, warnings)
            trial_key = self._trial_identifier(trial)
            for cf in collected:
                if cf.content_hash not in present_hashes:
                    if not dry_run:
                        shutil.copy2(cf.path, output_dir / cf.content_hash)
                    present_hashes.add(cf.content_hash)
                    new_files += 1
                entry = files_map.get(cf.content_hash)
                if entry is None:
                    entry = {
                        "size": cf.size,
                        "original_names": [cf.path.name],
                        "first_trial": trial_key,
                    }
                    if cf.relative_time is not None:
                        entry["relative_time"] = cf.relative_time
                    files_map[cf.content_hash] = entry
                else:
                    names = entry.setdefault("original_names", [])
                    if cf.path.name not in names:
                        names.append(cf.path.name)
                    if "relative_time" not in entry and cf.relative_time is not None:
                        entry["relative_time"] = cf.relative_time

            source_trials.append(
                {
                    "path": trial_key,
                    "crs_run_start_time": trial.crs_start_time,
                    "file_count": len(collected),
                }
            )

        if not dry_run:
            manifest = {
                "total_files": len(files_map),
                "source_trials": source_trials,
                "updated_at": datetime.now(UTC).isoformat(),
                "files": files_map,
            }
            _write_manifest(output_dir, manifest)

        return CollectionResult(
            benchmark_name=benchmark,
            harness_name=harness,
            total_files=len(files_map),
            new_files=new_files,
            source_trials=len(trials),
            output_dir=output_dir,
            warnings=warnings,
        )

    def _collect_trial_files(
        self, trial: TrialSource, warnings: list[str]
    ) -> list[CorpusFile]:
        files: list[CorpusFile] = []
        for path in sorted(trial.seeds_dir.iterdir()):
            if not path.is_file() or path.name.startswith("."):
                continue

            stat = path.stat()
            relative_time: Optional[float] = None
            if trial.crs_start_time is not None:
                relative_time = stat.st_mtime - trial.crs_start_time
                if relative_time < 0:
                    warnings.append(
                        f"Skipped file with negative relative time: "
                        f"{path.name} (trial={trial.trial_dir})"
                    )
                    continue

            files.append(
                CorpusFile(
                    path=path,
                    content_hash=_compute_hash(path),
                    size=stat.st_size,
                    relative_time=relative_time,
                )
            )
        return files

    def _trial_identifier(self, trial: TrialSource) -> str:
        try:
            return str(trial.trial_dir.relative_to(self.experiment_dir))
        except ValueError:
            return str(trial.trial_dir)


def _compute_hash(file_path: Path) -> str:
    hasher = hashlib.sha256()
    with file_path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()[:16]


def _load_manifest(corpus_dir: Path) -> dict:
    manifest_path = corpus_dir / "manifest.json"
    if not manifest_path.is_file():
        return {}
    try:
        with manifest_path.open() as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def _write_manifest(corpus_dir: Path, manifest: dict) -> None:
    manifest_path = corpus_dir / "manifest.json"
    with manifest_path.open("w") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
        f.write("\n")
