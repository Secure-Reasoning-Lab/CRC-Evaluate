# Merge Experiment Results

Use `scripts/merge_experiment_results.py` when separate CRSBench runs produced
separate `experiment-data` trees and you want one combined result tree for
reporting or follow-up analysis.

The common case is rerunning an experiment with `runtime.trials: 1` several
times, then merging those successful single-trial runs so they look like one
multi-trial experiment.

## What The Script Does

- Scans one or more input paths for `experiment-data` directories.
- Finds trial directories in both supported layouts:
  - `crs/benchmark/harness/mode/sanitizer/trial-N`
  - `crs/benchmark/harness/cpv/mode/sanitizer/trial-N`
- Copies only successful trials, identified by a `.success` marker.
- Skips failed or incomplete trials, identified by `.fail` or by missing status
  markers.
- Removes copied `crs-build/` directories to avoid moving large build artifacts.
- Writes a merged `trial_matrix.json` when source matrices are available.

By default, duplicate successful trials with the same identity are treated as a
conflict. A trial identity includes CRS, benchmark, harness, optional CPV, mode,
sanitizer, and trial number.

## Merge Three Single-Trial Runs

If three independent runs all used `runtime.trials: 1`, their successful trial
directories will usually all be named `trial-1`. Use `--renumber-trials` to keep
all three runs and rewrite them as `trial-1`, `trial-2`, and `trial-3` per
logical CRS/benchmark/harness/mode/sanitizer stream.

```bash
uv run python scripts/merge_experiment_results.py \
  --input-dirs \
    /data/crsbench/experiment-data/run-a \
    /data/crsbench/experiment-data/run-b \
    /data/crsbench/experiment-data/run-c \
  --output-dir /data/crsbench/experiment-data/merged-three-trials \
  --renumber-trials \
  --experiment-name merged-three-trials
```

With `--renumber-trials`, the script rewrites:

- the destination trial directory name;
- `metadata.json` field `trial_num`;
- trial identity fields in `llm-usage.json`, including `trial_id`,
  `key_alias`, and nested `trial_num` values;
- copied `trial_matrix.json` entries so `trial_num` matches the new destination
  trial number.

## Input Selection

Use `--input-dirs` when you know the exact run directories:

```bash
uv run python scripts/merge_experiment_results.py \
  --input-dirs /data/crsbench/experiment-data/run-a /data/crsbench/experiment-data/run-b \
  --output-dir /data/crsbench/experiment-data/merged
```

Use `--input-pattern` when the run directories follow a predictable naming
pattern:

```bash
uv run python scripts/merge_experiment_results.py \
  --input-pattern "/data/crsbench/experiment-data/cc-find-*-1trial" \
  --output-dir /data/crsbench/experiment-data/cc-find-merged \
  --renumber-trials
```

The input paths can point directly at `experiment-data` directories or at parent
directories. Parent directories are searched recursively for `experiment-data`
children.

## Conflict Handling

Without `--renumber-trials`, the script fails if more than one successful trial
has the same full identity. That is the right behavior when duplicate `trial-N`
directories might represent accidental duplicate data.

Use `--renumber-trials` only when duplicate trial numbers are expected and each
input run should become a distinct trial in the merged output. This is the mode
for combining several separate `runtime.trials: 1` executions.

## Output Safety

Prefer an empty output directory. If a destination trial path already exists,
that trial is skipped instead of overwritten.

The merged output is not a byte-for-byte copy of the source trees:

- failed or incomplete trials are omitted;
- `crs-build/` is removed from copied successful trials;
- trial numbering metadata is rewritten when `--renumber-trials` is enabled.

The merge preserves copied trial directory mtimes and copied JSON file mtimes,
including rewritten `metadata.json` and `llm-usage.json` files in renumber mode.
Downstream scripts can continue to use trial mtimes for ordering.

Keep the original source run directories until reports and any downstream checks
have consumed the merged tree.

## Quick Checks

Count successful trials before merging:

```bash
find /data/crsbench/experiment-data/run-a -name .success | wc -l
find /data/crsbench/experiment-data/run-b -name .success | wc -l
find /data/crsbench/experiment-data/run-c -name .success | wc -l
```

Inspect the merged trial numbers after a renumbered merge:

```bash
find /data/crsbench/experiment-data/merged-three-trials -type d -name 'trial-*' \
  | sort
```

Check the merged matrix size:

```bash
jq '.total_trials' /data/crsbench/experiment-data/merged-three-trials/trial_matrix.json
```
