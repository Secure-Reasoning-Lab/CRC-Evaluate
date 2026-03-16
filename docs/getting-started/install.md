# Install CRSBench

Use this page for the first-time bootstrap path. For environment variables and
deployment scenarios, see [configuration.md](./configuration.md).

## Prerequisites

- Python 3.12+
- `uv`
- Docker
- Git

## Bootstrap

```bash
git clone <repo>
cd CRSBench
uv sync
./scripts/setup-third-party.sh
uv run crsbench prepare
uv run crsbench prepare --coverage
```

`crsbench prepare` initializes the managed `third_party/oss-fuzz` checkout and
pulls the base images CRSBench relies on for benchmark evaluation.

`scripts/setup-third-party.sh` clones the pinned Team Atlanta
`atlantis-multilang-given_fuzzer` checkout into
`third_party/atlantis-multilang-given_fuzzer` alongside the managed
`third_party/oss-fuzz` checkout.

`crsbench prepare --coverage` prepares the Atlantis/given_fuzzer coverage
backend from that fixed checkout. It first pulls the canonical Team Atlanta
GHCR `1.0.0` prepare/runtime images, retags them onto the canonical local
Atlantis image names, and falls back to local `oss-crs prepare` only if those
images are unavailable. Benchmark-specific coverage builds remain lazy;
`crsbench coverage` runs Atlantis `oss-crs build-target` on first use and
reuses the normalized build output afterward.

Coverage runtime notes:

- `crsbench coverage --experiment-config ...` and `--experiment-dir ...` use
  Atlantis `oss-crs build-target` outputs for each `(benchmark, harness)` pair.
- Experiment-backed coverage accepts `--output-dir`; when set, CRSBench writes
  results under `<output-dir>/<experiment-name>/.../trial-N/coverage` instead
  of mutating the source experiment tree.
- `crsbench coverage` does not accept `--oss-fuzz-path`; post-trial coverage
  analysis uses the Atlantis/given_fuzzer backend directly.
- `--jobs` is the number of parallel `(benchmark, harness)` jobs.
- `--cores-per-job` is the number of warm one-core coverage containers per
  `(benchmark, harness)` job. CRSBench shards seeds across those containers and
  keeps each shard sequential inside its warm runner.
- `--build-workers` and `--verify-workers` remain hidden compatibility aliases
  for the old interface.

## Next Steps

1. Configure environment and LiteLLM: [configuration.md](./configuration.md)
2. Run a first experiment: [first-experiment.md](./first-experiment.md)
3. Author or inspect config files: [../guides/experiments/config-reference.md](../guides/experiments/config-reference.md)
