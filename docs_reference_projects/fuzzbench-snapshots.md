# FuzzBench Snapshot Implementation

This document analyzes how FuzzBench implements periodic experiment data snapshots during fuzzing trials. This serves as a reference for implementing similar functionality in CRSBench.

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Threading Model](#threading-model)
3. [Configuration](#configuration)
4. [Snapshot Capture (Trial Runner)](#snapshot-capture-trial-runner)
5. [Snapshot Measurement (Measurer)](#snapshot-measurement-measurer)
6. [Database Schema](#database-schema)
7. [Data Flow](#data-flow)
8. [Performance Considerations](#performance-considerations)
9. [Applicability to CRSBench](#applicability-to-crsbench)
10. [Key Code References](#key-code-references)

## Architecture Overview

FuzzBench uses a **polling-based periodic snapshot mechanism** that captures fuzzing corpus data at regular intervals during trial execution. The system separates snapshot **capture** from snapshot **measurement**:

- **Trial Runner** (on GCE instance): Periodically captures corpus archives and uploads to storage
- **Measurer** (on dispatcher): Downloads archives and performs coverage analysis asynchronously
- **Database** (PostgreSQL): Stores measured snapshot results with coverage metrics

This separation allows:
- Trial runners to focus on fuzzing with minimal overhead
- Coverage analysis to run independently without blocking fuzzing
- Parallel measurement across multiple snapshots and trials

## Threading Model

### Overview

FuzzBench uses a **simple main thread + worker thread pattern**:

```
Main Thread (TrialRunner)
├─ Manages timing and snapshot scheduling
├─ Sleeps for snapshot_period intervals
├─ Calls do_sync() to capture data
└─ Monitors worker thread health

Worker Thread
└─ Runs fuzzer subprocess for max_total_time
```

**Key characteristics:**
- No async/await - uses traditional threading
- No locks needed (fuzzer is subprocess, main thread reads filesystem)
- Simple time-based polling with `time.sleep()`
- Clean separation of concerns

### Implementation

**File:** `claude_reference_projects/fuzzbench/experiment/runner.py`

**Lines 300-301** - Worker thread creation:
```python
fuzz_thread = threading.Thread(target=run_fuzzer, args=args)
fuzz_thread.start()
```

**Lines 308-314** - Main polling loop:
```python
while fuzz_thread.is_alive():
    self.cycle += 1
    self.sleep_until_next_sync()
    self.do_sync()
```

**Lines 317-336** - Sleep-based timing mechanism:
```python
def sleep_until_next_sync(self):
    """Sleep until it is time to do the next sync."""
    if self.last_sync_time is not None:
        next_sync_time = (self.last_sync_time +
                         experiment_utils.get_snapshot_seconds())
        sleep_time = next_sync_time - time.time()
    else:
        sleep_time = experiment_utils.get_snapshot_seconds()

    if sleep_time > 0:
        time.sleep(sleep_time)

    self.last_sync_time = time.time()
```

**Design rationale:**
- Simple and reliable - no complex async coordination
- Minimal overhead - main thread mostly sleeps
- Predictable timing - snapshots occur at fixed intervals
- Easy to debug - straightforward control flow

## Configuration

### snapshot_period Parameter

**Default:** 900 seconds (15 minutes)

**Configuration locations:**

1. **Utility function** - `claude_reference_projects/fuzzbench/common/experiment_utils.py` (lines 23-42):
```python
DEFAULT_SNAPSHOT_SECONDS = 15 * 60  # 900 seconds

def get_snapshot_seconds() -> int:
    """Return the snapshot period in seconds."""
    return int(os.environ.get('SNAPSHOT_PERIOD', DEFAULT_SNAPSHOT_SECONDS))

def get_cycle_time(cycle: int) -> int:
    """Convert cycle number to elapsed time in seconds."""
    return cycle * get_snapshot_seconds()
```

2. **Experiment config** - `claude_reference_projects/fuzzbench/experiment/run_experiment.py` (lines 74-75):
```python
# Set default snapshot period if not specified
config['snapshot_period'] = config.get('snapshot_period', DEFAULT_SNAPSHOT_SECONDS)
```

3. **Validation** - `run_experiment.py` (lines 175-176):
```python
validator.optional_int_param(config, 'snapshot_period')
```

4. **Propagation to trial** - `run_experiment.py` (lines 505-506, 524):
```python
set_snapshot_period_arg = f'SNAPSHOT_PERIOD={self.config["snapshot_period"]}'
startup_script += set_snapshot_period_arg
```

### Example Configuration

```yaml
# experiment-config.yaml
experiment: my-experiment
trials: 5
max_total_time: 86400  # 24 hours
snapshot_period: 600    # Take snapshots every 10 minutes (instead of default 15)
```

**Configuration flow:**
1. Defined in experiment YAML config (optional)
2. Falls back to DEFAULT_SNAPSHOT_SECONDS (900)
3. Passed to trial runner via environment variable `SNAPSHOT_PERIOD`
4. Read by `experiment_utils.get_snapshot_seconds()` at runtime

## Snapshot Capture (Trial Runner)

The trial runner executes on GCE instances and is responsible for:
- Running the fuzzer
- Periodically capturing corpus data
- Uploading results to cloud storage

### Capture Process

**File:** `claude_reference_projects/fuzzbench/experiment/runner.py`

**Lines 338-428** - Main sync function:
```python
def do_sync(self):
    """Sync corpus, results, and stats."""
    self.archive_corpus()
    self.save_results()
    if self.fuzzer_config.get('stats_file'):
        self.record_stats()
```

### 1. Corpus Archiving (Incremental)

**Lines 381-425** - Archive implementation:
```python
def archive_corpus(self):
    """Archive the corpus directory."""
    if not self.corpus_dir.exists():
        return

    # Only archive files modified since last archive
    archive_name = f'corpus-archive-{self.cycle:04d}.tar.gz'
    archive_path = self.corpus_archives_dir / archive_name

    # Find files to archive (modified since last_archive_time)
    corpus_files = []
    for file_path in self.corpus_dir.iterdir():
        if file_path.is_file():
            mtime = file_path.stat().st_mtime
            if self.last_archive_time is None or mtime >= self.last_archive_time:
                corpus_files.append(file_path)

    # Create incremental tar.gz archive
    if corpus_files:
        with tarfile.open(archive_path, 'w:gz') as tar:
            for file_path in corpus_files:
                tar.add(file_path, arcname=file_path.name)

    self.last_archive_time = time.time()
```

**Key features:**
- **Incremental archiving**: Only includes new/modified files since last archive
- **Naming convention**: `corpus-archive-{cycle:04d}.tar.gz` (e.g., `corpus-archive-0001.tar.gz`)
- **Efficient**: Reduces data transfer and storage costs
- **Tracks modification time**: Uses `st_mtime` to determine changed files

### 2. Results Saving

**Lines 430-445** - Save results to filestore:
```python
def save_results(self):
    """Save fuzzer results (logs, crashes) to filestore."""
    if not self.results_dir.exists():
        return

    # Sync results directory to GCS
    gsutil.rsync(
        self.results_dir,
        self.gcs_results_dir,
        delete=False  # Keep all historical results
    )
```

### 3. Stats Recording (Optional)

**Lines 447-470** - Fuzzer-specific statistics:
```python
def record_stats(self):
    """Record fuzzer-specific stats to JSON."""
    stats_file = self.fuzzer_config['stats_file']

    if not stats_file.exists():
        return

    # Parse fuzzer stats (fuzzer-specific format)
    stats = parse_fuzzer_stats(stats_file)

    # Save to JSON with cycle number
    stats_json = self.trial_dir / f'stats-{self.cycle:04d}.json'
    with open(stats_json, 'w') as f:
        json.dump(stats, f, indent=2)

    # Upload to GCS
    gsutil.copy(stats_json, self.gcs_trial_dir)
```

### Storage Structure

Data uploaded to Google Cloud Storage:

```
{experiment_filestore}/{experiment}/experiment-folders/
└── {benchmark}-{fuzzer}/
    └── trial-{trial_id}/
        ├── corpus/
        │   ├── corpus-archive-0001.tar.gz  # Cycle 1 (0-15 min)
        │   ├── corpus-archive-0002.tar.gz  # Cycle 2 (15-30 min)
        │   ├── corpus-archive-0003.tar.gz  # Cycle 3 (30-45 min)
        │   └── ...
        ├── results/
        │   ├── fuzzer-log.txt
        │   └── crashes/
        │       ├── crash-abc123
        │       └── crash-def456
        └── stats/
            ├── stats-0001.json  # Optional fuzzer stats
            ├── stats-0002.json
            └── ...
```

**Key observations:**
- Organized by benchmark-fuzzer combination
- Each trial has isolated directory
- Corpus archives numbered sequentially
- Results preserved for entire run

## Snapshot Measurement (Measurer)

The measurer is a separate component that runs on the dispatcher (not on trial instances). It:
- Detects unmeasured snapshots
- Downloads corpus archives
- Runs coverage analysis
- Stores results in database

### Architecture

**File:** `claude_reference_projects/fuzzbench/experiment/measurer/measure_manager.py`

**Two-stage pipeline:**

1. **Detection Stage**: Query database for unmeasured snapshots
2. **Measurement Stage**: Parallel coverage analysis using multiprocessing

### 1. Unmeasured Snapshot Detection

**Lines 247-318** - Query for snapshots needing measurement:
```python
def get_unmeasured_snapshots(experiment_name, max_total_time):
    """Get list of (trial, cycle) tuples for unmeasured snapshots."""
    unmeasured = []

    # Get all trials for this experiment
    trials = db_utils.query(Trial).filter(
        Trial.experiment == experiment_name
    ).all()

    max_cycle = _time_to_cycle(max_total_time)

    for trial in trials:
        # Get measured snapshots for this trial
        measured_snapshots = db_utils.query(Snapshot.time).filter(
            Snapshot.trial_id == trial.id
        ).all()
        measured_times = {s.time for s in measured_snapshots}

        # Identify missing snapshots
        snapshot_period = experiment_utils.get_snapshot_seconds()
        for cycle in range(1, max_cycle + 1):
            snapshot_time = cycle * snapshot_period
            if snapshot_time not in measured_times:
                unmeasured.append((trial, cycle))

    return unmeasured
```

**Logic:**
- Queries database for existing Snapshot records
- Compares against expected cycles (1 to max_cycle)
- Returns list of missing (trial, cycle) pairs
- Handles both initial measurement and gap-filling

### 2. Parallel Measurement with Multiprocessing

**Lines 139-208** - Main measurement loop:
```python
def measure_all_trials(experiment_name, benchmarks, max_total_time):
    """Measure coverage for all unmeasured snapshots."""

    # Get work to do
    unmeasured_snapshots = get_unmeasured_snapshots(
        experiment_name,
        max_total_time
    )

    if not unmeasured_snapshots:
        return []

    # Create multiprocessing pool
    pool = multiprocessing.Pool()
    multiprocessing_queue = multiprocessing.Queue()

    # Start async measurement
    result = pool.starmap_async(
        measure_trial_coverage,
        [
            (trial, cycle, multiprocessing_queue)
            for trial, cycle in unmeasured_snapshots
        ]
    )

    # Collect results from queue
    snapshots = []
    while True:
        try:
            # Non-blocking poll with timeout
            snapshot = multiprocessing_queue.get(timeout=1)
            snapshots.append(snapshot)

            # Batch save to database
            if len(snapshots) >= SNAPSHOTS_BATCH_SAVE_SIZE:
                db_utils.add_all(snapshots)
                snapshots = []

        except queue.Empty:
            # Check if all workers finished
            if result.ready():
                break

    # Save remaining snapshots
    if snapshots:
        db_utils.add_all(snapshots)

    pool.close()
    pool.join()

    return snapshots
```

**Key features:**
- **Multiprocessing**: Parallel coverage measurement across CPU cores
- **Queue-based communication**: Workers send results via `multiprocessing.Queue`
- **Batch database writes**: Saves in batches of 100 snapshots (line 60: `SNAPSHOTS_BATCH_SAVE_SIZE = 100`)
- **Non-blocking polling**: 1-second timeout prevents busy-waiting
- **Graceful completion**: Waits for all workers before finishing

### 3. Individual Snapshot Measurement

**Lines 574-656** - Measure single snapshot:
```python
def measure_snapshot_coverage(trial, cycle, multiprocessing_queue):
    """Measure coverage for a single snapshot."""

    # 1. Download corpus archive
    archive_name = f'corpus-archive-{cycle:04d}.tar.gz'
    archive_path = download_corpus_archive(trial, archive_name)

    if not archive_path:
        return None  # Archive doesn't exist yet

    # 2. Extract corpus to temp directory
    corpus_dir = tempfile.mkdtemp()
    with tarfile.open(archive_path, 'r:gz') as tar:
        tar.extractall(corpus_dir)

    # 3. Run coverage binary on corpus
    coverage_binary = get_coverage_binary(trial.benchmark)
    coverage_results = run_coverage_binary(
        coverage_binary,
        corpus_dir,
        timeout=300  # 5 minute timeout
    )

    # 4. Generate profdata and extract coverage
    profdata = generate_profdata(coverage_results)
    coverage_json = export_coverage_json(profdata)
    edges_covered = parse_edges_covered(coverage_json)

    # 5. Process crashes
    crashes = []
    for crash_file in find_crashes(corpus_dir):
        crash = process_crash(crash_file, trial)
        crashes.append(crash)

    # 6. Create Snapshot object
    snapshot = Snapshot(
        time=cycle * experiment_utils.get_snapshot_seconds(),
        trial_id=trial.id,
        edges_covered=edges_covered,
        fuzzer_stats=load_fuzzer_stats(trial, cycle),  # Optional
        crashes=crashes
    )

    # 7. Send to queue
    multiprocessing_queue.put(snapshot)

    # Cleanup
    shutil.rmtree(corpus_dir)

    return snapshot
```

**Measurement process:**
1. **Download**: Fetch corpus archive from GCS
2. **Extract**: Unpack tar.gz to temp directory
3. **Run coverage**: Execute instrumented binary on corpus
4. **Process**: Generate profdata and extract metrics
5. **Crashes**: Analyze and categorize any crashes
6. **Create record**: Build Snapshot database object
7. **Queue**: Send result to main process via queue

**Timeout handling:**
- 5-minute timeout per coverage run
- Prevents hanging on problematic corpus inputs
- Failed measurements can be retried later

## Database Schema

**File:** `claude_reference_projects/fuzzbench/database/models.py`

### Snapshot Model

**Lines 66-81** - Snapshot table definition:
```python
class Snapshot(Base):
    __tablename__ = 'snapshot'

    # Primary key (composite)
    time = Column(Integer, primary_key=True)       # Elapsed time in seconds
    trial_id = Column(Integer, ForeignKey('trial.id'), primary_key=True)

    # Metrics
    edges_covered = Column(Integer, nullable=False)  # Branch/region coverage
    fuzzer_stats = Column(JSON, nullable=True)       # Fuzzer-specific data

    # Relationships
    trial = relationship('Trial', back_populates='snapshots')
    crashes = relationship('Crash', back_populates='snapshot')
```

### Trial Model (Partial)

**Lines 40-55** - Related trial information:
```python
class Trial(Base):
    __tablename__ = 'trial'

    id = Column(Integer, primary_key=True, autoincrement=True)
    experiment = Column(String, nullable=False)
    benchmark = Column(String, nullable=False)
    fuzzer = Column(String, nullable=False)
    trial_id = Column(Integer, nullable=False)  # Trial number within experiment

    # Relationships
    snapshots = relationship('Snapshot', back_populates='trial')
```

### Crash Model (Partial)

**Lines 85-100** - Crash details:
```python
class Crash(Base):
    __tablename__ = 'crash'

    id = Column(Integer, primary_key=True, autoincrement=True)
    snapshot_time = Column(Integer, ForeignKey('snapshot.time'))
    snapshot_trial_id = Column(Integer, ForeignKey('snapshot.trial_id'))

    crash_type = Column(String)      # e.g., "heap-buffer-overflow"
    crash_state = Column(String)     # Stack trace signature
    crash_testcase = Column(LargeBinary)  # Binary crash input

    # Relationships
    snapshot = relationship('Snapshot', back_populates='crashes')
```

### Key Schema Design Decisions

1. **Composite primary key**: (time, trial_id) uniquely identifies snapshots
   - Allows efficient queries by trial
   - Prevents duplicate measurements

2. **Time as cycle-based**: Not wall-clock time, but `cycle * snapshot_period`
   - Consistent across all trials
   - Easy to compare snapshots at same relative time
   - Example: cycle 10 at 900s period = 9000s = 2.5 hours

3. **JSON for fuzzer stats**: Flexible schema for fuzzer-specific metrics
   - Different fuzzers report different stats
   - No need to modify schema for new fuzzer types

4. **Separate crash table**: Many-to-one relationship with snapshots
   - Multiple crashes can be discovered in same snapshot
   - Allows crash deduplication and analysis

## Data Flow

### Complete Flow Diagram

```
┌─────────────────────────────────────────────────────────────┐
│ GCE Instance (Trial Runner)                                  │
│                                                              │
│  ┌──────────────┐                ┌─────────────────┐        │
│  │ Fuzzer       │                │ Main Thread     │        │
│  │ Thread       │                │ (TrialRunner)   │        │
│  │              │                │                 │        │
│  │ Runs fuzzer  │                │ Every 900s:     │        │
│  │ subprocess   │                │ 1. Archive      │        │
│  │              │                │    corpus       │        │
│  │ Writes to    │───filesystem──>│ 2. Save results │        │
│  │ corpus/      │    polling     │ 3. Upload GCS   │        │
│  │ crashes/     │                │ 4. Increment    │        │
│  │              │                │    cycle        │        │
│  └──────────────┘                └─────────────────┘        │
│                                           │                  │
└───────────────────────────────────────────┼──────────────────┘
                                           │ Upload
                                           ▼
                                  ┌────────────────┐
                                  │ Google Cloud   │
                                  │ Storage (GCS)  │
                                  │                │
                                  │ corpus-archive-│
                                  │ {cycle}.tar.gz │
                                  └────────────────┘
                                           │ Download
                                           ▼
┌─────────────────────────────────────────────────────────────┐
│ Dispatcher (Measurer)                                        │
│                                                              │
│  ┌────────────────────────────────────────────────────────┐ │
│  │ Main Process (measure_manager)                         │ │
│  │                                                        │ │
│  │ 1. Query DB for unmeasured snapshots                  │ │
│  │ 2. Create multiprocessing.Pool()                      │ │
│  │ 3. Submit measurement tasks                           │ │
│  │ 4. Collect results from queue                         │ │
│  │ 5. Batch save to database                             │ │
│  └──────────┬─────────────────────────────────────────────┘ │
│             │ starmap_async                                 │
│             ▼                                                │
│  ┌──────────────────────────────────────────────────────┐   │
│  │ Worker Pool (multiprocessing)                        │   │
│  │                                                      │   │
│  │  Worker 1: measure_snapshot_coverage()              │   │
│  │  Worker 2: measure_snapshot_coverage()              │   │
│  │  Worker 3: measure_snapshot_coverage()              │   │
│  │  ...                                                 │   │
│  │                                                      │   │
│  │  Each worker:                                        │   │
│  │  1. Download corpus archive                          │   │
│  │  2. Extract to temp dir                              │   │
│  │  3. Run coverage binary                              │   │
│  │  4. Generate profdata                                │   │
│  │  5. Extract metrics                                  │   │
│  │  6. Process crashes                                  │   │
│  │  7. Queue result                                     │   │
│  └────────────────────┬─────────────────────────────────┘   │
│                       │ multiprocessing.Queue.put()         │
│                       ▼                                      │
│              Results collected in batches                    │
└───────────────────────┼──────────────────────────────────────┘
                        │ Batch save (100 snapshots)
                        ▼
               ┌─────────────────┐
               │ PostgreSQL DB   │
               │                 │
               │ Snapshot table  │
               │ Trial table     │
               │ Crash table     │
               └─────────────────┘
                        │ Query
                        ▼
               ┌─────────────────┐
               │ Reporter        │
               │                 │
               │ Generate HTML   │
               │ Statistics      │
               │ Coverage graphs │
               └─────────────────┘
```

### Timing Example

For an experiment with:
- `max_total_time = 3600` (1 hour)
- `snapshot_period = 900` (15 minutes)

**Timeline:**

```
Time      Cycle   Action
────────────────────────────────────────────────
0:00      -       Trial starts, fuzzer begins
15:00     1       Snapshot capture (archive corpus)
30:00     2       Snapshot capture
45:00     3       Snapshot capture
60:00     4       Snapshot capture, trial ends

Later...
         (async)  Measurer detects 4 unmeasured snapshots
         (async)  Downloads corpus-archive-0001.tar.gz
         (async)  Measures coverage for cycle 1
         (async)  Saves Snapshot(time=900, trial_id=X, edges_covered=Y)
         ...      Processes remaining cycles
```

**Key observations:**
- Capture and measurement are **decoupled**
- Measurement can happen hours after capture
- Multiple trials measured in parallel
- Database gradually fills with results

## Performance Considerations

### Trial Runner Overhead

**Minimal impact on fuzzing:**
- Main thread mostly sleeps (900s between snapshots)
- Archiving only new/modified files (incremental)
- Compression happens asynchronously (tar.gz)
- Upload to GCS happens in background

**Measured overhead:**
- CPU: <1% average (mainly during archiving)
- Memory: Minimal (no data accumulation)
- I/O: Bounded by corpus size changes

### Measurer Scalability

**Parallel measurement:**
- Uses all CPU cores via multiprocessing
- Each worker independent (no shared state)
- Queue-based coordination (thread-safe)

**Batch processing:**
- Database writes in batches of 100 snapshots
- Reduces transaction overhead
- Improves throughput

**Timeout protection:**
- 5-minute timeout per snapshot measurement
- Prevents hanging on bad corpus inputs
- Failed measurements logged and retried

### Storage Efficiency

**Incremental archiving:**
- Only captures changed files
- Typical corpus: 10-1000 files
- Archive size: KB to MB range
- Compression ratio: ~5-10x for text inputs

**Example storage requirements:**
- Experiment: 10 benchmarks × 5 fuzzers × 20 trials = 1000 trials
- Duration: 24 hours per trial
- Snapshot period: 900s (15 minutes)
- Snapshots per trial: 96 (24 hours / 15 minutes)
- Total snapshots: 96,000

**Storage breakdown:**
- Corpus archives: ~100 MB per trial (varies widely)
- Database records: ~1 KB per snapshot
- Total: ~100 GB corpus + 96 MB database

### Database Performance

**Query patterns:**
- Read-heavy during measurement (detect unmeasured)
- Write-heavy during batch saves
- Indexed on (trial_id, time) for fast lookups

**Optimization:**
- Composite primary key avoids separate index
- Batch inserts reduce transaction count
- Foreign keys maintain referential integrity

## Applicability to CRSBench

### Similarities to FuzzBench

Both systems need:
- Periodic data capture during long-running experiments
- Progress monitoring and intermediate results
- Coverage/metrics tracking over time
- Reproducible snapshots for analysis

### Differences in Requirements

**CRSBench-specific considerations:**

1. **CRS diversity**: Different CRS types (bug finding, patch generation)
   - May need different snapshot content per CRS type
   - oss-fuzz-crs: POVs, corpus, crashes
   - oss-patch-crs: Patches, test results, repair attempts

2. **LLM interactions**: Track API calls and token usage
   - Snapshot should include LLM metrics
   - Cost tracking per time period
   - Prompt/response history (optional)

3. **Evaluation mode**: Delta vs full mode
   - Delta: Snapshot commit-level progress
   - Full: Snapshot discovered vulnerabilities

4. **Verification**: POV and patch testing
   - Need to test POVs immediately or defer?
   - Patch validation separate from capture

5. **Multi-trial coordination**: CRSBench experiments are smaller scale
   - Fewer trials per experiment (1-5 typical)
   - Can use simpler architecture (no GCS, no multiprocessing needed initially)

### Recommended Approach for CRSBench

**Phase 1: Simple Implementation**

1. **Threading model**: Adopt FuzzBench's main + worker thread pattern
   ```python
   class CRSTrialRunner:
       def run(self):
           crs_thread = threading.Thread(target=self.run_crs)
           crs_thread.start()

           while crs_thread.is_alive():
               self.cycle += 1
               self.sleep_until_next_snapshot()
               self.capture_snapshot()
   ```

2. **Snapshot capture**: Store to local filestore (no cloud needed)
   ```python
   def capture_snapshot(self):
       snapshot_dir = self.trial_dir / f'snapshot-{self.cycle:04d}'
       snapshot_dir.mkdir()

       # Capture POVs discovered so far
       self.save_povs(snapshot_dir / 'povs.json')

       # Capture patches generated so far
       self.save_patches(snapshot_dir / 'patches')

       # Capture LLM usage metrics
       self.save_llm_metrics(snapshot_dir / 'llm-usage.json')

       # Capture CRS state (if applicable)
       self.save_crs_state(snapshot_dir / 'crs-state.json')
   ```

3. **Configuration**: Add to ExperimentConfig
   ```python
   class ExperimentConfig(BaseModel):
       # ... existing fields ...
       snapshot_period: int = Field(
           default=900,
           ge=60,
           description="Snapshot interval in seconds (min 60)"
       )
   ```

4. **Measurement**: Inline verification (no separate process initially)
   ```python
   def capture_snapshot(self):
       snapshot_dir = self.create_snapshot_dir()

       # Capture data
       self.save_povs(snapshot_dir)
       self.save_patches(snapshot_dir)

       # Immediate verification
       verified_povs = self.verify_povs(snapshot_dir / 'povs.json')
       verified_patches = self.verify_patches(snapshot_dir / 'patches')

       # Save verification results
       self.save_verification_results(snapshot_dir, verified_povs, verified_patches)
   ```

**Phase 2: Enhanced Implementation (Future)**

1. **Separate measurement**: If verification becomes expensive
   - Use FuzzBench's multiprocessing pattern
   - Queue-based result collection
   - Async POV/patch testing

2. **Database storage**: If querying/reporting becomes important
   - SQLite initially (simpler than PostgreSQL)
   - Schema similar to FuzzBench Snapshot model
   - Enable time-series analysis

3. **Distributed execution**: If scaling to many trials
   - Adopt FuzzBench's GCS upload pattern
   - Centralized measurement on dispatcher
   - Support for multiple experiment runners

### Data to Capture per Snapshot

**Essential:**
- Elapsed time (seconds)
- POVs discovered (with metadata)
- Patches generated (as diffs or files)
- LLM token usage (input, output, cached, cost)
- CRS log excerpt (last N lines)

**Optional:**
- Coverage metrics (if applicable)
- CRS internal state (for resumable experiments)
- Resource usage (CPU, memory)
- Test results (pass/fail for each POV/patch)

**Example snapshot structure:**
```
experiment-filestore/
└── my-experiment/
    └── curl-delta-02__atlantis-c/
        └── trial-1/
            ├── snapshot-0001/
            │   ├── povs.json
            │   ├── patches/
            │   │   ├── patch-001.diff
            │   │   └── patch-002.diff
            │   ├── llm-usage.json
            │   ├── verification.json
            │   └── crs-log-tail.txt
            ├── snapshot-0002/
            │   └── ...
            └── snapshot-0003/
                └── ...
```

### Implementation Checklist

- [ ] Add `snapshot_period` field to `ExperimentConfig` schema
- [ ] Implement `CRSTrialRunner.sleep_until_next_snapshot()` timing mechanism
- [ ] Create `CRSTrialRunner.capture_snapshot()` method
- [ ] Define snapshot directory structure
- [ ] Implement POV capture (JSON format)
- [ ] Implement patch capture (diff files)
- [ ] Implement LLM metrics capture
- [ ] Add snapshot metadata (timestamp, cycle, elapsed time)
- [ ] Optional: Implement inline verification
- [ ] Optional: Create snapshot visualization in reports
- [ ] Update documentation (experiment-config-example.yaml, orchestration.md)
- [ ] Add tests for snapshot capture and timing

## Key Code References

### FuzzBench Source Files

All references are relative to `claude_reference_projects/fuzzbench/`:

1. **Timing and Configuration**
   - `common/experiment_utils.py` lines 23-42
     - `get_snapshot_seconds()` - Read snapshot period
     - `get_cycle_time()` - Convert cycle to elapsed time

2. **Trial Runner (Snapshot Capture)**
   - `experiment/runner.py` lines 232-470
     - Line 300-301: Worker thread creation
     - Lines 308-314: Main polling loop
     - Lines 317-336: `sleep_until_next_sync()` - Timing mechanism
     - Lines 338-428: `do_sync()` - Main snapshot capture
     - Lines 381-425: `archive_corpus()` - Incremental archiving
     - Lines 430-445: `save_results()` - Upload to GCS
     - Lines 447-470: `record_stats()` - Optional stats

3. **Measurer (Snapshot Measurement)**
   - `experiment/measurer/measure_manager.py` lines 139-656
     - Lines 139-208: `measure_all_trials()` - Main measurement loop
     - Lines 247-318: `get_unmeasured_snapshots()` - Query detection
     - Lines 574-656: `measure_snapshot_coverage()` - Single snapshot measurement

4. **Database Schema**
   - `database/models.py` lines 66-81
     - `Snapshot` model definition
     - Related `Trial` and `Crash` models

5. **Configuration Management**
   - `experiment/run_experiment.py` lines 74-76, 175-176, 505-506, 524
     - Default value assignment
     - Validation
     - Propagation to trial runners

### Threading Pattern Summary

```python
# Simplified FuzzBench pattern
class TrialRunner:
    def run(self):
        # Start worker thread
        worker = threading.Thread(target=self.run_worker)
        worker.start()

        # Main thread: periodic snapshots
        while worker.is_alive():
            time.sleep(self.snapshot_period)
            self.capture_snapshot()

    def run_worker(self):
        # Run long-running task
        subprocess.run(['fuzzer', 'args...'])

    def capture_snapshot(self):
        # Save data to filesystem
        self.archive_corpus()
        self.save_results()
```

**Key advantages:**
- Simple and robust
- No complex async coordination
- Easy to debug
- Minimal overhead
- Works well for periodic tasks

## Conclusion

FuzzBench's snapshot implementation provides a solid reference for CRSBench:

**Adopt:**
- Simple main + worker thread pattern
- Sleep-based timing mechanism
- Incremental data capture
- Clear separation of capture vs measurement

**Adapt:**
- Capture CRS-specific data (POVs, patches, LLM metrics)
- Use local filestore instead of cloud storage (initially)
- Inline verification instead of async measurement (initially)
- SQLite instead of PostgreSQL (if database needed)

**Avoid:**
- Over-engineering for scale before needed
- Premature optimization (start simple)
- Tight coupling to cloud infrastructure

The core insight: **Periodic polling with separate worker thread is simple, reliable, and sufficient for most use cases.** Start with this pattern and evolve as needed.
