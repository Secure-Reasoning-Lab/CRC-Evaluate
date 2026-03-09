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
- producing the benchmark-owned SARIF artifacts later selected by the runtime
  input contract

The module is not responsible for:
- defining benchmark difficulty policy on its own
- owning experiment workflow or operator tutorials
- serving as the canonical explanation of SARIF input semantics
- deciding which hint level is delivered to a CRS run

## Inputs

The module consumes benchmark vulnerability metadata, especially fields needed
to derive:
- vulnerability class / CWE mapping
- file-level location
- function-level location when present
- line/region-level location when present

Typical `vuln.yaml` shape consumed by this module:

```yaml
id: cpv_0
name: Heap Based Buffer Overflow in UTF-32 implementation
cwes:
  - CWE-122
description: |
  Detailed description of the vulnerability...
locations:
  - path_from_root: xmlIO.c
    function_name: UTF32ToUTF8
    startLine: 2168
    startColumn: 17
    endLine: 2177
    endColumn: 4
```

## Outputs

The module produces benchmark hint artifacts suitable for staging through the
runtime input contract, typically under benchmark `.aixcc/.../hints/` paths.

Callers should treat generated hints as benchmark artifacts, not as ad hoc
runtime state.

## Runtime Selection Contract

CRSBench does not use a separate hint-generation control plane at run time.
Instead, SARIF delivery is controlled by the experiment config input contract:

```yaml
runtime:
  inputs:
    sarif:
      level: 3
```

That setting selects which pre-generated `level_N.sarif` benchmark artifact is
staged for a trial. The generator documented here is responsible for producing
those benchmark artifacts; trial-time selection happens elsewhere.

## Module Structure

```text
crsbench/hint_generation/
├── cwe_mapping.py
├── generate_hints.py
├── sarif_generator_simple.py
└── sarif_model.py   # legacy model helpers
```

Key responsibilities by file:
- `cwe_mapping.py`: groups specific CWEs into higher-level vulnerability
  classes used by coarse hints
- `sarif_generator_simple.py`: generates SARIF artifacts by hint level
- `generate_hints.py`: CLI entrypoint for bulk generation

## Generation Commands

Generate hints for all benchmarks:

```bash
uv run python -m crsbench.hint_generation.generate_hints
```

Generate hints for one benchmark:

```bash
uv run python -m crsbench.hint_generation.generate_hints --benchmark libxml2-delta-03
```

Generate only selected levels:

```bash
uv run python -m crsbench.hint_generation.generate_hints --levels 1 2 3
```

Verbose run:

```bash
uv run python -m crsbench.hint_generation.generate_hints -v
```

## Programmatic Usage

```python
from pathlib import Path
from crsbench.hint_generation.sarif_generator_simple import (
    HintLevel,
    SarifHintGenerator,
    VulnInfo,
    generate_hints_for_benchmark,
)

vuln_info = VulnInfo.from_yaml(Path("vuln.yaml"))
generator = SarifHintGenerator(vuln_info)
sarif_json = generator.generate(HintLevel.WITH_FUNCTION)

output_files = generate_hints_for_benchmark(
    vuln_yaml_path=Path("benchmarks/example/.aixcc/html/cpv_0/vuln.yaml"),
    output_dir=Path("benchmarks/example/.aixcc/html/cpv_0/hints"),
    levels=[HintLevel.GENERAL_CLASS, HintLevel.WITH_LINES],
)
```

## CWE Mapping Notes

`cwe_mapping.py` groups specific CWE IDs into broader vulnerability classes used
by low-information hint levels. If a new CWE needs support, extend the mapping
there rather than inventing ad hoc naming elsewhere.

Representative categories include:
- memory safety issues
- memory management issues
- input validation issues
- resource management issues
- concurrency issues

## Validation and Testing

When changing hint generation behavior, validate both generation and staging:

```bash
uv run pytest tests/test_hint_generation_integration.py
uv run pytest tests/test_trial_preparation.py -k hints
uv run pytest tests/test_validation.py -k sarif
```

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
