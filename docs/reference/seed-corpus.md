# Seed Corpus Collection and Reuse

This document describes how to collect seed corpus files from CRS experiment
output and reuse them as initial inputs in later experiments.

## Overview

During CRS execution, fuzzers discover inputs that trigger new coverage. These
files can be collected into a benchmark's ground truth so future experiments
start with known-good inputs.

## Workflow

```
1. Run one or more experiments
   └─> CRS writes corpus to trial-N/output/seeds/

2. Import seed corpus
   └─> crsbench benchmark seed-import <experiment-dir> [--all]

3. Reuse in future experiments
   └─> Enable `runtime.inputs.seed` in config
```

## Importing Seeds

```bash
crsbench benchmark seed-import <experiment-dir> \
  [--benchmarks <path>] [--all] \
  [--benchmark <name>] [--harness <name>] [--force]
```

### Arguments

| Argument | Description |
|----------|-------------|
| `experiment-dir` | Path to an experiment output directory or combined tree. |
| `--benchmarks` | Directory containing benchmarks (default: `./benchmarks`). |
| `--all` | Import every `(benchmark, harness)` group found under `experiment-dir`. |
| `--benchmark` | Only import trials whose benchmark matches this name. |
| `--harness` | Only import trials whose harness matches this name. |
| `--force` | Replace the existing corpus directory instead of merging. |
| `--dry-run` | Report what would be imported without writing anything to disk. |

By default the importer **merges** new files into any existing
`.aixcc/{harness}/corpus/` directory. Pass `--force` to wipe and rewrite.

### Input Layouts

`seed-import` discovers trials recursively. Each discovered trial must contain
either `trial-N/output/seeds/` (current contract) or `trial-N/output/corpus/`
(legacy).

A trial's `(benchmark, harness)` is determined in order:

1. `trial-N/metadata.json` (`benchmark_name`/`harness_name` fields)
2. `trial-N/config.yaml`
3. Path inference: `<...>/<project>/<harness>/<mode>/<sanitizer>/trial-N`

### Examples

```bash
# A single experiment with one benchmark/harness pair
crsbench benchmark seed-import \
  ./experiment-data/e2e-bugfind/afc-curl-delta-02/curl_fuzzer/delta/address/

# A combined tree containing many projects at once
crsbench benchmark seed-import \
  /data/final-combined/atlantis-multilang-given_fuzzer --all

# Restrict to one pair when scanning a combined tree
crsbench benchmark seed-import \
  /data/final-combined/atlantis-multilang-given_fuzzer --all \
  --benchmark afc-curl-delta-02 --harness curl_fuzzer_ws

# Replace existing corpus rather than merging
crsbench benchmark seed-import ./experiment-data/... --force

# Preview what would be imported (no files copied, no manifest written)
crsbench benchmark seed-import \
  /data/final-combined/atlantis-multilang-given_fuzzer --all --dry-run
```

### Output Structure

Imported files land under the benchmark's per-harness corpus directory:

```
benchmarks/<project>/.aixcc/<harness>/corpus/
├── manifest.json
├── 00c3d860b1a0b8da     # Seed file (named by SHA256-16 content hash)
├── 00d6dca7560f6081
└── ...
```

### Manifest Format

```json
{
  "total_files": 2,
  "updated_at": "2026-04-20T12:00:00+00:00",
  "source_trials": [
    {
      "path": "afc-curl-delta-02/curl_fuzzer_ws/delta/address/trial-1",
      "crs_run_start_time": 1769748907.0,
      "file_count": 2
    }
  ],
  "files": {
    "00c3d860b1a0b8da": {
      "size": 26,
      "original_names": ["2a0ac780c371363b"],
      "relative_time": 283.3,
      "first_trial": "afc-curl-delta-02/curl_fuzzer_ws/delta/address/trial-1"
    }
  }
}
```

- `total_files`: unique file count across all merged trials.
- `source_trials`: one entry per trial contributing to the merged corpus.
- `relative_time`: seconds after the trial's `crs_run_start_time` when the
  file first appeared. Recorded only when a trial exposes `pov_store.json`.
- `first_trial`: the trial in which this hash was first seen.

### Distribution via HuggingFace

Each benchmark's corpus is uploaded as its own archive:

- `benchmark.tar.gz` — project files (excluding `.aixcc/`)
- `ground-truth.tar.gz` — `.aixcc/` metadata, **excluding** `*/corpus/`
- `corpus.tar.gz` — every `.aixcc/<harness>/corpus/` directory

Downloaders can opt out with `--no-corpus` (corpus archive only) or
`--no-ground-truth` (both GT and corpus).

## Using Seeds in Experiments

Enable the seed corpus at runtime:

```yaml
runtime:
  inputs:
    seed:
      # Optional: only use seeds discovered within the first hour.
      max_time: 3600
```

Canonical config shape reference: `docs/experiment-config-distributed-example.yaml`

### Configuration Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `runtime.inputs.seed` | object | disabled when absent | Enables seed corpus input when present. |
| `runtime.inputs.seed.max_time` | int | `null` | Max relative time (seconds) for filtering seeds. |

### Time-based Filtering

`runtime.inputs.seed.max_time` filters by `relative_time` from the manifest.
Seeds missing `relative_time` are kept only when no filter is set.

## How It Works

When `runtime.inputs.seed` is enabled:

1. `SeedCorpusPreparer` reads `manifest.json` from `.aixcc/{harness}/corpus/`.
2. Filters files by `relative_time <= runtime.inputs.seed.max_time` (if set).
3. Copies the filtered seed files to `trial_dir/seeds/` with preserved mtimes.
4. Passes the directory to CRS via `oss-crs run --seed-dir <path>`.

The manifest itself is not copied into the staged runtime directory.

Legacy keys (`seed_corpus_enabled`, `seed_corpus_max_time`) remain
compatibility-only.

## Experiment Output Contract

`seed-import` reads files from:

- current: `trial-N/output/seeds/`
- legacy:  `trial-N/output/corpus/`

It does not scan nested `crs-build/run/...` directories.

## Best Practices

1. **Collect from successful runs**: seeds from runs that found vulnerabilities
   are the most valuable reuse material.
2. **Use time filtering**: start with shorter windows (e.g. 1h) and expand.
3. **Per-harness collection**: seeds are stored per harness.
4. **Dedup by content hash**: duplicates across trials collapse to one entry.
