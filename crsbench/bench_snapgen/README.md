# Benchmark Snapshot Generator (bench_snapgen)

Generate realistic trial snapshots from CRSBench benchmark ground truth data for testing, demonstration, and validation purposes.

## Overview

The `bench_snapgen` module simulates realistic CRS trial progression by:

1. **Loading ground truth** from benchmark `.aixcc/` directories (POVs, patches, metadata)
2. **Modeling discovery timelines** with difficulty-based timing (when POVs/patches are "discovered")
3. **Generating snapshot archives** at configured intervals with incremental capture

Unlike runtime snapshot generation (which captures actual CRS execution), `bench_snapgen` simulates trial progression from ground truth.

## Quick Start

### Basic Usage

```bash
# Generate snapshots from a benchmark
python -m crsbench.bench_snapgen.generator \
    --benchmark benchmarks/afc-curl-delta-01 \
    --output /tmp/simulated-trial
```

This creates snapshots in `/tmp/simulated-trial/`:

```
/tmp/simulated-trial/
├── snapshot-0001.tar.gz
├── snapshot-0001.complete
├── snapshot-0002.tar.gz
├── snapshot-0002.complete
...
```

### Python API

```python
from pathlib import Path
from crsbench.bench_snapgen import BenchmarkSnapshotGenerator

generator = BenchmarkSnapshotGenerator(
    benchmark_path=Path("benchmarks/afc-curl-delta-01"),
    output_dir=Path("/tmp/trial"),
    trial_duration=7200,      # 2 hours
    snapshot_period=900       # 15 minutes
)

output_dir = generator.generate_trial_snapshots(
    mode='bug-finding',
    difficulty_level=2
)

print(f"Snapshots generated in: {output_dir}")
```

## Features

### Generation Modes

**Bug-Finding Mode** (default):
- Simulates POV discovery only
- Use for testing vulnerability detection pipelines

```bash
python -m crsbench.bench_snapgen.generator \
    --benchmark benchmarks/afc-curl-delta-01 \
    --output /tmp/trial \
    --mode bug-finding
```

**Patch-Generation Mode**:
- Simulates both POV discovery and patch generation
- Patches appear after POVs with realistic delay

```bash
python -m crsbench.bench_snapgen.generator \
    --benchmark benchmarks/afc-curl-delta-01 \
    --output /tmp/trial \
    --mode patch-generation
```

### Difficulty Levels

Control discovery timing with difficulty levels 1-5:

| Level | Description | POV Discovery Time | Patch Delay |
|-------|-------------|-------------------|-------------|
| 1 | Easy | 10-30% of trial | 5-15 min |
| 2 | Medium-Low | 25-50% of trial | 10-20 min |
| 3 | Medium | 30-60% of trial | 15-30 min |
| 4 | Hard | 50-75% of trial | 20-40 min |
| 5 | Very Hard | 50-90% of trial | 30-60 min |

```bash
python -m crsbench.bench_snapgen.generator \
    --benchmark benchmarks/afc-curl-delta-01 \
    --output /tmp/trial \
    --difficulty 3
```

### Fault Injection

Test evaluation robustness by injecting invalid POVs/patches:

```bash
python -m crsbench.bench_snapgen.generator \
    --benchmark benchmarks/afc-curl-delta-01 \
    --output /tmp/trial \
    --fault-injection-rate 0.10  # 10% invalid data
```

**Injected fault types:**
- **Invalid POVs**: Random data that won't trigger crashes
- **Invalid Patches**:
  - `syntax_error`: Malformed diff format
  - `wrong_file`: Patches non-existent files
  - `incomplete`: Partial fixes
  - `breaks_build`: Introduces syntax errors

### Custom Timing

Adjust trial duration and snapshot frequency:

```bash
python -m crsbench.bench_snapgen.generator \
    --benchmark benchmarks/afc-curl-delta-01 \
    --output /tmp/trial \
    --duration 3600 \         # 1 hour trial
    --snapshot-period 600      # 10 minute snapshots
```

### Multiple Trials

Generate multiple independent trials:

```bash
python -m crsbench.bench_snapgen.generator \
    --benchmark benchmarks/afc-curl-delta-01 \
    --output /tmp/trials \
    --trials 3
```

Creates:
```
/tmp/trials/
├── trial-001/
│   ├── snapshot-0001.tar.gz
│   └── ...
├── trial-002/
│   └── ...
└── trial-003/
    └── ...
```

## Snapshot Format

Each snapshot archive (`snapshot-NNNN.tar.gz`) contains:

```
snapshot-0001.tar.gz:
├── metadata.json          # Cycle, timestamp, elapsed time
├── config.yaml            # Experiment configuration
├── execution.json         # Trial metadata
├── llm-usage.json         # Simulated LLM metrics
├── crs-output.log         # Simulated CRS log
├── povs/                  # Incremental: new POVs only
│   ├── pov_0
│   └── pov_1
└── patches/               # Incremental: new patches only
    └── cpv_0/
        └── patch_0.diff
```

**Incremental vs Full Capture:**

| Data Type | Strategy | Tracking |
|-----------|----------|----------|
| POVs | Incremental | Filename set |
| Patches | Incremental | Filename set |
| Logs | Full | Complete at each snapshot |
| Config | Full | Static |

## Use Cases

### 1. Testing Evaluation Pipelines

Validate snapshot processing without expensive CRS runs:

```python
from crsbench.bench_snapgen import BenchmarkSnapshotGenerator

# Generate test snapshots
generator = BenchmarkSnapshotGenerator(
    benchmark_path=Path("benchmarks/afc-curl-delta-01"),
    output_dir=Path("/tmp/test-trial")
)
generator.generate_trial_snapshots(mode='bug-finding', difficulty_level=1)

# Test your evaluation pipeline
from my_evaluator import process_snapshots
results = process_snapshots("/tmp/test-trial")
```

### 2. Demonstration

Show snapshot format with real data:

```bash
# Generate snapshots for documentation
python -m crsbench.bench_snapgen.generator \
    --benchmark benchmarks/afc-curl-delta-01 \
    --output docs/examples/trial \
    --duration 1800 \
    --snapshot-period 600
```

### 3. Deduplication Testing

Test POV deduplication with fault injection:

```bash
python -m crsbench.bench_snapgen.generator \
    --benchmark benchmarks/afc-curl-delta-01 \
    --output /tmp/dedup-test \
    --fault-injection-rate 0.20  # 20% invalid POVs
```

### 4. Patch Validation

Test patch validation pipelines:

```bash
python -m crsbench.bench_snapgen.generator \
    --benchmark benchmarks/afc-curl-delta-01 \
    --output /tmp/patch-test \
    --mode patch-generation \
    --fault-injection-rate 0.15  # Include invalid patches
```

## CLI Reference

### Required Arguments

- `--benchmark PATH`: Benchmark directory containing `.aixcc/`
- `--output PATH`: Output directory for snapshots

### Optional Arguments

**Timing:**
- `--duration SECONDS`: Trial duration (default: 7200)
- `--snapshot-period SECONDS`: Snapshot interval (default: 900)

**Mode and Difficulty:**
- `--mode {bug-finding,patch-generation}`: Generation mode (default: bug-finding)
- `--difficulty {1,2,3,4,5}`: Difficulty level (default: 1)

**Fault Injection:**
- `--fault-injection-rate FLOAT`: Invalid data rate 0.0-1.0 (default: 0.0)

**Multiple Trials:**
- `--trials COUNT`: Generate N independent trials (default: 1)

### Examples

```bash
# Patch mode, medium difficulty
python -m crsbench.bench_snapgen.generator \
    --benchmark benchmarks/atlanta-activemq-delta-01 \
    --output /tmp/patch-trial \
    --mode patch-generation \
    --difficulty 3

# Late discovery with high fault injection
python -m crsbench.bench_snapgen.generator \
    --benchmark benchmarks/afc-curl-delta-01 \
    --output /tmp/late-discovery \
    --difficulty 5 \
    --fault-injection-rate 0.25

# Multiple trials at different difficulties
for diff in {1..5}; do
    python -m crsbench.bench_snapgen.generator \
        --benchmark benchmarks/afc-curl-delta-01 \
        --output /tmp/trials/difficulty-$diff \
        --difficulty $diff
done
```

## Architecture

### Module Structure

```
crsbench/bench_snapgen/
├── __init__.py           # Public API
├── generator.py          # Main generator & CLI
├── timeline.py           # Discovery timing models
├── fault_injection.py    # Invalid data generation
├── builder.py            # Snapshot archive creation
└── README.md             # This file
```

### Key Components

**BenchmarkSnapshotGenerator**:
- Orchestrates snapshot generation
- Loads ground truth from `.aixcc/`
- Creates discovery timeline
- Generates snapshots at intervals

**DiscoveryTimeline**:
- Models POV/patch discovery over time
- Difficulty-based timing
- POV clustering (multiple POVs from same root cause)

**SnapshotBuilder**:
- Creates snapshot archives
- Incremental tracking for POVs/patches
- Full capture for logs/config

**FaultInjector**:
- Generates invalid POVs (random data)
- Generates invalid patches (various fault types)

## Testing

Run tests:

```bash
# All bench_snapgen tests
uv run pytest tests/test_bench_snapgen.py -v

# Specific test class
uv run pytest tests/test_bench_snapgen.py::TestDiscoveryTimeline -v

# With coverage
uv run pytest tests/test_bench_snapgen.py --cov=crsbench.bench_snapgen
```

## Comparison to Other Tools

### vs. Runtime SnapshotManager

| Aspect | SnapshotManager | bench_snapgen |
|--------|----------------|---------------|
| Purpose | Capture live CRS execution | Simulate from ground truth |
| Data Source | Running CRS container | Benchmark `.aixcc/` |
| Timing | Real-time capture | Simulated discovery |
| Use Case | Production trials | Testing/demonstration |

### vs. snapshot-examples/

| Aspect | snapshot-examples | bench_snapgen |
|--------|------------------|---------------|
| Data | Hardcoded dummy data | Real benchmark ground truth |
| Timing | Fake timestamps | Realistic difficulty-based timing |
| Purpose | Format demonstration | Production-quality simulation |

## Implementation Details

### Ground Truth Loading

Reads from benchmark `.aixcc/` structure:

```
.aixcc/
├── meta.yaml
└── [harness_name]/
    └── cpv_N/
        ├── blobs/pov_N.blob
        ├── patches/patch_N.diff
        ├── logs/pov_N.log
        └── hints/level_N.sarif
```

### Discovery Timing Model

**POV Discovery:**
- Difficulty level maps to discovery time range (% of trial)
- Multiple POVs for same vulnerability clustered with ±5-10% jitter
- Simulates finding variations after initial discovery

**Patch Generation:**
- Appears after first POV with difficulty-based delay
- Delay increases with difficulty (5-60 minutes)
- Never exceeds trial duration

### Incremental Tracking

```python
class SnapshotBuilder:
    captured_povs: Set[str]      # Track by filename
    captured_patches: Set[str]   # Track by path

    def _write_incremental_povs(events):
        for event in events:
            if pov_id not in captured_povs:
                write_pov(pov_id)
                captured_povs.add(pov_id)
```

## Troubleshooting

### No POVs found

```
ERROR: Loaded 0 POVs from benchmark
```

**Solution:** Check benchmark structure:
```bash
ls benchmarks/afc-curl-delta-01/.aixcc/
ls benchmarks/afc-curl-delta-01/.aixcc/*/cpv_*/blobs/
```

### Invalid meta.yaml

```
ValueError: Invalid meta.yaml
```

**Solution:** Validate benchmark:
```bash
python -m crsbench.validation.format_validator \
    benchmarks/afc-curl-delta-01
```

### Snapshot archive corruption

```
ERROR: Failed to open archive
```

**Solution:** Check disk space and permissions:
```bash
df -h /tmp
ls -la /tmp/trial/
```

## Contributing

When adding features:

1. Update design doc: `design-docs/bench_snapgen/bench_snapgen.md`
2. Add tests to `tests/test_bench_snapgen.py`
3. Update this README
4. Run tests: `uv run pytest tests/test_bench_snapgen.py -v`

## See Also

- **Design Doc**: `design-docs/bench_snapgen/bench_snapgen.md`
- **Snapshot Format**: `docs/benchmark-spec.md`
- **Runtime Snapshots**: `crsbench/evaluation/snapshot_manager.py`
- **Validation**: `crsbench/validation/`
