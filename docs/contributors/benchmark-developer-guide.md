# Benchmark Developer Guide

Use this guide when creating or updating CRSBench benchmarks.

## Start Here

1. [Benchmark Specification (RFC)](../RFC.md)  
Defines required benchmark structure, metadata, and validation rules.

2. [Module Docs: Benchmark](../modules/benchmark/README.md)  
Benchmark packaging/runtime workflow and CLI usage.

3. [Benchmark CI Guide](../guides/benchmark-ci/reference.md)  
How to validate build/POV/patch/test/coverage in CI.

4. [Testing Setup](./testing.md)  
Local test setup and execution guidance.

## Common Tasks

- Validate benchmark format:
  - `crsbench benchmark validate benchmarks/<benchmark-name>`
- Enforce benchmark path portability policy:
  - `uv run python scripts/ci-tests/check_benchmark_paths.py`
  - Benchmark scripts must use `SRC`/`OUT`/`WORK` and must not use
    legacy hard-coded roots like `/built-src` or `/test-src`.
- Run full benchmark CI checks:
  - `crsbench benchmark ci all benchmarks/<benchmark-name>`
- Build benchmark package:
  - `crsbench benchmark bundle benchmarks/<benchmark-name>`

## Canary and Contamination Lifecycle

Use this flow when preparing datasets for release:

1. Inject canaries into benchmark metadata:
   - `crsbench benchmark inject-canary benchmarks/ --filter "<pattern>"`
2. Validate benchmark format and CI before packaging:
   - `crsbench benchmark validate ...`
   - `crsbench benchmark ci all ...`
3. Package and upload with gated access / DUA flow:
   - `crsbench benchmark bundle ...`
   - `crsbench benchmark upload --dataset crsbench`

Reference:
- [Benchmark Data Protection and AI Contamination](../design/benchmark-protection-and-contamination.md)
- [Dataset Module](../modules/dataset.md)

## Related Design Docs

- [Benchmark Lifecycle Design](../design/benchmark/benchmark-lifecycle.md)
- [Benchmark CI Design](../design/benchmark-ci/benchmark-ci.md)
