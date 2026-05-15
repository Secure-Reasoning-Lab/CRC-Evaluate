# Experiment Configs

User-facing templates. Each YAML carries only the fields required by the
[ExperimentConfig schema](../crsbench/validation/schemas.py); every other knob
falls back to its in-code default. Edit a copy to customize.

## Layout

```
experiment-configs/
├── local/              # Single-machine runs, 1 CRS (Claude Code)
│   ├── bug-finding.yaml
│   ├── bug-fixing.yaml
│   ├── full-pipeline-finding.yaml
│   └── full-pipeline-fixing.yaml
├── gcp/                # GCE-hosted runs, 1 CRS (Claude Code)
│   ├── bug-finding.yaml
│   ├── bug-fixing.yaml
│   ├── full-pipeline-finding.yaml
│   └── full-pipeline-fixing.yaml
├── smoke-testing/      # Tiny single-CRS smoke run
│   └── smoke.yaml
└── agentic-cli/        # Multi-CRS comparison across agentic CLIs
    ├── bug-finding.yaml
    ├── bug-fixing.yaml
    ├── full-pipeline-finding.yaml
    └── full-pipeline-fixing.yaml
```

Older preset/AFC/sanity configs live under `experiment-configs.bak/` and are
retained only for reference.

## Required fields

Every config must set the following (defaults handle everything else):

- `experiment.name` — unique identifier
- `experiment.task` — `bugfinding` or `bugfixing`
- `experiment.mode` — `delta`, `full`, `all`, or `auto`
- `experiment.benchmark_suite` **or** `experiment.benchmarks` — exactly one
- `runtime.trials` — integer ≥ 1
- `runtime.max_total_time` — seconds per trial
- `storage.experiment_filestore` — output directory
- `storage.report_filestore` — report output directory
- `crs_compose.oss_crs_infra` — `{shared: true}` or `{num_cores: N}`
- At least one `crs_compose.<crs-name>` entry with `num_cores`

`cloud:` is additionally required for GCE runs and must declare `providers`,
`orchestrator`, and `workers`.

## Common usage

Local:

```bash
uv run crsbench run --config experiment-configs/local/bug-finding.yaml
```

GCP (requires `gcloud auth login`, `cloud keygen`, and HF/LLM env vars):

```bash
CONFIG=experiment-configs/gcp/bug-finding.yaml
uv run crsbench cloud launch   --config "$CONFIG"
uv run crsbench cloud monitor  --config "$CONFIG"
uv run crsbench cloud collect  --config "$CONFIG" --force
uv run crsbench cloud teardown --config "$CONFIG" --force
```

Full-pipeline two-phase chaining: launch `full-pipeline-finding.yaml` first;
once results are collected, launch `full-pipeline-fixing.yaml`. The fixing
config references the finding output via `inputs.pov.from_experiment_by_crs` —
adjust the path if you ran phase 1 with a different `storage.experiment_filestore`.

## Validation

```bash
scripts/ci-tests/run-local.sh checks
```
