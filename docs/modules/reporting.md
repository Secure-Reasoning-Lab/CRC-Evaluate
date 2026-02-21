# Reporting Module

This module handles report generation for CRSBench experiments, transforming raw trial snapshots into comprehensive, actionable reports.

## Purpose

Generate comprehensive reports from trial snapshots after experiment completion, including:

- **Performance Analysis**: Evaluate CRS effectiveness across benchmarks
- **Time-Series Analysis**: Track POV/patch discovery over time
- **Resource Analysis**: Understand LLM token usage and cost patterns
- **Comparative Analysis**: Compare different CRS implementations

## Usage

### CLI Commands

```bash
# Generate report for an experiment
crsbench report --experiment test-experiment --output ./reports

# Generate JSON reports only
crsbench report --experiment test-experiment --format json

# Generate HTML reports only
crsbench report --experiment test-experiment --format html

# Validate experiment completeness only
crsbench report --experiment test-experiment --validate-only

# Generate report for a single trial
crsbench report --trial ./experiment_filestore/test-exp/json-c__ensemble-c/trial-1
```

### Python API

```python
from crsbench.reporting import ReportGenerator
from pathlib import Path

generator = ReportGenerator(output_dir=Path("./reports"))

# Generate reports for an experiment
result = generator.generate_experiment_report(
    experiment_dir=Path("./experiment_filestore/test-exp"),
    format="both"  # "json", "html", or "both"
)

print(f"JSON report: {result['json']}")
print(f"HTML report: {result['html']}")

# Validate experiment completeness
validation_report = generator.validate_experiment(
    Path("./experiment_filestore/test-exp")
)
print(validation_report)
```

## Module Structure

```
crsbench/reporting/
├── __init__.py              # Public API exports
├── errors.py                # Reporting-specific exceptions
├── models.py                # Pydantic data models
├── snapshot_loader.py       # Snapshot extraction and parsing
├── metrics.py               # Metrics aggregation logic
├── validator.py             # Experiment validation
├── orchestrator.py          # Main orchestrator (ReportGenerator)
├── generators/              # Report generators
│   ├── __init__.py
│   ├── html.py              # HTML report with Plotly charts
│   └── json.py              # JSON report generation
└── cli/                     # CLI integration
    ├── __init__.py
    └── report_command.py    # CLI command implementation
```

## Output Structure

```
report_filestore/
└── {experiment_name}/
    ├── experiment-{name}.json      # Aggregate metrics
    ├── experiment-{name}.html      # Interactive dashboard
    └── trial-reports/
        ├── trial-{id}.json
        ├── trial-{id}.html
        └── ...
```

## Key Components

### SnapshotLoader
Loads and parses snapshot archives from trial directories.

```python
from crsbench.reporting import SnapshotLoader

loader = SnapshotLoader()
snapshots = loader.load_trial_snapshots(trial_dir)
```

### MetricsAggregator
Computes trial and experiment level metrics.

```python
from crsbench.reporting import MetricsAggregator, TrialInfo

aggregator = MetricsAggregator()
trial_metrics = aggregator.aggregate_trial(
    trial_info=trial_info,  # TrialInfo from discover_trials()
    snapshots=snapshots
)
```

### ExperimentValidator
Validates experiment completeness and detects issues.

```python
from crsbench.reporting import ExperimentValidator

validator = ExperimentValidator()
result = validator.validate_experiment_completeness(experiment_dir)
print(validator.generate_completeness_report(result))
```

## Report Types

### Trial Report
Individual trial analysis including:
- POV discovery timeline
- Patch generation summary
- LLM usage breakdown
- Cost analysis

### Experiment Report
Aggregate analysis across all trials:
- Per-CRS performance comparison
- Per-benchmark difficulty analysis
- Cost-effectiveness ranking
- Success rate statistics

## Documentation

See [report generation design](../design/reporting/report-generation.md).
