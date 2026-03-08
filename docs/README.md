# CRSBench Documentation Index

This directory is the canonical entry point for CRSBench documentation.

## Start Here

- Bootstrap prerequisites first: `uv sync && crsbench prepare`
- [Environment Setup](./environment-setup.md)
- [Environment Variables](./environment-variables.md)
- [Experiment Workflow](./experiment-workflow.md)
- [Benchmark Specification (RFC)](./RFC.md)

## Documentation Governance

- [Documentation Taxonomy and Canonical Map](./documentation-taxonomy.md)
- [Documentation Inventory and Audit](./documentation-inventory.md)
- [Documentation Maintenance Guide](./documentation-maintenance.md)

## Contributor Guides

- [Framework Developer Guide](./framework-developer-guide.md)
- [Benchmark Developer Guide](./benchmark-developer-guide.md)
- [Testing Setup](./testing-setup.md)
- [Coding Standards](./coding-standards.md)
- [Manual Validation Guideline](./manual-validation-guideline.md)

## Runtime and Operations

- [OSS-CRS Interface](./ossfuzz-crs-interface.md)
- [Evaluator and CI Result Semantics](./design/evaluation/evaluation.md#distributed-evaluator-and-ci-result-semantics)
- [Logger Usage Guide](./logger-usage-guide.md)
- [Seed Corpus](./seed-corpus.md)
- [Snapshot Examples](./snapshot-examples.md)
- [Experiment Config Example](./experiment-config-example.yaml)
- [Distributed Config Example](./experiment-config-distributed-example.yaml)

## Architecture and Design

- [Design Index](./design/README.md)
- [Architecture Overview](./design/architecture.md)
- [Distributed Execution](./design/distributed/)
- [Configless Runtime](./design/distributed/configless-runtime.md)
- [Evaluation](./design/evaluation/)
- [Dataset](./design/dataset/dataset.md)
- [Validation](./design/validation/validation.md)
- [Reporting](./design/reporting/report-generation.md)
- [Benchmark CI](./design/benchmark-ci/)
- [Migration](./design/migration/)

## Module Reference

- [Module Index](./modules/README.md)

## Adjacent Component Readmes

- [scripts/README.md](../scripts/README.md)
- [benchmark-suites/README.md](../benchmark-suites/README.md)
- [crses/README.md](../crses/README.md)
- [experiment-configs/README.md](../experiment-configs/README.md)
- Smoke and local CI scripts: `scripts/ci-tests/run-local.sh`
- [snapshot-examples/README.md](../snapshot-examples/README.md)
- [dashboard/README.md](../dashboard/README.md)

## Integrated Upstream Projects

- [oss-crs/README.md](../oss-crs/README.md)
- [third_party/oss-fuzz/README.md](../third_party/oss-fuzz/README.md)
