# Hint Level Support Design Document

## Overview

Add support for configurable hint levels in CRSBench experiment configuration. Hints come in SARIF format with levels 1-5 (1 = least helpful, 5 = most helpful). Future support for pre-fuzz corpus with separate level configuration is planned.

## Current State

### Existing Hints Structure
```
benchmarks/<project>/.aixcc/<harness>/cpv_N/hints/
├── level_1.sarif   # Vague: "Memory Safety Issue"
├── level_2.sarif   # CWE type: "CWE-416 - Use After Free"
├── level_3.sarif   # + function location
├── level_4.sarif   # + line number ranges
└── level_5.sarif   # + vulnerability name and description
```

### Existing Config (in use but NOT in schema)
- `hints_enabled` (bool) - hardcoded in `jobs.py`
- `hints_corpus_level` ("1h" | "1d") - legacy corpus level

## Requirements

1. Add `hint_sarif_level` to experiment config (1-5)
2. Add `hint_corpus_level` as placeholder for future corpus support
3. Aggregate hints from all CPVs across all harnesses into trial directory
4. Use non-leaky filenames (e.g., `0.sarif`, `1.sarif`) to avoid revealing CPV info
5. Pass aggregated hints directory to `oss-bugfind-crs` / `oss-bugfix-crs`

## Design

### Config Schema Changes

File: `crsbench/validation/schemas.py`

Add to `ExperimentConfig`:
```python
hints_enabled: bool = Field(
    default=False,
    description="Enable hints for CRS"
)
hint_sarif_level: Optional[int] = Field(
    default=None,
    ge=1, le=5,
    description="SARIF hint level (1=vague, 5=detailed). None disables SARIF hints."
)
hint_corpus_level: Optional[int] = Field(
    default=None,
    ge=1, le=5,
    description="Pre-fuzz corpus level (1=minimal, 5=comprehensive). None disables corpus. [placeholder]"
)
```

Add validator:
```python
@model_validator(mode='after')
def check_hints_config(self):
    if self.hints_enabled:
        if self.hint_sarif_level is None and self.hint_corpus_level is None:
            raise ValueError("hints_enabled=True requires at least one of hint_sarif_level or hint_corpus_level")
    return self
```

### CLI Arguments (Optional Override)

File: `crsbench/run_experiment.py`

Add:
```
--hints-enabled         Enable hints
--hint-sarif-level N    Override SARIF hint level (1-5)
--hint-corpus-level N   Override corpus hint level (1-5) [placeholder]
```

### Trial Hints Preparation

File: `crsbench/evaluation/trial_preparation.py`

Modify `_prepare_hints()` to:

1. **Discover all CPVs**: Read `meta.yaml` → iterate harness_files → iterate vulns → get cpv paths
2. **Filter by level**: Select `level_{N}.sarif` based on `hint_sarif_level`
3. **Aggregate with unique names**: Copy to `trial/hints/` as `0.sarif`, `1.sarif`, etc.
4. **Future: corpus**: Placeholder for corpus aggregation when available

```python
def _prepare_hints(self, benchmark: str, trial_dir: Path) -> Optional[Path]:
    """Aggregate hints from all CPVs based on configured levels."""
    if not self.config.get("hints_enabled", False):
        return None

    sarif_level = self.config.get("hint_sarif_level")
    # corpus_level = self.config.get("hint_corpus_level")  # placeholder

    if sarif_level is None:
        return None

    hints_dir = trial_dir / "hints"
    hints_dir.mkdir(parents=True, exist_ok=True)

    # Load meta.yaml to discover all CPVs
    meta = self._load_benchmark_meta(benchmark)

    sarif_index = 0
    for harness in meta.get("harness_files", []):
        harness_name = harness["name"]
        for vuln in harness.get("vulns", []):
            cpv_keyword = vuln["vuln_keyword"]
            cpv_hints_dir = benchmark_dir / ".aixcc" / harness_name / cpv_keyword / "hints"

            sarif_file = cpv_hints_dir / f"level_{sarif_level}.sarif"
            if sarif_file.exists():
                dest = hints_dir / f"{sarif_index}.sarif"
                shutil.copy2(sarif_file, dest)
                sarif_index += 1

    return hints_dir if sarif_index > 0 else None
```

### Config Flow Updates

File: `crsbench/distributed/jobs.py`

Update `run_crs_trial()`:
```python
crs_executor.configure_crs({
    'build_timeout': config.get('build_timeout', 3600),
    'run_timeout': config.get('max_total_time', 7200),
    'hints_enabled': config.get('hints_enabled', False),
    'hint_sarif_level': config.get('hint_sarif_level'),
    'hint_corpus_level': config.get('hint_corpus_level'),  # placeholder
})
```

### Experiment Config Template

File: `docs/experiment-config-example.yaml`

Add:
```yaml
# ===== Hints Configuration =====

# Enable hints for CRS evaluation
hints_enabled: false

# SARIF hint level (1-5, where 1 is vaguest, 5 is most detailed)
# Set to null/omit to disable SARIF hints
hint_sarif_level: 3

# Pre-fuzz corpus level (1-5) [PLACEHOLDER - not yet implemented]
# Set to null/omit to disable corpus hints
# hint_corpus_level: null
```

## Trial Directory Structure

```
trial-N/
├── hints/
│   ├── 0.sarif      # From first CPV at configured level
│   ├── 1.sarif      # From second CPV at configured level
│   └── 2.sarif      # etc.
└── ...
```

## Files to Modify

1. `crsbench/validation/schemas.py` - Add hint config fields to ExperimentConfig
2. `crsbench/run_experiment.py` - Add CLI arguments for hint overrides
3. `crsbench/evaluation/trial_preparation.py` - Update `_prepare_hints()` for level-based aggregation
4. `crsbench/distributed/jobs.py` - Pass hint config to executor
5. `docs/experiment-config-example.yaml` - Document new config options

## Future Work

- Implement corpus level support when corpus directories are added to benchmarks
- Consider separate sarif/corpus level granularity if needed
