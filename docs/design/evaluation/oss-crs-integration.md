# CRSBench Integration with oss-crs CLI

This document describes how CRSBench orchestrates CRS execution using the `oss-crs` command-line interface with trial isolation and parameter management.

> Note: Some command examples in this design doc are historical and may not match the latest `oss-crs` CLI flags exactly.
> For current behavior, see [docs/reference/oss-crs-interface.md](../../reference/oss-crs-interface.md) and [oss-crs/docs/design/parallel.md](../../../oss-crs/docs/design/parallel.md).

## Purpose

CRSBench uses the standardized `oss-crs` CLI to:
- Build CRS Docker images for bug finding
- Execute CRS trials with isolated output directories
- Resolve CRS metadata from the canonical registry (`oss-crs/registry`)
- Control OSS-Fuzz and build directory locations
- Pre-clone and checkout source code at specific commits

## Architecture Overview

```
CRSBench Orchestrator
    ↓
Trial-Specific Configuration
    ├── --build-dir (unique per trial)
    ├── --oss-fuzz-dir (shared managed checkout)
    ├── --registry-dir (default: oss-crs/registry; override via `registry_dir`)
    ├── --project-path (from benchmarks/)
    └── source-path (pre-cloned by CRSBench)
    ↓
oss-crs CLI
    ↓
CRS Docker Container
    ↓
Trial Outputs
```

## Parameter Mappings

### 1. Build Directory (`--build-dir`)

**Purpose**: Isolate each trial's build artifacts and outputs.

**CRSBench Strategy**: Generate unique build directory per trial.

```python
# In CRSBench orchestrator/executor
trial_build_dir = experiment_output_dir / f"trial-{trial_num}" / "build"
trial_build_dir.mkdir(parents=True, exist_ok=True)
```

**Usage**:
```bash
# Each trial gets its own build directory
oss-crs build --build-dir /experiments/exp-1/trial-0/build \
              example_configs/ensemble-c json-c

oss-crs run --build-dir /experiments/exp-1/trial-0/build \
            example_configs/ensemble-c json-c json_array_fuzzer
```

**Directory Structure**:
```
/experiments/exp-1/
├── trial-0/
│   └── build/
│       ├── crs/              # CRS Docker images and config
│       ├── out/              # Build outputs and fuzzing results
│       └── src/              # Cloned project sources
├── trial-1/
│   └── build/
│       ├── crs/
│       ├── out/
│       └── src/
└── trial-2/
    └── build/
        ├── crs/
        ├── out/
        └── src/
```

**Benefits**:
- Complete trial isolation
- Parallel execution without conflicts
- Easy cleanup per trial
- Clear separation of outputs

**Note**: The `--build-dir` must be consistent between `build` and `run` commands for the same trial.

### 2. OSS-Fuzz Directory (`--oss-fuzz-dir`)

**Purpose**: Specify the OSS-Fuzz repository location.

**CRSBench Strategy**: Use the managed `third_party/oss-fuzz` checkout in the CRSBench repository.

```python
# In CRSBench configuration
oss_fuzz_dir = CRSBENCH_ROOT / "third_party" / "oss-fuzz"
```

**Usage**:
```bash
# All trials share the same OSS-Fuzz directory
oss-crs build --oss-fuzz-dir /path/to/CRSBench/third_party/oss-fuzz \
              --build-dir /experiments/exp-1/trial-0/build \
              example_configs/ensemble-c json-c
```

**Benefits**:
- Single OSS-Fuzz installation for all trials
- Consistent build infrastructure
- Version control via managed sparse checkout
- No redundant OSS-Fuzz clones

**Note**: OSS-Fuzz directory is **shared** across trials, while build-dir is **isolated** per trial.

### 3. CRS Registry Directory (`--registry-dir`)

**Purpose**: Specify where CRS metadata and configurations are stored.

**CRSBench Strategy**: Use `oss-crs/registry` as the canonical source.
`registry_dir` can override this path when needed (for example, workers with
different mount points), but there is no mode-based registry selection.

**Usage**:
```bash
# Canonical registry usage
oss-crs build --registry-dir /path/to/CRSBench/oss-crs/registry \
              --build-dir /experiments/exp-1/trial-0/build \
              example_configs/atlantis-c-libafl json-c
```

**Registry Structure**:
```
oss-crs/registry/
└── crs/
    ├── atlantis-c-libafl/
    │   └── pkg.yaml
    ├── crs-libfuzzer/
    │   └── pkg.yaml
    └── mock-crs/
        └── pkg.yaml
```

**Benefits**:
- Single canonical CRS metadata source
- Simpler config contract (no registry mode switches)
- Consistent behavior across local and distributed workers

**Configuration**:
```yaml
# In experiment config
registry_dir: ./oss-crs/registry  # optional override; default shown
```

**Implementation**:
```python
def get_registry_dir(config):
    return Path(config.get("registry_dir") or CRSBENCH_ROOT / "oss-crs/registry")
```

### 4. Project Path (`--project-path`)

**Purpose**: Provide custom OSS-Fuzz compatible project directory.

**CRSBench Strategy**: Use benchmark directory from `benchmarks/` as project path.

```python
# In CRSBench orchestrator
benchmark_dir = CRSBENCH_ROOT / "benchmarks" / benchmark_name
```

**Usage**:
```bash
# Use benchmark directory as custom project
oss-crs build --project-path /path/to/CRSBench/benchmarks/json-c-delta-01 \
              --build-dir /experiments/exp-1/trial-0/build \
              example_configs/ensemble-c \
              json-c-delta-01
```

**Benchmark Structure**:
```
benchmarks/json-c-delta-01/
├── project.yaml          # OSS-Fuzz project metadata
├── Dockerfile            # Build instructions
├── build.sh              # Build script
└── .aixcc/               # CRSBench metadata
    └── meta.yaml         # Benchmark configuration
```

**Benefits**:
- Out-of-tree benchmark projects
- No modification to OSS-Fuzz repository
- Benchmark-specific build configurations
- Preserves OSS-Fuzz compatibility

**Note**: `--overwrite` flag is automatically used to replace existing project in OSS-Fuzz during build.

### 5. Pre-cloned Source Code

**Purpose**: Provide source code at specific commit for reproducible builds.

**CRSBench Strategy**: Clone source based on `meta.yaml` commit specification before running oss-crs.

**Source Code Management**:

```python
# In CRSBench repository manager
def prepare_source_for_trial(benchmark_dir, trial_build_dir):
    """
    Clone source code at commit specified in meta.yaml.

    Returns:
        Path to cloned source directory
    """
    meta = load_meta_yaml(benchmark_dir / ".aixcc" / "meta.yaml")
    project_yaml = load_yaml(benchmark_dir / "project.yaml")

    repo_url = project_yaml["main_repo"]
    commit = meta["delta_mode"]["base_commit"]  # or full_mode.base_commit

    # Clone to trial-specific source directory
    source_dir = trial_build_dir / "src" / project_name

    if not source_dir.exists():
        # Clone with depth 1 for speed
        subprocess.run([
            "git", "clone", "--depth", "1",
            "--branch", commit,  # or use git checkout after clone
            repo_url, str(source_dir)
        ])

    return source_dir
```

**Usage**:
```bash
# CRSBench clones source first
# (handled internally by CRSBench)

# Then passes source path to oss-crs
oss-crs build --project-path /path/to/benchmarks/json-c-delta-01 \
              --build-dir /experiments/exp-1/trial-0/build \
              example_configs/ensemble-c \
              json-c-delta-01 \
              /experiments/exp-1/trial-0/build/src/json-c
```

**Benefits**:
- Reproducible builds at exact commits
- Source code matches meta.yaml specification
- No network access during build (offline capable)
- Trial-specific source isolation

**Commit Resolution**:

From `meta.yaml`:
```yaml
delta_mode:
  base_commit: "abc123def456"  # Used for delta mode
  ref_commit: "def456abc789"   # Reference commit

full_mode:
  base_commit: "abc123def456"  # Used for full mode
```

**Implementation Details**:
```python
class SourceManager:
    def clone_at_commit(self, repo_url, commit, dest_dir):
        """Clone repository and checkout specific commit."""
        if dest_dir.exists():
            logger.info(f"Source already exists at {dest_dir}")
            return dest_dir

        logger.info(f"Cloning {repo_url} at {commit}")

        # Clone with single branch
        subprocess.run([
            "git", "clone",
            "--depth", "1",
            "--single-branch",
            "--recurse-submodules",
            repo_url, str(dest_dir)
        ], check=True)

        # Checkout specific commit (may need to fetch if not in initial clone)
        subprocess.run([
            "git", "-C", str(dest_dir),
            "checkout", commit
        ], check=True)

        return dest_dir
```

## Complete Command Examples

### Bug Finding CRS (oss-crs)

**Build Phase**:
```bash
oss-crs build \
  --build-dir /experiments/exp-1/trial-0/build \
  --oss-fuzz-dir /path/to/CRSBench/third_party/oss-fuzz \
  --registry-dir /path/to/CRSBench/oss-crs/registry \
  --project-path /path/to/CRSBench/benchmarks/json-c-delta-01 \
  example_configs/ensemble-c \
  json-c-delta-01 \
  /experiments/exp-1/trial-0/build/src/json-c
```

**Run Phase**:
```bash
oss-crs run \
  --build-dir /experiments/exp-1/trial-0/build \
  --oss-fuzz-dir /path/to/CRSBench/third_party/oss-fuzz \
  --registry-dir /path/to/CRSBench/oss-crs/registry \
  example_configs/ensemble-c \
  json-c-delta-01 \
  json_array_fuzzer \
  --output /experiments/exp-1/trial-0/output \
  --hints /experiments/exp-1/trial-0/hints
```

### Patch Generation CRS (oss-crs)

**Build Phase**:
```bash
oss-crs build \
  --build-dir /experiments/exp-1/trial-0/build \
  --oss-fuzz-dir /path/to/CRSBench/third_party/oss-fuzz \
  --registry-dir /path/to/CRSBench/oss-crs/registry \
  --project-path /path/to/CRSBench/benchmarks/json-c-delta-01 \
  example_configs/patch-agent \
  json-c-delta-01 \
  /experiments/exp-1/trial-0/build/src/json-c
```

**Run Phase**:
```bash
oss-crs run \
  --build-dir /experiments/exp-1/trial-0/build \
  --oss-fuzz-dir /path/to/CRSBench/third_party/oss-fuzz \
  --registry-dir /path/to/CRSBench/oss-crs/registry \
  example_configs/patch-agent \
  json-c-delta-01 \
  --harness json_array_fuzzer \
  --povs /experiments/exp-1/trial-0/povs \
  --hints /experiments/exp-1/trial-0/hints \
  --output /experiments/exp-1/trial-0/output \
  --litellm-base https://api.litellm.com \
  --litellm-key sk-key
```

## Trial Isolation Strategy

### Directory Layout

```
/experiments/experiment-1/
├── config.yaml                  # Experiment configuration
├── trial-0/                     # Trial 0 isolation
│   ├── build/                   # Trial-specific build dir
│   │   ├── crs/                 # CRS Docker images
│   │   ├── out/                 # Build outputs
│   │   └── src/                 # Pre-cloned source
│   │       └── json-c/          # At base_commit
│   ├── output/                  # CRS outputs (POVs, patches)
│   ├── hints/                   # Prepared hints
│   ├── povs/                    # Prepared POVs (patch gen)
│   ├── config.yaml              # Trial config
│   └── execution.json           # Execution metadata
├── trial-1/                     # Trial 1 isolation
│   ├── build/
│   ├── output/
│   └── ...
└── report/                      # Aggregated results
    ├── summary.json
    └── detailed-results.json
```

### Isolation Benefits

**1. Parallel Execution**:
- Multiple trials can run simultaneously
- No file conflicts or race conditions
- Each trial has independent build artifacts

**2. Reproducibility**:
- Complete trial state in single directory
- Easy to archive or replay
- Source code at exact commit

**3. Debugging**:
- Clear separation of trial outputs
- Easy to inspect individual trial state
- Logs and metadata per trial

**4. Cleanup**:
- Remove trial directory to cleanup
- Selective cleanup (keep successful trials)
- Disk space management

## Registry Directory Selection

CRSBench resolves CRS metadata from `oss-crs/registry` by default.
Use `registry_dir` only when you need an explicit path override.

**Typical uses for override**:
- Worker machines mount CRSBench at different filesystem roots
- Test harnesses need a temporary forked registry path
- CI jobs stage registry content in alternate workspaces

**Configuration**:
```yaml
# experiment-config.yaml
# optional (default: ./oss-crs/registry)
registry_dir: /opt/oss-crs/registry
```

### Implementation

```python
from crsbench.evaluation.adapter import create_adapter

adapter = create_adapter(
    config=config,
    crs_config_name=crs,
    oss_fuzz_path=oss_fuzz_path,
    registry_dir=registry_dir,      # default oss-crs/registry, optional override
    benchmarks_root=benchmarks_root,
    mode=crs_type,                   # "bug-finding" or "bug-fixing"
)

# OssCrsAdapter uses crs-compose lifecycle:
#   prepare (once per CRS) → build-target (once per project) → run (per harness)
adapter.configure({...})
```

## Source Code Management

### Repository Manager Integration

CRSBench uses the repository manager to handle source code cloning:

```python
from crsbench.migration.repo_manager import ensure_project_repository

def prepare_trial_source(benchmark_dir, trial_build_dir):
    """
    Prepare source code for trial using repository manager.

    The repository manager:
    1. Reads main_repo from project.yaml
    2. Reads base_commit from meta.yaml
    3. Clones (or reuses cached) source at specific commit
    4. Returns path to source directory
    """
    source_path = ensure_project_repository(
        benchmark_dir=str(benchmark_dir),
        dest_dir=str(trial_build_dir / "src"),
        verbose=True
    )

    if not source_path:
        raise EvaluationError(
            f"Failed to clone source for {benchmark_dir.name}. "
            "Check project.yaml main_repo and meta.yaml commits."
        )

    return Path(source_path)
```

### Commit Checkout Strategy

**From meta.yaml**:
```yaml
delta_mode:
  base_commit: "abc123"  # Vulnerable version
  ref_commit: "def456"   # Fixed version

full_mode:
  base_commit: "abc123"  # Vulnerable version
```

**Checkout Logic**:
```python
def get_checkout_commit(meta, mode):
    """Determine which commit to checkout based on mode."""
    if mode == "delta":
        return meta["delta_mode"]["base_commit"]
    elif mode == "full":
        return meta["full_mode"]["base_commit"]
    else:
        raise ValueError(f"Unknown mode: {mode}")
```

## Error Handling

### Build Directory Conflicts

**Problem**: Build directory already exists from previous trial.

**Solution**: Use `--overwrite` flag or unique trial IDs.

```python
# Generate unique build directory
import uuid
trial_id = str(uuid.uuid4())[:8]
trial_build_dir = experiment_dir / f"trial-{trial_num}-{trial_id}" / "build"
```

### Source Clone Failures

**Problem**: Network issues, invalid commit, missing repo.

**Solution**: Retry with backoff, fallback to cached source.

```python
def clone_with_retry(repo_url, commit, dest_dir, retries=3):
    for attempt in range(retries):
        try:
            return clone_at_commit(repo_url, commit, dest_dir)
        except subprocess.CalledProcessError as e:
            if attempt < retries - 1:
                logger.warning(f"Clone failed (attempt {attempt+1}/{retries}): {e}")
                time.sleep(2 ** attempt)  # Exponential backoff
            else:
                raise
```

### Registry Not Found

**Problem**: CRS configuration not in specified registry.

**Solution**: Validate against the effective registry directory and fail with a clear error.

```python
def resolve_crs_config(crs_name, registry_dir):
    crs_dir = registry_dir / "crs" / crs_name
    if not crs_dir.exists():
        raise EvaluationError(
            f"CRS configuration '{crs_name}' not found in registry {registry_dir}. "
            f"Available CRS: {list_available_crs(registry_dir)}"
        )

    return crs_dir
```

## Performance Considerations

### Build Directory Size

**Typical Sizes**:
- CRS Docker image layers: 1-5 GB
- Build artifacts: 100-500 MB
- Source code: 10-100 MB
- Total per trial: ~2-6 GB

**Disk Space Planning**:
```python
def estimate_disk_usage(num_trials, num_crses):
    """Estimate disk space needed for experiment."""
    avg_trial_size_gb = 3.0  # Average GB per trial
    total_trials = num_trials * num_crses
    estimated_gb = total_trials * avg_trial_size_gb

    logger.info(f"Estimated disk usage: {estimated_gb:.1f} GB for {total_trials} trials")
    return estimated_gb
```

### Source Clone Optimization

**Strategy**: Reuse cloned source across trials when possible.

**Implementation**:
```python
# Shared source cache (optional optimization)
SHARED_SOURCE_CACHE = CRSBENCH_ROOT / ".cache" / "sources"

def get_cached_source(repo_url, commit):
    """Check if source already cloned in shared cache."""
    cache_key = hashlib.sha256(f"{repo_url}:{commit}".encode()).hexdigest()[:16]
    cached_path = SHARED_SOURCE_CACHE / cache_key

    if cached_path.exists():
        logger.info(f"Using cached source from {cached_path}")
        return cached_path

    return None

def clone_with_cache(repo_url, commit, dest_dir):
    """Clone with optional caching."""
    cached = get_cached_source(repo_url, commit)

    if cached:
        # Copy from cache instead of cloning
        shutil.copytree(cached, dest_dir)
        return dest_dir

    # Clone normally
    clone_at_commit(repo_url, commit, dest_dir)

    # Optionally cache for future use
    cache_source(repo_url, commit, dest_dir)

    return dest_dir
```

**Trade-offs**:
- Faster trial setup (no git clone)
- More disk space (shared cache)
- Complexity (cache invalidation)

## Integration with Orchestrator

### Orchestrator Responsibilities

```python
class ExperimentOrchestrator:
    def run_trial(self, crs, benchmark, trial_num, config):
        """
        Orchestrate single trial execution.

        1. Create trial directory structure
        2. Prepare source code at specified commit
        3. Build CRS with appropriate parameters
        4. Prepare hints and POVs
        5. Run CRS
        6. Collect and analyze outputs
        """
        # 1. Create trial directory
        trial_dir = self.create_trial_directory(crs, benchmark, trial_num)

        # 2. Prepare source code
        source_path = self.prepare_source_code(benchmark, trial_dir)

        # 3. Build CRS
        self.build_crs(
            crs=crs,
            benchmark=benchmark,
            trial_dir=trial_dir,
            source_path=source_path
        )

        # 4. Prepare hints and POVs
        self.prepare_trial_inputs(benchmark, trial_dir, config)

        # 5. Run CRS
        result = self.run_crs(
            crs=crs,
            benchmark=benchmark,
            trial_dir=trial_dir,
            config=config
        )

        # 6. Collect outputs
        return self.collect_trial_results(trial_dir, result)

    def build_crs(self, crs, benchmark, trial_dir, source_path):
        """Build CRS using oss-crs CLI."""
        registry_dir = self.get_registry_dir()
        benchmark_dir = CRSBENCH_ROOT / "benchmarks" / benchmark

        cmd = [
            "oss-crs", "build",
            "--build-dir", str(trial_dir / "build"),
            "--oss-fuzz-dir", str(CRSBENCH_ROOT / "third_party" / "oss-fuzz"),
            "--registry-dir", str(registry_dir),
            "--project-path", str(benchmark_dir),
            f"example_configs/{crs}",
            benchmark,
            str(source_path)
        ]

        subprocess.run(cmd, check=True)
```

## Testing Strategy

### Unit Tests

Test parameter construction:
```python
def test_build_command_construction():
    orchestrator = ExperimentOrchestrator(config)
    cmd = orchestrator.build_oss_crs_command(
        crs="ensemble-c",
        benchmark="json-c",
        trial_dir=Path("/tmp/trial-0"),
        source_path=Path("/tmp/trial-0/build/src/json-c")
    )

    assert "--build-dir" in cmd
    assert "--oss-fuzz-dir" in cmd
    assert "--registry-dir" in cmd
    assert "--project-path" in cmd
```

### Integration Tests

Test with mock oss-crs:
```python
def test_trial_execution_with_mock_crs(tmp_path):
    # Create mock directory structure
    trial_dir = tmp_path / "trial-0"
    trial_dir.mkdir()

    # Create mock benchmark
    benchmark_dir = tmp_path / "benchmarks" / "test-bench"
    create_mock_benchmark(benchmark_dir)

    # Run trial
    orchestrator = ExperimentOrchestrator(test_config)
    result = orchestrator.run_trial(
        crs="mock-crs",
        benchmark="test-bench",
        trial_num=0,
        config=test_config
    )

    # Verify directory structure
    assert (trial_dir / "build").exists()
    assert (trial_dir / "output").exists()
    assert result["success"] == True
```

## References

- [oss-crs README](../../../oss-crs/README.md): oss-crs CLI documentation
- [Evaluation Module Design](./evaluation.md): Runner and adapter orchestration
- [OSS-CRS Interface](../../reference/oss-crs-interface.md): Interface specification
- [Repository Manager](../migration/repo-manager.md): Source code management
- [Orchestration Design](../orchestration.md): Experiment orchestration
