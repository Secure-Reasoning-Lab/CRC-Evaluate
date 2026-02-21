# Snapshot System - User Guide and Examples

This guide explains how to use CRSBench's snapshot system to monitor CRS trial progress.

## Overview

Snapshots are periodic captures of CRS trial state during execution, enabling:
- **Progress monitoring** - Track POV discovery over time
- **Early debugging** - Detect issues before trial completion
- **Resource tracking** - Monitor LLM token usage patterns
- **Partial results** - Recover data from interrupted trials
- **Time-series analysis** - Understand CRS behavior dynamics

## Quick Start

Add `snapshot_period` to your experiment config:

```yaml
# experiment-config.yaml
experiment: my-experiment
trials: 3
max_total_time: 7200  # 2 hours
snapshot_period: 600   # Snapshot every 10 minutes
difficulty_level: 1
experiment_filestore: /tmp/crsbench/experiments
report_filestore: /tmp/crsbench/reports
crses:
  - atlantis-c
benchmark_suite: crsbench-afc-c
```

Run experiment:
```bash
crsbench run --experiment-config experiment-config.yaml
```

Snapshots will be saved in:
```
{experiment_filestore}/{experiment}/
└── atlantis-c_benchmark-name_trial0/
    ├── snapshot-0001.tar.gz
    ├── snapshot-0001.complete
    ├── snapshot-0002.tar.gz
    ├── snapshot-0002.complete
    └── ...
```

## Configuration

### snapshot_period

**Type**: Integer (seconds)
**Default**: 900 (15 minutes)
**Range**: 0 (disabled), or 60-86400 seconds

**Examples**:
```yaml
# Disable snapshots
snapshot_period: 0

# Short trials - snapshot every 5 minutes
snapshot_period: 300

# Standard - snapshot every 15 minutes (default)
snapshot_period: 900

# Long trials - snapshot every 30 minutes
snapshot_period: 1800
```

### Choosing the Right Period

**For short trials (<1 hour)**:
- Use 300-600s (5-10 minutes)
- Need fine-grained monitoring
- Storage overhead is minimal

**For standard trials (2-6 hours)**:
- Use 900-1800s (15-30 minutes)
- Default 900s works well
- Good balance of monitoring vs storage

**For long trials (>12 hours)**:
- Use 1800-3600s (30-60 minutes)
- Minimize storage overhead
- Still get enough progress visibility

**When to disable (snapshot_period: 0)**:
- Very short trials (<15 minutes)
- Testing/development
- Storage constrained environments

## Snapshot Contents

Each snapshot archive contains:

### Metadata Files (Full - Always Complete)
- `metadata.json` - Snapshot cycle, timestamp, elapsed time
- `config.yaml` - Experiment configuration
- `execution.json` - Trial execution metadata
- `llm-usage.json` - Cumulative LLM metrics
- `crs-output.log` - Complete CRS output log

### Trial Outputs (Incremental - Only New Data)
- `povs/` - New POVs discovered since last snapshot
- `patches/pov_N/` - New patches organized by POV ID
- `seeds/` - New/modified seed files
- `crs-data/` - CRS-specific outputs (if any)

## Example Configurations

### Example 1: Standard Evaluation

```yaml
# config-standard.yaml
experiment: standard-eval
trials: 3
max_total_time: 7200  # 2 hours
snapshot_period: 900   # Every 15 minutes (8 snapshots per trial)
difficulty_level: 1
experiment_filestore: /data/experiments
report_filestore: /data/reports
crses:
  - atlantis-c
  - atlantis-multilang
benchmarks:
  - libjpeg-turbo
  - libxml2
```

**Expected output**:
- 6 trials (2 CRS × 3 trials)
- ~8 snapshots per trial
- ~48 snapshots total
- ~5-10 GB storage

### Example 2: Quick Testing

```yaml
# config-quick-test.yaml
experiment: quick-test
trials: 1
max_total_time: 1800  # 30 minutes
snapshot_period: 300   # Every 5 minutes (6 snapshots)
difficulty_level: 0
experiment_filestore: /tmp/crsbench/test-exp
report_filestore: /tmp/crsbench/test-reports
crses:
  - test-crs
benchmarks:
  - test-benchmark
```

**Expected output**:
- 1 trial
- ~6 snapshots
- ~100 MB storage

### Example 3: Long-Running Experiment

```yaml
# config-long-run.yaml
experiment: long-eval
trials: 5
max_total_time: 86400  # 24 hours
snapshot_period: 3600   # Every 1 hour (24 snapshots per trial)
difficulty_level: 2
experiment_filestore: /mnt/storage/experiments
report_filestore: /mnt/storage/reports
crses:
  - advanced-crs
benchmark_suite: crsbench-all
```

**Expected output**:
- Many trials (depends on suite)
- ~24 snapshots per trial
- Large storage requirements

### Example 4: No Snapshots (Testing Only)

```yaml
# config-no-snapshots.yaml
experiment: no-snapshots
trials: 1
max_total_time: 600
snapshot_period: 0  # Disabled
difficulty_level: 0
experiment_filestore: /tmp/crsbench/exp
report_filestore: /tmp/crsbench/reports
crses:
  - test-crs
benchmarks:
  - simple-benchmark
```

**Use case**: Quick testing, development, or when snapshots not needed

## Inspecting Snapshots

### Using the Snapshot Generator Tool

```bash
# List all snapshots in a trial directory
python snapshot-examples/generate_snapshot.py --list /path/to/trial_dir

# List detailed contents of a specific snapshot
python snapshot-examples/generate_snapshot.py --list-snapshot /path/to/snapshot-0001.tar.gz

# Validate snapshot structure
python snapshot-examples/generate_snapshot.py --validate /path/to/trial_dir
```

### Manual Inspection

```bash
# List archive contents
tar -tzf snapshot-0001.tar.gz

# Extract snapshot
tar -xzf snapshot-0001.tar.gz

# View metadata
cat metadata.json

# View LLM usage
cat llm-usage.json

# Count POVs in this snapshot
ls -1 povs/ | wc -l

# View CRS log
less crs-output.log
```

### Python API

```python
from crsbench.evaluation.snapshot import (
    list_snapshots,
    inspect_snapshot,
    extract_snapshot,
    load_snapshot_metadata
)
from pathlib import Path

# List all snapshots
trial_dir = Path("/data/experiments/my-exp/trial0")
snapshots = list_snapshots(trial_dir)

for snapshot in snapshots:
    print(f"Snapshot {snapshot.cycle}:")
    print(f"  Complete: {snapshot.is_complete}")
    print(f"  Size: {snapshot.archive_size_bytes} bytes")

    # Load metadata
    if snapshot.is_complete:
        metadata = load_snapshot_metadata(snapshot.archive_path)
        print(f"  Elapsed: {metadata.elapsed_time}s")

# Inspect specific snapshot
summary = inspect_snapshot(trial_dir / "snapshot-0005.tar.gz")
print(f"Files: {summary.file_count}")
print(f"Cycle: {summary.metadata.cycle}")

# Extract for analysis
extract_snapshot(
    archive_path=trial_dir / "snapshot-0005.tar.gz",
    extract_dir=Path("/tmp/snapshot-analysis")
)
```

## Understanding Incremental Capture

Snapshots use **incremental capture** to minimize storage:

### Snapshot 1 (15 minutes):
```
povs/
  pov_001  ← NEW
  pov_002  ← NEW
patches/
  pov_0/
    patch.diff  ← NEW
```

### Snapshot 2 (30 minutes):
```
povs/
  pov_003  ← NEW (only this one)
patches/
  pov_1/
    patch.diff  ← NEW
  pov_2/
    patch.diff  ← NEW
```

### Snapshot 3 (45 minutes):
```
povs/
  pov_004  ← NEW
  pov_005  ← NEW
patches/
  pov_3/
    patch.diff  ← NEW
```

**Result**: Each snapshot only contains NEW data, but logs are complete.

To reconstruct the full state at snapshot 3:
1. Extract all 3 snapshots
2. Merge POVs: `pov_001, pov_002, pov_003, pov_004, pov_005`
3. Merge patches: `pov_0/, pov_1/, pov_2/, pov_3/`
4. Use logs from snapshot 3 (already complete)

## Storage Estimates

**Per snapshot** (typical):
- Metadata/config: ~1-5 KB
- LLM usage log: ~1-10 KB
- CRS log: ~10-100 KB (grows over time)
- POVs (incremental): ~1-50 KB (few new POVs per cycle)
- Patches (incremental): ~1-20 KB (few new patches per cycle)
- Corpus (incremental): ~10-100 KB

**Compressed**: ~50-500 KB per snapshot

**For a 2-hour trial with 15-min snapshots**:
- 8 snapshots
- ~4-8 MB total (compressed)

**For a 24-hour trial with 1-hour snapshots**:
- 24 snapshots
- ~50-100 MB total (compressed)

## Troubleshooting

### No snapshots are created

**Check**:
1. Is `snapshot_period` > 0 in config?
2. Does trial run long enough (>= snapshot_period)?
3. Check trial output directory exists
4. Check logs for snapshot errors

### Snapshots incomplete (.complete marker missing)

**Cause**: Trial crashed or was killed during snapshot capture

**Solution**: Ignore incomplete snapshots (no .complete marker)

### Large snapshot files

**Check**:
- CRS log growing very large?
- Corpus directory very large?
- Many POVs discovered?

**Solution**: Increase `snapshot_period` to reduce frequency

### Permission errors

**Check**: Trial output directory permissions
**Solution**: Ensure CRSBench can write to experiment_filestore

## Best Practices

1. **Start with default (900s)** - Works for most use cases
2. **Test snapshot period** - Run short trial to verify settings
3. **Monitor storage** - Check disk usage during long experiments
4. **Keep .complete markers** - Only use snapshots with completion markers
5. **Archive old snapshots** - Compress/move after trial completes
6. **Use for debugging** - Check early snapshots if trial fails
7. **Validate critical snapshots** - Use validation tool before relying on data

## Advanced Usage

### Snapshot-Based Monitoring Script

```python
#!/usr/bin/env python3
"""Monitor trial progress via snapshots."""

import time
from pathlib import Path
from crsbench.evaluation.snapshot import list_snapshots, load_snapshot_metadata

def monitor_trial(trial_dir: Path, check_interval: int = 60):
    """Monitor trial progress by checking snapshots."""
    last_cycle = 0

    while True:
        snapshots = list_snapshots(trial_dir)

        if not snapshots:
            print("No snapshots yet...")
            time.sleep(check_interval)
            continue

        latest = snapshots[-1]

        if latest.cycle > last_cycle and latest.is_complete:
            metadata = load_snapshot_metadata(latest.archive_path)

            print(f"[{metadata.cycle}] Elapsed: {metadata.elapsed_time:.0f}s")
            print(f"  Snapshot: {latest.archive_path.name}")
            print(f"  Size: {latest.archive_size_bytes / 1024:.1f} KB")

            last_cycle = latest.cycle

        time.sleep(check_interval)

if __name__ == "__main__":
    monitor_trial(Path("/data/experiments/my-exp/trial0"))
```

### Progress Dashboard

```python
from crsbench.evaluation.snapshot import list_snapshots, extract_snapshot
from pathlib import Path
import json

def generate_progress_dashboard(trial_dir: Path):
    """Generate progress dashboard from snapshots."""
    snapshots = list_snapshots(trial_dir)

    pov_counts = []
    timestamps = []

    for snapshot in snapshots:
        if not snapshot.is_complete:
            continue

        # Extract to temp location
        temp_dir = Path(f"/tmp/snapshot-{snapshot.cycle}")
        extract_snapshot(snapshot.archive_path, temp_dir)

        # Count POVs
        pov_dir = temp_dir / "povs"
        pov_count = len(list(pov_dir.glob("*"))) if pov_dir.exists() else 0

        # Get timestamp
        metadata_path = temp_dir / "metadata.json"
        with open(metadata_path) as f:
            metadata = json.load(f)

        pov_counts.append(pov_count)
        timestamps.append(metadata['elapsed_time'])

        # Cleanup
        import shutil
        shutil.rmtree(temp_dir)

    # Plot or display
    for t, count in zip(timestamps, pov_counts):
        print(f"{t:6.0f}s: {'#' * count} ({count} POVs)")

if __name__ == "__main__":
    generate_progress_dashboard(Path("/data/experiments/my-exp/trial0"))
```

## See Also

- [Design Documentation](./design/evaluation/snapshots.md) - Technical details
- [Sample Snapshots](../snapshot-examples/) - Example snapshots for testing
- [API Reference](../crsbench/evaluation/snapshot.py) - Python API documentation
