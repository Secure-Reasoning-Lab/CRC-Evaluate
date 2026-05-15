# Standalone Verification and Coverage

CRSBench exposes verification and coverage commands that run outside of a full
experiment, against existing benchmarks or pre-collected seeds/POVs/patches.

## Commands

```bash
uv run crsbench verify       benchmarks/project --pov-dir ./povs/ --jobs 4 --cores-per-job 2
uv run crsbench patch-verify benchmarks/project --patch-dir ./patches --pov-dir ./povs --jobs 4 --cores-per-job 2
uv run crsbench coverage     --experiment-config ./experiment.yaml      # seed coverage over time
uv run crsbench coverage     --experiment-dir ./experiment-filestore/experiment-name       # seed coverage over time
uv run crsbench coverage     --experiment-dir ./experiment-filestore/experiment-name --output-dir ./coverage-out
uv run crsbench coverage     --seed-dir ./seeds --benchmark project --harness fuzz_target --output-dir ./coverage-out
uv run crsbench coverage     --seed-dir ./seeds --experiment-start-time 1710000000 --benchmark project --harness fuzz_target --output-dir ./coverage-out
```

## Parallelism

For standalone `verify` / `patch-verify`, `--jobs` and `--cores-per-job` are
the primary parallelism flags. Legacy `--build-workers` and `--verify-workers`
remain accepted as hidden compatibility aliases.

## Timeline Coverage Behavior

Timeline coverage mode persists raw per-seed artifacts under the target
coverage directory's `raw/` subdirectory. Each analyzed seed keeps its
normalized `.cov` result and any captured crash log alongside the JSON/CSV/PNG
timeline outputs:

- `coverage_timeline.json` stores one row per normalized seed.
- `coverage_timeline.csv` emits one row per normalized seed.
- `coverage_timeline.png` plots cumulative covered lines directly from those
  per-seed replay results.

Time origin:

- Direct `--seed-dir` mode derives relative time from each input seed file's
  original `mtime`, using the first retained seed as the origin.
  `--experiment-start-time` overrides the origin with an explicit Unix
  timestamp.
- `--experiment-dir` and `--experiment-config` instead use
  `povs/pov_store.json.crs_run_start_time` as the origin and clamp the x-axis
  to the recorded trial `run_time` from `metadata.json`.

Output layout:

- When `--output-dir` is supplied for experiment-backed coverage, CRSBench
  mirrors the experiment under
  `<output-dir>/<experiment-name>/.../trial-N/coverage`.
- Otherwise it writes to each source trial's in-place `coverage/` directory.

The Atlantis timeline path does not run a separate whole-corpus denominator
pass, so total-line percentages may be reported as unavailable. Coverage
analysis uses the Atlantis/given_fuzzer warm-runner backend and does not
accept an `--oss-fuzz-path` override.
