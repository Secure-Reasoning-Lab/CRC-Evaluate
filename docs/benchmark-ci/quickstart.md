# Benchmark CI Quickstart

Use this page for the common `benchmark ci` flow.

## Single Benchmark

```bash
uv run crsbench benchmark ci all benchmarks/afc-curl-delta-01
```

## All Benchmarks

```bash
uv run crsbench benchmark ci all --all --output-dir ./ci-results
```

## Distributed CI

```bash
uv run python scripts/valkey-helper.py --password start
uv run crsbench evaluator --ci --jobs 8 --cores-per-job 16
uv run crsbench benchmark ci all --all --distributed --mode snapshot --output-dir ./ci-output
```

## See Also

- Full CLI/options reference: [reference.md](../reference/benchmark-ci.md)
- CI distributed topology: [distributed.md](./distributed.md)
