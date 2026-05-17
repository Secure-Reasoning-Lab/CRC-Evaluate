# Experiments

CRSBench runs experiments through `crsbench run`. Pick the workflow that matches
what you want to measure, copy the minimal config, then submit the run.

For the canonical config schema and every field, see
[Experiment Config Reference](../reference/experiment-config.md).

## Bug-Finding

Measure whether a CRS discovers known vulnerabilities in benchmark projects.

```yaml
experiment:
  name: my-finding
  task: bugfinding
  mode: full
  benchmark_suite: smoke/sanity
  sanitizers: [address]

runtime:
  trials: 1
  max_total_time: 3600
  build_timeout: 900
  run_timeout: 1800
  verify_timeout: 900
  redis_host: localhost:6379
  litellm:
    skip: true

storage:
  experiment_filestore: ./results/experiment-data
  report_filestore: ./results/report-data

crs_compose:
  atlantis-multilang-given_fuzzer:
    num_cores: 4
```

```bash
uv run crsbench worker --experiment-config my-finding.yaml   # terminal 1
uv run crsbench run    --experiment-config my-finding.yaml   # terminal 2
```

## Bug-Fixing

Measure whether a CRS patches a known vulnerability. Set `task: bugfixing` and
declare a POV input source under `runtime.inputs.pov`. The default consumes
benchmark ground-truth POVs from `.aixcc/`.

```yaml
experiment:
  name: my-fixing
  task: bugfixing
  mode: delta
  benchmark_suite: smoke/sanity
  sanitizers: [address]

runtime:
  trials: 1
  inputs:
    pov:
      max_variants_per_cpv: 1
  # ... timeouts and redis_host same as above
```

## Discovery-Only OSS-Fuzz

Run CRSBench against arbitrary OSS-Fuzz projects that do not have CRSBench
ground truth. Use this for discovery on upstream OSS-Fuzz targets.

Set `experiment.mode: full`, `experiment.only_cpv_harnesses: false`,
`runtime.skip_verification: true`, and point `benchmarks_root` at an OSS-Fuzz
`projects/` checkout. Before submitting the run, initialize the benchmarks once
so CRSBench can write `.aixcc/meta.yaml`:

```bash
uv run crsbench benchmark init --experiment-config discovery.yaml
uv run crsbench run            --experiment-config discovery.yaml
```

Full workflow including parallel initialization, harness selectors, and managed
cloud bootstrap: [Discovery-only OSS-Fuzz](../experiments/discovery-only.md).

## Full-Pipeline (Finding → Fixing)

Chain a bug-finding run into a bug-fixing run so the fixing CRS operates on
POVs the finding CRS actually produced. The two phases are independent
`crsbench run` invocations. The fixing config sets
`runtime.inputs.pov.from_experiment_by_crs` to point at the finding run's
output:

```yaml
runtime:
  inputs:
    pov:
      max_variants_per_cpv: 1
      from_experiment_by_crs:
        crs-claude-code: .run/local/experiment-data/find/find/find/crs-bug-finding-claude-code
```

```bash
uv run crsbench run --experiment-config find.yaml
uv run crsbench run --experiment-config fix.yaml
```

Per-CRS pairing, path layout, and managed-cloud chaining:
[Full-Pipeline](../experiments/full-pipeline.md).

## Replay Historical POVs

Take POVs found in prior bug-finding trials and replay them against the latest
OSS-Fuzz project HEAD and every current harness:

```bash
uv run crsbench replay-povs \
  --source-dir /data/crsbench/experiment-a \
  --output /tmp/replay-results \
  --cache-root /data/crsbench/replay-cache \
  --sync-projects \
  --jobs 8 \
  --resume
```

Output layout, dedup semantics, and resume behavior:
[Replay POVs](../experiments/replay-povs.md).
