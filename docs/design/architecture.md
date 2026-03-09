# CRSBench Architecture

Audience: contributors changing system-level structure or cross-module contracts.
Scope: repository-wide architecture intent, boundaries, and major design decisions.

This document describes the architecture intent for CRSBench and the main design
tradeoffs behind its current structure.

## Context

CRSBench evaluates Cyber Reasoning Systems (CRS) over benchmarks with known
ground truth. The platform has two hard constraints:

1. Preserve benchmark and vulnerability ground truth integrity.
2. Support reproducible execution from local development to distributed runs.

The repository combines benchmark lifecycle tooling, distributed execution, and
evaluation/reporting in one Python package (`crsbench/`) plus integrated
upstream components (`oss-fuzz/`, `oss-crs/`).

## Architectural Goals

- Keep benchmark format and validation explicit and machine-checkable.
- Separate orchestration from worker execution for horizontal scaling.
- Preserve reproducibility through explicit configs, artifacts, and reports.
- Keep module boundaries understandable for contributors and agents.

## Non-Goals

- CRSBench does not define CRS model internals.
- CRSBench does not replace upstream `oss-fuzz` or `oss-crs` docs.
- CRSBench does not optimize for single-file simplicity over modularity.

## Repository Shape

```
CRSBench/
├── crsbench/                 # Core package
│   ├── benchmark/            # Benchmark packaging/runtime lifecycle
│   ├── benchmark_ci/         # CI pipeline for benchmark quality
│   ├── dataset/              # Dataset publishing/downloading
│   ├── distributed/          # Queue, worker, distributed orchestration
│   ├── evaluation/           # Trial execution, POV/patch verification
│   ├── hint_generation/      # Hint/corpus difficulty controls
│   ├── migration/            # Format migration tooling
│   ├── reporting/            # Reports and dashboard outputs
│   ├── statistics/           # Benchmark and experiment stats
│   ├── validation/           # Schema + semantic validation
│   └── run_experiment.py     # CLI entrypoint
├── docs/                     # User docs, module docs, design docs
├── benchmarks/               # Benchmark artifacts
├── experiment-configs/       # Experiment yaml configs
├── oss-crs/                  # Integrated upstream project
└── third_party/oss-fuzz/     # Official OSS-Fuzz (sparse checkout)
```

## Core Design Decisions

### 1. Spec-First Benchmark Quality Gate

Decision:
- Use `docs/RFC.md` + validation modules as the source of truth for benchmark
  structure and required metadata.

Why:
- Prevents silent benchmark drift and keeps evaluation semantics comparable.

Alternative considered:
- Best-effort validation only at runtime.
- Rejected because failures appear too late and are harder to debug.

### 2. Queue-Based Distributed Execution

Decision:
- Use Redis/Valkey-backed job queues with separated roles:
  orchestrator (`run`), workers (`worker`), evaluator (`evaluator`).

Why:
- Supports scaling trials independently of coordinator lifecycle.
- Allows optional verification workers without blocking baseline runs.

Alternative considered:
- Single process that performs all phases.
- Rejected due to weak scaling and poor failure isolation.

### 3. Separation of Module Docs and Design Docs

Decision:
- Keep operational module docs in `docs/modules/` and deep rationale in
  `docs/design/`.

Why:
- Reduces duplication and lets contributors find either "how to use" or
  "why it is designed this way" quickly.

Alternative considered:
- Put all content in per-module READMEs.
- Rejected because mixed audiences caused readability and maintenance issues.

### 4. Integrated but Non-Canonical Upstream Docs

Decision:
- Treat `oss-crs/` and `third_party/oss-fuzz/` as integrated upstream references, but keep
  CRSBench behavior docs canonical in root `docs/`.

Why:
- Avoids duplicating upstream documentation while preserving project-specific
  execution contracts.

## Tradeoffs and Risks

- Tradeoff: Modular docs improve maintainability but require disciplined linking.
  - Mitigation: canonical map and maintenance guide.
- Tradeoff: Distributed runtime improves throughput but increases operational
  complexity.
  - Mitigation: explicit workflow docs and config examples.
- Risk: historical design intent can be partially reconstructed.
  - Mitigation: label inferred rationale and keep open questions visible.

## Inferred Historical Rationale

The following rationale is inferred from repository structure and existing docs,
not guaranteed by an explicit historical record:

- Inference: module docs were introduced to reduce deep design duplication.
- Inference: queue separation was prioritized for experiment throughput and
  mixed local/remote worker deployment.

## Open Questions

- Should design rationale be further split into subsystem ADR-style records?
- Which architecture guarantees should become explicit CI checks?
- Should local markdown-link validation be mandatory in CI for docs-only PRs?

## Related Docs

- [Design Index](./README.md)
- [Orchestration](./orchestration.md)
- [Distributed Design](./distributed/distributed-evaluation.md)
- [Evaluation Design](./evaluation/evaluation.md)
- [Validation Design](./validation/validation.md)
