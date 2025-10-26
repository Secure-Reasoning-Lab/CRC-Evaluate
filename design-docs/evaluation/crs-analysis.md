# CRS Analysis Module Design

## Overview

### Purpose

The CRS analysis module provides a **pluggable interface** for CRS-specific data analysis. Each CRS may produce custom debug data, intermediate artifacts, or metrics in the `crs-data/` directory during trial execution. This module allows CRS developers to provide their own analysis scripts to extract insights from their CRS-specific data.

**Key Principle**: CRSBench provides the **interface and orchestration**, CRS developers provide the **analysis logic**.

### Scope

**What this module does:**
- Defines a standard interface for CRS-specific analysis scripts
- Discovers and loads CRS-specific analyzers (e.g., `atlantis_c.py`, `patchagent.py`)
- Executes analyzers on snapshot or final trial data
- Aggregates analysis results for reporting
- Handles errors gracefully (missing analyzer = skip, not fail)

**What this module does NOT do:**
- **No format enforcement**: CRS can write anything to `crs-data/`
- **No data validation**: Analyzers are responsible for handling their own data
- **No built-in analytics**: All CRS-specific logic is in analyzer scripts
- **No modification of CRS data**: Read-only analysis

### Use Cases

1. **Progress Monitoring**: Extract CRS-specific metrics from snapshots
   - Example (Atlantis): Count fuzzing iterations, coverage growth rate
   - Example (PatchAgent): Track patch generation attempts, LLM reasoning steps

2. **Debugging**: Understand CRS behavior during trial
   - Example: Visualize search strategy evolution
   - Example: Identify bottlenecks or stuck states

3. **Custom Metrics**: Compute CRS-specific performance indicators
   - Example: Fuzzer efficiency (execs/sec, coverage/time)
   - Example: LLM API efficiency (tokens/patch, cost/POV)

4. **Comparative Analysis**: Compare CRS behavior across trials
   - Example: How does fuzzing strategy differ between C and Java targets?
   - Example: Which LLM prompting strategy is most effective?

## Architecture

### Module Structure

```
crsbench/evaluation/
├── analysis/
│   ├── __init__.py              # Exports AnalyzerInterface, AnalysisResult, etc.
│   ├── base.py                  # AnalyzerInterface ABC
│   ├── manager.py               # AnalysisManager - discovers and runs analyzers
│   └── analyzers/               # CRS-specific analyzer implementations
│       ├── __init__.py
│       ├── atlantis_c.py        # Atlantis C analyzer
│       ├── atlantis_multilang.py
│       ├── patchagent.py        # PatchAgent analyzer
│       └── example.py           # Reference implementation
```

### Interface Design

#### AnalyzerInterface (ABC)

```python
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Dict, Any, Optional
from dataclasses import dataclass

@dataclass
class AnalysisResult:
    """Result from CRS-specific analysis."""
    crs_name: str
    metrics: Dict[str, Any]  # CRS-specific metrics (e.g., {"fuzzing_iters": 1000})
    summary: str              # Human-readable summary
    warnings: list[str]       # Any issues during analysis
    metadata: Dict[str, Any]  # Additional metadata (e.g., analyzer version)

class AnalyzerInterface(ABC):
    """Abstract base class for CRS-specific analyzers.

    CRS developers implement this interface to analyze their CRS's
    custom data in the `crs-data/` directory.
    """

    @property
    @abstractmethod
    def crs_name(self) -> str:
        """Return CRS name (e.g., 'atlantis-c', 'patchagent')."""
        pass

    @abstractmethod
    def analyze_snapshot(self, snapshot_dir: Path) -> Optional[AnalysisResult]:
        """Analyze a single snapshot's crs-data.

        Args:
            snapshot_dir: Extracted snapshot directory containing crs-data/

        Returns:
            AnalysisResult if analysis succeeds, None if crs-data missing/invalid

        Note:
            - This method should be read-only (no modifications to snapshot_dir)
            - Should handle missing/malformed data gracefully
            - Should NOT raise exceptions (return None instead)
        """
        pass

    @abstractmethod
    def analyze_trial(self, trial_dir: Path) -> Optional[AnalysisResult]:
        """Analyze final trial data (from trial_dir/output/crs-data/).

        Args:
            trial_dir: Trial output directory

        Returns:
            AnalysisResult if analysis succeeds, None if crs-data missing/invalid
        """
        pass

    def analyze_time_series(self, snapshots: list[Path]) -> Optional[Dict[str, Any]]:
        """Optional: Analyze time-series data across multiple snapshots.

        Args:
            snapshots: List of extracted snapshot directories (sorted by cycle)

        Returns:
            Dict with time-series metrics (e.g., {"coverage_over_time": [...]})
            None if not implemented or insufficient data
        """
        return None  # Default: not implemented
```

#### AnalysisManager

```python
class AnalysisManager:
    """Discovers and executes CRS-specific analyzers.

    Responsibilities:
    - Auto-discover analyzer modules in crsbench/evaluation/analysis/analyzers/
    - Load analyzers dynamically based on CRS name
    - Execute analyzers on snapshots or trial data
    - Aggregate results for reporting
    """

    def __init__(self):
        """Initialize manager and discover available analyzers."""
        self.analyzers: Dict[str, AnalyzerInterface] = {}
        self._discover_analyzers()

    def _discover_analyzers(self):
        """Scan analyzers/ directory and load all analyzer modules."""
        # Auto-import all .py files in analyzers/
        # Instantiate classes implementing AnalyzerInterface
        # Map crs_name -> analyzer instance
        pass

    def get_analyzer(self, crs_name: str) -> Optional[AnalyzerInterface]:
        """Get analyzer for a CRS (None if not available)."""
        return self.analyzers.get(crs_name)

    def analyze_snapshot(self, crs_name: str, snapshot_dir: Path) -> Optional[AnalysisResult]:
        """Analyze a snapshot for a specific CRS."""
        analyzer = self.get_analyzer(crs_name)
        if not analyzer:
            logger.debug(f"No analyzer for {crs_name}")
            return None

        try:
            return analyzer.analyze_snapshot(snapshot_dir)
        except Exception as e:
            logger.warning(f"Analyzer {crs_name} failed: {e}")
            return None

    def analyze_trial(self, crs_name: str, trial_dir: Path) -> Optional[AnalysisResult]:
        """Analyze final trial data for a specific CRS."""
        analyzer = self.get_analyzer(crs_name)
        if not analyzer:
            return None

        try:
            return analyzer.analyze_trial(trial_dir)
        except Exception as e:
            logger.warning(f"Analyzer {crs_name} failed: {e}")
            return None
```

## Implementation Examples

**NOTE**: These are hypothetical examples showing the analyzer pattern. Actual CRS implementations (Atlantis, PatchAgent) do not yet have defined `crs-data/` output formats. CRS developers will define their own formats when they implement analyzers.

### Example: Minimal Reference Analyzer

```python
# crsbench/evaluation/analysis/analyzers/example.py

from pathlib import Path
from typing import Optional
import json
from crsbench.evaluation.analysis.base import AnalyzerInterface, AnalysisResult

class ExampleAnalyzer(AnalyzerInterface):
    """Minimal reference implementation showing the analyzer pattern.

    This is a working example that CRS developers can copy and adapt.
    It assumes crs-data contains a simple metrics.json file.
    """

    @property
    def crs_name(self) -> str:
        return "example-crs"

    def analyze_snapshot(self, snapshot_dir: Path) -> Optional[AnalysisResult]:
        """Analyze example CRS snapshot data."""
        crs_data_dir = snapshot_dir / "crs-data"
        if not crs_data_dir.exists():
            return None

        metrics_file = crs_data_dir / "metrics.json"
        if not metrics_file.exists():
            return AnalysisResult(
                crs_name=self.crs_name,
                metrics={},
                summary="No metrics available",
                warnings=["metrics.json not found in crs-data/"],
                metadata={"analyzer_version": "1.0"}
            )

        try:
            with open(metrics_file) as f:
                data = json.load(f)

            # Extract whatever metrics your CRS writes
            metrics = {
                "iterations": data.get("iterations", 0),
                "discoveries": data.get("discoveries", 0),
            }

            summary = f"Example CRS: {metrics['iterations']} iterations, {metrics['discoveries']} discoveries"

            return AnalysisResult(
                crs_name=self.crs_name,
                metrics=metrics,
                summary=summary,
                warnings=[],
                metadata={"analyzer_version": "1.0"}
            )

        except json.JSONDecodeError as e:
            return AnalysisResult(
                crs_name=self.crs_name,
                metrics={},
                summary="Analysis failed",
                warnings=[f"Invalid JSON in metrics.json: {e}"],
                metadata={"analyzer_version": "1.0"}
            )
        except Exception as e:
            return AnalysisResult(
                crs_name=self.crs_name,
                metrics={},
                summary="Analysis failed",
                warnings=[f"Error reading metrics: {e}"],
                metadata={"analyzer_version": "1.0"}
            )

    def analyze_trial(self, trial_dir: Path) -> Optional[AnalysisResult]:
        """Analyze final trial data."""
        crs_data_dir = trial_dir / "output" / "crs-data"
        if not crs_data_dir.exists():
            return None

        # Reuse snapshot logic by creating a "snapshot-like" view
        # This assumes output/ is structured like: output/crs-data/...
        temp_snapshot = trial_dir / "output"
        return self.analyze_snapshot(temp_snapshot.parent)

    def analyze_time_series(self, snapshots: list[Path]) -> Optional[Dict[str, Any]]:
        """Optional: Analyze trends across snapshots."""
        iterations_over_time = []
        discoveries_over_time = []

        for snapshot_dir in sorted(snapshots):
            result = self.analyze_snapshot(snapshot_dir)
            if result and result.metrics:
                iterations_over_time.append(result.metrics.get("iterations", 0))
                discoveries_over_time.append(result.metrics.get("discoveries", 0))

        if not iterations_over_time:
            return None

        return {
            "iterations_over_time": iterations_over_time,
            "discoveries_over_time": discoveries_over_time,
            "avg_iterations_per_cycle": sum(iterations_over_time) / len(iterations_over_time),
        }
```

### Hypothetical Example: Fuzzer-Based CRS

This shows what a fuzzer-based CRS analyzer might look like (NOT based on real Atlantis implementation):

```python
# crsbench/evaluation/analysis/analyzers/fuzzer_example.py
# NOTE: This is a HYPOTHETICAL example, not tied to any real CRS

from pathlib import Path
from typing import Optional
import json
from crsbench.evaluation.analysis.base import AnalyzerInterface, AnalysisResult

class FuzzerExampleAnalyzer(AnalyzerInterface):
    """Hypothetical analyzer for a fuzzer-based CRS.

    Assumes the CRS writes fuzzing statistics to crs-data/stats.json.
    This is an example pattern - actual format depends on CRS implementation.
    """

    @property
    def crs_name(self) -> str:
        return "fuzzer-example"

    def analyze_snapshot(self, snapshot_dir: Path) -> Optional[AnalysisResult]:
        crs_data_dir = snapshot_dir / "crs-data"
        if not crs_data_dir.exists():
            return None

        # Example: Read fuzzing stats if available
        stats_file = crs_data_dir / "stats.json"
        if not stats_file.exists():
            return None

        try:
            with open(stats_file) as f:
                stats = json.load(f)

            metrics = {
                "execs": stats.get("total_execs", 0),
                "coverage": stats.get("coverage_size", 0),
                "runtime": stats.get("runtime_sec", 0),
            }

            summary = f"Fuzzer: {metrics['execs']} execs, {metrics['coverage']} edges"

            return AnalysisResult(
                crs_name=self.crs_name,
                metrics=metrics,
                summary=summary,
                warnings=[],
                metadata={"analyzer_version": "1.0"}
            )
        except Exception as e:
            return AnalysisResult(
                crs_name=self.crs_name,
                metrics={},
                summary="Failed to parse stats",
                warnings=[str(e)],
                metadata={"analyzer_version": "1.0"}
            )

    def analyze_trial(self, trial_dir: Path) -> Optional[AnalysisResult]:
        crs_data_dir = trial_dir / "output" / "crs-data"
        if not crs_data_dir.exists():
            return None
        temp_snapshot = trial_dir / "output"
        return self.analyze_snapshot(temp_snapshot.parent)
```

### Hypothetical Example: LLM-Based CRS

This shows what an LLM-based patch generation CRS analyzer might look like:

```python
# crsbench/evaluation/analysis/analyzers/llm_example.py
# NOTE: This is a HYPOTHETICAL example, not tied to any real CRS

from pathlib import Path
from typing import Optional
import json
from crsbench.evaluation.analysis.base import AnalyzerInterface, AnalysisResult

class LLMExampleAnalyzer(AnalyzerInterface):
    """Hypothetical analyzer for an LLM-based patch generation CRS.

    Assumes the CRS writes reasoning logs to crs-data/reasoning.jsonl.
    """

    @property
    def crs_name(self) -> str:
        return "llm-example"

    def analyze_snapshot(self, snapshot_dir: Path) -> Optional[AnalysisResult]:
        crs_data_dir = snapshot_dir / "crs-data"
        if not crs_data_dir.exists():
            return None

        # Example: Read LLM reasoning logs (JSONL format)
        log_file = crs_data_dir / "reasoning.jsonl"
        if not log_file.exists():
            return None

        try:
            events = []
            with open(log_file) as f:
                for line in f:
                    if line.strip():
                        events.append(json.loads(line))

            # Example metrics
            patch_attempts = sum(1 for e in events if e.get("type") == "patch_attempt")
            llm_calls = sum(1 for e in events if e.get("type") == "llm_call")

            metrics = {
                "patch_attempts": patch_attempts,
                "llm_calls": llm_calls,
                "total_events": len(events),
            }

            summary = f"LLM CRS: {patch_attempts} patch attempts, {llm_calls} LLM calls"

            return AnalysisResult(
                crs_name=self.crs_name,
                metrics=metrics,
                summary=summary,
                warnings=[],
                metadata={"analyzer_version": "1.0"}
            )
        except Exception as e:
            return AnalysisResult(
                crs_name=self.crs_name,
                metrics={},
                summary="Failed to parse logs",
                warnings=[str(e)],
                metadata={"analyzer_version": "1.0"}
            )

    def analyze_trial(self, trial_dir: Path) -> Optional[AnalysisResult]:
        crs_data_dir = trial_dir / "output" / "crs-data"
        if not crs_data_dir.exists():
            return None
        temp_snapshot = trial_dir / "output"
        return self.analyze_snapshot(temp_snapshot.parent)
```

## Integration Points

### With Snapshot System

**During snapshot capture** (optional - for monitoring):
```python
# In SnapshotManager.capture_snapshot()
from crsbench.evaluation.analysis.manager import AnalysisManager

analysis_manager = AnalysisManager()

# After creating snapshot archive
if config.get('enable_snapshot_analysis', False):
    # Extract snapshot temporarily
    with tempfile.TemporaryDirectory() as temp_extract:
        extract_snapshot(archive_path, temp_extract)

        # Analyze CRS-specific data
        result = analysis_manager.analyze_snapshot(crs_name, Path(temp_extract))

        if result:
            # Log analysis summary
            logger.info(f"Snapshot {cycle} analysis: {result.summary}")

            # Optionally save to snapshot metadata
            metadata_path = trial_dir / f"snapshot-{cycle:04d}-analysis.json"
            with open(metadata_path, 'w') as f:
                json.dump(result.metrics, f, indent=2)
```

**After trial completion** (recommended - for final reporting):
```python
# In BenchmarkRunner or report generation
analysis_manager = AnalysisManager()

result = analysis_manager.analyze_trial(crs_name, trial_dir)
if result:
    # Include in trial report
    trial_report["crs_analysis"] = {
        "metrics": result.metrics,
        "summary": result.summary,
        "warnings": result.warnings
    }
```

### With Reporting System

```python
# In report generation (future enhancement)
from crsbench.evaluation.analysis.manager import AnalysisManager

def generate_trial_report(trial_dir: Path, crs_name: str) -> Dict[str, Any]:
    """Generate comprehensive trial report."""
    # Standard CRSBench metrics
    report = {
        "povs_found": count_povs(trial_dir),
        "patches_generated": count_patches(trial_dir),
        "llm_usage": load_llm_usage(trial_dir),
        # ...
    }

    # CRS-specific analysis (optional)
    analysis_manager = AnalysisManager()
    analysis_result = analysis_manager.analyze_trial(crs_name, trial_dir)

    if analysis_result:
        report["crs_specific_metrics"] = analysis_result.metrics
        report["crs_analysis_summary"] = analysis_result.summary
        report["crs_analysis_warnings"] = analysis_result.warnings

    return report
```

## CRS Developer Workflow

### Adding a New Analyzer

1. **Create analyzer file**: `crsbench/evaluation/analysis/analyzers/my_crs.py`

2. **Implement AnalyzerInterface**:
```python
from crsbench.evaluation.analysis.base import AnalyzerInterface, AnalysisResult

class MyCRSAnalyzer(AnalyzerInterface):
    @property
    def crs_name(self) -> str:
        return "my-crs"

    def analyze_snapshot(self, snapshot_dir: Path) -> Optional[AnalysisResult]:
        # Read crs-data from snapshot_dir / "crs-data"
        # Parse custom data format
        # Return AnalysisResult
        pass

    def analyze_trial(self, trial_dir: Path) -> Optional[AnalysisResult]:
        # Read crs-data from trial_dir / "output" / "crs-data"
        pass
```

3. **Auto-discovery**: AnalysisManager automatically discovers and loads the analyzer

4. **Test**:
```python
# tests/test_analysis_my_crs.py
from crsbench.evaluation.analysis.analyzers.my_crs import MyCRSAnalyzer

def test_my_crs_analyzer(tmp_path):
    # Create mock crs-data
    crs_data_dir = tmp_path / "crs-data"
    crs_data_dir.mkdir()
    (crs_data_dir / "my_data.json").write_text('{"metric": 123}')

    analyzer = MyCRSAnalyzer()
    result = analyzer.analyze_snapshot(tmp_path)

    assert result is not None
    assert result.metrics["metric"] == 123
```

### CRS Data Format Recommendations

**No strict format**, but some guidelines:

1. **Use JSON for structured data** (easier to parse)
   - Example: `fuzzing_stats.json`, `agent_log.jsonl`

2. **Use meaningful filenames**
   - Good: `fuzzing_stats.json`, `llm_reasoning.jsonl`
   - Bad: `output.txt`, `data.bin`

3. **Include metadata** (version, timestamp)
   ```json
   {
     "version": "1.0",
     "timestamp": 1234567890.0,
     "metrics": {...}
   }
   ```

4. **Be append-only friendly** (for incremental capture)
   - Use JSONL for logs: `{"timestamp": 123, "event": "foo"}\n`
   - Avoid overwriting files

## Configuration

Add to ExperimentConfig (optional):

```python
class ExperimentConfig(BaseModel):
    # ... existing fields ...

    enable_snapshot_analysis: bool = Field(
        default=False,
        description="Run CRS-specific analysis on each snapshot (slower but provides real-time insights)"
    )

    enable_trial_analysis: bool = Field(
        default=True,
        description="Run CRS-specific analysis on final trial data (for reporting)"
    )
```

## Error Handling

**Design Philosophy**: Missing analyzer or malformed data should NEVER fail a trial.

```python
# Always catch exceptions
try:
    result = analyzer.analyze_snapshot(snapshot_dir)
except Exception as e:
    logger.warning(f"Analyzer {crs_name} failed: {e}")
    result = None

# Gracefully handle missing analyzer
analyzer = analysis_manager.get_analyzer(crs_name)
if not analyzer:
    logger.debug(f"No analyzer for {crs_name}, skipping analysis")
    # Continue with trial (no error)
```

## Testing Strategy

### Unit Tests

```python
# tests/test_analysis_base.py
- Test AnalysisResult dataclass
- Test AnalyzerInterface ABC (cannot instantiate directly)

# tests/test_analysis_manager.py
- Test analyzer discovery
- Test get_analyzer()
- Test analyze_snapshot() with mock analyzers
- Test error handling (missing analyzer, analyzer exception)

# tests/test_analysis_atlantis_c.py
- Test AtlantisCAnalyzer with sample data
- Test missing data handling
- Test malformed data handling
- Test time-series analysis

# tests/test_analysis_patchagent.py
- Test PatchAgentAnalyzer with sample data
```

### Integration Tests

```python
# tests/test_snapshot_with_analysis.py
- Test SnapshotManager with analysis enabled
- Test analyzer execution on real snapshot
- Test analysis results saved correctly

# tests/test_runner_with_analysis.py
- Test BenchmarkRunner with trial analysis
- Test analysis results included in report
```

## Performance Considerations

**Snapshot Analysis** (real-time):
- Adds ~100ms-1s per snapshot (depends on data size)
- Only enabled if `enable_snapshot_analysis=True`
- Runs in main thread (after snapshot capture completes)
- Should NOT block CRS execution

**Trial Analysis** (post-execution):
- Adds ~100ms-5s to final report generation
- No impact on trial execution time
- Can be more thorough (more complex analysis)

## Future Extensions

1. **Time-series visualizations**: Generate plots from analyze_time_series()
2. **Comparative dashboards**: Compare CRS behavior across trials
3. **Anomaly detection**: Detect unusual CRS behavior patterns
4. **Cost optimization**: Analyze LLM usage efficiency
5. **Plugin system**: External analyzers (not in crsbench/ tree)

## Implementation Checklist

- [ ] Create `crsbench/evaluation/analysis/` directory structure
- [ ] Implement `base.py` (AnalyzerInterface, AnalysisResult)
- [ ] Implement `manager.py` (AnalysisManager with auto-discovery)
- [ ] Implement `analyzers/example.py` (reference implementation)
- [ ] Write unit tests for base and manager
- [ ] Optional: Implement Atlantis C analyzer
- [ ] Optional: Implement PatchAgent analyzer
- [ ] Optional: Integrate with snapshot system
- [ ] Optional: Integrate with reporting system
- [ ] Update documentation

## Design Decisions

### Why Optional Analyzers?

**Decision**: Missing analyzer should not fail trials

**Rationale**:
- Many CRS won't have analyzers initially
- CRS developers may not want to write analyzers
- Core CRSBench functionality should work without analyzers
- Analysis is for **insights**, not **validation**

### Why ABC Instead of Protocol?

**Decision**: Use ABC (Abstract Base Class) for AnalyzerInterface

**Rationale**:
- Clearer contract for CRS developers
- Better IDE support (autocomplete, type checking)
- Easier to document required methods
- Consistent with other CRSBench interfaces

### Why Auto-Discovery?

**Decision**: Automatically discover analyzers in `analyzers/` directory

**Rationale**:
- No manual registration needed
- Easy to add new analyzers (just create file)
- Follows Python conventions (like pytest plugin discovery)
- Simpler for CRS developers

### Why Read-Only Analysis?

**Decision**: Analyzers should never modify CRS data

**Rationale**:
- Avoids corruption of trial data
- Keeps analysis separate from execution
- Enables parallel analysis (multiple analyzers on same data)
- Simpler reasoning about system state
