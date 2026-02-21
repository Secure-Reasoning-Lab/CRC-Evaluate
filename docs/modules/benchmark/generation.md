# Benchmark Generation

This module will provide tools to **create new benchmarks** from various vulnerability sources.

## Status

**Not yet implemented** - Placeholder for future development.

## Scope

This page is intentionally concise. Detailed design and future-generation plans are maintained in:
- `docs/design/benchmark/benchmark-lifecycle.md`

### Planned Capability Summary

- Source adapters for vulnerability feeds and manual input
- Normalization into CRSBench benchmark structure
- Generator orchestration that outputs packaging-ready benchmark directories
- Future CLI entrypoint under `crsbench benchmark generate ...`

## Related Modules

- `../packaging/` - Package generated benchmarks for distribution
- `../runtime/` - Load packaged benchmarks for evaluation
