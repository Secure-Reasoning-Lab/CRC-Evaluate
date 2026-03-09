# Benchmark Module

This module manages the complete benchmark lifecycle in CRSBench.

## Lifecycle Phases

```
Generation ──► Packaging ──► Publish ──► Runtime
(create)       (bundle)      (distribute) (load & use)
```

## Module Structure

```
benchmark/
├── generation/     # Create benchmarks from external sources (future)
├── packaging/      # Bundle benchmarks for distribution
└── runtime/        # Load benchmarks for CRS evaluation
```

## Reference Surface

### Packaging APIs

```python
from crsbench.benchmark import bundle_benchmark, validate_benchmark
```

Packaging command usage is documented in the benchmark contributor guide.

### Runtime APIs

```python
from crsbench.benchmark import load_benchmark_source
```

Runtime command usage is documented in the experiment guides.

## Documentation

- Design: `../../design/benchmark/benchmark-lifecycle.md`
- Generation plan: `./generation.md`
- Contributor workflow: `../../contributors/benchmark-developer-guide.md`
