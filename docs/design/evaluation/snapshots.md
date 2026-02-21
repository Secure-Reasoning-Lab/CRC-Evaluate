# Snapshot Implementation Design

This document describes the design and implementation of periodic snapshot functionality for CRSBench, enabling progress monitoring during long-running CRS trials.

## Table of Contents

1. [Overview](#overview)
2. [Incremental vs Full Snapshot Strategy](#incremental-vs-full-snapshot-strategy)
3. [Architecture](#architecture)
4. [Configuration](#configuration)
5. [Threading Model](#threading-model)
6. [Snapshot Capture Process](#snapshot-capture-process)
7. [Data Structures](#data-structures)
8. [Storage Structure](#storage-structure)
9. [Implementation Details](#implementation-details)
10. [Integration Points](#integration-points)
11. [Testing Strategy](#testing-strategy)
12. [Performance Considerations](#performance-considerations)
13. [Comparison with FuzzBench](#comparison-with-fuzzbench)
14. [Implementation Checklist](#implementation-checklist)
15. [Future Extensions](#future-extensions)

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
- Corpus files (if CRS generates corpus)
- LLM usage metrics (tokens, costs, API calls)
- CRS logs (complete output)

**What snapshots do NOT do:**
- **No verification**: POVs/patches are not tested during snapshot
- **No scoring**: No difficulty calculation or evaluation metrics
- **No reporting**: Snapshots are raw data only

Verification and scoring happen **only at trial end**, keeping snapshots fast and non-intrusive.

### Design Goals

1. **Minimal overhead**: Snapshots should not significantly slow CRS execution
2. **Filesystem-only**: No database dependencies, compressed tar.gz archives
3. **Extensible**: Easy to add new snapshot data types
4. **Thread-safe**: Safe concurrent access between CRS and snapshot threads
5. **Efficient storage**: Compressed archives with incremental capture

## Incremental vs Full Snapshot Strategy

### Overview

CRSBench uses a **hybrid capture strategy** inspired by FuzzBench:
- **Incremental capture**: Only new data since last snapshot (saves storage)
- **Full capture**: Complete state at each snapshot (easier inspection)
- **Compression**: All snapshots compressed to tar.gz (reduces storage overhead)

### Capture Strategy by Data Type

| Data Type          | Strategy    | Tracking Method   | Deduplication     | Rationale                                                             |
|--------------------|-------------|-------------------|-------------------|-----------------------------------------------------------------------|
| **POVs**           | Incremental | Filename set      | During validation | New POVs tracked by filename; stored as-is; deduped later             |
| **Patches**        | Incremental | Filename set      | During validation | New patches tracked by filename; stored as-is; deduped later          |
| **Corpus**         | Incremental | Modification time | N/A               | New/modified corpus files tracked by mtime (like FuzzBench)           |
| **Config**         | Full        | N/A               | N/A               | Experiment config (config.yaml); static, copied once                  |
| **CRS data**       | Incremental | Modification time | N/A               | CRS-specific outputs tracked by mtime                                 |
| **Execution meta** | Full        | N/A               | N/A               | Execution metadata (execution.json); static, copied once              |
| **LLM logs**       | Full        | N/A               | N/A               | Complete llm-usage.json; simpler than computing JSON diffs            |
| **CRS logs**       | Full        | N/A               | N/A               | Complete crs-output.log; easier to inspect any snapshot independently |
| **Metadata**       | Full        | N/A               | N/A               | Always complete for that snapshot                                     |

### Rationale

**Why incremental for POVs/patches/corpus?**
- File-based data with clear identifiers (filenames)
- Easy to track what's new using sets (POVs/patches) or timestamps (corpus)
- Significant storage savings (only store each POV/patch once)
- Follows FuzzBench pattern for corpus

**Note on POV/patch deduplication:**
- Snapshots store POVs/patches **as-is** (no deduplication during capture)
- CRS may generate duplicate POVs (same vulnerability, different inputs)
- CRS may generate duplicate patches (same fix, different variations)
- **Deduplication happens during validation/evaluation phase**
- This keeps snapshot capture fast and simple
- Allows post-hoc analysis of CRS behavior (e.g., how many duplicates generated)

**Why full for LLM/CRS logs?**
- Complex nested JSON structure (LLM logs) - computing diffs is error-prone
- Plain text logs (CRS logs) - users want complete log at any snapshot
- Compression mitigates storage overhead (logs compress well)
- Simpler implementation and easier debugging

**Why compression?**
- Follows FuzzBench pattern (tar.gz archives)
- Significant space savings (especially for logs and text data)
- Easy to decompress and inspect with standard tools
- Typical compression ratio: 5-10x for text/JSON

### Storage Comparison

**Without compression (current design):**
- 24-hour trial, 15-min snapshots = 96 snapshots
- ~2 MB per snapshot = ~200 MB per trial

**With compression and incremental capture:**
- Same trial configuration
- Incremental POVs/patches: ~100 KB per snapshot (only new files)
- Full logs with compression: ~500 KB per snapshot
- **Total: ~50-100 MB per trial (50-75% reduction)**

## Architecture

### Threading and Process Model

**IMPORTANT: Process vs Thread Distinction**

CRSBench uses a **hybrid model** with processes for parallelism and threads for monitoring:

```
Orchestrator (Main Process)
│
├── Local Mode (Sequential)
│   └── For each trial (sequential in main process):
│       ├── Process: run_crs_trial() in main process
│       │   ├── Thread 1 (Main): BenchmarkRunner orchestration
│       │   │   └── Spawns CRS subprocess (Docker container)
│       │   └── Thread 2 (Daemon): SnapshotManager
│       │       └── Periodic snapshot capture
│       └── Process: CRS subprocess (Docker)
│           └── Writes outputs: POVs, patches, logs
│
└── Distributed Mode (Parallel)
    └── RQ Workers (separate processes, potentially on different machines)
        └── For each worker:
            └── Executes jobs from queue:
                └── run_crs_trial() job:
                    ├── Thread 1 (Main): BenchmarkRunner orchestration
                    │   └── Spawns CRS subprocess (Docker container)
                    └── Thread 2 (Daemon): SnapshotManager
                        └── Periodic snapshot capture
```

**Key Points:**

1. **Parallelism Model**:
   - **Local mode**: Trials execute sequentially in orchestrator process
   - **Distributed mode**: Trials execute in parallel across multiple RQ worker processes
   - Each worker process handles one trial at a time

2. **Threading Within Each Trial**:
   - **Thread 1 (Main)**: Runs in BenchmarkRunner.run_benchmark()
     - Orchestrates CRS execution
     - Spawns CRS as subprocess (Docker container)
     - Waits for CRS to complete
     - Collects results
   - **Thread 2 (Daemon)**: SnapshotManager thread
     - Started before CRS subprocess
     - Runs independently, capturing snapshots periodically
     - Stopped when CRS completes
     - Dies automatically if main thread exits (daemon=True)

3. **CRS Subprocess**:
   - **Not a thread** - it's a separate process (Docker container)
   - Spawned by BenchmarkRunner via OssCrsAdapter
   - Writes outputs to trial_output_dir
   - BenchmarkRunner waits for subprocess to complete
   - Snapshot thread reads outputs while subprocess runs

4. **No Concurrency Within Trial**:
   - Each trial has exactly 2 threads (main + snapshot)
   - Only 1 CRS subprocess per trial
   - Threads don't compete for resources (read vs write)
   - Filesystem provides natural synchronization

5. **Multi-Trial Concurrency** (Distributed Mode Only):
   - Multiple RQ worker processes run concurrently
   - Each worker has its own thread pair (main + snapshot)
   - Workers are isolated (different trial_output_dirs)
   - No shared state between workers

### Resource Usage Examples

**Local Mode (Sequential) - 3 trials**:
```
Time →
Trial 1: [Main Thread + Snapshot Thread + CRS Subprocess] ────────→
         └── Completes
Trial 2:                                                    [Main Thread + Snapshot Thread + CRS Subprocess] ────────→
                                                            └── Completes
Trial 3:                                                                                                       [Main Thread + Snapshot Thread + CRS Subprocess] ────────→

Total concurrent: 1 trial at a time (2 threads + 1 subprocess per trial)
```

**Distributed Mode (Parallel) - 3 workers, 9 trials**:
```
Time →
Worker 1: [Trial 1: Main+Snap+CRS] → [Trial 4: Main+Snap+CRS] → [Trial 7: Main+Snap+CRS]
Worker 2: [Trial 2: Main+Snap+CRS] → [Trial 5: Main+Snap+CRS] → [Trial 8: Main+Snap+CRS]
Worker 3: [Trial 3: Main+Snap+CRS] → [Trial 6: Main+Snap+CRS] → [Trial 9: Main+Snap+CRS]

Total concurrent: 3 trials (6 threads + 3 subprocesses total)
```

**Per-Trial Resource Breakdown**:
- **Threads**: 2 (main + snapshot, both in Python)
- **Processes**: 1 (CRS subprocess, Docker container)
- **Memory**:
  - Main thread: ~50 MB (BenchmarkRunner, result collection)
  - Snapshot thread: ~20 MB (SnapshotManager state tracking)
  - CRS subprocess: Variable (depends on CRS, typically 100MB-2GB)
- **CPU**:
  - Main thread: Minimal (mostly waiting for CRS)
  - Snapshot thread: Periodic spikes during capture (~1-5% average)
  - CRS subprocess: Heavy (fuzzing, static analysis, LLM calls)

### High-Level Design (Single Trial)

```
BenchmarkRunner (in main thread)
│
├── Main Thread
│   ├── 1. Create SnapshotManager
│   ├── 2. Start snapshot thread (daemon=True)
│   ├── 3. Spawn CRS subprocess (Docker)
│   ├── 4. Wait for CRS subprocess to complete
│   ├── 5. Stop snapshot thread
│   └── 6. Join snapshot thread (timeout=5s)
│
├── Snapshot Thread (daemon)
│   └── Periodic polling loop:
│       ├── Sleep for snapshot_period seconds
│       └── Capture snapshot:
│           ├── Read CRS output files
│           ├── Collect LLM metrics
│           ├── Copy logs
│           └── Write snapshot archive
│
└── CRS Subprocess (Docker container)
    └── Writes outputs:
        ├── POVs to output/povs/
        ├── Patches to output/patches/pov_N/
        ├── Seeds to output/seeds/
        ├── LLM usage to llm-usage.json
        └── Logs to crs-output.log
```

**Key characteristics:**
- **Location**: Trial runner (`crsbench/evaluation/`)
- **Threading**: Main + snapshot thread (no async/await)
- **Subprocess**: CRS runs in Docker container (separate process)
- **Storage**: Local filesystem in `experiment_filestore`
- **Timing**: Fixed interval polling (default 900s / 15 minutes)
- **Isolation**: Each trial has independent directory structure

### Comparison with FuzzBench

| Aspect           | FuzzBench                      | CRSBench                    |
|------------------|--------------------------------|-----------------------------|
| Threading        | Main + worker thread           | **Same**                    |
| Timing           | Sleep-based polling            | **Same**                    |
| Storage          | Google Cloud Storage           | **Filesystem only**         |
| Compression      | tar.gz archives                | **tar.gz archives (same)**  |
| Incremental data | Corpus only                    | **Corpus + POVs + patches** |
| Full data        | Crashes (not used for ranking) | **LLM logs + CRS logs**     |
| Measurement      | Separate measurer process      | **No separate process**     |
| Verification     | Async coverage analysis        | **No verification**         |
| Database         | PostgreSQL                     | **None**                    |

**What we adopt:**
- Simple main + worker thread pattern
- Sleep-based timing mechanism
- Incremental capture (only new data)
- tar.gz compression

**What we adapt:**
- Local filesystem instead of cloud storage
- CRS-specific data (POVs, patches, LLM metrics)
- No separate measurement process
- Incremental strategy extended to POVs/patches (not just corpus)

**What we skip:**
- Database storage
- Async verification
- Cloud infrastructure

## Configuration

### Snapshot Period Constraints

**Default**: 900 seconds (15 minutes)
**Minimum**: 60 seconds (1 minute)
**Maximum**: 86400 seconds (24 hours)
**Special**: 0 (snapshots disabled)

**Rationale for constraints**:

**Minimum (60 seconds)**:
- **I/O overhead**: Creating tar.gz archives and file copying takes time (~5-30 seconds depending on data size)
- **Filesystem pressure**: Too-frequent snapshots cause excessive I/O, potentially slowing CRS execution
- **Diminishing returns**: For typical CRS runs (hours long), sub-minute granularity provides little additional insight
- **Storage waste**: More snapshots = more duplicate log data (logs are captured fully, not incrementally)
- **Practical lower bound**: Even 60s is aggressive; typical use cases favor 5-15 minute intervals

**Maximum (24 hours)**:
- **Trial duration**: Most trials complete within 2-24 hours; longer periods defeat snapshot purpose
- **Progress visibility**: Beyond 24h intervals, snapshots lose value for monitoring
- **Failure recovery**: If trial crashes after 23 hours without snapshot, significant data loss
- **Sanity check**: Values >24h likely indicate configuration error (typo in seconds vs hours)

**Why 900s (15 minutes) default?**:
- Matches FuzzBench's snapshot frequency
- Good balance: frequent enough for progress tracking, infrequent enough to minimize overhead
- For typical 2-hour trial: 8 snapshots, manageable storage (~100-200 MB)
- For 24-hour trial: 96 snapshots, still reasonable (~1-2 GB compressed)

**When to use different values**:
- **300-600s (5-10 min)**: Short trials (<1 hour), need fine-grained monitoring
- **1800-3600s (30-60 min)**: Long trials (>12 hours), minimize storage overhead
- **0**: Very short trials (<15 min), testing, or storage-constrained environments

### ExperimentConfig Schema

Add new field to `crsbench/validation/schemas.py`:

```python
class ExperimentConfig(BaseModel):
    """Experiment configuration schema."""

    # ... existing fields ...

    snapshot_period: Optional[int] = Field(
        default=900,
        ge=0,
        description="Snapshot interval in seconds (0 to disable, default 900 = 15 minutes)"
    )

    @field_validator('snapshot_period')
    @classmethod
    def validate_snapshot_period(cls, v):
        """Validate snapshot period is reasonable."""
        if v is None:
            return 900  # Default: 15 minutes

        if v == 0:
            return 0  # Disabled

        if v < 60:
            raise ValueError("snapshot_period must be at least 60 seconds (or 0 to disable)")

        if v > 86400:
            raise ValueError(f"snapshot_period of {v}s (>{v/3600:.1f} hours) exceeds maximum of 24 hours")

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
experiment_filestore: /tmp/crsbench/experiments
report_filestore: /tmp/crsbench/reports
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
1. Create temp snapshot directory
   └─ {trial_dir}/.snapshot-{cycle:04d}/

2. Capture metadata
   └─ Write metadata.json (timestamp, cycle, elapsed_time)

3. Capture POVs (incremental - new only)
   └─ Read {trial_dir}/output/povs/
   └─ Track by filename set
   └─ Copy new POVs to snapshot/povs/

4. Capture patches (incremental - new only)
   └─ Read {trial_dir}/output/patches/
   └─ Track by filename set
   └─ Copy new patches to snapshot/patches/

5. Capture corpus (incremental - new/modified only)
   └─ Read {trial_dir}/output/seeds/
   └─ Track by modification time (mtime)
   └─ Copy new/modified corpus to snapshot/seeds/

6. Capture CRS-specific data (incremental - new/modified only)
   └─ Read {trial_dir}/output/crs-data/
   └─ Track by modification time (mtime)
   └─ Copy new/modified files to snapshot/crs-data/

7. Capture experiment config (full)
   └─ Copy {trial_dir}/config.yaml
   └─ Experiment configuration from orchestrator

8. Capture execution metadata (full)
   └─ Copy {trial_dir}/execution.json
   └─ Execution details from executor

9. Capture LLM logs (full)
   └─ Copy complete {trial_dir}/llm-usage.json
   └─ Includes all cumulative metrics

10. Capture CRS logs (full)
    └─ Copy complete {trial_dir}/crs-output.log
    └─ Entire log file

11. Compress snapshot
    └─ Create snapshot-{cycle:04d}.tar.gz
    └─ Compress entire .snapshot-{cycle:04d}/ directory
    └─ Delete temp directory

12. Mark snapshot complete
    └─ Create snapshot-{cycle:04d}.complete marker file
```

### Incremental Tracking

**Implementation approach:**

Track captured files to identify new data:

```python
class SnapshotManager:
    def __init__(self, ...):
        # Incremental tracking
        self.captured_pov_ids: Set[str] = set()        # POVs: track by filename
        self.captured_patch_ids: Set[str] = set()      # Patches: track by filename
        self.last_corpus_archive_time: float = 0.0     # Corpus: track by mtime

    def _get_new_povs(self) -> List[Path]:
        """Get POVs discovered since last snapshot."""
        pov_dir = self.trial_dir / "output" / "povs"
        # Return POV files not in captured_pov_ids set
        # Track by filename (e.g., "pov_001")

    def _get_new_patches(self) -> List[Path]:
        """Get patches generated since last snapshot."""
        patches_dir = self.trial_dir / "output" / "patches"
        # Return patch files not in captured_patch_ids set
        # Track by POV directory path (e.g., "pov_0/patch.diff")

    def _get_new_corpus_files(self) -> List[Path]:
        """Get new/modified corpus files since last snapshot."""
        corpus_dir = self.trial_dir / "output" / "seeds"
        # Return corpus files where mtime > last_corpus_archive_time
        # Like FuzzBench's incremental corpus archiving

    def _get_new_crs_data_files(self) -> List[Path]:
        """Get new/modified CRS-specific data files since last snapshot."""
        crs_data_dir = self.trial_dir / "output" / "crs-data"
        # Return files where mtime > last_crs_data_archive_time
        # Track by modification time like corpus
```

**Key methods:**
- `capture_snapshot()`: Main capture orchestration
- `_get_new_povs()`: Identify new POV files
- `_get_new_patches()`: Identify new patch files
- `_get_new_corpus_files()`: Identify new/modified corpus files
- `_get_new_crs_data_files()`: Identify new/modified CRS-specific data
- `_create_tar_gz()`: Compress snapshot directory
- `_cleanup_temp_dir()`: Remove temporary snapshot directory

See `crsbench/evaluation/snapshot_manager.py` for full implementation.

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

**Files:** `povs/` directory (inside snapshot tar.gz)

**Capture strategy:** Incremental (new POVs only)

Each snapshot contains only POV files that are new since the last snapshot. POVs are stored **as-is** without deduplication.

**Format:** Binary blobs (test inputs that trigger vulnerabilities)

```
povs/
├── pov_001        # Binary blob (not .json)
├── pov_002        # Binary blob
└── pov_003        # Binary blob
```

**Note:**
- POVs are binary test case files, not JSON
- Each POV is a test input that triggers a vulnerability when run against the harness
- CRS may generate duplicate POVs for the same vulnerability
- Snapshots store all POVs; deduplication happens during the validation/evaluation phase
- No metadata stored in snapshot - POV validation will determine sanitizer output, error tokens, etc.

### Patch Data Format

**Files:** `patches/` directory (inside snapshot tar.gz)

**Capture strategy:** Incremental (new patches only)

Each snapshot contains only patch files that are new since the last snapshot. Patches are stored **as-is** exactly as reported by the CRS, without deduplication.

Patches are organized by POV ID in subdirectories:

```
patches/
├── pov_0/
│   └── patch.diff
├── pov_1/
│   └── patch.diff
└── pov_2/
    └── patch.diff
```

**Example patch file (pov_0/patch.diff):**

Standard unified diff format (or whatever format CRS produces):

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

**Note:**
- Patches stored exactly as CRS generates them (no metadata.json)
- CRS may generate duplicate or similar patches
- Snapshots store all patches; deduplication and validation happen during the evaluation phase

### LLM Usage Data Format

**File:** `llm-usage.json` (inside snapshot tar.gz)

**Capture strategy:** Full cumulative snapshot

Each snapshot contains the **complete** LLM usage metrics up to that point:

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

**File:** `crs-output.log` (inside snapshot tar.gz)

**Capture strategy:** Full log (not tail)

Each snapshot contains the **complete** CRS output log:

```
[2025-01-15 10:00:00] INFO: CRS starting up
[2025-01-15 10:00:05] INFO: Initializing fuzzing engine
[2025-01-15 10:45:23] INFO: Starting fuzzing campaign
[2025-01-15 10:45:30] INFO: Generated 1000 test cases
[2025-01-15 10:46:15] INFO: Found crash: heap-buffer-overflow
[2025-01-15 10:47:00] INFO: Analyzing crash with LLM
[2025-01-15 10:47:45] INFO: Generated POV candidate pov_001
...
(complete log from trial start to snapshot time)
```

### Corpus Data Format (if CRS generates corpus)

**Files:** `corpus/` directory (inside snapshot tar.gz)

**Capture strategy:** Incremental (new/modified files only)

Each snapshot contains only corpus files that are new or modified since the last snapshot:

```
corpus/
├── input-001        # Binary test input
├── input-002
└── input-003
```

## Storage Structure

### Directory Layout

```
experiment_filestore/
└── {experiment_name}/
    └── {benchmark_id}__{crs_name}/
        └── trial-{trial_id}/
            ├── snapshot-0001.tar.gz         # Compressed snapshot cycle 1
            ├── snapshot-0001.complete       # Marker: snapshot 1 finished
            ├── snapshot-0002.tar.gz         # Compressed snapshot cycle 2
            ├── snapshot-0002.complete       # Marker: snapshot 2 finished
            ├── snapshot-0003.tar.gz
            ├── snapshot-0003.complete
            ├── output/                      # CRS outputs (what we snapshot)
            │   ├── povs/                    # POVs discovered by CRS
            │   │   ├── pov_001              # Binary blob
            │   │   ├── pov_002
            │   │   └── ...
            │   ├── patches/                 # Patches generated by CRS (organized by POV ID)
            │   │   ├── pov_0/
            │   │   │   └── patch.diff
            │   │   ├── pov_1/
            │   │   │   └── patch.diff
            │   │   └── ...
            │   ├── seeds/                   # Seeds generated by CRS
            │   │   ├── input-001
            │   │   └── ...
            │   └── crs-data/                # CRS-specific outputs
            │       └── ...
            ├── hints/                       # Prepared hints (input to CRS, not snapshotted)
            │   ├── sarif/
            │   └── corpus/
            ├── povs/                        # Prepared POVs (input for patch gen, not snapshotted)
            │   ├── pov_0
            │   └── ...
            ├── config.yaml                  # Experiment config (from orchestrator)
            ├── execution.json               # Execution metadata (from executor)
            ├── llm-usage.json              # Cumulative LLM metrics
            ├── crs-output.log              # Complete CRS log
            └── final-report.json           # Trial result (after completion)
```

**Note:** Snapshots capture CRS **outputs** from `output/` directory, not the prepared **inputs** in `hints/` and `povs/` directories.

### Inside Each Snapshot Archive

When extracted, `snapshot-0001.tar.gz` contains:

```
snapshot-0001/
├── metadata.json                # Snapshot metadata
├── povs/                        # New POVs only (incremental)
│   └── pov_001                  # Binary blob
├── patches/                     # New patches only (incremental, organized by POV ID)
│   └── pov_0/
│       └── patch.diff
├── seeds/                       # New/modified seeds only (incremental)
│   └── input-001
├── crs-data/                    # CRS-specific outputs (incremental)
│   └── analysis-report.txt
├── config.yaml                  # Experiment config (full)
├── execution.json               # Execution metadata (full)
├── llm-usage.json              # Full cumulative LLM metrics
└── crs-output.log              # Full CRS log
```

When extracted, `snapshot-0002.tar.gz` contains:

```
snapshot-0002/
├── metadata.json
├── povs/                        # Only pov_002 (new since snapshot 1)
│   └── pov_002                  # Binary blob
├── patches/                     # Only new patches (new since snapshot 1, organized by POV ID)
│   └── pov_1/
│       └── patch.diff
├── seeds/                       # Only new/modified seeds
│   └── input-002
├── crs-data/                    # Only new/modified CRS data
│   └── debug-trace.log
├── config.yaml                  # Full experiment config
├── execution.json               # Full execution metadata
├── llm-usage.json              # Full cumulative metrics (updated)
└── crs-output.log              # Full log (grown since snapshot 1)
```

### File Naming Conventions

- **Snapshot archives**: `snapshot-{cycle:04d}.tar.gz` (e.g., `snapshot-0001.tar.gz`, `snapshot-0023.tar.gz`)
- **Completion markers**: `snapshot-{cycle:04d}.complete` (empty file, presence indicates complete snapshot)
- **POV files**: `pov_{id}` where `id` is sequential or hash-based (binary blobs, no extension)
- **Patch files**: `patch-{id}.diff` where `id` is sequential or hash-based
- **Corpus files**: Any filename (binary test inputs)

### Snapshot Validation

**Completion check:**
- Snapshot is complete if `snapshot-{cycle:04d}.complete` marker exists
- Marker created only after successful compression and cleanup

**Integrity check:**
- tar.gz archive can be extracted without errors
- Required files exist inside archive:
  - `metadata.json`
  - `llm-usage.json`
  - `crs-output.log`
- metadata.json contains valid SnapshotMetadata structure

**Implementation:** See `crsbench/evaluation/snapshot_manager.py` for validation functions.

## Implementation Details

### SnapshotManager Class

**File:** `crsbench/evaluation/snapshot_manager.py` (new file)

**Key responsibilities:**
- Run in separate thread for periodic snapshots
- Track incremental state (captured POVs/patches/corpus)
- Create compressed snapshot archives
- Cleanup temporary directories

**Main interfaces:**

```python
class SnapshotManager:
    def __init__(self, trial_dir: Path, snapshot_period: int = 900):
        """Initialize with trial directory and snapshot interval."""

    def run(self):
        """Main snapshot loop (runs in separate thread)."""

    def stop(self):
        """Stop snapshot loop gracefully."""

    def capture_snapshot(self):
        """Capture single snapshot with compression."""
```

**Key methods:**
- `sleep_until_next_snapshot()`: Timing mechanism with quick shutdown support
- `_get_new_povs()`: Identify new POV files (filename tracking)
- `_get_new_patches()`: Identify new patch files (filename tracking)
- `_get_new_corpus_files()`: Identify new/modified corpus (mtime tracking)
- `_create_tar_gz()`: Compress snapshot directory
- `_cleanup_temp_dir()`: Remove temporary snapshot directory

**State tracking:**
- `captured_pov_ids: Set[str]`: Filenames of captured POVs
- `captured_patch_ids: Set[str]`: Filenames of captured patches
- `last_corpus_archive_time: float`: Timestamp for corpus incremental tracking

See full implementation in `crsbench/evaluation/snapshot_manager.py`.

### Integration with BenchmarkRunner

**File:** `crsbench/evaluation/runner.py` (modifications)

**Changes needed:**
1. Add `snapshot_period` parameter to `__init__()`
2. Add `trial_output_dir` parameter to `run_benchmark()`
3. Start snapshot thread before CRS execution
4. Stop snapshot thread after CRS completes

**Integration pattern:**
```python
class BenchmarkRunner:
    def __init__(self, adapter, snapshot_period=None):
        self.snapshot_period = snapshot_period

    def run_benchmark(self, ..., trial_output_dir=None):
        # Start snapshot manager if configured
        if self.snapshot_period and trial_output_dir:
            snapshot_manager = SnapshotManager(trial_output_dir, self.snapshot_period)
            snapshot_thread = threading.Thread(target=snapshot_manager.run, daemon=True)
            snapshot_thread.start()

        try:
            # Run CRS evaluation
            result = self._run_crs_evaluation(...)
            return result
        finally:
            # Stop snapshot thread
            if snapshot_manager:
                snapshot_manager.stop()
                snapshot_thread.join(timeout=5.0)
```

### Integration with Orchestrator

**File:** `crsbench/run_experiment.py` (modifications)

**Changes needed:**
1. Pass `config.snapshot_period` to `BenchmarkRunner`
2. Create trial output directories before running
3. Pass `trial_output_dir` to `run_benchmark()`

**Integration pattern:**
```python
def run_experiment_local(experiment_name, config, benchmarks, crses):
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

                # Create runner with snapshot support
                adapter = create_adapter(config, crs_name, oss_fuzz_path, registry_dir, benchmarks_root)
                runner = BenchmarkRunner(
                    adapter=adapter,
                    snapshot_period=config.snapshot_period
                )

                # Run with output directory
                result = runner.run_benchmark(
                    benchmark_path=benchmark_path,
                    trial_output_dir=trial_output_dir,
                    mode="auto"
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

    adapter = create_adapter(trial_config.config, trial_config.crs, ...)
    runner = BenchmarkRunner(
        adapter=adapter,
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
    for snapshot_archive in sorted(trial_dir.glob("snapshot-*.tar.gz")):
        if not (trial_dir / f"{snapshot_archive.stem}.complete").exists():
            continue

        # Extract and analyze snapshot
        with tarfile.open(snapshot_archive, 'r:gz') as tar:
            tar.extractall(temp_dir)

            metadata = json.loads((temp_dir / "metadata.json").read_text())

            # Count POV files
            pov_count = len(list((temp_dir / "povs").glob("pov_*"))) if (temp_dir / "povs").exists() else 0

            llm_usage = json.loads((temp_dir / "llm-usage.json").read_text())

            snapshots.append({
                "cycle": metadata["cycle"],
                "elapsed_time": metadata["elapsed_time"],
                "pov_count": pov_count,
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

        # Create first POV (binary blob)
        (pov_dir / "pov_001").write_bytes(b'\x00\x01\x02\x03')

        new_povs = manager._get_new_povs()
        assert len(new_povs) == 1
        assert new_povs[0].name == "pov_001"

        # Second call should return empty (already captured)
        new_povs = manager._get_new_povs()
        assert len(new_povs) == 0

        # Create second POV
        (pov_dir / "pov_002").write_bytes(b'\x04\x05\x06\x07')

        new_povs = manager._get_new_povs()
        assert len(new_povs) == 1
        assert new_povs[0].name == "pov_002"
```

#### 2. Integration Tests

```python
def test_snapshot_capture_with_benchmark_runner():
    """Test snapshot integration with BenchmarkRunner."""
    with tempfile.TemporaryDirectory() as tmpdir:
        trial_dir = Path(tmpdir)

        mock_adapter = MagicMock()
        mock_adapter.mode = "bug-finding"
        runner = BenchmarkRunner(
            adapter=mock_adapter,
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
        trial_dir = Path(tmpdir)
        snapshot_archive = trial_dir / "snapshot-0001.tar.gz"
        completion_marker = trial_dir / "snapshot-0001.complete"

        # Incomplete snapshot (no marker)
        assert not is_snapshot_complete(trial_dir, cycle=1)

        # Create empty tar.gz
        with tarfile.open(snapshot_archive, 'w:gz') as tar:
            pass

        # Still incomplete (no marker)
        assert not is_snapshot_complete(trial_dir, cycle=1)

        # Add completion marker
        completion_marker.touch()
        assert is_snapshot_complete(trial_dir, cycle=1)
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
- CPU: ~2-3% during capture (compression overhead)
- Memory: <10 MB (tracking sets and file handles)
- I/O: Depends on data volume

**Per-snapshot timing:**
- File copying: <0.5s (incremental POVs/patches/corpus)
- tar.gz compression: 1-3s (depends on log size)
- Total: ~2-4s per snapshot

**CRS execution impact:**
- Minimal: Snapshot thread reads files, doesn't block CRS
- Filesystem provides natural buffering and caching
- No locks or synchronization needed
- Compression happens in separate thread

### Scalability

**Per-trial storage (with compression):**
- Without compression/incremental: ~200 MB for 96 snapshots (24h, 15min interval)
- With compression/incremental: ~50-100 MB (50-75% reduction)

**Breakdown per snapshot (compressed):**
- Incremental POVs/patches: ~50-100 KB (only new files)
- Incremental corpus: ~100-500 KB (depends on generation rate)
- Full LLM logs: ~200-300 KB (JSON compresses well)
- Full CRS logs: ~200-500 KB (text compresses well)
- **Total: ~600 KB - 1.5 MB per snapshot**

**Compression ratios:**
- JSON (LLM logs): ~5-8x compression
- Text logs (CRS logs): ~5-10x compression
- Binary corpus: ~1-2x compression (varies widely)

### Timing Accuracy

**Expected timing:**
- Snapshot period: ±1 second accuracy
- Compression adds 1-3s to capture time
- Good enough for 15-minute (900s) intervals

**Jitter sources:**
- Snapshot capture: 2-4s (copying + compression)
- OS scheduling delays
- I/O wait time
- Disk speed (especially for large logs)

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

## Implementation Status and Notes

### Sample Snapshot Generator ✅

**Status**: COMPLETE

A sample snapshot generator has been implemented in `snapshot-examples/generate_snapshot.py` with:
- Full generation, validation, and listing capabilities
- Demonstrates incremental capture strategy
- Provides reference implementation for snapshot format
- Includes comprehensive validation

This serves as:
1. Reference for implementing production snapshot system
2. Testing tool for snapshot parsing code
3. Documentation of snapshot format

### Production Snapshot System (This Section)

**Status**: IN PROGRESS

This design doc describes the production snapshot system that will:
- Capture snapshots during actual CRS execution
- Run in separate thread alongside CRS subprocess
- Integrate with BenchmarkRunner and Orchestrator
- Handle real-time incremental tracking

### Trial Directory Structure

The production snapshot system operates on the following directory structure:

```
trial_output_dir/                          # Created by Orchestrator
├── output/                                # Created by CRS (snapshotted)
│   ├── povs/                              # CRS writes POVs here
│   │   ├── pov_001
│   │   ├── pov_002
│   │   └── ...
│   ├── patches/                           # CRS writes patches here (organized by POV ID)
│   │   ├── pov_0/
│   │   │   └── patch.diff
│   │   ├── pov_1/
│   │   │   └── patch.diff
│   │   └── ...
│   ├── seeds/                             # CRS writes seeds here (optional)
│   │   ├── input-001
│   │   └── ...
│   └── crs-data/                          # CRS-specific outputs (optional)
│       └── ...
├── hints/                                 # Prepared by BenchmarkRunner (NOT snapshotted)
│   ├── sarif/                             # Filtered SARIF reports
│   │   └── report.sarif
│   └── corpus/                            # Pre-fuzzing corpus
│       └── ...
├── povs/                                  # For patch-gen mode (NOT snapshotted)
│   ├── pov_0
│   └── ...
├── config.yaml                            # Experiment config (copied once, static)
├── execution.json                         # Execution metadata (written by BenchmarkRunner, static)
├── llm-usage.json                         # LLM metrics (updated by CRS, snapshotted fully)
├── crs-output.log                         # CRS stdout/stderr (growing, snapshotted fully)
├── snapshot-0001.tar.gz                   # Snapshot archives
├── snapshot-0001.complete                 # Completion markers
├── snapshot-0002.tar.gz
├── snapshot-0002.complete
└── ...
```

**Key Points**:
- **Snapshots capture `output/` directory contents** (POVs, patches, seeds, crs-data)
- **Snapshots also capture logs** (`llm-usage.json`, `crs-output.log`)
- **Snapshots also capture static config** (`config.yaml`, `execution.json`)
- **Snapshots do NOT capture inputs** (`hints/`, `povs/` for patch-gen)
- **CRS creates `output/` and subdirectories** as needed
- **BenchmarkRunner creates `hints/` and `povs/`** before CRS execution

### LLM Usage Tracking

**Who writes `llm-usage.json`?**
- CRS is responsible for writing/updating `llm-usage.json`
- CRS should update this file periodically during execution
- SnapshotManager reads and copies the complete file

**Format** (based on design doc example):
```json
{
  "total_api_calls": 150,
  "total_input_tokens": 45000,
  "total_output_tokens": 12000,
  "total_cached_tokens": 20000,
  "total_cost_usd": 1.25,
  "by_model": {
    "claude-sonnet-4": {...},
    "gpt-4": {...}
  },
  "by_operation": {
    "fuzzing": {...},
    "static_analysis": {...},
    "patch_generation": {...}
  }
}
```

**If CRS doesn't use LLM**:
- CRS should either not create `llm-usage.json`
- Or create empty/minimal structure: `{"total_api_calls": 0, "total_cost_usd": 0.0}`
- Snapshot will copy file if it exists, skip if it doesn't

### Snapshot Disabling

Snapshots can be disabled when not needed:

**Configuration**:
```yaml
# Disable snapshots
snapshot_period: 0

# Or explicitly null
snapshot_period: null
```

**When to disable**:
- Very short trials (<15 minutes) where snapshots add overhead without value
- Testing/development scenarios
- When storage is limited

**Implementation**:
- `snapshot_period: 0` or `null` → SnapshotManager not created
- BenchmarkRunner checks `if snapshot_period and snapshot_period > 0` before starting thread

### Thread Lifecycle and Error Handling

**Thread Start Sequence**:
1. BenchmarkRunner creates trial_output_dir
2. BenchmarkRunner starts CRS subprocess
3. BenchmarkRunner creates SnapshotManager with trial_output_dir
4. BenchmarkRunner starts snapshot thread (daemon=True)
5. Snapshot thread begins periodic capture loop

**Thread Stop Sequence**:
1. CRS subprocess completes (success or failure)
2. BenchmarkRunner calls `snapshot_manager.stop()`
3. SnapshotManager sets `self.running = False`
4. Snapshot thread wakes from sleep, sees `running=False`, exits loop
5. BenchmarkRunner calls `snapshot_thread.join(timeout=5.0)`
6. If join times out, log warning (daemon thread will be killed anyway)

**Error Handling Strategy**:

**Snapshot Capture Errors**:
- Snapshot failures should NOT crash CRS execution
- Log error with full traceback
- Continue to next snapshot cycle
- Mark trial as "partial snapshots" in metadata

**File Access Errors**:
- Handle ENOENT (file doesn't exist yet)
- Handle permission errors
- Handle disk full errors
- Retry logic for transient errors

**Implementation**:
```python
def capture_snapshot(self):
    """Capture single snapshot with error handling."""
    try:
        self.cycle += 1
        logger.info(f"Capturing snapshot {self.cycle}")

        # Create temp directory
        temp_dir = self.trial_dir / f".snapshot-{self.cycle:04d}"
        temp_dir.mkdir(exist_ok=True)

        try:
            # Capture all data
            self._capture_metadata(temp_dir)
            self._capture_povs(temp_dir)
            self._capture_patches(temp_dir)
            # ... more capture methods

            # Compress to tar.gz
            archive_path = self.trial_dir / f"snapshot-{self.cycle:04d}.tar.gz"
            self._create_tar_gz(temp_dir, archive_path)

            # Mark complete
            marker_path = self.trial_dir / f"snapshot-{self.cycle:04d}.complete"
            marker_path.touch()

            logger.info(f"Snapshot {self.cycle} completed successfully")

        finally:
            # Always cleanup temp directory
            if temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)

    except Exception as e:
        logger.error(f"Snapshot {self.cycle} failed: {e}", exc_info=True)
        # Don't raise - continue to next snapshot
```

**Graceful Shutdown**:
- Use daemon thread so Python doesn't wait indefinitely
- Implement quick shutdown check (every second during sleep)
- Join with timeout to avoid hanging

**Race Conditions**:
- CRS may be writing files while snapshot reads
- Use `try/except` for file operations
- Partial files in snapshots are acceptable (will be complete in next snapshot)
- No locks needed (filesystem provides natural ordering)

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
            # Run POVs against harness to verify they trigger crashes
            verified_results = self._verify_povs_immediate(new_pov_files)
            # Store verification results as metadata
            self._write_json(snapshot_dir / "verification-results.json", verified_results)
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

## Sample Snapshot Generation

For testing and documentation purposes, CRSBench provides a script to generate realistic sample snapshots.

### Generator Script

**Location:** `snapshot-examples/generate_snapshot.py`

**Features:**
- Generates realistic snapshot archives with proper tar.gz compression
- Simulates incremental POV/patch discovery over time
- Creates proper directory structure and file formats
- Includes snapshot validation functionality

### Generation Design

The generator creates snapshots that demonstrate the full snapshot specification:

**Snapshot 1 (15 minutes):**
- 2 initial POVs discovered
- 1 patch generated for pov_0
- 2 initial corpus files
- 50 LLM API calls, 25K tokens

**Snapshot 2 (30 minutes):**
- 1 new POV (total: 3)
- 2 new patches for pov_1 and pov_2 (total: 3)
- 3 new corpus files (total: 5)
- 95 LLM API calls (cumulative), 47.5K tokens

**Snapshot 3 (45 minutes):**
- 2 new POVs (total: 5)
- 1 new patch for pov_3 (total: 4)
- 1 new corpus file (total: 6)
- 130 LLM API calls (cumulative), 65K tokens

### Data Generation Methods

**POVs:** Binary blobs (256 bytes of test data)
- Stored as `povs/pov_NNN` (no extension)
- Incremental tracking by filename

**Patches:** Standard unified diff format
- Stored as `patches/pov_N/patch.diff`
- Organized by POV ID in subdirectories
- Incremental tracking by path

**Corpus:** Binary test inputs
- Stored as `corpus/input-NNN`
- Incremental tracking by modification time

**LLM Usage:** Cumulative JSON metrics
- Full snapshot of all metrics up to that point
- Includes breakdowns by model and operation
- Realistic token counts and costs

**CRS Logs:** Complete log from trial start
- Full log file (not incremental)
- Includes log entries for all discovered POVs
- Shows realistic CRS execution flow

### Listing Functionality

The generator includes snapshot listing capabilities:

```python
class SnapshotLister:
    """List snapshot contents and provide summaries."""

    def list_directory(self, snapshot_dir: Path):
        """List all snapshots in a directory with summaries."""
        # Shows overview of all snapshots

    def list_snapshot(self, archive_path: Path):
        """List detailed contents of a single snapshot."""
        # Shows file tree with sizes and metadata
```

**Directory listing features:**
- Summary of each snapshot (cycle, elapsed time, file count)
- Categorized file counts (POVs, patches, corpus, config, logs)
- File size for each archive
- POV and patch names

**Single snapshot listing features:**
- Metadata display (cycle, timestamp, elapsed time, period)
- Complete file tree with proper indentation
- File sizes in human-readable format (B, K, M)
- Directory structure visualization

**Example output (directory listing):**
```
[Snapshot 0001] snapshot-0001.tar.gz (1.8K)
  Cycle: 1, Elapsed: 900s (15m)
  Files: 10 total
    - POVs: 2 (pov_001, pov_002)
    - Patches: 1 (patches/pov_0)
    - Corpus: 2 files
```

**Example output (single snapshot listing):**
```
Metadata:
  Cycle: 1
  Timestamp: 2025-10-25 18:19:41
  Elapsed: 900s (15m)

Files (10 total):
  patches/
    pov_0/
      patch.diff (183B)
  povs/
    pov_001 (256B)
```

### Validation Functionality

The generator includes comprehensive validation:

```python
class SnapshotValidator:
    """Validate snapshot format and structure."""

    def validate_all(self) -> bool:
        """Validate all snapshots in directory."""
        # Checks all snapshot archives

    def validate_snapshot(self, archive_path: Path) -> bool:
        """Validate a single snapshot archive."""
        # Validates structure, files, and metadata
```

**Validation checks:**
1. Completion marker exists (`.complete` file)
2. Archive is valid tar.gz
3. Required files present
4. JSON structure valid
5. Metadata cycle matches filename
6. Patch directory structure correct (organized by POV ID)

### Usage

**Generate snapshots:**
```bash
python snapshot-examples/generate_snapshot.py [output_dir]
```

**List snapshots:**
```bash
# List all snapshots in directory with summaries
python snapshot-examples/generate_snapshot.py --list [snapshot_dir]

# List detailed contents of specific snapshot
python snapshot-examples/generate_snapshot.py --list-snapshot <snapshot.tar.gz>
```

**Validate snapshots:**
```bash
python snapshot-examples/generate_snapshot.py --validate [snapshot_dir]
```

**Inspect snapshots (manual):**
```bash
# List contents
tar -tzf snapshot-examples/trial-example/snapshot-0001.tar.gz

# Extract
tar -xzf snapshot-examples/trial-example/snapshot-0001.tar.gz

# View metadata
cat metadata.json
```

### Integration with Testing

The generated snapshots are used for:

1. **Unit tests:** Test snapshot parsing and validation logic
2. **Integration tests:** Test report generation from snapshots
3. **Documentation:** Demonstrate snapshot format
4. **Development:** Quick reference for snapshot structure

Example test usage:

```python
def test_snapshot_parsing():
    """Test parsing snapshot metadata."""
    snapshot_dir = Path("snapshot-examples/trial-example")

    # Load snapshot
    with tarfile.open(snapshot_dir / "snapshot-0001.tar.gz", 'r:gz') as tar:
        metadata_file = tar.extractfile("metadata.json")
        metadata = json.load(metadata_file)

    # Validate structure
    assert metadata["cycle"] == 1
    assert "timestamp" in metadata
    assert "elapsed_time" in metadata
```

### Design Rationale

**Why a generator script?**
- Manual creation is error-prone and time-consuming
- Ensures snapshots match specification exactly
- Easy to regenerate when format changes
- Provides validation for real snapshots

**Why include validation?**
- Catch format errors early
- Ensure compliance with specification
- Provide clear error messages
- Support development and debugging

**Why realistic data?**
- Tests must handle real-world scenarios
- Documentation examples should be authentic
- Helps developers understand format
- Enables meaningful integration tests

### Maintenance

When updating the snapshot specification:

1. Update the generator script to match new format
2. Regenerate sample snapshots
3. Run validation to ensure compliance
4. Update tests that depend on snapshot structure
5. Update documentation examples

The generator script serves as executable documentation of the snapshot format.

## References

- [Evaluation Module Design](./evaluation.md): Evaluation module overview
- [Architecture](../architecture.md): Overall CRSBench architecture
- [Orchestration](../orchestration.md): Experiment orchestration design
- [Sample Snapshots](../../../snapshot-examples/): Generated snapshot examples and tools
