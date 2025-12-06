# Trial Directory Preparation Design

This document describes the implementation of trial directory preparation in CRSBench, which creates isolated directory structures for each trial execution.

## Purpose

Trial directory preparation provides:
- Isolated directory structure for each trial execution
- Pre-cloned source code at specific commits from meta.yaml
- Prepared hints directory with filtered content
- Prepared POVs directory for patch generation CRS
- Build directory for oss-crs CLI usage
- Clean separation enabling parallel execution

## Architecture Overview

```
CRSBench Orchestrator
    ↓
TrialDirectoryPreparer.prepare_trial()
    ↓
├── create_trial_structure()          # Create base directories
├── prepare_source_code()             # Clone source at commit
├── prepare_hints()                   # Copy and filter hints
├── prepare_povs()                    # Copy and flatten POVs
└── create_build_directory()          # Create build dir for oss-crs
    ↓
Trial Directory (Ready for CRS Execution)
```

## Trial Directory Structure

### Complete Directory Layout

```
/experiments/experiment-1/
├── config.yaml                       # Experiment configuration
├── trial-0/                          # Trial 0 (CRS: ensemble-c, Benchmark: json-c)
│   ├── build/                        # Build directory (--build-dir for oss-crs)
│   │   ├── crs/                      # CRS Docker images (created by oss-crs)
│   │   ├── out/                      # Build outputs (created by oss-crs)
│   │   └── src/                      # Pre-cloned source code
│   │       └── json-c/               # Cloned at base_commit from meta.yaml
│   │           ├── .git/
│   │           ├── src/
│   │           └── ...
│   ├── output/                       # CRS outputs (--output for oss-crs)
│   │   ├── povs/                     # POVs discovered (bug finding)
│   │   ├── patches/                  # Patches generated (patch gen)
│   │   ├── corpus/                   # Fuzzing corpus
│   │   └── crs-data/                 # CRS-specific data
│   ├── hints/                        # Prepared hints (--hints for oss-crs)
│   │   ├── sarif/                    # Filtered SARIF files
│   │   │   ├── codeql.sarif
│   │   │   └── semgrep.sarif
│   │   └── corpus/                   # Filtered corpus (1h or 1d)
│   │       ├── input-001
│   │       └── input-002
│   ├── povs/                         # Prepared POVs (--povs for oss-bugfix-crs)
│   │   ├── pov_0                     # Flattened POV blobs
│   │   ├── pov_1
│   │   └── pov_2
│   ├── config.yaml                   # Trial configuration
│   ├── execution.json                # Execution metadata (from executor)
│   └── metadata.json                 # Trial preparation metadata
├── trial-1/                          # Trial 1 (separate isolation)
│   └── ...
└── report/                           # Aggregated results
    ├── summary.json
    └── detailed-results.json
```

### Directory Responsibilities

**Created by TrialDirectoryPreparer**:
- `trial-N/` - Trial root directory
- `trial-N/build/` - Build directory for oss-crs
- `trial-N/build/src/<project>/` - Pre-cloned source code
- `trial-N/output/` - Base output directory (subdirs created by CRS)
- `trial-N/hints/` - Prepared hints with filtering
- `trial-N/povs/` - Prepared POVs with filtering (patch gen only)
- `trial-N/metadata.json` - Preparation metadata

**Created by oss-crs CLI**:
- `trial-N/build/crs/` - CRS Docker images
- `trial-N/build/out/` - Build artifacts
- `trial-N/output/povs/` - POVs discovered (bug finding)
- `trial-N/output/patches/` - Patches generated (patch gen)
- `trial-N/output/corpus/` - Fuzzing corpus
- `trial-N/output/crs-data/` - CRS-specific data

**Created by Orchestrator/Executor**:
- `trial-N/config.yaml` - Trial configuration
- `trial-N/execution.json` - Execution metadata

## Module Design

### Class: TrialDirectoryPreparer

```python
from pathlib import Path
from typing import Optional, Dict, Any
from dataclasses import dataclass

@dataclass
class TrialPreparationResult:
    """Result of trial directory preparation."""
    trial_dir: Path
    build_dir: Path
    source_path: Path
    output_dir: Path
    hints_dir: Optional[Path]
    povs_dir: Optional[Path]
    metadata: Dict[str, Any]
    success: bool
    error: Optional[str] = None


class TrialDirectoryPreparer:
    """Prepares isolated directory structure for CRS trial execution."""

    def __init__(
        self,
        experiment_dir: Path,
        benchmarks_root: Path,
        oss_fuzz_dir: Path,
        config: Dict[str, Any]
    ):
        """
        Initialize trial directory preparer.

        Args:
            experiment_dir: Root directory for experiment
            benchmarks_root: Path to benchmarks directory
            oss_fuzz_dir: Path to oss-fuzz submodule
            config: Experiment configuration
        """
        self.experiment_dir = experiment_dir
        self.benchmarks_root = benchmarks_root
        self.oss_fuzz_dir = oss_fuzz_dir
        self.config = config

    def prepare_trial(
        self,
        crs: str,
        benchmark: str,
        harness: str,
        trial_num: int,
        mode: str = "bug_finding"
    ) -> TrialPreparationResult:
        """
        Prepare complete trial directory structure.

        Args:
            crs: CRS name
            benchmark: Benchmark name
            harness: Harness name
            trial_num: Trial number
            mode: "bug_finding" or "patch_generation"

        Returns:
            TrialPreparationResult with all prepared paths
        """
        # Create trial root
        trial_dir = self._create_trial_directory(crs, benchmark, trial_num)

        # Create directory structure
        build_dir = trial_dir / "build"
        output_dir = trial_dir / "output"
        build_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)

        # Prepare source code
        source_path = self._prepare_source_code(benchmark, build_dir)

        # Prepare hints (if enabled)
        hints_dir = self._prepare_hints(benchmark, harness, trial_dir)

        # Prepare POVs (if patch generation mode)
        povs_dir = None
        if mode == "patch_generation":
            povs_dir = self._prepare_povs(benchmark, harness, trial_dir)

        # Store preparation metadata
        metadata = self._create_metadata(
            crs=crs,
            benchmark=benchmark,
            harness=harness,
            trial_num=trial_num,
            mode=mode,
            source_path=source_path,
            hints_dir=hints_dir,
            povs_dir=povs_dir
        )
        self._write_metadata(trial_dir, metadata)

        return TrialPreparationResult(
            trial_dir=trial_dir,
            build_dir=build_dir,
            source_path=source_path,
            output_dir=output_dir,
            hints_dir=hints_dir,
            povs_dir=povs_dir,
            metadata=metadata,
            success=True
        )
```

### Core Methods

#### 1. Create Trial Directory

```python
def _create_trial_directory(
    self,
    crs: str,
    benchmark: str,
    trial_num: int
) -> Path:
    """
    Create trial root directory.

    Args:
        crs: CRS name
        benchmark: Benchmark name
        trial_num: Trial number

    Returns:
        Path to trial directory
    """
    # Generate trial directory name
    trial_name = f"trial-{trial_num}"

    # Create trial directory
    trial_dir = self.experiment_dir / trial_name
    trial_dir.mkdir(parents=True, exist_ok=True)

    logger.info(f"Created trial directory: {trial_dir}")
    return trial_dir
```

#### 2. Prepare Source Code

```python
def _prepare_source_code(
    self,
    benchmark: str,
    build_dir: Path
) -> Path:
    """
    Clone source code at commit specified in meta.yaml.

    Args:
        benchmark: Benchmark name
        build_dir: Build directory

    Returns:
        Path to cloned source code

    Process:
        1. Read benchmark meta.yaml to get base_commit
        2. Read benchmark project.yaml to get main_repo URL
        3. Clone source at specific commit
        4. Store in build/src/<project-name>/
    """
    from crsbench.migration.repo_manager import ensure_project_repository

    benchmark_dir = self.benchmarks_root / benchmark

    # Use repository manager to clone source
    source_dest = build_dir / "src" / benchmark
    source_path = ensure_project_repository(
        benchmark_dir=str(benchmark_dir),
        project_dir=str(source_dest),
        verbose=self.config.get("verbose", False)
    )

    if not source_path:
        raise TrialPreparationError(
            f"Failed to clone source for {benchmark}. "
            "Check project.yaml main_repo and meta.yaml commits."
        )

    logger.info(f"Prepared source code at: {source_path}")
    return Path(source_path)
```

#### 3. Prepare Hints Directory

```python
def _prepare_hints(
    self,
    benchmark: str,
    harness: str,
    trial_dir: Path
) -> Optional[Path]:
    """
    Prepare hints directory with filtered content.

    Args:
        benchmark: Benchmark name
        harness: Harness name
        trial_dir: Trial directory

    Returns:
        Path to prepared hints directory, or None if not enabled

    Process:
        1. Check if hints are enabled in config
        2. Find source hints in benchmark .aixcc/<harness>/hints/
        3. Create trial-specific hints directory
        4. Copy SARIF files
        5. Copy corpus based on config (1h or 1d)
    """
    if not self.config.get("hints_enabled", False):
        return None

    benchmark_dir = self.benchmarks_root / benchmark
    source_hints = benchmark_dir / ".aixcc" / harness / "hints"

    if not source_hints.exists():
        logger.warning(f"No hints found for {benchmark}/{harness}")
        return None

    # Create trial hints directory
    hints_dir = trial_dir / "hints"
    hints_dir.mkdir(parents=True, exist_ok=True)

    # Copy SARIF files
    sarif_copied = self._copy_sarif_files(source_hints, hints_dir)

    # Copy corpus based on level
    corpus_copied = self._copy_corpus_files(source_hints, hints_dir)

    if sarif_copied or corpus_copied:
        logger.info(f"Prepared hints at: {hints_dir}")
        return hints_dir
    else:
        logger.warning(f"No hints content copied for {benchmark}/{harness}")
        return None


def _copy_sarif_files(
    self,
    source_hints: Path,
    hints_dir: Path
) -> bool:
    """
    Copy SARIF files from source hints.

    Args:
        source_hints: Source hints directory
        hints_dir: Destination hints directory

    Returns:
        True if any files copied
    """
    import shutil

    source_sarif = source_hints / "sarif"
    if not source_sarif.exists():
        return False

    dest_sarif = hints_dir / "sarif"
    dest_sarif.mkdir(exist_ok=True)

    copied = 0
    for sarif_file in source_sarif.glob("*.sarif"):
        shutil.copy2(sarif_file, dest_sarif)
        copied += 1

    logger.info(f"Copied {copied} SARIF files")
    return copied > 0


def _copy_corpus_files(
    self,
    source_hints: Path,
    hints_dir: Path
) -> bool:
    """
    Copy corpus files from source hints based on config level.

    Args:
        source_hints: Source hints directory
        hints_dir: Destination hints directory

    Returns:
        True if any files copied
    """
    import shutil

    corpus_level = self.config.get("hints_corpus_level", "1h")
    source_corpus = source_hints / "corpus" / corpus_level

    if not source_corpus.exists():
        logger.warning(f"Corpus level '{corpus_level}' not found in hints")
        return False

    dest_corpus = hints_dir / "corpus"
    dest_corpus.mkdir(exist_ok=True)

    copied = 0
    for corpus_file in source_corpus.iterdir():
        if corpus_file.is_file():
            shutil.copy2(corpus_file, dest_corpus)
            copied += 1

    logger.info(f"Copied {copied} corpus files (level: {corpus_level})")
    return copied > 0
```

#### 4. Prepare POVs Directory

```python
def _prepare_povs(
    self,
    benchmark: str,
    harness: str,
    trial_dir: Path
) -> Optional[Path]:
    """
    Prepare POVs directory for patch generation.

    Args:
        benchmark: Benchmark name
        harness: Harness name
        trial_dir: Trial directory

    Returns:
        Path to prepared POVs directory, or None if no POVs

    Process:
        1. Find source POVs in benchmark .aixcc/<harness>/cpv_*/blobs/
        2. Create trial-specific POVs directory
        3. Copy and flatten POV blobs (remove cpv_* subdirectories)
        4. Filter based on config (target_povs list)
    """
    benchmark_dir = self.benchmarks_root / benchmark
    source_harness_dir = benchmark_dir / ".aixcc" / harness

    if not source_harness_dir.exists():
        logger.warning(f"No harness directory for {benchmark}/{harness}")
        return None

    # Create trial POVs directory
    povs_dir = trial_dir / "povs"
    povs_dir.mkdir(parents=True, exist_ok=True)

    # Collect POVs from all cpv_* directories
    pov_count = 0
    for cpv_dir in sorted(source_harness_dir.glob("cpv_*")):
        blobs_dir = cpv_dir / "blobs"
        if not blobs_dir.exists():
            continue

        for pov_blob in sorted(blobs_dir.glob("*.blob")):
            # Filter based on config
            if self._should_include_pov(pov_blob.stem):
                # Copy and flatten: pov_0.blob -> povs/pov_0
                dest_name = pov_blob.stem  # Remove .blob extension
                shutil.copy2(pov_blob, povs_dir / dest_name)
                pov_count += 1

    if pov_count > 0:
        logger.info(f"Prepared {pov_count} POVs at: {povs_dir}")
        return povs_dir
    else:
        logger.warning(f"No POVs found for {benchmark}/{harness}")
        return None


def _should_include_pov(self, pov_name: str) -> bool:
    """
    Check if POV should be included based on config.

    Args:
        pov_name: POV name (e.g., "pov_0")

    Returns:
        True if POV should be included
    """
    target_povs = self.config.get("target_povs")

    if not target_povs:
        # No filter, include all POVs
        return True

    # Check if POV is in target list
    return pov_name in target_povs
```

#### 5. Create Metadata

```python
def _create_metadata(
    self,
    crs: str,
    benchmark: str,
    harness: str,
    trial_num: int,
    mode: str,
    source_path: Path,
    hints_dir: Optional[Path],
    povs_dir: Optional[Path]
) -> Dict[str, Any]:
    """
    Create trial preparation metadata.

    Args:
        crs: CRS name
        benchmark: Benchmark name
        harness: Harness name
        trial_num: Trial number
        mode: Execution mode
        source_path: Path to source code
        hints_dir: Path to hints (or None)
        povs_dir: Path to POVs (or None)

    Returns:
        Metadata dictionary
    """
    from datetime import datetime

    # Get source commit from git
    source_commit = self._get_git_commit(source_path)

    # Count files in hints/povs
    hints_stats = self._get_hints_stats(hints_dir) if hints_dir else None
    povs_stats = self._get_povs_stats(povs_dir) if povs_dir else None

    return {
        "timestamp": datetime.now().isoformat(),
        "trial_num": trial_num,
        "crs": crs,
        "benchmark": benchmark,
        "harness": harness,
        "mode": mode,
        "source": {
            "path": str(source_path),
            "commit": source_commit
        },
        "hints": hints_stats,
        "povs": povs_stats,
        "config": {
            "hints_enabled": self.config.get("hints_enabled", False),
            "hints_corpus_level": self.config.get("hints_corpus_level"),
            "target_povs": self.config.get("target_povs")
        }
    }


def _get_git_commit(self, source_path: Path) -> Optional[str]:
    """Get current commit hash from git repository."""
    import subprocess

    try:
        result = subprocess.run(
            ["git", "-C", str(source_path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        return None


def _get_hints_stats(self, hints_dir: Path) -> Dict[str, Any]:
    """Get statistics about prepared hints."""
    sarif_dir = hints_dir / "sarif"
    corpus_dir = hints_dir / "corpus"

    return {
        "path": str(hints_dir),
        "sarif_count": len(list(sarif_dir.glob("*.sarif"))) if sarif_dir.exists() else 0,
        "corpus_count": len(list(corpus_dir.iterdir())) if corpus_dir.exists() else 0
    }


def _get_povs_stats(self, povs_dir: Path) -> Dict[str, Any]:
    """Get statistics about prepared POVs."""
    return {
        "path": str(povs_dir),
        "pov_count": len(list(povs_dir.iterdir()))
    }


def _write_metadata(self, trial_dir: Path, metadata: Dict[str, Any]) -> None:
    """Write metadata to trial directory."""
    import json

    metadata_file = trial_dir / "metadata.json"
    with open(metadata_file, "w") as f:
        json.dump(metadata, f, indent=2)

    logger.info(f"Wrote trial metadata to {metadata_file}")
```

## Error Handling

### Custom Exceptions

```python
class TrialPreparationError(Exception):
    """Raised when trial preparation fails."""
    pass


class SourceCloneError(TrialPreparationError):
    """Raised when source code cloning fails."""
    pass


class HintsPreparationError(TrialPreparationError):
    """Raised when hints preparation fails."""
    pass


class POVsPreparationError(TrialPreparationError):
    """Raised when POVs preparation fails."""
    pass
```

### Error Handling Strategy

```python
def prepare_trial_safe(
    self,
    crs: str,
    benchmark: str,
    harness: str,
    trial_num: int,
    mode: str = "bug_finding"
) -> TrialPreparationResult:
    """
    Safe version of prepare_trial that catches exceptions.

    Returns:
        TrialPreparationResult with success=False on error
    """
    try:
        return self.prepare_trial(crs, benchmark, harness, trial_num, mode)
    except SourceCloneError as e:
        logger.error(f"Source clone failed: {e}")
        return TrialPreparationResult(
            trial_dir=None,
            build_dir=None,
            source_path=None,
            output_dir=None,
            hints_dir=None,
            povs_dir=None,
            metadata={},
            success=False,
            error=f"Source clone failed: {e}"
        )
    except Exception as e:
        logger.error(f"Trial preparation failed: {e}")
        return TrialPreparationResult(
            trial_dir=None,
            build_dir=None,
            source_path=None,
            output_dir=None,
            hints_dir=None,
            povs_dir=None,
            metadata={},
            success=False,
            error=str(e)
        )
```

## Integration with Orchestrator

### Orchestrator Usage

```python
class ExperimentOrchestrator:
    def run_trial(
        self,
        crs: str,
        benchmark: str,
        harness: str,
        trial_num: int,
        config: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Execute single trial."""

        # 1. Prepare trial directory
        preparer = TrialDirectoryPreparer(
            experiment_dir=self.experiment_dir,
            benchmarks_root=CRSBENCH_ROOT / "benchmarks",
            oss_fuzz_dir=CRSBENCH_ROOT / "oss-fuzz",
            config=config
        )

        mode = self._determine_mode(crs, config)
        prep_result = preparer.prepare_trial(
            crs=crs,
            benchmark=benchmark,
            harness=harness,
            trial_num=trial_num,
            mode=mode
        )

        if not prep_result.success:
            logger.error(f"Trial preparation failed: {prep_result.error}")
            return {"success": False, "error": prep_result.error}

        # 2. Write trial config
        self._write_trial_config(prep_result.trial_dir, config)

        # 3. Execute CRS
        executor = self._create_executor(crs, mode, config)
        result = executor.run_crs(
            benchmark_path=self.benchmarks_root / benchmark,
            harness=harness,
            trial_output_dir=prep_result.trial_dir,
            build_dir=prep_result.build_dir,
            source_path=prep_result.source_path,
            hints_dir=prep_result.hints_dir,
            povs_dir=prep_result.povs_dir
        )

        # 4. Collect results
        return self._collect_trial_results(prep_result.trial_dir, result)
```

## Performance Considerations

### Source Clone Optimization

**Problem**: Cloning source for each trial is slow and wastes disk space.

**Solution**: Cache clones in shared location, copy to trial directory.

```python
def _prepare_source_code_with_cache(
    self,
    benchmark: str,
    build_dir: Path
) -> Path:
    """
    Clone source with caching for performance.

    Strategy:
        1. Check shared cache for existing clone
        2. If found, copy to trial directory
        3. If not found, clone and add to cache
    """
    from crsbench.migration.repo_manager import ensure_project_repository

    # Shared cache location
    shared_cache = CRSBENCH_ROOT / ".cache" / "sources"
    shared_cache.mkdir(parents=True, exist_ok=True)

    benchmark_dir = self.benchmarks_root / benchmark

    # Try to get from cache
    cached_source = self._get_cached_source(benchmark, shared_cache)

    if cached_source and cached_source.exists():
        # Copy from cache to trial directory
        source_dest = build_dir / "src" / benchmark
        shutil.copytree(cached_source, source_dest)
        logger.info(f"Used cached source from {cached_source}")
        return source_dest
    else:
        # Clone normally
        source_dest = build_dir / "src" / benchmark
        source_path = ensure_project_repository(
            benchmark_dir=str(benchmark_dir),
            dest_dir=str(source_dest),
            verbose=self.config.get("verbose", False)
        )

        # Add to cache for future trials
        self._cache_source(benchmark, Path(source_path), shared_cache)

        return Path(source_path)
```

### Parallel Preparation

Enable parallel trial preparation when running distributed mode:

```python
from concurrent.futures import ThreadPoolExecutor

def prepare_trials_parallel(
    self,
    trials: List[Trial],
    max_workers: int = 4
) -> List[TrialPreparationResult]:
    """
    Prepare multiple trials in parallel.

    Args:
        trials: List of trials to prepare
        max_workers: Maximum parallel workers

    Returns:
        List of preparation results
    """
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(
                self.prepare_trial,
                trial.crs,
                trial.benchmark,
                trial.harness,
                trial.trial_num
            )
            for trial in trials
        ]

        results = [future.result() for future in futures]

    return results
```

## Testing Strategy

### Unit Tests

```python
def test_create_trial_directory(tmp_path):
    """Test trial directory creation."""
    preparer = TrialDirectoryPreparer(
        experiment_dir=tmp_path,
        benchmarks_root=tmp_path / "benchmarks",
        oss_fuzz_dir=tmp_path / "oss-fuzz",
        config={}
    )

    trial_dir = preparer._create_trial_directory("test-crs", "test-bench", 0)

    assert trial_dir.exists()
    assert trial_dir.name == "trial-0"
    assert trial_dir.parent == tmp_path


def test_prepare_source_code(tmp_path):
    """Test source code preparation."""
    # Create mock benchmark with meta.yaml and project.yaml
    benchmark_dir = tmp_path / "benchmarks" / "test-bench"
    create_mock_benchmark(benchmark_dir)

    preparer = TrialDirectoryPreparer(
        experiment_dir=tmp_path,
        benchmarks_root=tmp_path / "benchmarks",
        oss_fuzz_dir=tmp_path / "oss-fuzz",
        config={}
    )

    build_dir = tmp_path / "trial-0" / "build"
    build_dir.mkdir(parents=True)

    source_path = preparer._prepare_source_code("test-bench", build_dir)

    assert source_path.exists()
    assert (source_path / ".git").exists()
    assert source_path.name == "test-bench"


def test_prepare_hints(tmp_path):
    """Test hints preparation."""
    # Create mock benchmark with hints
    benchmark_dir = tmp_path / "benchmarks" / "test-bench"
    create_mock_benchmark_with_hints(benchmark_dir, "test_harness")

    preparer = TrialDirectoryPreparer(
        experiment_dir=tmp_path,
        benchmarks_root=tmp_path / "benchmarks",
        oss_fuzz_dir=tmp_path / "oss-fuzz",
        config={"hints_enabled": True, "hints_corpus_level": "1h"}
    )

    trial_dir = tmp_path / "trial-0"
    trial_dir.mkdir()

    hints_dir = preparer._prepare_hints("test-bench", "test_harness", trial_dir)

    assert hints_dir is not None
    assert hints_dir.exists()
    assert (hints_dir / "sarif").exists()
    assert (hints_dir / "corpus").exists()
    assert len(list((hints_dir / "sarif").glob("*.sarif"))) > 0


def test_prepare_povs(tmp_path):
    """Test POVs preparation."""
    # Create mock benchmark with POVs
    benchmark_dir = tmp_path / "benchmarks" / "test-bench"
    create_mock_benchmark_with_povs(benchmark_dir, "test_harness")

    preparer = TrialDirectoryPreparer(
        experiment_dir=tmp_path,
        benchmarks_root=tmp_path / "benchmarks",
        oss_fuzz_dir=tmp_path / "oss-fuzz",
        config={}
    )

    trial_dir = tmp_path / "trial-0"
    trial_dir.mkdir()

    povs_dir = preparer._prepare_povs("test-bench", "test_harness", trial_dir)

    assert povs_dir is not None
    assert povs_dir.exists()
    assert len(list(povs_dir.iterdir())) > 0
```

### Integration Tests

```python
def test_prepare_trial_complete(tmp_path):
    """Test complete trial preparation."""
    # Setup mock environment
    benchmark_dir = tmp_path / "benchmarks" / "test-bench"
    create_complete_mock_benchmark(benchmark_dir)

    preparer = TrialDirectoryPreparer(
        experiment_dir=tmp_path / "experiment",
        benchmarks_root=tmp_path / "benchmarks",
        oss_fuzz_dir=tmp_path / "oss-fuzz",
        config={
            "hints_enabled": True,
            "hints_corpus_level": "1h"
        }
    )

    # Prepare trial
    result = preparer.prepare_trial(
        crs="test-crs",
        benchmark="test-bench",
        harness="test_harness",
        trial_num=0,
        mode="patch_generation"
    )

    # Verify result
    assert result.success
    assert result.trial_dir.exists()
    assert result.build_dir.exists()
    assert result.source_path.exists()
    assert result.output_dir.exists()
    assert result.hints_dir.exists()
    assert result.povs_dir.exists()
    assert (result.trial_dir / "metadata.json").exists()
```

## Future Enhancements

### 1. Incremental Preparation

Only prepare changed components when re-running trials:

```python
def prepare_trial_incremental(
    self,
    trial_dir: Path,
    force_source: bool = False,
    force_hints: bool = False,
    force_povs: bool = False
) -> TrialPreparationResult:
    """
    Incrementally prepare trial, reusing existing artifacts.

    Args:
        trial_dir: Existing trial directory
        force_source: Force re-clone source
        force_hints: Force re-prepare hints
        force_povs: Force re-prepare POVs
    """
    # Check what already exists and skip if not forced
    # Useful for resuming failed trials
```

### 2. Template-Based Preparation

Use trial templates for common configurations:

```python
def prepare_from_template(
    self,
    template_name: str,
    overrides: Dict[str, Any]
) -> TrialPreparationResult:
    """Prepare trial from predefined template."""
    # Load template configuration
    # Apply overrides
    # Prepare trial
```

### 3. Cleanup Strategy

Implement smart cleanup for old trials:

```python
def cleanup_trial(
    self,
    trial_dir: Path,
    keep_outputs: bool = True,
    keep_sources: bool = False
) -> None:
    """
    Clean up trial directory to save space.

    Args:
        trial_dir: Trial directory to clean
        keep_outputs: Keep output directory
        keep_sources: Keep source code
    """
    # Remove build artifacts but keep outputs for analysis
    # Optionally compress old trials
```

## References

- [OSS-CRS Integration](./oss-crs-integration.md): oss-crs CLI parameter mappings
- [Repository Manager](../migration/repo-manager.md): Source code management
- [CRS Executors](./crs-executors.md): Executor implementation using prepared directories
- [Orchestration](../orchestration.md): Experiment orchestration workflow
