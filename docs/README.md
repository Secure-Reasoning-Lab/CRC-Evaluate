# CRSBench Documentation Index

This directory is the central home for project documentation.

## User Docs

- [Benchmark Developer Guide](./benchmark-developer-guide.md)
- [Framework Developer Guide](./framework-developer-guide.md)
- [Coding Standards](./coding-standards.md)
- [Benchmark Data Protection and AI Contamination](./design/benchmark-protection-and-contamination.md)
- [Environment Setup](./environment-setup.md)
- [Experiment Workflow](./experiment-workflow.md)
- [Experiment Config Example](./experiment-config-example.yaml)
- [Distributed Config Example](./experiment-config-distributed-example.yaml)
- [Benchmark Specification](./benchmark-spec.md)
- [OSS-CRS Interface](./ossfuzz-crs-interface.md)
- [Seed Corpus](./seed-corpus.md)
- [Snapshot Examples](./snapshot-examples.md)
- [Testing Setup](./testing-setup.md)
- [Manual Validation Guideline](./manual-validation-guideline.md)
- [Logger Usage Guide](./logger-usage-guide.md)

## Design Docs

Internal architecture and implementation design docs now live under [`docs/design/`](./design/) and are indexed at [`docs/design/README.md`](./design/README.md):

- [Architecture Overview](./design/architecture.md)
- [Distributed Execution](./design/distributed/)
- [Evaluation](./design/evaluation/)
- [Dataset](./design/dataset/dataset.md)
- [Validation](./design/validation/validation.md)
- [Reporting](./design/reporting/report-generation.md)
- [Benchmark CI](./design/benchmark-ci/)
- [Migration](./design/migration/)

## Module Docs

CRSBench module docs are centralized under [`docs/modules/`](./modules/) and indexed at [`docs/modules/README.md`](./modules/README.md).

Other project areas keep local READMEs:

- `services/*/README.md`
- `scripts/README.md`
- `benchmark-suites/README.md`
- `snapshot-examples/README.md`

## Notes

- External/upstream submodules (`oss-fuzz/`, `oss-crs/`) keep their own documentation layout.
- Key OSS-CRS references:
  - `oss-crs/README.md`
  - `oss-crs/docs/README.md`
  - `oss-crs/docs/design/parallel.md`
