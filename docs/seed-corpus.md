# Seed Corpus Collection and Reuse

This document describes how to collect corpus files from CRS experiment output and reuse them as seeds in subsequent experiments.

## Overview

During CRS execution, fuzzers discover new inputs that trigger new code coverage. These corpus files can be collected and reused to accelerate future experiments by starting with known-good inputs.

## Workflow

```
1. Run initial experiment
   └─> CRS discovers corpus files during fuzzing

2. Collect corpus files
   └─> crsbench benchmark seed-import <experiment-dir>

3. Reuse in future experiments
   └─> Enable seed_corpus_enabled in config
```

## Collecting Seeds

After an experiment completes, collect corpus files using:

```bash
crsbench benchmark seed-import <experiment-dir> [--benchmarks <path>] [--force]
```

### Arguments

| Argument | Description |
|----------|-------------|
| `experiment-dir` | Path to experiment output directory (e.g., `./experiment-data/trial-1/`) |
| `--benchmarks` | Directory containing benchmarks (default: `./benchmarks`) |
| `--force` | Overwrite existing corpus directory |

### Example

```bash
# Collect from experiment output
crsbench benchmark seed-import ./experiment-data/e2e-bugfind/afc-curl-delta-02/curl_fuzzer/delta/address/

# With custom benchmarks path
crsbench benchmark seed-import ./experiment-data/... --benchmarks /path/to/benchmarks
```

### Output Structure

Seeds are stored in the benchmark's `.aixcc/{harness}/corpus/` directory:

```
benchmarks/afc-curl-delta-02/.aixcc/curl_fuzzer_ws/corpus/
├── manifest.json       # Metadata for all files
├── 00c3d860b1a0b8da    # Corpus file (named by content hash)
├── 00d6dca7560f6081
└── ...
```

### Manifest Format

The `manifest.json` contains metadata for each file:

```json
{
  "crs_run_start_time": 1769748907.0,
  "files": {
    "00c3d860b1a0b8da": {
      "relative_time": 283.3,
      "original_name": "2a0ac780c371363b",
      "size": 26
    }
  }
}
```

- `crs_run_start_time`: Unix timestamp when CRS run started
- `relative_time`: Seconds after CRS start when this file was discovered
- `original_name`: Original filename from fuzzer
- `size`: File size in bytes

## Using Seeds in Experiments

Enable seed corpus in your experiment config:

```yaml
# Enable seed corpus from collected files
seed_corpus_enabled: true

# Optional: only use seeds discovered within first hour
seed_corpus_max_time: 3600
```

### Configuration Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `seed_corpus_enabled` | bool | `false` | Enable seed corpus |
| `seed_corpus_max_time` | int | `null` | Max relative time (seconds) for filtering seeds |

### Time-based Filtering

The `seed_corpus_max_time` option filters seeds based on when they were discovered:

```yaml
# Use all collected seeds
seed_corpus_enabled: true
seed_corpus_max_time: null

# Use only seeds from first hour
seed_corpus_enabled: true
seed_corpus_max_time: 3600

# Use only seeds from first 5 minutes
seed_corpus_enabled: true
seed_corpus_max_time: 300
```

This is useful for:
- Replicating early-stage findings without full corpus
- Testing how quickly CRS finds bugs with vs without seed assistance
- Reducing experiment startup time with smaller seed sets

## How It Works

At runtime, when `seed_corpus_enabled: true`:

1. `SeedCorpusPreparer` reads `manifest.json` from `.aixcc/{harness}/corpus/`
2. Filters files by `relative_time <= seed_corpus_max_time` (if configured)
3. Copies selected files to `trial_dir/seed_corpus/`
4. Passes directory to CRS via `oss-bugfind-crs run --corpus <path>`

The CRS then uses these files as initial fuzzing seeds, potentially finding bugs faster.

## Best Practices

1. **Collect from successful runs**: Seeds from runs that found vulnerabilities are most valuable
2. **Use time filtering**: Start with shorter time windows (1h) and expand if needed
3. **Per-harness collection**: Seeds are collected per-harness, so each harness gets relevant seeds
4. **Deduplication**: Files are stored by content hash, avoiding duplicates across collections
