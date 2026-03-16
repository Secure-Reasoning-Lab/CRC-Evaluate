# Design Documentation

Architecture and implementation design documents for CRSBench.
This section is for internal design details; user workflows belong in grouped
reader-facing docs such as `docs/getting-started/`, `docs/guides/`,
`docs/reference/`, and `docs/contributors/`. Operational module guidance
belongs in `docs/modules/`.

## Core

- [Architecture Overview](./architecture.md)
- [Orchestration](./orchestration.md)
- [Benchmark Data Protection](./benchmark-protection-and-contamination.md)
- [Design Doc Authoring Guidelines](./doc-authoring-guidelines.md)

## Subsystems

- [Distributed Queue Semantics](./distributed/distributed-job-queue.md)
- [Distributed Evaluator and Worker Execution](./distributed/distributed-evaluation.md)
- [Evaluation Contract](./evaluation/evaluation.md)
- [Validation Contract](./validation/validation.md)
- [Dataset Contract](./dataset/dataset.md)
- [Reporting Contract](./reporting/report-generation.md)
- [Benchmark CI Contract](./benchmark-ci/benchmark-ci.md)
- [Benchmark Lifecycle Contract](./benchmark/benchmark-lifecycle.md)
- [GCE Cloud Workers](./distributed/gce-cloud-workers.md)
- [Migration Design](./migration/migration-validation.md)
- [Logging Contract](./logging/logging-architecture.md)
- [Services Contract](./services/litellm.md)
- [GCE Cloud Orchestrator Launch](./distributed/gce-cloud-orchestrator.md)

## Scope

- Use this folder for architecture rationale, data models, and internals.
- Avoid duplicating CLI quick-start or setup instructions from grouped
  reader-facing docs.
- Keep design docs contract-focused; follow `doc-authoring-guidelines.md`.

## Canonical Contract Map

Use this map to decide where normative contract updates belong.
When adding a new contract topic, add its canonical location here in the same PR.
`architecture.md` is a system overview and context anchor; subsystem contract changes
should be applied to the dedicated canonical docs below.

| Contract Topic | Canonical Doc |
|---|---|
| End-to-end system architecture | `architecture.md` |
| Experiment orchestration and mode selection | `orchestration.md` |
| Distributed queue semantics | `distributed/distributed-job-queue.md` |
| Distributed evaluator/worker execution | `distributed/distributed-evaluation.md` |
| Configless runtime discovery/registration | `distributed/configless-runtime.md` |
| Evaluation contract and verdict semantics | `evaluation/evaluation.md` |
| Validation schema/normalization contracts | `validation/validation.md` |
| Dataset import/export and packaging contracts | `dataset/dataset.md` |
| Reporting and result schema contracts | `reporting/report-generation.md` |
| Benchmark CI DAG/result aggregation contracts | `benchmark-ci/benchmark-ci.md` |
| Logging format/semantics contracts | `logging/logging-architecture.md` |
| Service/backend integration contracts | `services/litellm.md` |
| GCE cloud worker fleet lifecycle | `distributed/gce-cloud-workers.md` |
| GCE remote orchestrator launch lifecycle | `distributed/gce-cloud-orchestrator.md` |
