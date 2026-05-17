# Example Configs

Use these example files as reference inputs:

- [Distributed experiment config](../../docs/experiment-config-distributed-example.yaml)
  — annotated source-of-truth YAML for the grouped contract.
- [Benchmark suite example](../../docs/benchmark-suite-example.yaml)
- [Meta example](../../docs/meta-example.yaml)
- Repository config sets: [`experiment-configs/README.md`](../../experiment-configs/README.md)

## Checked-in templates

The repository ships five template directories under `experiment-configs/`:

- [`local/`](../../experiment-configs/local/) — single-machine runs with one
  CRS (Claude Code).
- [`gcp/`](../../experiment-configs/gcp/) — GCE-orchestrated runs with one CRS.
- [`agentic-cli/`](../../experiment-configs/agentic-cli/) — multi-CRS
  comparison across agentic CLIs (claude-code, codex, copilot-cli, gemini-cli,
  opencode).
- [`smoke-testing/`](../../experiment-configs/smoke-testing/) — tiny single-CRS
  smoke run with explicit per-harness benchmark selectors.
- [`discovery/`](../../experiment-configs/discovery/) — discovery-only
  OSS-Fuzz runs (no CRSBench ground truth, verification skipped).

Each `local/`, `gcp/`, and `agentic-cli/` tier contains `bug-finding.yaml`,
`bug-fixing.yaml`, and `full-pipeline-fixing.yaml`. `full-pipeline-fixing.yaml`
consumes POVs produced by a prior `bug-finding.yaml` run via
`inputs.pov.from_experiment_by_crs`. See
[full-pipeline.md](../experiments/full-pipeline.md) for how the two phases
are chained.

## Discovery-only workflow

See [discovery-only.md](../experiments/discovery-only.md) for the full
workflow. A minimal checked-in template lives at
[`experiment-configs/discovery/discovery-libyang.yaml`](../../experiment-configs/discovery/discovery-libyang.yaml)
— adjust `benchmarks_root` to your OSS-Fuzz `projects/` checkout before
running.
