# Validation Module

Module scope: schema parsing, normalization, and validation result surfaces for
benchmark suites and experiment configuration.

This page is intentionally reference-oriented. Use it to understand the module
boundary and the result contract. Detailed validation workflow, contributor
procedures, and deeper rationale belong in:
- [Framework Developer Guide](../contributors/framework-developer-guide.md)
- [Benchmark Developer Guide](../contributors/benchmark-developer-guide.md)

## Responsibilities

The validation module is responsible for:
- parsing and validating benchmark metadata
- parsing and validating benchmark-suite definitions
- parsing, normalizing, and validating experiment configuration
- returning structured validation results that callers can serialize or inspect

The module is not responsible for:
- mutating benchmark content
- running experiments
- owning contributor workflow or CLI tutorials

## Public Surface

Primary public entry points are the validation helpers exposed from
`crsbench.validation` and the schema models used by experiment and benchmark
configuration handling.

Callers should rely on the module for:
- success/failure status
- structured issues with severity and field context
- normalized configuration objects where applicable

Common entry points include:
- `validate_benchmark(path)`
- benchmark-suite validation helpers
- experiment-config validation / normalization helpers
- structured result models such as `ValidationResult`

## Result Contract

Validation results must provide:
- overall validity
- structured issues with severity and field context
- metadata useful to downstream callers
- a serializable representation for tooling and agents

In practice, callers should expect:
- `is_valid`
- issue collections by severity
- metadata/counters about what was validated
- serialization helpers for downstream tooling

Issue classes include:
- file and parse failures
- schema violations
- semantic configuration conflicts
- best-practice warnings where CRSBench intentionally does not hard-fail

## Core Validation Areas

### Benchmark metadata
- required files and readable YAML
- benchmark schema conformance
- harness and CPV structure validity
- vulnerability/POV metadata consistency

### Benchmark suites
- suite schema conformance
- benchmark list structure and duplicates
- nested harness/CPV selection validity

### Experiment config
- grouped config contract conformance
- runtime/storage/resource schema validation
- `crs_compose` shape and normalization
- worker/evaluator/distributed settings validity
- input contract validity for POV/SARIF/seed/diff selection

## Operational Boundaries

The validation module should remain:
- read-only
- deterministic for the same input
- safe to call repeatedly from orchestration, agents, and tests
- suitable for serialization across process boundaries

## Integration Notes

CRSBench uses the validation module before running experiments and benchmark
operations. Callers should treat validation failure as an input or configuration
problem, not as a runtime execution result.

## Canonical References

- [Experiment Config Reference](../guides/experiments/config-reference.md)
- [Distributed Example Config](../experiment-config-distributed-example.yaml)
- [Benchmark RFC](../RFC.md)

## Implementation Pointers

- `crsbench/validation/`
- `tests/test_validation.py`
