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
   └─> Enable `runtime.inputs.seed` in config
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
| `--force` | Overwrite existing seeds directory |

### Example

```bash
# Collect from experiment output
crsbench benchmark seed-import ./experiment-data/e2e-bugfind/afc-curl-delta-02/curl_fuzzer/delta/address/

# With custom benchmarks path
crsbench benchmark seed-import ./experiment-data/... --benchmarks /path/to/benchmarks
```

### Output Structure

Seeds are stored in the benchmark's `.aixcc/{harness}/seeds/` directory:

```
benchmarks/afc-curl-delta-02/.aixcc/curl_fuzzer_ws/seeds/
├── manifest.json       # Metadata for all files
├── 00c3d860b1a0b8da    # Seed file (named by content hash)
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

### Timestamp Derivation

`seed-import` does not require the experiment output to store per-seed timestamp
metadata explicitly.

- current direct trial output (`trial-N/output/seeds/`) does not include a
  separate manifest
- legacy `trial-N/output/corpus/` sidecars do not carry `relative_time`

Instead, CRSBench derives `relative_time` during import from:

- the seed file's filesystem `mtime`
- minus `crs_run_start_time` from the trial metadata

That derived value is then normalized into
`.aixcc/{harness}/seeds/manifest.json`.

## Using Seeds in Experiments

Enable seed corpus in your experiment config:

Canonical config shape reference: `docs/experiment-config-distributed-example.yaml`

```yaml
runtime:
  inputs:
    seed:
      # Optional: only use seeds discovered within first hour.
      max_time: 3600
```

### Configuration Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `runtime.inputs.seed` | object | disabled when absent | Enables seed corpus input when present |
| `runtime.inputs.seed.max_time` | int | `null` | Max relative time (seconds) for filtering seeds |

### Time-based Filtering

The `runtime.inputs.seed.max_time` option filters seeds based on when they were discovered:

```yaml
# Use all collected seeds
runtime:
  inputs:
    seed: {}

# Use only seeds from first hour
runtime:
  inputs:
    seed:
      max_time: 3600

# Use only seeds from first 5 minutes
runtime:
  inputs:
    seed:
      max_time: 300
```

This is useful for:
- Replicating early-stage findings without full corpus
- Testing how quickly CRS finds bugs with vs without seed assistance
- Reducing experiment startup time with smaller seed sets

## How It Works

At runtime, when `runtime.inputs.seed` is enabled:

1. `SeedCorpusPreparer` reads `manifest.json` from `.aixcc/{harness}/seeds/`
2. Filters files by `relative_time <= runtime.inputs.seed.max_time` (if configured)
3. Copies the seed files that passed the optional time filter to `trial_dir/seeds/`
4. Passes directory to CRS via `oss-crs run --seed-dir <path>`

Notes:
- `manifest.json` is not copied into `trial_dir/seeds/`
- file copies use metadata-preserving copy semantics, so imported seed mtimes
  remain stable in the staged runtime directory

The CRS then uses these files as initial fuzzing seeds, potentially finding bugs faster.

Legacy keys (`seed_corpus_enabled`, `seed_corpus_max_time`) remain compatibility-only.

## Experiment Output Contract

`seed-import` reads corpus files from the trial output directory using:

- current contract: `trial-N/output/seeds/`
- legacy compatibility: `trial-N/output/corpus/`

It does not scan nested `crs-build/run/...` directories. CRSBench's current
`oss-crs` integration copies CRS-produced seeds into `trial/output/seeds/`
before import.

## Best Practices

1. **Collect from successful runs**: Seeds from runs that found vulnerabilities are most valuable
2. **Use time filtering**: Start with shorter time windows (1h) and expand if needed
3. **Per-harness collection**: Seeds are collected per-harness, so each harness gets relevant seeds
4. **Deduplication**: Files are stored by content hash, avoiding duplicates across collections
