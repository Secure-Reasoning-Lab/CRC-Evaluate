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
   oss-bugfind-crs build/run
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

## Resource Specification and Generation

### User-Friendly Resource Input

Instead of requiring users to manually specify `cpuset` strings, the system accepts simple integer values and **generates** the proper format strings:

**Input (experiment_config.yaml):**
```yaml
crs_overrides:
  default:
    resources:
      workers:
        local:
          # Option 1: Simple count (most common)
          cpu_count: 8      # Integer: number of cores
          memory_gb: 32     # Integer: gigabytes

          # Option 2: Explicit CPU list (for non-contiguous cores)
          # cpus: [1, 3, 5, 7]  # List: specific CPU indices
```

**Generated (config-resource.yaml):**
```yaml
workers:
  local:
    cpuset: "0-7"    # Generated from cpu_count=8
    memory: "32G"    # Generated from memory_gb=32
```

**Auto-populated (.env):**
```bash
CPUSET_CPUS=0-7      # Automatically set from generated cpuset
```

### Resource Generation Functions

The config renderer includes utilities to convert user-friendly input to system-required formats:

```python
def generate_cpuset_from_count(cpu_count: int, start: int = 0) -> str:
    """Generate contiguous cpuset string from CPU count.

    Args:
        cpu_count: Number of CPUs to allocate
        start: Starting CPU index (default 0)

    Returns:
        Cpuset string in format "start-end" or "N" for single CPU

    Examples:
        (8, 0) → "0-7"
        (4, 8) → "8-11"
        (1, 0) → "0"
        (16, 0) → "0-15"
    """

def generate_cpuset_from_list(cpus: List[int]) -> str:
    """Generate optimized cpuset string from explicit CPU list.

    Automatically compresses contiguous ranges for cleaner output.

    Args:
        cpus: List of CPU indices to include

    Returns:
        Optimized cpuset string with compressed ranges

    Examples:
        [1, 3, 5, 7] → "1,3,5,7"
        [0, 1, 2, 3, 8, 9, 10, 11] → "0-3,8-11"
        [0, 1, 2, 3] → "0-3"
        [5] → "5"
    """

def generate_memory(memory_gb: int) -> str:
    """Generate memory string from gigabytes.

    Args:
        memory_gb: Memory in gigabytes

    Returns:
        Memory string in format "NG"

    Examples:
        32 → "32G"
        64 → "64G"
        128 → "128G"
    """
```

### Parsing Functions (for reading existing configs)

```python
def parse_cpuset(cpuset: str) -> List[int]:
    """Parse cpuset string to list of CPU indices.

    Examples:
        "0-7" → [0, 1, 2, 3, 4, 5, 6, 7]
        "1,3,5,7" → [1, 3, 5, 7]
        "0-3,8-11" → [0, 1, 2, 3, 8, 9, 10, 11]
    """

def parse_memory(memory: str) -> int:
    """Parse memory string to gigabytes.

    Examples:
        "64G" → 64
        "128G" → 128
    """
```

### Rationale

**Why generate instead of direct input?**
1. **User Experience**: `cpu_count: 8` is simpler than `cpuset: "0-7"`
2. **Less Error-Prone**: Users don't need to calculate ranges or format strings
3. **Consistency**: System ensures proper format in all generated configs
4. **CPUSET_CPUS Auto-Population**: `.env` automatically gets matching value

**When to use `cpus` list?**
- **Non-contiguous cores**: When CPUs aren't adjacent (e.g., NUMA aware allocation)
- **Example**: `cpus: [0, 2, 4, 6, 16, 18, 20, 22]` for specific core selection
- System still optimizes: `[0, 1, 2, 3, 8, 9, 10, 11]` → `"0-3,8-11"`

## CPU Allocation for Concurrent Trials

### Problem

When running multiple trials concurrently, each trial needs **non-overlapping CPU sets** to avoid resource contention.

**Example scenario**:
- Config specifies: `cpu_count: 8`
- 4 trials run concurrently on a 32-core machine
- **Without tracking**: All 4 trials would try to use CPUs `0-7` → conflict!
- **With tracking**: Trials get `0-7`, `8-15`, `16-23`, `24-31` → no conflict

### Solution: CPUAllocator

**File**: `crsbench/evaluation/cpu_allocator.py`

A thread-safe CPU allocator that tracks usage across concurrent trials:

```python
class CPUAllocator:
    """Tracks CPU allocation across concurrent trials.

    Thread-safe allocator that assigns non-overlapping CPU sets.
    Ensures each trial gets exclusive access to its allocated CPUs.
    """

    def __init__(self, available_cpus: List[int]):
        """Initialize with available CPU pool.

        Args:
            available_cpus: List of CPU indices available for allocation
                           (e.g., [0, 1, 2, ..., 31] for 32-core machine)
        """
        self.available_cpus = set(available_cpus)
        self.allocated_cpus: Dict[str, Set[int]] = {}  # trial_id -> cpu set
        self._lock = threading.Lock()

    def allocate(self, trial_id: str, cpu_count: int) -> List[int]:
        """Allocate non-overlapping CPUs for a trial.

        Args:
            trial_id: Unique trial identifier (e.g., "trial-0")
            cpu_count: Number of CPUs needed

        Returns:
            List of allocated CPU indices (sorted)

        Raises:
            ResourceError: If not enough free CPUs available

        Note:
            Allocation strategy: Allocate lowest available CPUs first
            for predictable placement and better cache locality.
        """
        with self._lock:
            free_cpus = self.available_cpus - self._all_allocated()
            if len(free_cpus) < cpu_count:
                raise ResourceError(
                    f"Not enough CPUs: need {cpu_count}, "
                    f"available {len(free_cpus)} of {len(self.available_cpus)}"
                )

            # Allocate lowest available CPUs
            allocated = sorted(free_cpus)[:cpu_count]
            self.allocated_cpus[trial_id] = set(allocated)

            logger.info(f"Allocated CPUs {allocated} to {trial_id}")
            return allocated

    def release(self, trial_id: str) -> None:
        """Release CPUs when trial completes.

        Args:
            trial_id: Trial identifier whose CPUs should be released
        """
        with self._lock:
            if trial_id in self.allocated_cpus:
                released = self.allocated_cpus.pop(trial_id)
                logger.info(f"Released CPUs {sorted(released)} from {trial_id}")

    def _all_allocated(self) -> Set[int]:
        """Get all currently allocated CPUs across all trials."""
        return set().union(*self.allocated_cpus.values()) if self.allocated_cpus else set()

    def get_allocation(self, trial_id: str) -> Optional[List[int]]:
        """Get current CPU allocation for a trial.

        Args:
            trial_id: Trial identifier

        Returns:
            List of allocated CPUs, or None if not allocated
        """
        with self._lock:
            cpus = self.allocated_cpus.get(trial_id)
            return sorted(cpus) if cpus else None
```

### Integration with Trial Preparation

**File**: `crsbench/evaluation/trial_preparation.py`

The allocator is initialized once at experiment start and passed to trial preparer:

```python
class TrialDirectoryPreparer:
    def __init__(
        self,
        experiment_dir: Path,
        benchmarks_root: Path,
        oss_fuzz_dir: Path,
        config: Dict[str, Any],
        cpu_allocator: Optional[CPUAllocator] = None  # NEW
    ):
        # ... existing init ...
        self.cpu_allocator = cpu_allocator

    def _prepare_crs_config(self, crs: str, trial_dir: Path, trial_id: str) -> Path:
        """Generate trial-specific CRS config from template + overrides."""
        template_dir = Path(__file__).parent.parent.parent / "crses" / "configs" / crs
        crs_overrides = self.config.get("crs_overrides", {})

        # If CPU allocator provided and cpu_count specified, allocate CPUs
        if self.cpu_allocator:
            cpu_count = self._get_cpu_count_from_overrides(crs_overrides, crs)
            if cpu_count:
                # Allocate non-overlapping CPUs for this trial
                allocated_cpus = self.cpu_allocator.allocate(trial_id, cpu_count)

                # Override with allocated CPUs (as explicit list)
                self._set_allocated_cpus(crs_overrides, crs, allocated_cpus)

        # Render config with allocated (or original) resources
        renderer = CRSConfigRenderer(template_dir, crs, crs_overrides)
        config_dir = trial_dir / "crs-config"
        renderer.render_to(config_dir)

        return config_dir

    def _get_cpu_count_from_overrides(self, overrides: Dict, crs: str) -> Optional[int]:
        """Extract cpu_count from merged overrides."""
        # Check default
        default_count = overrides.get("default", {}).get("resources", {}).get("workers", {}).get("local", {}).get("cpu_count")
        # Check per-CRS
        crs_count = overrides.get(crs, {}).get("resources", {}).get("workers", {}).get("local", {}).get("cpu_count")
        return crs_count if crs_count is not None else default_count

    def _set_allocated_cpus(self, overrides: Dict, crs: str, cpus: List[int]) -> None:
        """Set allocated CPUs in overrides, replacing cpu_count."""
        # Ensure structure exists
        if "default" not in overrides:
            overrides["default"] = {}
        if "resources" not in overrides["default"]:
            overrides["default"]["resources"] = {}
        if "workers" not in overrides["default"]["resources"]:
            overrides["default"]["resources"]["workers"] = {}
        if "local" not in overrides["default"]["resources"]["workers"]:
            overrides["default"]["resources"]["workers"]["local"] = {}

        # Set cpus list, remove cpu_count
        overrides["default"]["resources"]["workers"]["local"]["cpus"] = cpus
        overrides["default"]["resources"]["workers"]["local"].pop("cpu_count", None)
```

### Experiment Orchestrator Setup

**File**: `crsbench/run_experiment.py` (or distributed job manager)

```python
def run_experiment(config: ExperimentConfig):
    """Run complete experiment with CPU allocation tracking."""

    # Initialize CPU allocator based on available cores
    import multiprocessing
    available_cores = config.get("available_cores") or list(range(multiprocessing.cpu_count()))
    cpu_allocator = CPUAllocator(available_cores)

    # Create trial preparer with allocator
    preparer = TrialDirectoryPreparer(
        experiment_dir=experiment_dir,
        benchmarks_root=benchmarks_root,
        oss_fuzz_dir=oss_fuzz_dir,
        config=config,
        cpu_allocator=cpu_allocator  # Pass allocator
    )

    # Run trials (concurrent or sequential)
    for trial_id in trial_ids:
        try:
            # Prepare trial (allocates CPUs)
            result = preparer.prepare_trial(crs, benchmark, harness, trial_num, trial_id=trial_id)

            # Run trial
            run_crs_trial(result)

        finally:
            # Release CPUs when trial completes
            cpu_allocator.release(trial_id)
```

### Example: Concurrent Trial Execution

**Scenario**: 4 trials run concurrently on 32-core machine, each needs 8 CPUs

```
Config:
  cpu_count: 8
  available_cores: [0-31]

Timeline:
  T0: Trial 0 starts → allocates [0-7]   → cpuset: "0-7"
  T1: Trial 1 starts → allocates [8-15]  → cpuset: "8-15"
  T2: Trial 2 starts → allocates [16-23] → cpuset: "16-23"
  T3: Trial 3 starts → allocates [24-31] → cpuset: "24-31"

  T10: Trial 0 completes → releases [0-7]
  T11: Trial 4 starts → allocates [0-7]   → cpuset: "0-7" (reused)

  T15: Trial 1 completes → releases [8-15]
  T16: Trial 5 starts → allocates [8-15]  → cpuset: "8-15" (reused)
```

**Generated configs** (trial-0/crs-config/config-resource.yaml):
```yaml
workers:
  local:
    cpuset: "0-7"    # Generated from allocated_cpus=[0,1,2,3,4,5,6,7]
    memory: "32G"
```

**Generated .env** (trial-0/crs-config/.env):
```bash
CPUSET_CPUS=0-7      # Auto-populated from cpuset
```

### Benefits

1. **No Resource Conflicts**: Each trial gets exclusive CPU access
2. **Automatic Reuse**: CPUs freed by completed trials are immediately available
3. **Error Handling**: Fails fast if insufficient CPUs (better than runtime contention)
4. **Predictable Placement**: Lowest-first strategy provides consistent allocation
5. **Traceability**: Generated config shows exactly which CPUs each trial used

### Configuration in experiment_config.yaml

Add optional `available_cores` field:

```yaml
# experiment_config.yaml
experiment: "my-experiment"
trials: 4
crses: [atlantis-c]

# Optional: Specify available cores for allocation
# If omitted, uses all cores detected on machine
available_cores: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]

crs_overrides:
  default:
    resources:
      workers:
        local:
          cpu_count: 4  # Each trial gets 4 cores from pool
          memory_gb: 16
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
            3. Auto-populate CPUSET_CPUS if not explicitly set (from generated cpuset)
            4. Write merged .env

        Special handling:
            - CPUSET_CPUS automatically set from config-resource.yaml cpuset if not in overrides
            - Ensures .env and config-resource.yaml stay consistent
        """

    def _render_resource(self, output_dir: Path, resource_overrides: Dict) -> None:
        """Generate config-resource.yaml with computed cpuset/memory.

        Args:
            output_dir: Directory containing config-resource.yaml
            resource_overrides: Resource configuration overrides

        Process:
            1. Load template config-resource.yaml
            2. For each worker in overrides:
               - If `cpus` (list) specified: generate optimized cpuset string
               - Elif `cpu_count` specified: generate contiguous cpuset string
               - If `memory_gb` specified: generate memory string
            3. Deep merge resource_overrides with template (recursive dict merge)
            4. Write merged config-resource.yaml
            5. Store generated cpuset for .env CPUSET_CPUS population

        Example transformation:
            Input: {"workers": {"local": {"cpu_count": 8, "memory_gb": 32}}}
            After: {"workers": {"local": {"cpuset": "0-7", "memory": "32G"}}}
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
    """Construct oss-bugfind-crs build command."""
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
