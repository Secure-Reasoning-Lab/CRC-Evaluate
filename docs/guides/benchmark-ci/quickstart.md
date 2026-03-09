# Benchmark CI Quickstart

Use this page for the common `benchmark ci` flow.

## Single Benchmark

```bash
crsbench benchmark ci all benchmarks/afc-curl-delta-01
```

## All Benchmarks

```bash
crsbench benchmark ci all --all --output-dir ./ci-results
```

## Distributed CI

```bash
python scripts/valkey-helper.py --password start
crsbench evaluator --ci --build-jobs 8 --build-cores-per-job 16 --verify-jobs 8 --verify-cores-per-job 16
crsbench benchmark ci all --all --distributed --mode snapshot --output-dir ./ci-output
```

## See Also

- Full CLI/options reference: [reference.md](./reference.md)
- CI distributed topology: [distributed.md](./distributed.md)
