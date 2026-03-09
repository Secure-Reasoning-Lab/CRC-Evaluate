# Hint Generation Module

Module scope: generation and staging of benchmark hint artifacts, especially
SARIF-based hints derived from benchmark vulnerability metadata.

This page is intentionally reference-oriented. Use it to understand module
boundaries, supported hint surfaces, and how the module fits into CRSBench.
Detailed runtime semantics and configuration behavior belong in:
- [Hint Levels Design](../design/evaluation/hint-levels.md)
- [Evaluation Design](../design/evaluation/evaluation.md)
- [Experiment Config Reference](../guides/experiments/config-reference.md)

## Responsibilities

The hint-generation module is responsible for:
- deriving hint artifacts from benchmark metadata such as `vuln.yaml`
- producing SARIF hint outputs at supported detail levels
- materializing hint files into benchmark-owned hint directories
- supporting runtime staging of configured hint inputs

The module is not responsible for:
- defining benchmark difficulty policy on its own
- owning experiment workflow or operator tutorials
- serving as the canonical explanation of SARIF input semantics

## Inputs

The module consumes benchmark vulnerability metadata, especially fields needed
to derive:
- vulnerability class / CWE mapping
- file-level location
- function-level location when present
- line/region-level location when present

## Outputs

The module produces benchmark hint artifacts suitable for staging through the
runtime input contract, typically under benchmark `.aixcc/.../hints/` paths.

Callers should treat generated hints as benchmark artifacts, not as ad hoc
runtime state.

## Runtime Boundary

Runtime selection and staging are defined by the evaluation and experiment-config
contracts. This module only generates benchmark-owned hint artifacts that those
runtime paths later stage and deliver.

## Canonical References

- [Hint Levels Design](../design/evaluation/hint-levels.md)
- [Evaluation Design](../design/evaluation/evaluation.md)
- [Experiment Config Reference](../guides/experiments/config-reference.md)
- [Benchmark RFC](../RFC.md)

## Implementation Pointers

- `crsbench/hint_generation/`
- benchmark `.aixcc/.../hints/` artifacts
- tests covering hint generation and staging paths
