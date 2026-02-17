# Statistics Module

Benchmark statistics collection and reporting for CRSBench.

## Module Structure

```
statistics/
├── __init__.py      # Public API exports
├── models.py        # Data models (BenchmarkInfo, VulnEntry, BenchmarkStats)
├── collector.py     # Benchmark collection + utility functions
├── exporters.py     # CSV export + console output (print_summary)
└── cli.py           # CLI entry point
```

## CLI Usage

```bash
# Via main crsbench command
crsbench stats --summary-only
crsbench stats --output benchmarks.csv
crsbench stats --benchmarks atlanta-curl-delta-01,afc-libxml2-delta-01

# Standalone execution
python -m crsbench.statistics.cli --summary-only
```

### Options

| Option | Description |
|--------|-------------|
| `--summary-only` | Print summary only, no CSV export |
| `--output`, `-o` | Output CSV file path (default: benchmark_stats.csv) |
| `--benchmarks` | Specific benchmarks to analyze (space-separated) |
| `--benchmarks-dir` | Path to benchmarks directory |
| `--include-no-vulns` | Include benchmarks with no vulnerabilities |
| `--verbose`, `-v` | Enable verbose logging |

## Library Usage

```python
from crsbench.statistics import (
    collect_benchmark_stats,
    export_benchmarks_csv,
    print_summary,
    BenchmarkStats,
)

# Collect benchmark data
benchmarks = collect_benchmark_stats(benchmarks_dir)

# Calculate aggregate statistics
stats = BenchmarkStats.from_benchmarks(benchmarks)
print(f"Total: {stats.total_benchmarks} projects, {stats.total_vulns} vulns")

# Export to CSV
export_benchmarks_csv(benchmarks, output_path)

# Print summary to console
print_summary(benchmarks)
```

## Key Classes

### BenchmarkStats

Aggregated statistics with a single source of truth for calculations:

```python
stats = BenchmarkStats.from_benchmarks(benchmarks)
stats.total_benchmarks   # Number of projects
stats.total_vulns        # Number of vulnerabilities
stats.by_source          # Dict[str, CategoryStats] by source (AFC, ASC, etc.)
stats.by_mode            # Dict[str, CategoryStats] by mode (delta, full)
stats.by_language        # Dict[str, CategoryStats] by language
```
