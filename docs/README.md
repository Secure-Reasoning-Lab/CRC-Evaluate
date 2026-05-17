# CRSBench Documentation

**Start here:** [Getting Started](./getting-started/README.md) covers install,
configuration, the first experiment, experiment workflows, and deployment
modes — that's everything most users need.

1. [Install](./getting-started/install.md)
2. [Configuration](./getting-started/configuration.md)
3. [First Experiment](./getting-started/first-experiment.md)
4. [Experiments](./getting-started/experiments.md)
5. [Deployment](./getting-started/deployment.md)

## Deep Dives

For specific workflows or topologies, jump to:

- Experiment workflows: [`experiments/`](./experiments/README.md)
  ([discovery-only](./experiments/discovery-only.md),
  [replay POVs](./experiments/replay-povs.md),
  [full-pipeline](./experiments/full-pipeline.md),
  [merge results](./experiments/merge-results.md))
- Deployment topologies: [`deployment/`](./deployment/README.md)
  ([distributed](./deployment/distributed.md),
  [GCE cloud](./deployment/gce-cloud-orchestration.md),
  [local cloud rehearsal](./deployment/local-cloud-rehearsal.md))
- Operations: [`operations/`](./operations/README.md)
  ([queue & recovery](./operations/queue-and-recovery.md))
- Benchmark CI: [`benchmark-ci/`](./benchmark-ci/README.md)

## Reference

- [Benchmark RFC](./RFC.md) — normative benchmark format
- [Vulnerability Metadata RFC](./reference/vuln-yaml.md)
- [Experiment Config Reference](./reference/experiment-config.md)
- [Benchmark CI Config](./reference/benchmark-ci.md)
- [Environment Variables](./reference/environment-variables.md)
- [Benchmark Statistics](./reference/benchmark-statistics.md)
- [Standalone Verification and Coverage](./reference/standalone-verification.md)
- [OSS-CRS Interface](./reference/oss-crs-interface.md)
- [Seed Corpus](./reference/seed-corpus.md)
- [Snapshots](./reference/snapshots.md)
- [Logging](./reference/logging.md)
- [Example Configs](./reference/example-configs.md)

## Contributors

- [Framework Developer Guide](./contributors/framework-developer-guide.md)
- [Benchmark Developer Guide](./contributors/benchmark-developer-guide.md)
- [Testing](./contributors/testing.md)
- [Coding Standards](./contributors/coding-standards.md)
- [Manual Validation](./contributors/manual-validation.md)

## Module Reference

- [Module Index](./modules/README.md)

## Adjacent Repositories

- [experiment-configs/README.md](../experiment-configs/README.md)
- [scripts/README.md](../scripts/README.md)
- [benchmark-suites/README.md](../benchmark-suites/README.md)
- [oss-crs/README.md](../oss-crs/README.md)
- [OSS-Fuzz README](https://github.com/google/oss-fuzz/blob/master/README.md)
