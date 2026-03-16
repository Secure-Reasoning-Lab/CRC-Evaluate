# Benchmark Module

This module manages the complete benchmark lifecycle in CRSBench.

## Lifecycle Phases

```
Generation ──► Packaging ──► Publish ──► Runtime
(create)       (bundle)      (distribute) (load & use)
```

## Module Structure

```
benchmark/
├── generation/     # Create benchmarks from external sources (future)
├── packaging/      # Bundle benchmarks for distribution
└── runtime/        # Load benchmarks for CRS evaluation
```

## Reference Surface

### Packaging APIs

```python
from crsbench.benchmark import bundle_benchmark, validate_benchmark
```

Packaging command usage is documented in the benchmark contributor guide.

### Runtime APIs

```python
from crsbench.benchmark import load_benchmark_source
```

Runtime command usage is documented in the experiment guides.

### Coverage CLI

`crsbench coverage` supports the Atlantis-backed seed timeline modes:

- `crsbench coverage --experiment-config <config>`
- `crsbench coverage --experiment-dir ./experiment-filestore/experiment-name`
- `crsbench coverage --seed-dir <dir> --benchmarks <root> --benchmark <name> --harness <name> --output-dir <dir>`

The experiment-config and experiment-dir modes read seeds from
`trial-N/output/seeds/` and also support the legacy `trial-N/output/corpus/`
layout. Timeline outputs are written under `trial-N/coverage/` as JSON, CSV,
and PNG artifacts unless `--output-dir` is supplied, in which case CRSBench
mirrors `<experiment-name>/.../trial-N/coverage` under that root. Per-seed
analysis is executed through a warm `(benchmark, harness)` coverage worker and
persists raw artifacts under `trial-N/coverage/raw/`, including the normalized
`.cov` output and any captured crash log for that input.

Coverage execution uses the Atlantis/libCRS UniAFL runtime:

- `scripts/setup-third-party.sh` clones the pinned Team Atlanta Atlantis
  checkout into `third_party/atlantis-multilang-given_fuzzer`.
- `crsbench prepare --coverage` first pulls the canonical Team Atlanta GHCR
  image set and only falls back to Atlantis `oss-crs prepare` if those images
  are unavailable locally.
- Coverage builds are lazy and go through Atlantis `oss-crs build-target`.
- Atlantis prepare/runtime images default to
  `ghcr.io/team-atlanta/multilang-given_fuzzer-*`.
- `--jobs` controls how many `(benchmark, harness)` jobs run in parallel in
  experiment mode.
- `--cores-per-job` controls how many warm one-core coverage containers are
  used for a single `(benchmark, harness)` job. CRSBench shards the seed set
  across those workers and runs each shard sequentially inside its warm runner.

For experiment coverage, concurrency is split across two axes:

- `--jobs`: parallel `(benchmark, harness)` jobs
- `--cores-per-job`: warm one-core coverage containers per `(benchmark, harness)`
  job

## Documentation

- Design: `../../design/benchmark/benchmark-lifecycle.md`
- Generation plan: `./generation.md`
- Contributor workflow: `../../contributors/benchmark-developer-guide.md`
