# Snapshot Implementation Design

This document describes the design and implementation of periodic snapshot functionality for CRSBench, enabling progress monitoring during long-running CRS trials.

## Table of Contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [Configuration](#configuration)
4. [Threading Model](#threading-model)
5. [Snapshot Capture Process](#snapshot-capture-process)
6. [Data Structures](#data-structures)
7. [Storage Structure](#storage-structure)
8. [Implementation Details](#implementation-details)
9. [Integration Points](#integration-points)
10. [Testing Strategy](#testing-strategy)
11. [Performance Considerations](#performance-considerations)
12. [Comparison with FuzzBench](#comparison-with-fuzzbench)
13. [Implementation Checklist](#implementation-checklist)
14. [Future Extensions](#future-extensions)

## Overview

### Purpose

Snapshots provide **progress monitoring** for long-running CRS trials by periodically capturing intermediate results. This enables:

- **Progress tracking**: Monitor CRS discoveries over time
- **Early debugging**: Detect issues before trial completion
- **Resource tracking**: Observe LLM token usage patterns
- **Partial results**: Recover data from interrupted trials
- **Time-series analysis**: Understand CRS behavior dynamics

### Scope

**What snapshots capture:**
- POVs discovered so far
- Patches generated so far
- LLM usage metrics (tokens, costs, API calls)
- CRS log excerpts

**What snapshots do NOT do:**
- **No verification**: POVs/patches are not tested during snapshot
- **No scoring**: No difficulty calculation or evaluation metrics
- **No reporting**: Snapshots are raw data only

Verification and scoring happen **only at trial end**, keeping snapshots fast and non-intrusive.

### Design Goals

1. **Minimal overhead**: Snapshots should not significantly slow CRS execution
2. **Filesystem-only**: No database dependencies, simple JSON files
3. **Extensible**: Easy to add new snapshot data types
4. **Thread-safe**: Safe concurrent access between CRS and snapshot threads
5. **Simple storage**: Human-readable, self-contained snapshot directories

## Architecture

### High-Level Design

```
BenchmarkRunner
├── Main thread (snapshot manager)
│   └── Periodic polling loop:
│       ├── Sleep for snapshot_period seconds
│       └── Capture snapshot:
│           ├── Read CRS output files
│           ├── Collect LLM metrics
│           ├── Copy logs
│           └── Write snapshot directory
│
└── Worker thread (CRS execution)
    └── Run CRS subprocess:
        ├── Writes POVs to output directory
        ├── Writes patches to output directory
        └── Accumulates LLM usage
```

**Key characteristics:**
- **Location**: Trial runner (`crsbench/evaluation/`)
- **Threading**: Main + worker thread (no async/await)
- **Storage**: Local filesystem in `experiment_filestore`
- **Timing**: Fixed interval polling (default 900s / 15 minutes)

### Comparison with FuzzBench

| Aspect | FuzzBench | CRSBench |
|--------|-----------|----------|
| Threading | Main + worker thread | **Same** |
| Timing | Sleep-based polling | **Same** |
| Storage | Google Cloud Storage | **Filesystem only** |
| Measurement | Separate measurer process | **No separate process** |
| Verification | Async coverage analysis | **No verification** |
| Database | PostgreSQL | **None** |
| Data format | Tar.gz archives | **JSON + diffs** |

**What we adopt:**
- Simple main + worker thread pattern
- Sleep-based timing mechanism
- Incremental capture (only new data)

**What we adapt:**
- Local filesystem instead of cloud storage
- CRS-specific data (POVs, patches, LLM metrics)
- No separate measurement process

**What we skip:**
- Database storage
- Async verification
- Cloud infrastructure

## Configuration

### ExperimentConfig Schema

Add new field to `crsbench/validation/schemas.py`:

```python
class ExperimentConfig(BaseModel):
    """Experiment configuration schema."""

    # ... existing fields ...

    snapshot_period: int = Field(
        default=900,
        ge=60,
        description="Snapshot interval in seconds (minimum 60, default 900)"
    )

    @validator('snapshot_period')
    def validate_snapshot_period(cls, v):
        """Validate snapshot period is reasonable."""
        if v < 60:
            raise ValueError("snapshot_period must be at least 60 seconds")
        if v > 86400:  # 24 hours
            logger.warning(f"snapshot_period of {v}s (>{v/3600:.1f} hours) is unusually long")
        return v
```

### Example Configuration

```yaml
# experiment-config.yaml
experiment: "test-experiment"
trials: 3
max_total_time: 7200  # 2 hours
snapshot_period: 600   # Take snapshot every 10 minutes
difficulty_level: 1
experiment_filestore: /tmp/experiments
report_filestore: /tmp/reports
crses:
  - atlantis-c
benchmark_suite: crsbench-afc-c
```

### Configuration Propagation

```
experiment-config.yaml
    ↓ (loaded by orchestrator)
ExperimentConfig object
    ↓ (passed to trial execution)
BenchmarkRunner.__init__(snapshot_period=...)
    ↓ (used by snapshot manager)
SnapshotManager (creates snapshots every N seconds)
```

## Threading Model

### Overview

Adopt FuzzBench's simple and reliable threading pattern:

```python
class BenchmarkRunner:
    def run_benchmark(self, ...):
        # Create snapshot manager
        snapshot_manager = SnapshotManager(
            trial_dir=trial_output_dir,
            snapshot_period=self.snapshot_period
        )

        # Start snapshot thread
        snapshot_thread = threading.Thread(
            target=snapshot_manager.run,
            daemon=True
        )
        snapshot_thread.start()

        # Main thread: Run CRS
        try:
            result = self._run_crs_evaluation(...)
        finally:
            # Stop snapshot thread
            snapshot_manager.stop()
            snapshot_thread.join(timeout=5.0)

        return result
```

### Thread Responsibilities

**Main thread (CRS execution):**
- Runs CRS subprocess
- Writes POV files to output directory
- Writes patch files to output directory
- Accumulates LLM usage in shared structure
- No snapshot logic

**Snapshot thread (monitoring):**
- Sleeps for `snapshot_period` seconds
- Wakes up and captures snapshot
- Reads files written by CRS
- Writes snapshot directory
- Loops until stopped

**No locks needed:**
- CRS writes files, snapshot thread reads
- Filesystem provides natural synchronization
- No shared mutable state

### Timing Mechanism

Adapted from FuzzBench's `sleep_until_next_sync()`:

```python
class SnapshotManager:
    def __init__(self, trial_dir: Path, snapshot_period: int):
        self.trial_dir = trial_dir
        self.snapshot_period = snapshot_period
        self.last_snapshot_time: Optional[float] = None
        self.cycle = 0
        self.running = False

    def run(self):
        """Main snapshot loop (runs in separate thread)."""
        self.running = True
        logger.info(f"Snapshot thread started (period={self.snapshot_period}s)")

        while self.running:
            self.sleep_until_next_snapshot()

            if not self.running:
                break

            self.cycle += 1
            logger.info(f"Capturing snapshot {self.cycle}")

            try:
                self.capture_snapshot()
            except Exception as e:
                logger.error(f"Snapshot {self.cycle} failed: {e}")
                # Continue to next snapshot

        logger.info("Snapshot thread stopped")

    def sleep_until_next_snapshot(self):
        """Sleep until it's time for the next snapshot."""
        if self.last_snapshot_time is not None:
            next_snapshot_time = self.last_snapshot_time + self.snapshot_period
            sleep_time = next_snapshot_time - time.time()
        else:
            sleep_time = self.snapshot_period

        # Sleep in small increments to allow quick shutdown
        while sleep_time > 0 and self.running:
            time.sleep(min(sleep_time, 1.0))
            sleep_time -= 1.0

        self.last_snapshot_time = time.time()

    def stop(self):
        """Stop the snapshot loop."""
        self.running = False
```

**Design decisions:**
- **Fixed intervals**: Simple and predictable
- **Daemon thread**: Automatically terminates with main thread
- **Incremental sleep**: Check `self.running` every second for quick shutdown
- **Error handling**: Log and continue on snapshot failures

## Snapshot Capture Process

### Capture Flow

```
1. Create snapshot directory
   └─ {trial_dir}/snapshot-{cycle:04d}/

2. Capture metadata
   └─ Write metadata.json (timestamp, cycle, elapsed_time)

3. Capture POVs (if any new)
   └─ Read {trial_dir}/povs/
   └─ Write povs.json

4. Capture patches (if any new)
   └─ Read {trial_dir}/patches/
   └─ Copy to snapshot-{cycle}/patches/

5. Capture LLM metrics
   └─ Read LLM usage tracker
   └─ Write llm-usage.json

6. Capture CRS logs
   └─ Read {trial_dir}/crs-output.log
   └─ Write crs-log-tail.txt (last 100 lines)

7. Mark snapshot complete
   └─ Write .complete marker file
```

### Incremental Capture

Only capture **new data since last snapshot**:

```python
def capture_snapshot(self):
    """Capture a single snapshot."""
    snapshot_dir = self.trial_dir / f"snapshot-{self.cycle:04d}"
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    # 1. Metadata
    metadata = SnapshotMetadata(
        cycle=self.cycle,
        timestamp=time.time(),
        elapsed_time=time.time() - self.start_time,
        snapshot_period=self.snapshot_period
    )
    self._write_json(snapshot_dir / "metadata.json", metadata.to_dict())

    # 2. POVs (incremental)
    new_povs = self._get_new_povs()
    if new_povs:
        self._write_json(snapshot_dir / "povs.json", new_povs)

    # 3. Patches (incremental)
    new_patches = self._get_new_patches()
    if new_patches:
        self._copy_patches(new_patches, snapshot_dir / "patches")

    # 4. LLM metrics (cumulative)
    llm_usage = self._get_llm_usage()
    self._write_json(snapshot_dir / "llm-usage.json", llm_usage)

    # 5. CRS log tail
    log_tail = self._get_log_tail(lines=100)
    self._write_text(snapshot_dir / "crs-log-tail.txt", log_tail)

    # 6. Mark complete
    (snapshot_dir / ".complete").touch()

    logger.info(f"Snapshot {self.cycle} captured: {len(new_povs)} POVs, "
                f"{len(new_patches)} patches")
```

### Tracking State

Track what has been captured to enable incremental snapshots:

```python
class SnapshotManager:
    def __init__(self, ...):
        # ... other fields ...
        self.captured_pov_ids: Set[str] = set()
        self.captured_patch_ids: Set[str] = set()
        self.last_log_position: int = 0

    def _get_new_povs(self) -> List[Dict[str, Any]]:
        """Get POVs discovered since last snapshot."""
        pov_dir = self.trial_dir / "povs"
        if not pov_dir.exists():
            return []

        new_povs = []
        for pov_file in pov_dir.glob("*.json"):
            pov_id = pov_file.stem
            if pov_id not in self.captured_pov_ids:
                pov_data = self._read_json(pov_file)
                new_povs.append(pov_data)
                self.captured_pov_ids.add(pov_id)

        return new_povs

    def _get_new_patches(self) -> List[Path]:
        """Get patches generated since last snapshot."""
        patch_dir = self.trial_dir / "patches"
        if not patch_dir.exists():
            return []

        new_patches = []
        for patch_file in patch_dir.glob("*.diff"):
            patch_id = patch_file.stem
            if patch_id not in self.captured_patch_ids:
                new_patches.append(patch_file)
                self.captured_patch_ids.add(patch_id)

        return new_patches
```

## Data Structures

### SnapshotMetadata

```python
from dataclasses import dataclass, asdict
from typing import Optional

@dataclass
class SnapshotMetadata:
    """Metadata for a single snapshot."""

    cycle: int                      # Snapshot number (1, 2, 3, ...)
    timestamp: float                # Wall-clock time (Unix timestamp)
    elapsed_time: float             # Seconds since trial start
    snapshot_period: int            # Configured snapshot period

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> 'SnapshotMetadata':
        """Load from dictionary."""
        return cls(**data)
```

### POV Data Format

**File:** `snapshot-{cycle}/povs.json`

```json
[
  {
    "pov_id": "pov_001",
    "harness_name": "parse_harness",
    "discovered_at": 1234567890.5,
    "sanitizer": "address",
    "error_token": "heap-buffer-overflow",
    "crash_input": "base64_encoded_input...",
    "discovery_method": "fuzzing"
  },
  {
    "pov_id": "pov_002",
    "harness_name": "parse_harness",
    "discovered_at": 1234567950.2,
    "sanitizer": "address",
    "error_token": "use-after-free",
    "crash_input": "base64_encoded_input...",
    "discovery_method": "static_analysis"
  }
]
```

### Patch Data Format

**File:** `snapshot-{cycle}/patches/patch-{id}.diff`

Standard unified diff format:

```diff
--- a/src/parser.c
+++ b/src/parser.c
@@ -45,7 +45,7 @@
 void parse_input(char *input, size_t len) {
-    char buffer[256];
+    char buffer[512];
     memcpy(buffer, input, len);
 }
```

**File:** `snapshot-{cycle}/patches/metadata.json`

```json
[
  {
    "patch_id": "patch_001",
    "generated_at": 1234567920.0,
    "target_files": ["src/parser.c"],
    "description": "Increase buffer size to prevent overflow",
    "confidence": 0.85
  }
]
```

### LLM Usage Data Format

**File:** `snapshot-{cycle}/llm-usage.json`

```json
{
  "total_api_calls": 150,
  "total_input_tokens": 45000,
  "total_output_tokens": 12000,
  "total_cached_tokens": 20000,
  "total_cost_usd": 1.25,
  "by_model": {
    "claude-sonnet-4": {
      "calls": 100,
      "input_tokens": 30000,
      "output_tokens": 8000,
      "cost_usd": 0.95
    },
    "gpt-4": {
      "calls": 50,
      "input_tokens": 15000,
      "output_tokens": 4000,
      "cost_usd": 0.30
    }
  },
  "by_operation": {
    "fuzzing": {"calls": 80, "tokens": 35000},
    "static_analysis": {"calls": 40, "tokens": 15000},
    "patch_generation": {"calls": 30, "tokens": 7000}
  }
}
```

### CRS Log Format

**File:** `snapshot-{cycle}/crs-log-tail.txt`

Plain text, last N lines of CRS output:

```
[2025-01-15 10:45:23] INFO: Starting fuzzing campaign
[2025-01-15 10:45:30] INFO: Generated 1000 test cases
[2025-01-15 10:46:15] INFO: Found crash: heap-buffer-overflow
[2025-01-15 10:47:00] INFO: Analyzing crash with LLM
[2025-01-15 10:47:45] INFO: Generated POV candidate pov_001
...
(last 100 lines)
```

## Storage Structure

### Directory Layout

```
experiment_filestore/
└── {experiment_name}/
    └── {benchmark_id}__{crs_name}/
        └── trial-{trial_id}/
            ├── snapshot-0001/
            │   ├── .complete                 # Marker: snapshot finished
            │   ├── metadata.json             # Snapshot metadata
            │   ├── povs.json                 # POVs discovered (incremental)
            │   ├── patches/                  # Patches generated (incremental)
            │   │   ├── patch-001.diff
            │   │   ├── patch-002.diff
            │   │   └── metadata.json
            │   ├── llm-usage.json            # LLM metrics (cumulative)
            │   └── crs-log-tail.txt          # CRS log excerpt
            ├── snapshot-0002/
            │   ├── .complete
            │   ├── metadata.json
            │   ├── povs.json
            │   ├── patches/
            │   ├── llm-usage.json
            │   └── crs-log-tail.txt
            ├── snapshot-0003/
            │   └── ...
            ├── povs/                         # Full POV directory (source)
            │   ├── pov_001.json
            │   ├── pov_002.json
            │   └── ...
            ├── patches/                      # Full patch directory (source)
            │   ├── patch-001.diff
            │   ├── patch-002.diff
            │   └── ...
            ├── crs-output.log               # Full CRS output (source)
            └── final-report.json            # Trial result (after completion)
```

### File Naming Conventions

- **Snapshot directories**: `snapshot-{cycle:04d}` (e.g., `snapshot-0001`, `snapshot-0023`)
- **POV files**: `pov_{id}.json` where `id` is sequential or hash-based
- **Patch files**: `patch-{id}.diff` where `id` is sequential or hash-based
- **Completion marker**: `.complete` (empty file, presence indicates complete snapshot)

### Snapshot Validation

Check if snapshot is complete and valid:

```python
def is_snapshot_complete(snapshot_dir: Path) -> bool:
    """Check if snapshot completed successfully."""
    complete_marker = snapshot_dir / ".complete"
    metadata_file = snapshot_dir / "metadata.json"

    return complete_marker.exists() and metadata_file.exists()

def validate_snapshot(snapshot_dir: Path) -> bool:
    """Validate snapshot integrity."""
    if not is_snapshot_complete(snapshot_dir):
        return False

    # Check required files
    required_files = [
        "metadata.json",
        "llm-usage.json",
        "crs-log-tail.txt"
    ]

    for filename in required_files:
        if not (snapshot_dir / filename).exists():
            logger.warning(f"Missing required file: {filename}")
            return False

    # Validate JSON files
    try:
        metadata = SnapshotMetadata.from_dict(
            json.loads((snapshot_dir / "metadata.json").read_text())
        )
    except Exception as e:
        logger.error(f"Invalid metadata.json: {e}")
        return False

    return True
```

## Implementation Details

### SnapshotManager Class

**File:** `crsbench/evaluation/snapshot_manager.py` (new file)

```python
"""Snapshot manager for periodic trial progress capture."""

import time
import json
import logging
import threading
from pathlib import Path
from typing import Optional, List, Dict, Any, Set

logger = logging.getLogger(__name__)


class SnapshotManager:
    """Manages periodic snapshots of trial progress.

    This class runs in a separate thread and periodically captures
    trial state without interfering with CRS execution.
    """

    def __init__(
        self,
        trial_dir: Path,
        snapshot_period: int = 900,
        start_time: Optional[float] = None
    ):
        """Initialize snapshot manager.

        Args:
            trial_dir: Directory for trial output and snapshots
            snapshot_period: Seconds between snapshots (default 900 / 15 min)
            start_time: Trial start time (Unix timestamp), defaults to now
        """
        self.trial_dir = Path(trial_dir)
        self.snapshot_period = snapshot_period
        self.start_time = start_time or time.time()

        # State tracking
        self.cycle = 0
        self.running = False
        self.last_snapshot_time: Optional[float] = None

        # Incremental capture tracking
        self.captured_pov_ids: Set[str] = set()
        self.captured_patch_ids: Set[str] = set()
        self.last_log_position: int = 0

        logger.info(f"SnapshotManager initialized: period={snapshot_period}s")

    def run(self):
        """Main snapshot loop (runs in separate thread)."""
        self.running = True
        logger.info("Snapshot thread started")

        while self.running:
            self.sleep_until_next_snapshot()

            if not self.running:
                break

            self.cycle += 1
            logger.info(f"Capturing snapshot {self.cycle}")

            try:
                self.capture_snapshot()
            except Exception as e:
                logger.error(f"Snapshot {self.cycle} failed: {e}", exc_info=True)
                # Continue to next snapshot

        logger.info("Snapshot thread stopped")

    def stop(self):
        """Stop the snapshot loop."""
        logger.info("Stopping snapshot thread...")
        self.running = False

    def sleep_until_next_snapshot(self):
        """Sleep until it's time for the next snapshot."""
        if self.last_snapshot_time is not None:
            next_snapshot_time = self.last_snapshot_time + self.snapshot_period
            sleep_time = next_snapshot_time - time.time()
        else:
            sleep_time = self.snapshot_period

        # Sleep in 1-second increments to allow quick shutdown
        while sleep_time > 0 and self.running:
            time.sleep(min(sleep_time, 1.0))
            sleep_time -= 1.0

        self.last_snapshot_time = time.time()

    def capture_snapshot(self):
        """Capture a single snapshot."""
        snapshot_dir = self.trial_dir / f"snapshot-{self.cycle:04d}"
        snapshot_dir.mkdir(parents=True, exist_ok=True)

        # 1. Metadata
        metadata = {
            "cycle": self.cycle,
            "timestamp": time.time(),
            "elapsed_time": time.time() - self.start_time,
            "snapshot_period": self.snapshot_period
        }
        self._write_json(snapshot_dir / "metadata.json", metadata)

        # 2. POVs (incremental)
        new_povs = self._get_new_povs()
        if new_povs:
            self._write_json(snapshot_dir / "povs.json", new_povs)
            logger.debug(f"Captured {len(new_povs)} new POVs")

        # 3. Patches (incremental)
        new_patches = self._get_new_patches()
        if new_patches:
            self._copy_patches(new_patches, snapshot_dir / "patches")
            logger.debug(f"Captured {len(new_patches)} new patches")

        # 4. LLM metrics (cumulative)
        llm_usage = self._get_llm_usage()
        self._write_json(snapshot_dir / "llm-usage.json", llm_usage)

        # 5. CRS log tail
        log_tail = self._get_log_tail(lines=100)
        if log_tail:
            self._write_text(snapshot_dir / "crs-log-tail.txt", log_tail)

        # 6. Mark complete
        (snapshot_dir / ".complete").touch()

        logger.info(
            f"Snapshot {self.cycle} complete: "
            f"{len(new_povs)} POVs, {len(new_patches)} patches"
        )

    # === Incremental capture methods ===

    def _get_new_povs(self) -> List[Dict[str, Any]]:
        """Get POVs discovered since last snapshot."""
        pov_dir = self.trial_dir / "povs"
        if not pov_dir.exists():
            return []

        new_povs = []
        for pov_file in pov_dir.glob("*.json"):
            pov_id = pov_file.stem
            if pov_id not in self.captured_pov_ids:
                try:
                    pov_data = json.loads(pov_file.read_text())
                    new_povs.append(pov_data)
                    self.captured_pov_ids.add(pov_id)
                except Exception as e:
                    logger.warning(f"Failed to read POV {pov_id}: {e}")

        return new_povs

    def _get_new_patches(self) -> List[Path]:
        """Get patches generated since last snapshot."""
        patch_dir = self.trial_dir / "patches"
        if not patch_dir.exists():
            return []

        new_patches = []
        for patch_file in patch_dir.glob("*.diff"):
            patch_id = patch_file.stem
            if patch_id not in self.captured_patch_ids:
                new_patches.append(patch_file)
                self.captured_patch_ids.add(patch_id)

        return new_patches

    def _get_llm_usage(self) -> Dict[str, Any]:
        """Get cumulative LLM usage metrics."""
        llm_usage_file = self.trial_dir / "llm-usage.json"

        if not llm_usage_file.exists():
            return {
                "total_api_calls": 0,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_cost_usd": 0.0
            }

        try:
            return json.loads(llm_usage_file.read_text())
        except Exception as e:
            logger.warning(f"Failed to read LLM usage: {e}")
            return {}

    def _get_log_tail(self, lines: int = 100) -> str:
        """Get last N lines of CRS log."""
        log_file = self.trial_dir / "crs-output.log"

        if not log_file.exists():
            return ""

        try:
            with open(log_file, 'r') as f:
                # Read all lines and take last N
                all_lines = f.readlines()
                tail_lines = all_lines[-lines:]
                return ''.join(tail_lines)
        except Exception as e:
            logger.warning(f"Failed to read log tail: {e}")
            return ""

    # === Helper methods ===

    def _write_json(self, path: Path, data: Any):
        """Write data as JSON."""
        path.write_text(json.dumps(data, indent=2))

    def _write_text(self, path: Path, text: str):
        """Write text file."""
        path.write_text(text)

    def _copy_patches(self, patch_files: List[Path], dest_dir: Path):
        """Copy patch files to snapshot directory."""
        import shutil

        dest_dir.mkdir(parents=True, exist_ok=True)

        for patch_file in patch_files:
            dest_file = dest_dir / patch_file.name
            shutil.copy2(patch_file, dest_file)
```

### Integration with BenchmarkRunner

**File:** `crsbench/evaluation/runner.py` (modifications)

```python
class BenchmarkRunner:
    def __init__(
        self,
        crs_executor: Optional[CRSExecutor] = None,
        snapshot_period: Optional[int] = None  # New parameter
    ):
        """Initialize benchmark runner.

        Args:
            crs_executor: CRS executor instance
            snapshot_period: Seconds between snapshots (None = no snapshots)
        """
        self.crs_executor = crs_executor or StubCRSExecutor()
        self.snapshot_period = snapshot_period
        self.logger = logging.getLogger(__name__)

    def run_benchmark(
        self,
        benchmark_path: Union[str, Path],
        trial_output_dir: Optional[Path] = None,  # New parameter
        mode: Optional[str] = None,
        crs_config: Optional[Dict[str, Any]] = None
    ) -> EvaluationResult:
        """Run benchmark evaluation with optional snapshots.

        Args:
            benchmark_path: Path to benchmark
            trial_output_dir: Directory for trial outputs and snapshots
            mode: Evaluation mode
            crs_config: CRS configuration

        Returns:
            EvaluationResult
        """
        # ... existing validation and setup ...

        # Start snapshot manager if configured
        snapshot_manager = None
        snapshot_thread = None

        if self.snapshot_period and trial_output_dir:
            from crsbench.evaluation.snapshot_manager import SnapshotManager

            snapshot_manager = SnapshotManager(
                trial_dir=trial_output_dir,
                snapshot_period=self.snapshot_period,
                start_time=time.time()
            )

            snapshot_thread = threading.Thread(
                target=snapshot_manager.run,
                daemon=True
            )
            snapshot_thread.start()
            self.logger.info("Snapshot thread started")

        try:
            # Run CRS evaluation (existing code)
            # ... existing evaluation logic ...

            return result

        finally:
            # Stop snapshot thread
            if snapshot_manager:
                snapshot_manager.stop()
                if snapshot_thread:
                    snapshot_thread.join(timeout=5.0)
                    self.logger.info("Snapshot thread stopped")
```

### Integration with Orchestrator

**File:** `crsbench/run_experiment.py` (modifications)

Pass `snapshot_period` from experiment config to trial runner:

```python
def run_experiment_local(
    experiment_name: str,
    config: ExperimentConfig,
    benchmarks: List[str],
    crses: List[str]
) -> None:
    """Run experiment locally."""

    for benchmark_id in benchmarks:
        for crs_name in crses:
            for trial_num in range(1, config.trials + 1):
                # Create trial output directory
                trial_output_dir = (
                    Path(config.experiment_filestore) /
                    experiment_name /
                    f"{benchmark_id}__{crs_name}" /
                    f"trial-{trial_num}"
                )
                trial_output_dir.mkdir(parents=True, exist_ok=True)

                # Create runner with snapshot support
                runner = BenchmarkRunner(
                    crs_executor=create_crs_executor(crs_name),
                    snapshot_period=config.snapshot_period  # Pass from config
                )

                # Run benchmark
                result = runner.run_benchmark(
                    benchmark_path=benchmark_path,
                    trial_output_dir=trial_output_dir,  # Pass output dir
                    mode="auto",
                    crs_config=crs_config
                )
```

## Integration Points

### With Evaluation Module

**File:** `crsbench/evaluation/__init__.py`

Export snapshot manager:

```python
from crsbench.evaluation.snapshot_manager import SnapshotManager

__all__ = [
    # ... existing exports ...
    'SnapshotManager',
]
```

### With Validation Module

**File:** `crsbench/validation/schemas.py`

Add `snapshot_period` field (shown in [Configuration](#configuration) section).

### With Distributed Module

For distributed execution, snapshot manager runs on each worker:

```python
def run_trial_job(trial_config):
    """Run a single trial (executed on worker)."""

    runner = BenchmarkRunner(
        crs_executor=create_crs_executor(trial_config.crs),
        snapshot_period=trial_config.snapshot_period  # From job config
    )

    result = runner.run_benchmark(
        benchmark_path=trial_config.benchmark_path,
        trial_output_dir=trial_config.output_dir,
        mode=trial_config.mode
    )

    return result
```

### With Reporting Module (Future)

Snapshots enable time-series visualization:

```python
def generate_snapshot_report(trial_dir: Path) -> Dict[str, Any]:
    """Generate report from trial snapshots."""

    snapshots = []
    for snapshot_dir in sorted(trial_dir.glob("snapshot-*")):
        if not is_snapshot_complete(snapshot_dir):
            continue

        metadata = json.loads((snapshot_dir / "metadata.json").read_text())
        povs = json.loads((snapshot_dir / "povs.json").read_text()) if (snapshot_dir / "povs.json").exists() else []
        llm_usage = json.loads((snapshot_dir / "llm-usage.json").read_text())

        snapshots.append({
            "cycle": metadata["cycle"],
            "elapsed_time": metadata["elapsed_time"],
            "pov_count": len(povs),
            "llm_tokens": llm_usage.get("total_input_tokens", 0) + llm_usage.get("total_output_tokens", 0),
            "llm_cost": llm_usage.get("total_cost_usd", 0.0)
        })

    return {
        "snapshots": snapshots,
        "total_snapshots": len(snapshots),
        "final_pov_count": snapshots[-1]["pov_count"] if snapshots else 0
    }
```

## Testing Strategy

### Test File

**File:** `tests/test_snapshot_manager.py` (new file)

### Test Categories

#### 1. SnapshotManager Unit Tests

```python
def test_snapshot_manager_initialization():
    """Test SnapshotManager initialization."""
    manager = SnapshotManager(
        trial_dir=Path("/tmp/trial"),
        snapshot_period=60
    )
    assert manager.snapshot_period == 60
    assert manager.cycle == 0
    assert not manager.running

def test_snapshot_timing():
    """Test snapshot timing mechanism."""
    manager = SnapshotManager(
        trial_dir=Path("/tmp/trial"),
        snapshot_period=5  # 5 seconds for testing
    )

    start_time = time.time()
    manager.last_snapshot_time = start_time

    # Should sleep ~5 seconds
    manager.sleep_until_next_snapshot()

    elapsed = time.time() - start_time
    assert 4.5 <= elapsed <= 5.5  # Allow 0.5s tolerance

def test_incremental_pov_capture():
    """Test incremental POV capture."""
    with tempfile.TemporaryDirectory() as tmpdir:
        trial_dir = Path(tmpdir)
        pov_dir = trial_dir / "povs"
        pov_dir.mkdir()

        manager = SnapshotManager(trial_dir, snapshot_period=60)

        # Create first POV
        (pov_dir / "pov_001.json").write_text('{"id": "pov_001"}')

        new_povs = manager._get_new_povs()
        assert len(new_povs) == 1
        assert new_povs[0]["id"] == "pov_001"

        # Second call should return empty (already captured)
        new_povs = manager._get_new_povs()
        assert len(new_povs) == 0

        # Create second POV
        (pov_dir / "pov_002.json").write_text('{"id": "pov_002"}')

        new_povs = manager._get_new_povs()
        assert len(new_povs) == 1
        assert new_povs[0]["id"] == "pov_002"
```

#### 2. Integration Tests

```python
def test_snapshot_capture_with_benchmark_runner():
    """Test snapshot integration with BenchmarkRunner."""
    with tempfile.TemporaryDirectory() as tmpdir:
        trial_dir = Path(tmpdir)

        runner = BenchmarkRunner(
            crs_executor=StubCRSExecutor(),
            snapshot_period=2  # 2 seconds for fast testing
        )

        # Run short evaluation
        result = runner.run_benchmark(
            benchmark_path=Path("test_benchmark"),
            trial_output_dir=trial_dir,
            mode="auto"
        )

        # Check snapshots were created
        snapshots = list(trial_dir.glob("snapshot-*"))
        assert len(snapshots) > 0

        # Validate first snapshot
        first_snapshot = sorted(snapshots)[0]
        assert (first_snapshot / ".complete").exists()
        assert (first_snapshot / "metadata.json").exists()

def test_snapshot_thread_cleanup():
    """Test snapshot thread stops cleanly."""
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = SnapshotManager(Path(tmpdir), snapshot_period=10)

        thread = threading.Thread(target=manager.run, daemon=True)
        thread.start()

        time.sleep(0.5)  # Let it start
        assert thread.is_alive()

        manager.stop()
        thread.join(timeout=2.0)

        assert not thread.is_alive()
```

#### 3. Validation Tests

```python
def test_snapshot_validation():
    """Test snapshot validation logic."""
    with tempfile.TemporaryDirectory() as tmpdir:
        snapshot_dir = Path(tmpdir) / "snapshot-0001"
        snapshot_dir.mkdir()

        # Incomplete snapshot
        assert not is_snapshot_complete(snapshot_dir)

        # Add metadata
        (snapshot_dir / "metadata.json").write_text('{}')
        assert not is_snapshot_complete(snapshot_dir)

        # Add completion marker
        (snapshot_dir / ".complete").touch()
        assert is_snapshot_complete(snapshot_dir)
```

### Running Tests

```bash
# Run snapshot tests
pytest tests/test_snapshot_manager.py -v

# Run with coverage
pytest tests/test_snapshot_manager.py --cov=crsbench.evaluation.snapshot_manager

# Run integration tests only
pytest tests/test_snapshot_manager.py -k integration -v
```

## Performance Considerations

### Overhead Analysis

**Snapshot thread overhead:**
- CPU: <1% (mostly sleeping)
- Memory: <10 MB (tracking sets and file handles)
- I/O: Depends on data volume (typically KB-MB per snapshot)

**CRS execution impact:**
- Minimal: Snapshot thread reads files, doesn't block CRS
- Filesystem provides natural buffering and caching
- No locks or synchronization needed

### Scalability

**Per-trial resource usage:**
- Disk: ~1-10 MB per snapshot (depends on POVs/patches)
- Example: 10 snapshots/hour × 2 MB/snapshot = 20 MB/hour/trial
- 24-hour trial with 15-min snapshots = 96 snapshots = ~200 MB

**Storage optimization:**
- Incremental capture reduces duplication
- Patches stored as diffs (not full files)
- Logs tail only (not full log)
- No compression needed (JSON is compressible if needed later)

### Timing Accuracy

**Expected timing:**
- Snapshot period: ±1 second accuracy
- Good enough for 15-minute (900s) intervals
- If higher precision needed, reduce sleep increment from 1.0s

**Jitter sources:**
- Snapshot capture duration (typically <1s)
- OS scheduling delays
- I/O wait time

## Comparison with FuzzBench

### What We Adopt

✅ **Threading model:**
- Main thread + worker thread pattern
- Daemon thread for background work
- Simple and reliable

✅ **Timing mechanism:**
- Sleep-based polling
- Fixed interval snapshots
- Predictable behavior

✅ **Incremental capture:**
- Only capture new data since last snapshot
- Track what has been captured
- Reduces overhead

### What We Adapt

🔄 **Storage:**
- FuzzBench: Google Cloud Storage (GCS)
- CRSBench: Local filesystem

🔄 **Data format:**
- FuzzBench: Tar.gz archives of corpus
- CRSBench: JSON + diff files

🔄 **Measurement:**
- FuzzBench: Separate measurer process with multiprocessing
- CRSBench: No separate measurement (no verification)

### What We Skip

❌ **Database storage:**
- FuzzBench: PostgreSQL with Snapshot table
- CRSBench: Filesystem only (simpler)

❌ **Async verification:**
- FuzzBench: Coverage analysis in parallel
- CRSBench: No verification during snapshots

❌ **Cloud infrastructure:**
- FuzzBench: GCE instances + GCS + dispatcher
- CRSBench: Local execution

## Implementation Checklist

### Phase 1: Core Implementation

- [ ] **Schema updates**
  - [ ] Add `snapshot_period` to `ExperimentConfig` in `crsbench/validation/schemas.py`
  - [ ] Add validator for snapshot period (60s minimum)
  - [ ] Update `docs/experiment-config-example.yaml` with snapshot_period

- [ ] **Create SnapshotManager**
  - [ ] Create `crsbench/evaluation/snapshot_manager.py`
  - [ ] Implement `SnapshotManager` class with threading
  - [ ] Implement timing mechanism (`sleep_until_next_snapshot`)
  - [ ] Implement capture logic (`capture_snapshot`)
  - [ ] Implement incremental tracking (POVs, patches)

- [ ] **Integrate with BenchmarkRunner**
  - [ ] Modify `BenchmarkRunner.__init__` to accept `snapshot_period`
  - [ ] Modify `run_benchmark` to accept `trial_output_dir`
  - [ ] Start/stop snapshot thread during benchmark execution
  - [ ] Update `crsbench/evaluation/__init__.py` exports

- [ ] **Integrate with Orchestrator**
  - [ ] Modify `run_experiment_local` to pass `snapshot_period`
  - [ ] Create trial output directories
  - [ ] Pass directories to `BenchmarkRunner.run_benchmark`
  - [ ] Modify `run_experiment_distributed` for workers

### Phase 2: Testing

- [ ] **Unit tests**
  - [ ] Create `tests/test_snapshot_manager.py`
  - [ ] Test SnapshotManager initialization
  - [ ] Test timing mechanism
  - [ ] Test incremental capture (POVs, patches)
  - [ ] Test LLM usage capture
  - [ ] Test log tail capture

- [ ] **Integration tests**
  - [ ] Test with BenchmarkRunner
  - [ ] Test thread lifecycle
  - [ ] Test snapshot directory structure
  - [ ] Test snapshot validation

- [ ] **Run tests**
  - [ ] `pytest tests/test_snapshot_manager.py -v`
  - [ ] Verify all tests pass

### Phase 3: Documentation

- [ ] **Update design docs**
  - [ ] Update `design-docs/evaluation/evaluation.md` to mention snapshots
  - [ ] Update `design-docs/architecture.md` if needed

- [ ] **Update user docs**
  - [ ] Document snapshot_period in experiment config docs
  - [ ] Add snapshot directory structure to docs
  - [ ] Create example showing how to inspect snapshots

- [ ] **Code documentation**
  - [ ] Add docstrings to all SnapshotManager methods
  - [ ] Add module docstring to snapshot_manager.py
  - [ ] Update BenchmarkRunner docstrings

### Phase 4: Validation

- [ ] **Manual testing**
  - [ ] Run actual CRS trial with snapshots enabled
  - [ ] Verify snapshots are created at correct intervals
  - [ ] Inspect snapshot contents for correctness
  - [ ] Verify no CRS performance degradation

- [ ] **Edge case testing**
  - [ ] Test with very short snapshot period (60s)
  - [ ] Test with very long trial (>24 hours)
  - [ ] Test CRS that produces many POVs rapidly
  - [ ] Test CRS failure during trial

## Future Extensions

### 1. Optional Verification Phase

Add opt-in verification during snapshots:

```python
class SnapshotManager:
    def __init__(self, ..., verify_povs: bool = False):
        self.verify_povs = verify_povs

    def capture_snapshot(self):
        # ... existing capture ...

        if self.verify_povs:
            verified_povs = self._verify_povs_immediate(new_povs)
            self._write_json(snapshot_dir / "verified-povs.json", verified_povs)
```

### 2. Snapshot-Based Reporting

Generate HTML reports from snapshots:

```python
def generate_snapshot_timeline_html(trial_dir: Path) -> str:
    """Generate HTML timeline visualization from snapshots."""
    # Chart: POVs discovered over time
    # Chart: LLM tokens over time
    # Chart: Cost over time
    # Table: Snapshot details
```

### 3. Resumable Trials

Enable trial resumption from last snapshot:

```python
def resume_trial(trial_dir: Path) -> EvaluationResult:
    """Resume trial from last complete snapshot."""
    last_snapshot = find_last_complete_snapshot(trial_dir)

    # Restore CRS state
    crs_state = load_crs_state(last_snapshot)

    # Continue from last snapshot
    runner = BenchmarkRunner(...)
    result = runner.resume_from_snapshot(crs_state)
```

### 4. Database Storage

Add optional SQLite database for querying:

```python
class SnapshotDatabase:
    """SQLite database for snapshot storage and querying."""

    def __init__(self, db_path: Path):
        self.conn = sqlite3.connect(db_path)
        self._create_tables()

    def store_snapshot(self, snapshot: Snapshot):
        """Store snapshot in database."""
        # INSERT INTO snapshots ...

    def query_snapshots(self, trial_id: str) -> List[Snapshot]:
        """Query snapshots for a trial."""
        # SELECT * FROM snapshots WHERE trial_id = ?
```

### 5. Snapshot Compression

Add optional compression for large snapshots:

```python
def capture_snapshot(self):
    """Capture and optionally compress snapshot."""
    # ... capture data ...

    if self.compress_snapshots:
        self._compress_snapshot(snapshot_dir)
```

### 6. Custom Snapshot Data

Allow CRS to register custom snapshot data collectors:

```python
class SnapshotDataCollector(ABC):
    """Interface for custom snapshot data collectors."""

    @abstractmethod
    def collect(self, trial_dir: Path) -> Dict[str, Any]:
        """Collect custom data for snapshot."""
        pass

class SnapshotManager:
    def __init__(self, ..., collectors: List[SnapshotDataCollector] = None):
        self.collectors = collectors or []

    def capture_snapshot(self):
        # ... existing capture ...

        # Custom collectors
        for collector in self.collectors:
            data = collector.collect(self.trial_dir)
            self._write_json(snapshot_dir / f"{collector.name}.json", data)
```

## References

- [FuzzBench Snapshot Analysis](../../docs_reference_projects/fuzzbench-snapshots.md): Detailed analysis of FuzzBench implementation
- [Evaluation Module Design](./evaluation.md): Evaluation module overview
- [Architecture](../architecture.md): Overall CRSBench architecture
- [Orchestration](../orchestration.md): Experiment orchestration design
