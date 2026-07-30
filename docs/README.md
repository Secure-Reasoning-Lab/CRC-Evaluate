# CRC-Evaluate Documentation

Teams should start with the [CRC-Evaluate participant guide](../README.md), which defines the public sanity workflow, runtime configuration, and competition resource limits.

## Participant Documentation

1. [Participant guide](../README.md)
2. [Submission validation and registration](./getting-started/evaluating-submissions.md)

## Inherited CRSBench Framework Documentation

Except for the participant documents above, every guide and reference below describes the underlying CRSBench framework or an advanced operator workflow. These documents are not the source of CRC-Evaluate competition limits or public sanity commands.

1. [Framework installation](./getting-started/install.md)
2. [Configuration](./getting-started/configuration.md)
3. [First experiment](./getting-started/first-experiment.md)
4. [Experiment workflows](./getting-started/experiments.md)
5. [Advanced deployment](./getting-started/deployment.md)
6. [Experiment configuration reference](./reference/experiment-config.md)

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
