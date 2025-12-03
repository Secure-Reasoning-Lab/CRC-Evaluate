# CRS Config Templating and Runtime Override System

## Overview

This document describes the design for runtime CRS configuration generation using templates and experiment-specific overrides. The system enables experiment configurations to override CRS deployment settings (resources, secrets, LLM endpoints) while maintaining full traceability of template sources and generated configs.

## Problem Statement

Currently, CRS configurations in `crses/configs/<crs-name>/` are static:
- `.env` - Secrets (POSTGRES_PASSWORD, LITELLM_MASTER_KEY)
- `config-resource.yaml` - Workers, CPU, memory, LLM budgets
- `config-litellm.yaml` - LLM model endpoints

These static configs cannot be overridden at experiment runtime, limiting flexibility for:
- Different LLM budgets per experiment
- Dynamic resource allocation
- Experiment-specific secrets
- Per-CRS resource differentiation within an experiment

**Note**: The `experiment_config.yaml` already has a TODO comment acknowledging this need.

## Goals

1. **Runtime Configuration**: Generate trial-specific CRS configs from templates + experiment overrides
2. **Traceability**: Archive both templates and generated configs in trial directories
3. **Flexibility**: Support global defaults + per-CRS overrides
4. **Reproducibility**: Enable exact config reconstruction from archived templates and experiment config

## Design

### Architecture

```
experiment_config.yaml (crs_overrides)
        +
crses/configs/<crs-name>/ (templates)
        |
        v
   [CRSConfigRenderer]
        |
        v
trial-N/crs-config/ (generated configs)
        |
        v
   oss-crs build/run
```

### Key Components

1. **Templates**: Existing `crses/configs/<crs-name>/` files serve as base templates
2. **Overrides**: New `crs_overrides` section in `experiment_config.yaml`
3. **Renderer**: `CRSConfigRenderer` class merges templates + overrides
4. **Archive**: Trial directory stores both template snapshot and generated config

## Experiment Config Extension

Add `crs_overrides` section to `experiment_config.yaml`:

```yaml
# experiment_config.yaml
experiment: "my-experiment"
trials: 3
max_total_time: 7200
crses:
  - atlantis-c
  - mock-crs

# NEW: CRS configuration overrides
crs_overrides:
  # Global defaults (apply to ALL CRS unless overridden)
  default:
    env:
      LITELLM_MASTER_KEY: sk-experiment-key-123
      POSTGRES_PASSWORD: experiment-password
    resources:
      workers:
        local:
          cpuset: "0-7"
          memory: "32G"
      llm:
        max_budget: 50
        max_rpm: 1000
        max_tpm: 100000
    litellm:
      model_list:
        - model_name: gpt-4o-mini
          litellm_params:
            model: openai/gpt-4o-mini
            api_base: http://litellm-proxy:8000

  # Per-CRS overrides (merge with/override defaults)
  atlantis-c:
    resources:
      llm:
        max_budget: 100  # atlantis-c gets more budget
        max_rpm: 2000

  mock-crs:
    env:
      MOCK_MODE: "true"  # CRS-specific env var
```

### Override Semantics

**Merge Order** (later overrides earlier):
1. Template files from `crses/configs/<crs-name>/`
2. `crs_overrides.default` (global defaults)
3. `crs_overrides.<crs-name>` (per-CRS overrides)

**Merge Strategy**:
- **`.env`**: Key-value merge (overrides replace matching keys)
- **`config-resource.yaml`**: Deep merge (nested dicts merge recursively)
- **`config-litellm.yaml`**: Replace `model_list` entirely if specified

**No Environment Variable Expansion**:
- Values are literal strings (no `${VAR}` expansion)
- Use explicit values in config files

## Module: CRSConfigRenderer

**File**: `crsbench/evaluation/config_renderer.py`

### Class Design

```python
class CRSConfigRenderer:
    """Renders CRS config from template + experiment overrides.

    Merge order (later overrides earlier):
    1. Template files (crses/configs/<crs-name>/)
    2. crs_overrides.default (global defaults)
    3. crs_overrides.<crs-name> (per-CRS overrides)
    """

    def __init__(
        self,
        template_dir: Path,       # crses/configs/<crs-name>/
        crs_name: str,            # CRS name for per-CRS lookup
        crs_overrides: Dict[str, Any],  # Full crs_overrides section
    ):
        """Initialize renderer with template and overrides.

        Args:
            template_dir: Path to CRS config template directory
            crs_name: CRS name (e.g., "atlantis-c", "mock-crs")
            crs_overrides: crs_overrides section from experiment_config.yaml
        """

    def render_to(self, output_dir: Path) -> Path:
        """Render all config files to output directory.

        Args:
            output_dir: Destination directory for generated configs

        Returns:
            Path to generated config directory

        Process:
            1. Copy all template files to output_dir
            2. Merge global + per-CRS overrides
            3. Apply merged overrides to each config file
        """

    def _merge_overrides(self) -> Dict[str, Any]:
        """Merge default + per-CRS overrides (deep merge).

        Returns:
            Merged override dict with keys: env, resources, litellm
        """

    def _render_env(self, output_dir: Path, env_overrides: Dict[str, str]) -> None:
        """Merge env overrides with template .env.

        Args:
            output_dir: Directory containing .env file
            env_overrides: Environment variable overrides

        Process:
            1. Parse template .env (KEY=value format)
            2. Override with env_overrides (key replace)
            3. Write merged .env
        """

    def _render_resource(self, output_dir: Path, resource_overrides: Dict) -> None:
        """Deep merge resource overrides with config-resource.yaml.

        Args:
            output_dir: Directory containing config-resource.yaml
            resource_overrides: Resource configuration overrides

        Process:
            1. Load template config-resource.yaml
            2. Deep merge resource_overrides (recursive dict merge)
            3. Write merged config-resource.yaml
        """

    def _render_litellm(self, output_dir: Path, litellm_overrides: Dict) -> None:
        """Replace model_list in config-litellm.yaml.

        Args:
            output_dir: Directory containing config-litellm.yaml
            litellm_overrides: LiteLLM configuration overrides

        Process:
            1. Load template config-litellm.yaml
            2. Replace model_list if specified in overrides
            3. Write merged config-litellm.yaml
        """
```

### Deep Merge Utility

```python
def deep_merge(base: Dict, override: Dict) -> Dict:
    """Deep merge two dictionaries.

    Args:
        base: Base dictionary
        override: Override dictionary

    Returns:
        Merged dictionary (override takes precedence)

    Example:
        base = {"a": {"b": 1, "c": 2}}
        override = {"a": {"c": 3, "d": 4}}
        result = {"a": {"b": 1, "c": 3, "d": 4}}
    """
```

## Integration: Trial Preparation

### TrialDirectoryPreparer Updates

**File**: `crsbench/evaluation/trial_preparation.py`

Add two new methods:

```python
def _snapshot_crses(self, trial_dir: Path) -> Path:
    """Copy entire crses/ directory to trial for traceability.

    Args:
        trial_dir: Trial directory

    Returns:
        Path to crses snapshot directory

    Process:
        1. Copy crses/ to trial_dir/crses-snapshot/
        2. Exclude .git* files
        3. Preserve directory structure
    """
    crses_src = Path(__file__).parent.parent.parent / "crses"
    crses_dest = trial_dir / "crses-snapshot"
    shutil.copytree(crses_src, crses_dest, ignore=shutil.ignore_patterns('.git*'))
    logger.info(f"Snapshotted crses/ to {crses_dest}")
    return crses_dest

def _prepare_crs_config(self, crs: str, trial_dir: Path) -> Path:
    """Generate trial-specific CRS config from template + overrides.

    Args:
        crs: CRS name
        trial_dir: Trial directory

    Returns:
        Path to generated config directory

    Process:
        1. Resolve template directory: crses/configs/<crs>/
        2. Get crs_overrides from experiment config
        3. Use CRSConfigRenderer to generate config
        4. Output to trial_dir/crs-config/
    """
    # Resolve template
    template_dir = Path(__file__).parent.parent.parent / "crses" / "configs" / crs

    # Get overrides
    crs_overrides = self.config.get("crs_overrides", {})

    # Render config
    renderer = CRSConfigRenderer(template_dir, crs, crs_overrides)
    config_dir = trial_dir / "crs-config"
    renderer.render_to(config_dir)

    logger.info(f"Generated CRS config at {config_dir}")
    return config_dir
```

### Update prepare_trial()

```python
def prepare_trial(
    self,
    crs: str,
    benchmark: str,
    harness: str,
    trial_num: int,
    mode: str = "bug_finding"
) -> TrialPreparationResult:
    """Prepare complete trial directory structure."""

    # ... existing code (trial_dir, build_dir, source, hints, povs) ...

    # NEW: Snapshot crses/ directory for traceability
    crses_snapshot_dir = self._snapshot_crses(trial_dir)

    # NEW: Render CRS config for this trial
    crs_config_dir = self._prepare_crs_config(crs, trial_dir)

    # ... existing metadata creation ...

    return TrialPreparationResult(
        trial_dir=trial_dir,
        build_dir=build_dir,
        source_path=source_path,
        output_dir=output_dir,
        hints_dir=hints_dir,
        povs_dir=povs_dir,
        crses_snapshot_dir=crses_snapshot_dir,  # NEW
        crs_config_dir=crs_config_dir,          # NEW
        metadata=metadata,
        success=True
    )
```

### Update TrialPreparationResult

```python
@dataclass
class TrialPreparationResult:
    """Result of trial directory preparation."""
    trial_dir: Optional[Path]
    build_dir: Optional[Path]
    source_path: Optional[Path]
    output_dir: Optional[Path]
    hints_dir: Optional[Path]
    povs_dir: Optional[Path]
    crses_snapshot_dir: Optional[Path] = None  # NEW
    crs_config_dir: Optional[Path] = None      # NEW
    metadata: Dict[str, Any]
    success: bool
    error: Optional[str] = None
```

## Integration: CRS Executor

### CRSBugFindingExecutor Updates

**File**: `crsbench/evaluation/crs_bug_finding_executor.py`

Update `run_crs()` to accept and use generated config:

```python
def run_crs(
    self,
    benchmark_path: Path,
    harness: HarnessFile,
    trial_output_dir: Path,
    crs_config_dir: Path  # NEW: from TrialPreparationResult
) -> CRSResult:
    """Run CRS on a specific harness.

    Args:
        benchmark_path: Path to benchmark directory
        harness: Harness configuration
        trial_output_dir: Trial directory
        crs_config_dir: Path to generated CRS config (NEW)
    """

    # ... existing setup ...

    # Build command - use generated config
    cmd = self._construct_build_command(
        project_name=project_name,
        trial_build_dir=trial_build_dir,
        crs_config_dir=crs_config_dir  # Pass generated config
    )

    # ... existing execution logic ...
```

Update command construction:

```python
def _construct_build_command(
    self,
    project_name: str,
    trial_build_dir: Path,
    crs_config_dir: Path  # NEW
) -> List[str]:
    """Construct oss-crs build command."""
    cmd = [
        "oss-crs", "build",
        "--build-dir", str(trial_build_dir),
        "--oss-fuzz-dir", str(self.oss_fuzz_path),
        "--registry-dir", str(self.registry_dir),
        "--project-path", str(benchmark_path),
        str(crs_config_dir),  # Generated config (not template)
        project_name,
        str(source_path)
    ]
    return cmd
```

## Trial Directory Structure

### Before (Current)
```
trial-0/
├── build/
├── output/
├── hints/
└── metadata.json
```

### After (With Config Templating)
```
trial-0/
├── crs-config/              # Generated CRS config for this trial
│   ├── .env                 # Merged secrets
│   ├── config-crs.yaml      # CRS-specific config
│   ├── config-litellm.yaml  # Merged LiteLLM config
│   └── config-resource.yaml # Merged resource config
├── crses-snapshot/          # Snapshot of entire crses/ directory
│   ├── registry/
│   │   └── mock-crs/
│   │       ├── pkg.yaml
│   │       └── config-crs.yaml
│   └── configs/
│       └── mock-crs/
│           ├── .env              # Template (before overrides)
│           ├── config-crs.yaml
│           ├── config-litellm.yaml
│           └── config-resource.yaml
├── build/
├── output/
├── hints/
└── metadata.json
```

### Why Snapshot crses/?

**Full Traceability**:
- Know exactly what templates were used
- Archive templates alongside generated configs
- Track template evolution over time

**Reproducibility**:
- Re-derive runtime config from: `crses-snapshot/ + experiment_config.crs_overrides`
- Verify config generation logic
- Debug config issues

**Debugging**:
- Compare `crses-snapshot/configs/<crs>/.env` vs `crs-config/.env`
- See what changed due to overrides
- Understand trial-specific configuration

## Schema Updates

### ExperimentConfig Schema

**File**: `crsbench/validation/schemas.py`

Add `CRSOverrides` model and field:

```python
class CRSOverrideConfig(BaseModel):
    """Per-CRS or default override configuration."""
    env: Optional[Dict[str, str]] = None
    resources: Optional[Dict[str, Any]] = None
    litellm: Optional[Dict[str, Any]] = None

class CRSOverrides(BaseModel):
    """CRS configuration overrides for experiments."""
    default: Optional[CRSOverrideConfig] = None
    # Dynamic per-CRS fields handled via __root__ or extra="allow"

    class Config:
        extra = "allow"  # Allow per-CRS keys like "atlantis-c", "mock-crs"

class ExperimentConfig(BaseModel):
    """Experiment configuration."""
    experiment: str
    trials: int
    max_total_time: int
    # ... existing fields ...
    crs_overrides: Optional[CRSOverrides] = None  # NEW
```

## Testing Strategy

### Unit Tests

**File**: `tests/test_config_renderer.py`

Test cases:
1. **Template-only rendering** (no overrides)
2. **Global defaults only**
3. **Per-CRS overrides only**
4. **Global + per-CRS merge** (verify deep merge)
5. **Partial overrides** (only env, or only resources)
6. **Empty override sections** (graceful handling)

### Integration Tests

**File**: `tests/test_trial_preparation.py`

Test cases:
1. **crses snapshot creation** (verify directory structure)
2. **Config rendering integration** (verify generated files)
3. **Trial preparation with overrides** (end-to-end)

## Migration Path

### Phase 1: Add Optional Support
- Add `crs_overrides` as optional field
- If absent, use template configs directly (current behavior)
- No breaking changes to existing experiments

### Phase 2: Gradual Adoption
- Update experiment configs to use `crs_overrides`
- Validate override behavior in test experiments
- Document migration examples

### Phase 3: Deprecation (Future)
- Eventually require `crs_overrides` for all experiments
- Validate all required fields are overridden
- Remove fallback to static configs

## Example Usage

### Experiment 1: Simple Override (Global Defaults Only)

```yaml
# experiment_config.yaml
experiment: "test-1"
trials: 2
crses: [mock-crs]

crs_overrides:
  default:
    env:
      LITELLM_MASTER_KEY: sk-test-key
    resources:
      workers:
        local:
          cpuset: "0-3"
          memory: "16G"
```

**Result**: `mock-crs` uses global defaults, no per-CRS customization.

### Experiment 2: Per-CRS Differentiation

```yaml
# experiment_config.yaml
experiment: "test-2"
trials: 3
crses: [atlantis-c, mock-crs]

crs_overrides:
  default:
    resources:
      llm:
        max_budget: 50

  atlantis-c:
    resources:
      llm:
        max_budget: 100  # More budget for atlantis-c

  mock-crs:
    env:
      MOCK_MODE: "true"  # Mock-specific setting
```

**Result**:
- `atlantis-c` gets $100 LLM budget
- `mock-crs` gets $50 LLM budget + MOCK_MODE env var

## Future Extensions

### Template Inheritance
Support `extends` key in configs to inherit from base templates:
```yaml
# crses/configs/atlantis-c-custom/config.yaml
extends: atlantis-c
resources:
  llm:
    max_budget: 200
```

### Conditional Overrides
Support difficulty-level or benchmark-specific overrides:
```yaml
crs_overrides:
  default:
    resources:
      llm:
        max_budget: ${difficulty_level * 10}  # Computed values
```

### Validation
Add validation rules to CRSOverrides:
- Budget sum doesn't exceed total
- CPUset ranges don't overlap
- Required env vars are set

## References

- [CRS Resource Configuration Guide](../../crses/README.md) - Template format documentation
- [Trial Directory Preparation](trial-directory-preparation.md) - Trial setup process
- [Experiment Config Schema](../../crsbench/validation/schemas.py) - Config validation
