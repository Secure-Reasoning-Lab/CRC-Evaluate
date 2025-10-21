# Report Generation Design

This document describes the design and implementation of the report generation module for CRSBench, enabling comprehensive analysis and visualization of CRS trial results.

## Table of Contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [Data Flow](#data-flow)
4. [Core Components](#core-components)
5. [Report Types](#report-types)
6. [Output Formats](#output-formats)
7. [Integration Points](#integration-points)
8. [Snapshot Processing](#snapshot-processing)
9. [Metrics Aggregation](#metrics-aggregation)
10. [Implementation Checklist](#implementation-checklist)
11. [Future Extensions](#future-extensions)

## Overview

### Purpose

The report generation module transforms raw trial snapshots into comprehensive, actionable reports that enable:

- **Performance Analysis**: Evaluate CRS effectiveness across benchmarks
- **Comparative Analysis**: Compare different CRS implementations
- **Time-Series Analysis**: Track POV/patch discovery over time
- **Resource Analysis**: Understand LLM token usage and cost patterns
- **Quality Assessment**: Evaluate patch quality and POV deduplication

### Scope

**What the reporting module does:**
- Load and parse snapshot archives from completed trials
- Interface with reproducer module for POV/patch deduplication and validation
- Aggregate metrics across snapshots and trials
- Generate structured JSON reports
- Generate interactive HTML reports with visualizations

**What the reporting module does NOT do:**
- **POV reproduction/validation**: Handled by reproducer module
- **Patch validation/testing**: Handled by reproducer module
- **Deduplication logic**: Handled by reproducer module
- **Real-time reporting**: Only post-completion analysis

### Design Goals

1. **Post-Completion Focus**: Generate reports after trial completion
2. **Comprehensive Analysis**: Provide detailed metrics and insights
3. **Multiple Formats**: Support both machine-readable (JSON) and human-friendly (HTML) formats
4. **Modular Integration**: Clean interface with reproducer module
5. **Extensible**: Easy to add new report types and metrics

## Architecture

### High-Level Design

```
Experiment Filestore
├── snapshot-0001.tar.gz
├── snapshot-0002.tar.gz
└── snapshot-*.tar.gz
        ↓
    SnapshotLoader
    (extract & parse)
        ↓
    Raw POVs/Patches
        ↓
    ReproducerInterface
    (deduplicate & validate)
    ↓ (calls crsbench.reproducer)
    Deduplicated Results
        ↓
    MetricsAggregator
    (compute metrics)
        ↓
    ReportGenerator
    (JSON + HTML)
        ↓
    Report Filestore
    ├── trial-reports/
    ├── experiment-report.json
    └── experiment-report.html
```

### Module Structure

```
crsbench/reporting/
├── __init__.py              # Public API exports
├── snapshot_loader.py       # Snapshot extraction and parsing
├── reproducer_interface.py  # Interface to reproducer module
├── metrics.py               # Metrics aggregation logic
├── report_generator.py      # Base report generation
├── json_generator.py        # JSON report generation
├── html_generator.py        # HTML report generation
└── analyzers.py             # Time-series and analysis utilities
```

### Design Philosophy

1. **Separation of Concerns**: Reporting focuses on presentation, reproducer handles validation
2. **Late Binding**: Deduplication/validation happens during report generation, not during trial
3. **Snapshot-Centric**: All data sourced from snapshot archives
4. **Format Agnostic**: Core logic independent of output format

## Data Flow

### End-to-End Flow

```
1. Report Request
   └─ Experiment name, trial IDs

2. Snapshot Discovery
   └─ Scan experiment_filestore for snapshot archives
   └─ Load snapshot-*.tar.gz files

3. Snapshot Parsing
   └─ Extract metadata.json (timestamp, cycle, etc.)
   └─ Extract POVs/ directory (binary blobs)
   └─ Extract patches/ directory (organized by POV ID: patches/<pov_id>/patch.diff)
   └─ Extract llm-usage.json (full LLM metrics)
   └─ Extract crs-output.log (full CRS logs)

4. Deduplication & Validation (via Reproducer Module)
   └─ POV Deduplication
      └─ Input: All POV binary blobs
      └─ Output: Unique POVs by root cause
   └─ POV Validation
      └─ Input: Unique POVs
      └─ Output: Validation results (triggers crash, sanitizer output)
   └─ Patch Deduplication
      └─ Input: All patch files
      └─ Output: Unique patches
   └─ Patch Validation
      └─ Input: Unique patches
      └─ Output: Validation results (fixes vuln, passes invariants)

5. Metrics Aggregation
   └─ Time-to-discovery (first POV per vulnerability)
   └─ Cost-per-POV (LLM tokens + compute)
   └─ Discovery rate over time
   └─ Patch success rate
   └─ Resource utilization patterns

6. Report Generation
   └─ JSON: Structured data export
   └─ HTML: Interactive visualizations
```

### Snapshot Loading Process

```python
# Pseudocode
for snapshot_archive in trial_dir.glob("snapshot-*.tar.gz"):
    # Check completion marker
    if not (trial_dir / f"{snapshot_archive.stem}.complete").exists():
        continue  # Skip incomplete snapshots

    # Extract to temp directory
    with tarfile.open(snapshot_archive, 'r:gz') as tar:
        tar.extractall(temp_dir)

    # Parse snapshot data
    metadata = json.loads((temp_dir / "metadata.json").read_text())
    povs = list((temp_dir / "povs").glob("pov_*"))
    # Patches organized by POV ID: patches/<pov_id>/patch.diff
    patches = list((temp_dir / "patches").glob("*/patch.diff"))
    llm_usage = json.loads((temp_dir / "llm-usage.json").read_text())
    crs_log = (temp_dir / "crs-output.log").read_text()

    # Store parsed snapshot
    snapshots.append(SnapshotData(...))
```

## Core Components

### 1. SnapshotLoader

**Purpose**: Extract and parse snapshot archives.

**Responsibilities**:
- Discover snapshot archives in trial directories
- Validate snapshot completeness (check .complete markers)
- Extract tar.gz archives to temporary directories
- Parse snapshot metadata and contents
- Build SnapshotData objects

**Key Methods**:
```python
class SnapshotLoader:
    def load_trial_snapshots(self, trial_dir: Path) -> List[SnapshotData]:
        """Load all complete snapshots for a trial."""

    def load_snapshot(self, snapshot_path: Path) -> SnapshotData:
        """Load and parse a single snapshot archive."""

    def _validate_snapshot(self, snapshot_dir: Path) -> bool:
        """Validate snapshot structure and required files."""
```

**Data Structures**:
```python
@dataclass
class SnapshotData:
    """Parsed snapshot data."""
    cycle: int
    timestamp: float
    elapsed_time: float

    # Raw data (before deduplication)
    pov_files: List[Path]          # Binary POV blobs
    patch_files: List[Path]        # .diff patch files

    # Metadata
    llm_usage: Dict[str, Any]      # Full LLM usage metrics
    crs_log: str                   # Complete CRS log

    # Corpus (if present)
    corpus_files: List[Path]       # Fuzzing corpus files
```

### 2. ReproducerInterface

**Purpose**: Interface with reproducer module for deduplication and validation.

**Responsibilities**:
- Call reproducer module for POV deduplication
- Call reproducer module for POV validation
- Call reproducer module for patch deduplication
- Call reproducer module for patch validation
- Map reproducer results to report-friendly format

**Key Methods**:
```python
class ReproducerInterface:
    def deduplicate_povs(
        self,
        pov_files: List[Path],
        benchmark_path: Path
    ) -> DeduplicationResult:
        """Deduplicate POVs by root cause.

        Calls: crsbench.reproducer.deduplicate_povs()

        Returns:
            Unique POVs grouped by root cause
        """

    def validate_povs(
        self,
        unique_povs: List[Path],
        benchmark_path: Path
    ) -> ValidationResult:
        """Validate POVs trigger expected crashes.

        Calls: crsbench.reproducer.validate_povs()

        Returns:
            Validation results (crash info, sanitizer output)
        """

    def deduplicate_patches(
        self,
        patch_files: List[Path]
    ) -> DeduplicationResult:
        """Deduplicate patches.

        Calls: crsbench.reproducer.deduplicate_patches()
        """

    def validate_patches(
        self,
        unique_patches: List[Path],
        benchmark_path: Path
    ) -> ValidationResult:
        """Validate patches fix vulnerabilities.

        Calls: crsbench.reproducer.validate_patches()
        """
```

**Deduplication Result Format**:
```python
@dataclass
class DeduplicationResult:
    """Result from deduplication."""
    unique_items: List[Path]              # Deduplicated items
    duplicate_groups: Dict[str, List[Path]]  # Duplicates grouped by canonical item
    dedup_metadata: Dict[str, Any]        # Deduplication metadata
```

**Validation Result Format**:
```python
@dataclass
class ValidationResult:
    """Result from validation."""
    validated_items: List[ValidatedItem]
    validation_metadata: Dict[str, Any]

@dataclass
class ValidatedItem:
    file_path: Path
    is_valid: bool
    error_message: Optional[str]
    validation_details: Dict[str, Any]  # POV: sanitizer output, Patch: test results
```

### 3. MetricsAggregator

**Purpose**: Compute and aggregate metrics from snapshot data.

**Responsibilities**:
- Compute time-to-discovery metrics
- Calculate cost-per-POV
- Track discovery rate over time
- Aggregate LLM usage statistics
- Compute patch success rates

**Key Methods**:
```python
class MetricsAggregator:
    def aggregate_trial_metrics(
        self,
        snapshots: List[SnapshotData],
        dedup_results: DeduplicationResult,
        validation_results: ValidationResult
    ) -> TrialMetrics:
        """Aggregate metrics for a single trial."""

    def aggregate_experiment_metrics(
        self,
        trial_metrics: List[TrialMetrics]
    ) -> ExperimentMetrics:
        """Aggregate metrics across all trials."""

    def compute_time_series(
        self,
        snapshots: List[SnapshotData]
    ) -> TimeSeriesData:
        """Compute time-series metrics from snapshots."""
```

**Metrics Data Structures**:
```python
@dataclass
class TrialMetrics:
    """Metrics for a single trial."""
    trial_id: str
    benchmark: str
    crs: str

    # POV metrics
    total_povs_discovered: int
    unique_povs: int
    duplicate_povs: int
    povs_by_timestamp: List[Tuple[float, str]]  # (timestamp, pov_id)

    # Patch metrics
    total_patches_generated: int
    unique_patches: int
    valid_patches: int
    patch_success_rate: float

    # Cost metrics
    total_llm_cost: float
    total_llm_tokens: int
    cost_per_pov: float

    # Time metrics
    total_time: float
    time_to_first_pov: Optional[float]

    # Resource metrics
    llm_usage_by_model: Dict[str, Dict[str, Any]]

@dataclass
class ExperimentMetrics:
    """Aggregated metrics across experiment."""
    experiment_name: str
    total_trials: int

    # Summary statistics
    avg_povs_per_trial: float
    avg_patches_per_trial: float
    avg_cost_per_trial: float

    # Per-CRS breakdown
    metrics_by_crs: Dict[str, TrialMetrics]

    # Per-benchmark breakdown
    metrics_by_benchmark: Dict[str, TrialMetrics]

@dataclass
class TimeSeriesData:
    """Time-series data from snapshots."""
    timestamps: List[float]
    povs_discovered: List[int]       # Cumulative POVs at each timestamp
    patches_generated: List[int]     # Cumulative patches
    llm_tokens_used: List[int]       # Cumulative tokens
    llm_cost: List[float]            # Cumulative cost
```

### 4. ReportGenerator

**Purpose**: Generate reports in different formats.

**Base Class**:
```python
class ReportGenerator(ABC):
    """Abstract base for report generators."""

    @abstractmethod
    def generate_trial_report(
        self,
        trial_metrics: TrialMetrics,
        snapshots: List[SnapshotData]
    ) -> Path:
        """Generate report for a single trial."""

    @abstractmethod
    def generate_experiment_report(
        self,
        experiment_metrics: ExperimentMetrics,
        trial_reports: List[Path]
    ) -> Path:
        """Generate aggregate experiment report."""
```

**JSON Generator**:
```python
class JSONReportGenerator(ReportGenerator):
    """Generate JSON reports."""

    def generate_trial_report(self, trial_metrics: TrialMetrics, ...) -> Path:
        """Generate JSON trial report."""
        report = {
            "trial_id": trial_metrics.trial_id,
            "metrics": trial_metrics.to_dict(),
            "snapshots": [snap.to_dict() for snap in snapshots],
            "timeline": self._compute_timeline(snapshots)
        }

        output_path = self.output_dir / f"trial-{trial_metrics.trial_id}.json"
        output_path.write_text(json.dumps(report, indent=2))
        return output_path
```

**HTML Generator**:
```python
class HTMLReportGenerator(ReportGenerator):
    """Generate interactive HTML reports."""

    def generate_trial_report(self, trial_metrics: TrialMetrics, ...) -> Path:
        """Generate HTML trial report with charts."""
        # Use template engine (Jinja2) + charting library (Plotly)
        template = self._load_template("trial_report.html")

        charts = {
            "discovery_timeline": self._create_discovery_chart(snapshots),
            "llm_cost_breakdown": self._create_cost_chart(trial_metrics),
            "resource_usage": self._create_resource_chart(trial_metrics)
        }

        html = template.render(
            metrics=trial_metrics,
            charts=charts,
            snapshots=snapshots
        )

        output_path = self.output_dir / f"trial-{trial_metrics.trial_id}.html"
        output_path.write_text(html)
        return output_path
```

### 5. TimeSeriesAnalyzer

**Purpose**: Analyze time-series data from snapshots.

**Responsibilities**:
- Track POV discovery timeline
- Analyze discovery rate patterns
- Identify discovery plateaus
- Compute cost efficiency over time

**Key Methods**:
```python
class TimeSeriesAnalyzer:
    def analyze_discovery_timeline(
        self,
        snapshots: List[SnapshotData]
    ) -> DiscoveryTimeline:
        """Analyze POV discovery over time."""

    def compute_discovery_rate(
        self,
        timeline: DiscoveryTimeline
    ) -> List[float]:
        """Compute discovery rate (POVs per hour)."""

    def identify_plateaus(
        self,
        timeline: DiscoveryTimeline
    ) -> List[PlateauPeriod]:
        """Identify periods with no new discoveries."""
```

## Report Types

### 1. Trial Report

**Scope**: Individual trial (single CRS + benchmark + trial number)

**Contents**:
- Trial metadata (CRS, benchmark, trial number)
- POV discovery timeline
- Patch generation summary
- LLM usage breakdown
- Cost analysis
- Snapshot-by-snapshot progression

**Output Files**:
- `trial-<trial_id>.json`
- `trial-<trial_id>.html`

### 2. Experiment Report

**Scope**: Entire experiment (all CRSes, benchmarks, trials)

**Contents**:
- Experiment overview
- Aggregate metrics across all trials
- Per-CRS performance comparison
- Per-benchmark difficulty analysis
- Cost-effectiveness ranking
- Success rate statistics

**Output Files**:
- `experiment-<name>.json`
- `experiment-<name>.html`

### 3. Comparison Report

**Scope**: Compare multiple CRSes on same benchmarks

**Contents**:
- Side-by-side CRS comparison
- Relative performance metrics
- Statistical significance tests
- Strengths/weaknesses analysis
- Cost-performance trade-offs

**Output Files**:
- `comparison-<crs1>-vs-<crs2>.json`
- `comparison-<crs1>-vs-<crs2>.html`

### 4. Time-Series Report

**Scope**: Discovery timeline across snapshots

**Contents**:
- POV discovery curve
- Patch generation curve
- LLM token consumption over time
- Cost accumulation over time
- Discovery rate analysis

**Output Files**:
- `timeline-<trial_id>.json`
- `timeline-<trial_id>.html` (with interactive charts)

## Output Formats

### JSON Format

**Purpose**: Machine-readable structured data for programmatic analysis.

**Trial Report Structure**:
```json
{
  "report_type": "trial",
  "trial_id": "trial-1",
  "benchmark": "json-c",
  "crs": "ensemble-c",
  "timestamp": "2025-01-15T10:00:00Z",

  "summary": {
    "total_povs_discovered": 5,
    "unique_povs": 3,
    "total_patches": 8,
    "valid_patches": 6,
    "total_cost": 12.50,
    "total_time": 3600.0
  },

  "povs": [
    {
      "id": "pov_001",
      "discovered_at": 1200.0,
      "snapshot_cycle": 3,
      "is_unique": true,
      "duplicate_of": null,
      "validation": {
        "is_valid": true,
        "sanitizer": "address",
        "error_type": "heap-buffer-overflow"
      }
    }
  ],

  "patches": [
    {
      "id": "patch_001",
      "generated_at": 1500.0,
      "snapshot_cycle": 4,
      "is_unique": true,
      "validation": {
        "fixes_vuln": true,
        "passes_invariants": true
      }
    }
  ],

  "llm_usage": {
    "total_tokens": 50000,
    "total_cost": 10.00,
    "by_model": {
      "claude-sonnet-4": {
        "tokens": 30000,
        "cost": 7.50
      }
    }
  },

  "timeline": {
    "snapshots": [
      {
        "cycle": 1,
        "timestamp": 900.0,
        "povs_count": 2,
        "patches_count": 1,
        "llm_cost": 2.50
      }
    ]
  }
}
```

**Experiment Report Structure**:
```json
{
  "report_type": "experiment",
  "experiment_name": "test-experiment",
  "timestamp": "2025-01-15T12:00:00Z",

  "summary": {
    "total_trials": 6,
    "total_crses": 2,
    "total_benchmarks": 3,
    "avg_povs_per_trial": 4.2,
    "avg_cost_per_trial": 15.00
  },

  "by_crs": {
    "ensemble-c": {
      "trials": 3,
      "avg_povs": 5.0,
      "avg_cost": 12.00,
      "success_rate": 0.85
    },
    "multi-retrieval": {
      "trials": 3,
      "avg_povs": 3.4,
      "avg_cost": 18.00,
      "success_rate": 0.70
    }
  },

  "by_benchmark": {
    "json-c": {
      "difficulty": 3,
      "trials": 2,
      "avg_povs": 4.5,
      "avg_time_to_first_pov": 800.0
    }
  }
}
```

### HTML Format

**Purpose**: Interactive human-friendly reports with visualizations.

**Key Features**:
- **Interactive Charts**: Plotly.js or D3.js for dynamic visualizations
- **Responsive Layout**: Works on desktop and mobile
- **Exportable**: Save charts as images
- **Filterable**: Interactive filtering of data

**Visualization Types**:

1. **Discovery Timeline Chart**:
   - X-axis: Time (seconds or snapshots)
   - Y-axis: Cumulative POVs discovered
   - Line chart showing discovery curve

2. **Cost Breakdown Chart**:
   - Pie chart: LLM cost by model
   - Bar chart: Cost per CRS
   - Stacked area: Cost accumulation over time

3. **Resource Usage Chart**:
   - Line chart: Token usage over time
   - Bar chart: Tokens by operation type
   - Comparison: Cached vs new tokens

4. **Comparison Chart** (multi-CRS):
   - Grouped bar chart: POVs found per CRS
   - Box plot: Cost distribution
   - Scatter plot: Cost vs performance

**HTML Template Structure**:
```html
<!DOCTYPE html>
<html>
<head>
    <title>Trial Report - {{ trial_id }}</title>
    <script src="https://cdn.plot.ly/plotly-latest.min.js"></script>
    <style>
        /* Responsive CSS */
    </style>
</head>
<body>
    <h1>Trial Report: {{ trial_id }}</h1>

    <section id="summary">
        <h2>Summary</h2>
        <table>
            <tr><td>POVs Discovered:</td><td>{{ metrics.total_povs_discovered }}</td></tr>
            <tr><td>Unique POVs:</td><td>{{ metrics.unique_povs }}</td></tr>
            <tr><td>Total Cost:</td><td>${{ metrics.total_llm_cost }}</td></tr>
        </table>
    </section>

    <section id="charts">
        <h2>Visualizations</h2>
        <div id="discovery-timeline">{{ charts.discovery_timeline | safe }}</div>
        <div id="cost-breakdown">{{ charts.llm_cost_breakdown | safe }}</div>
        <div id="resource-usage">{{ charts.resource_usage | safe }}</div>
    </section>

    <section id="snapshots">
        <h2>Snapshot Timeline</h2>
        {% for snapshot in snapshots %}
        <div class="snapshot">
            <h3>Snapshot {{ snapshot.cycle }} ({{ snapshot.elapsed_time }}s)</h3>
            <p>POVs: {{ snapshot.pov_files | length }}</p>
            <p>Patches: {{ snapshot.patch_files | length }}</p>
        </div>
        {% endfor %}
    </section>
</body>
</html>
```

## Integration Points

### With Snapshot Module

**Input**: Reads snapshot archives from experiment_filestore

```python
# Snapshot directory structure (from snapshots design)
experiment_filestore/
└── {experiment_name}/
    └── {benchmark_id}__{crs_name}/
        └── trial-{trial_id}/
            ├── snapshot-0001.tar.gz
            ├── snapshot-0001.complete
            ├── snapshot-0002.tar.gz
            ├── snapshot-0002.complete
            └── ...
```

**Usage**:
```python
from crsbench.reporting import SnapshotLoader

loader = SnapshotLoader()
trial_dir = Path("experiment_filestore/test-exp/json-c__ensemble-c/trial-1")
snapshots = loader.load_trial_snapshots(trial_dir)
```

### With Reproducer Module

**Purpose**: Call reproducer for POV/patch deduplication and validation.

**Expected Interface** (to be implemented by reproducer module):
```python
# crsbench.reproducer module (separate, to be created)

def deduplicate_povs(
    pov_files: List[Path],
    benchmark_path: Path
) -> DeduplicationResult:
    """Deduplicate POVs by root cause analysis.

    Args:
        pov_files: List of POV binary blobs
        benchmark_path: Path to benchmark for context

    Returns:
        Deduplication result with unique POVs
    """

def validate_povs(
    pov_files: List[Path],
    benchmark_path: Path,
    harness_name: str
) -> ValidationResult:
    """Validate POVs trigger expected crashes.

    Args:
        pov_files: List of POV files to validate
        benchmark_path: Path to benchmark
        harness_name: Harness to run POVs against

    Returns:
        Validation result with crash details
    """

def deduplicate_patches(
    patch_files: List[Path]
) -> DeduplicationResult:
    """Deduplicate patches by semantic equivalence."""

def validate_patches(
    patch_files: List[Path],
    benchmark_path: Path
) -> ValidationResult:
    """Validate patches fix vulnerabilities and pass invariants."""
```

**Usage in Reporting**:
```python
from crsbench.reporting import ReproducerInterface
from crsbench.reproducer import (
    deduplicate_povs,
    validate_povs,
    deduplicate_patches,
    validate_patches
)

reproducer = ReproducerInterface()

# Deduplicate POVs
dedup_result = reproducer.deduplicate_povs(
    pov_files=[Path("pov_001"), Path("pov_002")],
    benchmark_path=Path("benchmarks/json-c")
)

# Validate unique POVs
val_result = reproducer.validate_povs(
    unique_povs=dedup_result.unique_items,
    benchmark_path=Path("benchmarks/json-c")
)
```

### With Validation Module

**Purpose**: Reuse benchmark validation utilities.

```python
from crsbench.validation import validate_benchmark

# Validate benchmark before processing
validation_result = validate_benchmark(benchmark_path)
if not validation_result.is_valid:
    raise ReportError(f"Invalid benchmark: {validation_result.summary()}")
```

### Output to Report Filestore

**Structure**:
```
report_filestore/
└── {experiment_name}/
    ├── experiment-{name}.json
    ├── experiment-{name}.html
    ├── trial-reports/
    │   ├── trial-1.json
    │   ├── trial-1.html
    │   ├── trial-2.json
    │   ├── trial-2.html
    │   └── ...
    ├── comparison-reports/
    │   ├── crs1-vs-crs2.json
    │   └── crs1-vs-crs2.html
    └── timeline-reports/
        ├── timeline-trial-1.json
        └── timeline-trial-1.html
```

**Usage**:
```python
from crsbench.reporting import ReportGenerator

generator = ReportGenerator(
    output_dir=Path("report_filestore/test-exp")
)

# Generate trial report
trial_report = generator.generate_trial_report(
    trial_metrics=metrics,
    snapshots=snapshots,
    format="both"  # JSON + HTML
)
```

## Snapshot Processing

### Snapshot Discovery

**Process**:
1. Scan trial directory for `snapshot-*.tar.gz` files
2. Check for corresponding `.complete` marker
3. Sort by cycle number (extracted from filename)
4. Return list of valid snapshot paths

**Implementation**:
```python
def discover_snapshots(trial_dir: Path) -> List[Path]:
    """Discover complete snapshots in trial directory."""
    snapshots = []

    for snapshot_archive in sorted(trial_dir.glob("snapshot-*.tar.gz")):
        # Extract cycle number from filename
        match = re.match(r"snapshot-(\d+)\.tar\.gz", snapshot_archive.name)
        if not match:
            continue

        cycle = int(match.group(1))

        # Check completion marker
        marker = trial_dir / f"snapshot-{cycle:04d}.complete"
        if not marker.exists():
            logger.warning(f"Skipping incomplete snapshot: {snapshot_archive}")
            continue

        snapshots.append(snapshot_archive)

    return snapshots
```

### Snapshot Extraction

**Process**:
1. Create temporary directory for extraction
2. Extract tar.gz archive
3. Validate required files exist
4. Parse and return snapshot data
5. Cleanup temporary directory

**Implementation**:
```python
def extract_snapshot(snapshot_path: Path) -> SnapshotData:
    """Extract and parse snapshot archive."""
    with tempfile.TemporaryDirectory() as tmpdir:
        temp_dir = Path(tmpdir)

        # Extract archive
        with tarfile.open(snapshot_path, 'r:gz') as tar:
            tar.extractall(temp_dir)

        # Validate structure
        snapshot_dir = temp_dir / snapshot_path.stem
        if not snapshot_dir.exists():
            raise ReportError(f"Invalid snapshot structure: {snapshot_path}")

        # Parse metadata
        metadata = json.loads((snapshot_dir / "metadata.json").read_text())

        # Parse POVs (binary blobs)
        pov_files = list((snapshot_dir / "povs").glob("pov_*")) if (snapshot_dir / "povs").exists() else []

        # Parse patches (organized by POV ID: patches/<pov_id>/patch.diff)
        patch_files = list((snapshot_dir / "patches").glob("*/patch.diff")) if (snapshot_dir / "patches").exists() else []

        # Parse LLM usage
        llm_usage = json.loads((snapshot_dir / "llm-usage.json").read_text())

        # Parse CRS log
        crs_log = (snapshot_dir / "crs-output.log").read_text()

        # Copy files to persistent location for reproducer
        persistent_dir = snapshot_path.parent / f".snapshot-data-{metadata['cycle']:04d}"
        persistent_dir.mkdir(exist_ok=True)

        for pov in pov_files:
            shutil.copy(pov, persistent_dir / pov.name)

        for patch in patch_files:
            shutil.copy(patch, persistent_dir / patch.name)

        return SnapshotData(
            cycle=metadata["cycle"],
            timestamp=metadata["timestamp"],
            elapsed_time=metadata["elapsed_time"],
            pov_files=[persistent_dir / p.name for p in pov_files],
            patch_files=[persistent_dir / p.name for p in patch_files],
            llm_usage=llm_usage,
            crs_log=crs_log
        )
```

### Data Aggregation Across Snapshots

**Process**:
1. Load all snapshots in chronological order
2. Aggregate incremental data (POVs, patches)
3. Compute cumulative metrics
4. Build complete timeline

**Implementation**:
```python
def aggregate_snapshot_data(snapshots: List[SnapshotData]) -> AggregatedData:
    """Aggregate data across all snapshots."""
    all_povs = []
    all_patches = []
    timeline = []

    for snapshot in sorted(snapshots, key=lambda s: s.cycle):
        # Collect POVs/patches (incremental in each snapshot)
        all_povs.extend(snapshot.pov_files)
        all_patches.extend(snapshot.patch_files)

        # Build timeline entry
        timeline.append({
            "cycle": snapshot.cycle,
            "timestamp": snapshot.timestamp,
            "elapsed_time": snapshot.elapsed_time,
            "povs_count": len(all_povs),
            "patches_count": len(all_patches),
            "llm_cost": snapshot.llm_usage.get("total_cost_usd", 0.0),
            "llm_tokens": snapshot.llm_usage.get("total_input_tokens", 0) + snapshot.llm_usage.get("total_output_tokens", 0)
        })

    return AggregatedData(
        all_povs=all_povs,
        all_patches=all_patches,
        timeline=timeline
    )
```

## Metrics Aggregation

### POV Metrics

**Time-to-Discovery**:
```python
def compute_time_to_discovery(
    snapshots: List[SnapshotData],
    dedup_result: DeduplicationResult
) -> Dict[str, float]:
    """Compute time-to-discovery for each unique POV."""

    discovery_times = {}

    for unique_pov in dedup_result.unique_items:
        # Find first snapshot containing this POV
        for snapshot in sorted(snapshots, key=lambda s: s.cycle):
            if unique_pov in snapshot.pov_files:
                discovery_times[unique_pov.name] = snapshot.elapsed_time
                break

    return discovery_times
```

**Discovery Rate**:
```python
def compute_discovery_rate(
    snapshots: List[SnapshotData]
) -> List[float]:
    """Compute discovery rate (POVs per hour)."""

    rates = []
    prev_povs = 0
    prev_time = 0.0

    for snapshot in sorted(snapshots, key=lambda s: s.cycle):
        povs_count = len(snapshot.pov_files)
        time_delta = snapshot.elapsed_time - prev_time

        if time_delta > 0:
            rate = (povs_count - prev_povs) / (time_delta / 3600.0)  # POVs per hour
            rates.append(rate)

        prev_povs = povs_count
        prev_time = snapshot.elapsed_time

    return rates
```

### Cost Metrics

**Cost-per-POV**:
```python
def compute_cost_per_pov(
    total_cost: float,
    unique_povs_count: int
) -> float:
    """Compute average cost per unique POV."""

    if unique_povs_count == 0:
        return 0.0

    return total_cost / unique_povs_count
```

**LLM Cost Breakdown**:
```python
def compute_llm_cost_breakdown(
    snapshots: List[SnapshotData]
) -> Dict[str, float]:
    """Compute LLM cost breakdown by model."""

    # Get final snapshot (cumulative data)
    final_snapshot = max(snapshots, key=lambda s: s.cycle)
    llm_usage = final_snapshot.llm_usage

    cost_by_model = {}
    if "by_model" in llm_usage:
        for model, usage in llm_usage["by_model"].items():
            cost_by_model[model] = usage.get("cost_usd", 0.0)

    return cost_by_model
```

### Patch Metrics

**Patch Success Rate**:
```python
def compute_patch_success_rate(
    validation_result: ValidationResult
) -> float:
    """Compute percentage of patches that fix vulnerabilities."""

    total_patches = len(validation_result.validated_items)
    if total_patches == 0:
        return 0.0

    valid_patches = sum(
        1 for item in validation_result.validated_items
        if item.is_valid
    )

    return valid_patches / total_patches
```

### Resource Metrics

**Token Efficiency**:
```python
def compute_token_efficiency(
    snapshots: List[SnapshotData],
    unique_povs_count: int
) -> Dict[str, Any]:
    """Compute token usage efficiency metrics."""

    final_snapshot = max(snapshots, key=lambda s: s.cycle)
    llm_usage = final_snapshot.llm_usage

    total_tokens = llm_usage.get("total_input_tokens", 0) + llm_usage.get("total_output_tokens", 0)
    cached_tokens = llm_usage.get("total_cached_tokens", 0)

    return {
        "total_tokens": total_tokens,
        "cached_tokens": cached_tokens,
        "cache_hit_rate": cached_tokens / total_tokens if total_tokens > 0 else 0.0,
        "tokens_per_pov": total_tokens / unique_povs_count if unique_povs_count > 0 else 0.0
    }
```

## Implementation Checklist

### Phase 1: Core Infrastructure

- [ ] Create `crsbench/reporting/` module
  - [ ] `__init__.py` with public API exports
  - [ ] `snapshot_loader.py` - Snapshot extraction
  - [ ] `reproducer_interface.py` - Interface to reproducer
  - [ ] `metrics.py` - Metrics aggregation
  - [ ] `errors.py` - Reporting-specific errors

### Phase 2: Report Generation

- [ ] Implement base report generator
  - [ ] `report_generator.py` - Abstract base class
  - [ ] `json_generator.py` - JSON report generation
  - [ ] `html_generator.py` - HTML report generation
  - [ ] HTML templates directory structure

### Phase 3: Analysis Utilities

- [ ] Implement analysis tools
  - [ ] `analyzers.py` - Time-series analysis
  - [ ] Chart generation utilities
  - [ ] Statistics computation

### Phase 4: Integration

- [ ] Integrate with existing modules
  - [ ] Interface with snapshot module (read-only)
  - [ ] Define reproducer interface (contract)
  - [ ] Integrate with validation module

### Phase 5: CLI Entry Point

- [ ] Add report generation command
  - [ ] `crsbench report --experiment <name>` command
  - [ ] CLI argument parsing
  - [ ] Progress reporting

### Phase 6: Testing

- [ ] Create test suite
  - [ ] `tests/test_snapshot_loader.py`
  - [ ] `tests/test_reproducer_interface.py`
  - [ ] `tests/test_metrics.py`
  - [ ] `tests/test_report_generation.py`
  - [ ] Integration tests with mock snapshots

### Phase 7: Documentation

- [ ] Update documentation
  - [ ] Module README.md
  - [ ] Usage examples
  - [ ] API documentation

## Future Extensions

### 1. Real-Time Reporting (Progressive Mode)

Add support for generating intermediate reports from partial snapshots:

```python
class ProgressiveReportGenerator:
    """Generate reports during trial execution."""

    def watch_trial(self, trial_dir: Path, interval: int = 900):
        """Watch trial directory and generate reports as snapshots arrive."""
        while trial_in_progress(trial_dir):
            new_snapshots = discover_new_snapshots(trial_dir)
            if new_snapshots:
                self.generate_intermediate_report(new_snapshots)
            time.sleep(interval)
```

### 2. Additional Report Formats

Support more output formats:

- **CSV**: Tabular data export for spreadsheet analysis
- **PDF**: Static printable reports
- **Markdown**: GitHub-friendly reports for CI/CD

### 3. Advanced Visualizations

Add more sophisticated visualizations:

- **Heatmaps**: POV discovery patterns across benchmarks
- **Sankey Diagrams**: Resource flow (compute → LLM → discoveries)
- **Network Graphs**: Vulnerability relationships
- **3D Plots**: Cost vs Performance vs Time

### 4. Statistical Analysis

Add statistical analysis capabilities:

- **Confidence Intervals**: For success rate metrics
- **Hypothesis Testing**: Compare CRS implementations statistically
- **Regression Analysis**: Predict performance based on features
- **Outlier Detection**: Identify anomalous trials

### 5. Report Templates

Support customizable report templates:

```python
class CustomReportTemplate:
    """User-defined report template."""

    def __init__(self, template_path: Path):
        self.template = self._load_template(template_path)

    def render(self, metrics: TrialMetrics) -> str:
        """Render report using custom template."""
```

### 6. Report Comparison

Compare reports across experiments:

```python
def compare_experiments(
    exp1_report: Path,
    exp2_report: Path
) -> ComparisonReport:
    """Generate comparison report between two experiments."""
```

### 7. Export to Analysis Platforms

Export to external analysis platforms:

- **Jupyter Notebooks**: Generate analysis notebooks
- **Weights & Biases**: Upload metrics to W&B
- **MLflow**: Log experiments to MLflow
- **TensorBoard**: Visualize in TensorBoard

### 8. Automated Insights

Add AI-powered insights:

```python
class InsightGenerator:
    """Generate automated insights from reports."""

    def analyze_performance(self, metrics: TrialMetrics) -> List[Insight]:
        """Generate insights about CRS performance."""
        insights = []

        # Cost efficiency insight
        if metrics.cost_per_pov > threshold:
            insights.append(Insight(
                type="cost",
                message="High cost per POV suggests inefficient LLM usage",
                recommendation="Consider caching or model optimization"
            ))

        return insights
```

## References

- [Snapshot Implementation](../evaluation/snapshots.md): Snapshot design and structure
- [Architecture](../architecture.md): Overall CRSBench architecture
- [Evaluation Module](../evaluation/evaluation.md): Evaluation and result collection
- [Benchmark Specification](../../docs/benchmark-spec.md): Benchmark format
