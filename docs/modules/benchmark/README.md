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

## Quick Start

### Packaging (Bundle a Benchmark)

```python
from crsbench.benchmark import bundle_benchmark, validate_benchmark

# Validate first
result = validate_benchmark(benchmark_path)
if result.valid:
    # Bundle to create pkgs/
    pkgs_dir = bundle_benchmark(benchmark_path)
```

CLI:
```bash
crsbench benchmark validate ./benchmarks/my-benchmark
crsbench benchmark bundle ./benchmarks/my-benchmark
```

### Runtime (Load for Evaluation)

```python
from crsbench.benchmark import load_benchmark_source

# Auto-detects pkgs/ vs git clone
source = load_benchmark_source(benchmark_path, dest_dir=trial_dir)

if source.requires_source_path:
    cmd.extend(["--source-path", str(source.path)])
```

## Documentation

- Design: `../../design/benchmark/benchmark-lifecycle.md`
- Generation plan: `./generation.md`
