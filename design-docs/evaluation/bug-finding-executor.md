# Bug Finding Executor Design

This document describes the implementation of `CRSBugFindingExecutor`, which executes bug finding CRS via the oss-bugfind-crs CLI.

## Purpose

`CRSBugFindingExecutor` is responsible for:
- Executing oss-bugfind-crs build and run commands for bug finding CRS
- Capturing execution outputs and metadata
- **NOT responsible for POV validation** - POV validation is handled by a separate snapshot module

## Architecture Overview

```
Orchestrator
    ↓
TrialDirectoryPreparer.prepare_trial()
    ↓ (returns TrialPreparationResult)
CRSBugFindingExecutor.run_crs()
    ↓
├── Build Phase: oss-bugfind-crs build
│   └── Creates CRS Docker image
├── Run Phase: oss-bugfind-crs run
│   └── Executes CRS bug finding campaign
└── Return CRSResult
    ↓
Snapshot Module (separate)
    └── Validates POVs, generates snapshots
```

## Responsibilities

### What Executor DOES:
- Execute oss-bugfind-crs build command with trial-specific parameters
- Execute oss-bugfind-crs run command with output and hints directories
- Capture stdout, stderr, exit code
- Store execution metadata (command, timing, outputs)
- Return CRSResult indicating execution success/failure

### What Executor DOES NOT Do:
- ✗ POV validation (done by snapshot module)
- ✗ POV replay with sanitizers (done by snapshot module)
- ✗ Sanitizer output parsing (done by snapshot module)
- ✗ Matching discovered POVs to expected POVs (done by snapshot module)

## Class Design

```python
from pathlib import Path
from typing import Dict, Any, Optional, List
from crsbench.evaluation.crs_executor import CRSExecutor, CRSResult
from crsbench.evaluation.results import POVResult
from crsbench.validation.schemas import HarnessFile

class CRSBugFindingExecutor(CRSExecutor):
    """Executor for bug finding CRS using oss-bugfind-crs CLI."""

    def __init__(
        self,
        experiment_dir: Path,
        benchmarks_root: Path,
        oss_fuzz_dir: Path,
        crs_registry_dir: Path
    ):
        """
        Initialize bug finding executor.

        Args:
            experiment_dir: Experiment directory
            benchmarks_root: Path to benchmarks directory
            oss_fuzz_dir: Path to oss-fuzz submodule
            crs_registry_dir: CRS registry (oss-crs-registry or crses)
        """
        self.experiment_dir = experiment_dir
        self.benchmarks_root = benchmarks_root
        self.oss_fuzz_dir = oss_fuzz_dir
        self.crs_registry_dir = crs_registry_dir
        self.config: Dict[str, Any] = {}

    def _resolve_crs_config_dir(self) -> Path:
        """
        Resolve CRS configuration directory path.

        Searches for CRS configuration in crses/ directory, which follows
        the same format as oss-crs/example_configs/.

        Note:
            This does NOT search in oss-crs-registry/. The registry is only
            used via --registry-dir parameter for oss-bugfind-crs CLI.

        Returns:
            Path to CRS config directory

        Example:
            crs_name: "ensemble-c"
            crses_dir: /path/to/CRSBench/crses
            Returns: /path/to/CRSBench/crses/ensemble-c/
        """
        crs_name = self.config.get("crs_name")
        if not crs_name:
            raise ExecutorError("crs_name not configured")

        # Check if full path provided
        config_path = Path(crs_name)
        if config_path.is_absolute() and config_path.exists():
            return config_path

        # Resolve from crses/ directory (NOT oss-crs-registry/)
        crses_dir = Path(__file__).parent.parent.parent / "crses"
        crs_config_dir = crses_dir / crs_name

        if not crs_config_dir.exists():
            raise ExecutorError(
                f"CRS config directory not found: {crs_config_dir}\n"
                f"Available in {crses_dir}: "
                f"{[d.name for d in crses_dir.iterdir() if d.is_dir()]}"
            )

        return crs_config_dir

    def configure_crs(self, config: Dict[str, Any]) -> None:
        """Configure the CRS executor."""
        self.config = config.copy()

    def run_crs(
        self,
        benchmark_path: Path,
        harness: HarnessFile,
        trial_output_dir: Path
    ) -> CRSResult:
        """
        Run CRS on a specific harness.

        Args:
            benchmark_path: Path to benchmark directory
            harness: Harness configuration
            trial_output_dir: Trial directory (from TrialDirectoryPreparer)

        Returns:
            CRSResult with execution details

        Note:
            Source code is already prepared at the correct commit by
            TrialDirectoryPreparer. The executor does not need commit
            information - it simply runs CRS on pre-prepared directories.
        """
        # Implementation details below

    def process_pov_results(
        self,
        crs_result: CRSResult,
        harness: HarnessFile,
        trial_output_dir: Path
    ) -> List[POVResult]:
        """
        Process CRS results.

        Note: This is a stub for bug finding executor.
        Actual POV validation is done by snapshot module.

        Returns:
            Empty list (POV validation handled separately)
        """
        return []
```

## Core Method: configure_crs()

```python
def configure_crs(self, config: Dict[str, Any]) -> None:
    """
    Configure CRS executor.

    Args:
        config: Configuration parameters
            - crs_name: CRS configuration name (e.g., "ensemble-c")
            - build_timeout: Build timeout in seconds (default: 3600)
            - run_timeout: Run timeout in seconds (default: 7200)
            - hints_enabled: Whether to provide hints (default: False)
    """
    self.config = config.copy()

    # Set defaults
    self.config.setdefault("build_timeout", 3600)  # 1 hour
    self.config.setdefault("run_timeout", 7200)    # 2 hours
    self.config.setdefault("hints_enabled", False)

    logger.debug(f"Configured CRSBugFindingExecutor: {self.config}")
```

## Core Method: run_crs()

```python
def run_crs(
    self,
    benchmark_path: Path,
    harness: HarnessFile,
    trial_output_dir: Path
) -> CRSResult:
    """
    Run CRS via oss-bugfind-crs CLI.

    Process:
        1. Extract paths from trial directory
        2. Build CRS Docker image (oss-bugfind-crs build)
        3. Run CRS bug finding campaign (oss-bugfind-crs run)
        4. Store execution metadata
        5. Return CRSResult

    Args:
        benchmark_path: Path to benchmark directory
        harness: Harness configuration
        trial_output_dir: Trial directory prepared by TrialDirectoryPreparer

    Returns:
        CRSResult with execution details

    Note:
        Source code is already cloned at the correct commit by TrialDirectoryPreparer.
        The executor does not need commit information.
    """
    start_time = time.time()

    try:
        # 1. Extract paths from trial directory structure
        build_dir = trial_output_dir / "build"
        hints_dir = trial_output_dir / "hints"
        source_path = self._find_source_path(build_dir, benchmark_path.name)

        if not build_dir.exists():
            raise ExecutorError(f"Build directory not found: {build_dir}")
        if not source_path.exists():
            raise ExecutorError(f"Source path not found: {source_path}")

        # Note: Output directory is auto-determined by oss-crs as:
        # {{ build_dir }}/artifacts/{{ crs_name }}/{{ project }}/
        # We'll retrieve it after execution using _get_crs_output_dir()

        # 2. Build CRS Docker image
        build_result = self._build_crs_image(
            benchmark_name=benchmark_path.name,
            build_dir=build_dir,
            source_path=source_path
        )

        if not build_result.success:
            execution_time = time.time() - start_time
            return CRSResult(
                harness_name=harness.name,
                execution_time=execution_time,
                success=False,
                output=build_result.output,
                error=f"Build failed: {build_result.error}"
            )

        # 3. Run CRS bug finding campaign
        run_result = self._run_crs_campaign(
            benchmark_name=benchmark_path.name,
            harness_name=harness.name,
            build_dir=build_dir,
            hints_dir=hints_dir if hints_dir.exists() else None
        )

        execution_time = time.time() - start_time

        # 4. Store execution metadata
        self._store_execution_metadata(
            trial_output_dir=trial_output_dir,
            harness=harness,
            build_result=build_result,
            run_result=run_result,
            execution_time=execution_time
        )

        # 5. Return result
        return CRSResult(
            harness_name=harness.name,
            execution_time=execution_time,
            success=run_result.success,
            output=run_result.output,
            error=run_result.error,
            povs_detected=None  # POV detection handled by snapshot module
        )

    except Exception as e:
        execution_time = time.time() - start_time
        logger.error(f"CRS execution failed: {e}", exc_info=True)
        return CRSResult(
            harness_name=harness.name,
            execution_time=execution_time,
            success=False,
            output="",
            error=str(e)
        )
```

## Core Method: process_pov_results()

```python
def process_pov_results(
    self,
    crs_result: CRSResult,
    harness: HarnessFile,
    trial_output_dir: Path
) -> List[POVResult]:
    """
    Process CRS results.

    Note: For bug finding executor, this is a stub.
    POV validation is handled by the snapshot module separately.

    Args:
        crs_result: CRS execution result
        harness: Harness configuration
        trial_output_dir: Trial directory

    Returns:
        Empty list (POV validation done by snapshot module)
    """
    logger.debug(
        f"Bug finding executor does not process POV results. "
        f"POV validation handled by snapshot module for {harness.name}"
    )
    return []
```

## Important: config_dir vs crs_name

The oss-bugfind-crs CLI takes a `config_dir` (Path) as the first positional argument, not a `crs_name` (str):

```bash
# Correct: config_dir is a path to the directory
oss-bugfind-crs build /path/to/crses/crs/ensemble-c json-c /path/to/source

# Wrong: Do not pass just the name
oss-bugfind-crs build ensemble-c json-c /path/to/source
```

**In CRSBench:**
- Executor stores `crs_name` in config (e.g., "ensemble-c")
- Helper method `_resolve_crs_config_dir()` converts name to full path
- Full path is passed to oss-bugfind-crs CLI: `crses/<crs_name>/`

**Example resolution:**
- `crs_name`: "ensemble-c"
- `crses_dir`: `/path/to/CRSBench/crses`
- `config_dir`: `/path/to/CRSBench/crses/ensemble-c/` ← This is what oss-crs receives

**Note**: The `crses/` directory follows the same format as `oss-crs/example_configs/`, NOT the registry format.

## Helper Methods

### 1. Find Source Path

```python
def _find_source_path(self, build_dir: Path, benchmark_name: str) -> Path:
    """
    Find source path in build directory.

    The source is at: build/src/<benchmark-name>/

    Args:
        build_dir: Build directory
        benchmark_name: Benchmark name

    Returns:
        Path to source directory

    Raises:
        ExecutorError: If source not found
    """
    source_path = build_dir / "src" / benchmark_name

    if not source_path.exists():
        raise ExecutorError(
            f"Source path not found: {source_path}. "
            "Ensure TrialDirectoryPreparer ran successfully."
        )

    return source_path
```

### 2. Build CRS Image

```python
def _build_crs_image(
    self,
    benchmark_name: str,
    build_dir: Path,
    source_path: Path
) -> subprocess.CompletedProcess:
    """
    Build CRS Docker image using oss-bugfind-crs CLI.

    Args:
        benchmark_name: Benchmark name
        build_dir: Build directory
        source_path: Path to source code

    Returns:
        CompletedProcess with build result
    """
    crs_config_dir = self._resolve_crs_config_dir()
    benchmark_dir = self.benchmarks_root / benchmark_name

    # Construct oss-bugfind-crs build command
    cmd = [
        "oss-crs", "build",
        str(crs_config_dir),  # config_dir (positional)
        benchmark_name,        # project (positional)
        str(source_path),      # source_path (optional positional)
        "--build-dir", str(build_dir),
        "--oss-fuzz-dir", str(self.oss_fuzz_dir),
        "--registry-dir", str(self.crs_registry_dir),
        "--project-path", str(benchmark_dir)
    ]

    logger.info(f"Building CRS image: {' '.join(cmd)}")

    # Execute with timeout
    timeout = self.config.get("build_timeout", 3600)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False
        )

        if result.returncode != 0:
            logger.error(f"Build failed with code {result.returncode}")
            logger.error(f"Build output: {result.stdout}")
            logger.error(f"Build error: {result.stderr}")

        return result

    except subprocess.TimeoutExpired as e:
        logger.error(f"Build timeout after {timeout}s")
        raise ExecutorError(f"Build timeout after {timeout}s") from e
```

### 3. Run CRS Campaign

```python
def _run_crs_campaign(
    self,
    benchmark_name: str,
    harness_name: str,
    build_dir: Path,
    hints_dir: Optional[Path]
) -> subprocess.CompletedProcess:
    """
    Run CRS bug finding campaign using oss-bugfind-crs CLI.

    Args:
        benchmark_name: Benchmark name
        harness_name: Harness name
        build_dir: Build directory
        hints_dir: Hints directory (optional)

    Returns:
        CompletedProcess with run result

    Note:
        Output directory is auto-determined by oss-crs as:
        {{ build_dir }}/artifacts/{{ crs_name }}/{{ project }}/
        No --output parameter is needed for oss-bugfind-crs run command.
    """
    crs_config_dir = self._resolve_crs_config_dir()

    # Construct oss-bugfind-crs run command
    cmd = [
        "oss-crs", "run",
        str(crs_config_dir),  # config_dir (positional)
        benchmark_name,        # project (positional)
        harness_name,          # fuzzer_name (positional)
        "--build-dir", str(build_dir),
        "--oss-fuzz-dir", str(self.oss_fuzz_dir),
        "--registry-dir", str(self.crs_registry_dir)
    ]

    # Add hints if available
    if hints_dir and hints_dir.exists():
        cmd.extend(["--hints", str(hints_dir)])
        logger.info(f"Using hints from: {hints_dir}")
    else:
        logger.info("Running without hints")

    logger.info(f"Running CRS: {' '.join(cmd)}")
    logger.info(f"Output will be at: {build_dir}/artifacts/{self.config.get('crs_name')}/{benchmark_name}/")

    # Execute with timeout
    timeout = self.config.get("run_timeout", 7200)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False
        )

        if result.returncode != 0:
            logger.warning(f"Run finished with code {result.returncode}")
            logger.debug(f"Run output: {result.stdout}")
            logger.debug(f"Run error: {result.stderr}")

        return result

    except subprocess.TimeoutExpired as e:
        logger.warning(f"Run timeout after {timeout}s (may be expected)")
        # Timeout is not necessarily an error - CRS may still have produced results
        # Return partial result
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=124,  # Standard timeout exit code
            stdout=e.stdout.decode() if e.stdout else "",
            stderr=f"Timeout after {timeout}s"
        )
```

### 4. Get CRS Output Directory

```python
def _get_crs_output_dir(self, build_dir: Path, benchmark_name: str) -> Path:
    """
    Get CRS output directory path.

    The output directory is auto-determined by oss-crs as:
    {{ build_dir }}/artifacts/{{ crs_name }}/{{ project }}/

    Args:
        build_dir: Build directory
        benchmark_name: Benchmark name

    Returns:
        Path to CRS output directory

    Note:
        This directory is created and populated by oss-crs during execution.
        It contains subdirectories: povs/, corpus/, crs-data/
    """
    crs_name = self.config.get("crs_name")
    if not crs_name:
        raise ExecutorError("crs_name not configured")

    return build_dir / "out" / crs_name / benchmark_name
```

### 5. Store Execution Metadata

```python
def _store_execution_metadata(
    self,
    trial_output_dir: Path,
    harness: HarnessFile,
    build_result: subprocess.CompletedProcess,
    run_result: subprocess.CompletedProcess,
    execution_time: float
) -> None:
    """
    Store execution metadata to trial directory.

    Args:
        trial_output_dir: Trial directory
        harness: Harness configuration
        build_result: Build command result
        run_result: Run command result
        execution_time: Total execution time
    """
    import json
    from datetime import datetime

    hints_dir = trial_output_dir / "hints"
    build_dir = trial_output_dir / "build"

    # Get actual output directory (auto-determined by oss-crs)
    crs_output_dir = self._get_crs_output_dir(build_dir, harness.name)

    metadata = {
        "timestamp": datetime.now().isoformat(),
        "executor": "CRSBugFindingExecutor",
        "harness": harness.name,
        "crs_config": self.config.get("crs_name"),
        "execution_time": execution_time,
        "build": {
            "command": " ".join(build_result.args),
            "returncode": build_result.returncode,
            "success": build_result.returncode == 0
        },
        "run": {
            "command": " ".join(run_result.args),
            "returncode": run_result.returncode,
            "timeout": run_result.returncode == 124
        },
        "hints": {
            "enabled": hints_dir.exists(),
            "path": str(hints_dir) if hints_dir.exists() else None
        },
        "outputs": {
            "crs_output_dir": str(crs_output_dir),
            "build_dir": str(build_dir),
            "note": "CRS output is at {{ build_dir }}/artifacts/{{ crs_name }}/{{ project }}/"
        }
    }

    metadata_file = trial_output_dir / "execution.json"
    with open(metadata_file, "w") as f:
        json.dump(metadata, f, indent=2)

    logger.debug(f"Stored execution metadata to {metadata_file}")
```

## Error Handling

### Custom Exception

```python
class ExecutorError(Exception):
    """Raised when executor encounters an error."""
    pass
```

### Error Cases

1. **Build Failure**:
   - Return CRSResult with success=False
   - Include build output in error field
   - Log build errors

2. **Build Timeout**:
   - Raise ExecutorError
   - Caught by run_crs() and returned as failed CRSResult

3. **Run Timeout**:
   - Return partial result (CRS may have produced results before timeout)
   - Use exit code 124 to indicate timeout
   - Not treated as hard failure

4. **Missing Directories**:
   - Check for build_dir and source_path existence
   - Raise ExecutorError with clear message

5. **Command Execution Errors**:
   - Catch subprocess exceptions
   - Log detailed error information
   - Return CRSResult with error details

## Integration with Trial Preparation

The executor uses directories prepared by `TrialDirectoryPreparer`:

```python
# Orchestrator creates trial directory
preparer = TrialDirectoryPreparer(...)
prep_result = preparer.prepare_trial(
    crs=crs_name,
    benchmark=benchmark_name,
    harness=harness_name,
    trial_num=trial_num,
    mode="bug_finding"
)

# Executor uses prepared directories
executor = CRSBugFindingExecutor(...)
executor.configure_crs(config)

crs_result = executor.run_crs(
    benchmark_path=benchmarks_root / benchmark_name,
    harness=harness,
    trial_output_dir=prep_result.trial_dir  # From preparer
)
```

**Expected Trial Directory Structure** (from preparer):
```
trial_output_dir/
├── build/                  # --build-dir for oss-crs
│   └── src/
│       └── <benchmark>/    # Pre-cloned source
├── output/                 # --output for oss-crs
├── hints/                  # --hints for oss-crs (optional)
│   ├── sarif/
│   └── corpus/
└── metadata.json           # From preparer
```

**After Executor Runs**:
```
trial_output_dir/
├── build/
│   ├── crs/                           # Created by oss-crs
│   ├── out/                           # Created by oss-crs (output location)
│   │   └── <crs_name>/
│   │       └── <project>/
│   │           ├── povs/              # Discovered POVs (if any)
│   │           ├── corpus/            # Generated corpus
│   │           └── crs-data/          # CRS-specific data
│   └── src/
├── hints/
├── metadata.json
└── execution.json         # Created by executor

Note: Bug finding CRS outputs to {{ build_dir }}/artifacts/{{ crs_name }}/{{ project }}/
      This is different from patch generation which outputs to trial_output_dir/output/
```

## POV Validation (Separate Module)

The snapshot module will:
1. Read POVs from `{{ build_dir }}/artifacts/{{ crs_name }}/{{ project }}/povs/`
   - Full path: `trial_output_dir/build/artifacts/<crs_name>/<project>/povs/`
2. Replay POVs with sanitizers
3. Parse sanitizer output
4. Match against expected POVs from harness
5. Generate POVResult objects
6. Create snapshots for validation

**Executor does NOT do POV validation.**

**Note on Output Location**: Unlike patch generation CRS which outputs to `trial_output_dir/output/`, bug finding CRS outputs to the build directory at `{{ build_dir }}/artifacts/{{ crs_name }}/{{ project }}/`. This is auto-determined by oss-crs and cannot be changed (no `--output` parameter for oss-bugfind-crs run).

## Configuration Parameters

```yaml
# In experiment config
crs_config:
  crs_name: "ensemble-c"      # CRS configuration name
  build_timeout: 3600         # Build timeout (seconds)
  run_timeout: 7200           # Run timeout (seconds)
  hints_enabled: true         # Whether to use hints
```

## CRS Configuration Source

**Important**: The `--registry-dir` parameter ALWAYS points to `oss-crs-registry/` (the only registry). CRS configurations are loaded from `crses/` directory.

```python
def get_crs_paths() -> tuple[Path, Path]:
    """
    Get CRS configuration and registry paths.

    Returns:
        Tuple of (crses_dir, registry_dir)
        - crses_dir: Where CRS configs are stored (crses/)
        - registry_dir: The CRS registry for --registry-dir (oss-crs-registry/)

    Note:
        crses/ is NOT a registry - it's a config directory following
        the same format as oss-crs/example_configs/.
    """
    crses_dir = CRSBENCH_ROOT / "crses"
    registry_dir = CRSBENCH_ROOT / "oss-crs-registry"

    return crses_dir, registry_dir
```

**Usage in Executor:**
- CRS configs loaded from: `crses/<crs_name>/` (via `_resolve_crs_config_dir()`)
- Registry passed to oss-crs: `--registry-dir oss-crs-registry/` (always)

## Testing Strategy

### Unit Tests

```python
def test_configure_crs():
    """Test CRS configuration."""
    executor = CRSBugFindingExecutor(...)
    executor.configure_crs({
        "crs_name": "test-crs",
        "build_timeout": 600
    })

    assert executor.config["crs_name"] == "test-crs"
    assert executor.config["build_timeout"] == 600
    assert executor.config["run_timeout"] == 7200  # Default


def test_find_source_path(tmp_path):
    """Test source path resolution."""
    build_dir = tmp_path / "build"
    source_path = build_dir / "src" / "test-bench"
    source_path.mkdir(parents=True)

    executor = CRSBugFindingExecutor(...)
    found_path = executor._find_source_path(build_dir, "test-bench")

    assert found_path == source_path


def test_build_command_construction(tmp_path):
    """Test oss-bugfind-crs build command construction."""
    executor = CRSBugFindingExecutor(...)
    executor.configure_crs({"crs_name": "test-crs"})

    # Mock _build_crs_image to capture command
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )

        executor._build_crs_image(
            benchmark_name="test-bench",
            build_dir=tmp_path / "build",
            source_path=tmp_path / "src"
        )

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "oss-crs"
        assert cmd[1] == "build"
        assert "--build-dir" in cmd
        assert "--oss-fuzz-dir" in cmd
        assert "--registry-dir" in cmd
        assert "--project-path" in cmd


def test_run_command_construction(tmp_path):
    """Test oss-bugfind-crs run command construction (without --output)."""
    executor = CRSBugFindingExecutor(...)
    executor.configure_crs({"crs_name": "test-crs"})

    # Mock _run_crs_campaign to capture command
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )

        executor._run_crs_campaign(
            benchmark_name="test-bench",
            harness_name="test_harness",
            build_dir=tmp_path / "build",
            hints_dir=None
        )

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "oss-crs"
        assert cmd[1] == "run"
        assert "--build-dir" in cmd
        assert "--oss-fuzz-dir" in cmd
        assert "--registry-dir" in cmd
        # Verify --output is NOT present
        assert "--output" not in cmd


def test_get_crs_output_dir(tmp_path):
    """Test CRS output directory path resolution."""
    executor = CRSBugFindingExecutor(...)
    executor.configure_crs({"crs_name": "test-crs"})

    build_dir = tmp_path / "build"
    output_dir = executor._get_crs_output_dir(build_dir, "test-bench")

    assert output_dir == build_dir / "out" / "test-crs" / "test-bench"
```

### Integration Tests

```python
def test_run_crs_full_execution(tmp_path):
    """Test full CRS execution with mock oss-crs."""
    # Setup trial directory structure
    trial_dir = tmp_path / "trial-0"
    setup_trial_directory(trial_dir)

    # Create executor
    executor = CRSBugFindingExecutor(...)
    executor.configure_crs({
        "crs_name": "test-crs",
        "build_timeout": 60,
        "run_timeout": 120
    })

    # Mock oss-crs commands
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="Build successful", stderr=""
        )

        # Run CRS
        result = executor.run_crs(
            benchmark_path=tmp_path / "benchmarks" / "test-bench",
            harness=test_harness,
            trial_output_dir=trial_dir
        )

        # Verify result
        assert result.success
        assert result.harness_name == test_harness.name
        assert (trial_dir / "execution.json").exists()

        # Verify commands called
        assert mock_run.call_count == 2  # build + run
```

## Future Enhancements

### 1. Build Caching

Cache Docker images across trials:

```python
def _should_rebuild(self, crs_name: str, benchmark_name: str) -> bool:
    """Check if CRS image needs rebuilding."""
    # Check for existing image
    # Compare timestamps
    # Return False if cache valid
```

### 2. Incremental Execution

Support resuming interrupted executions:

```python
def run_crs_incremental(self, trial_dir: Path) -> CRSResult:
    """Resume CRS execution from checkpoint."""
    # Check execution.json for previous state
    # Skip build if already built
    # Resume bug finding from checkpoint
```

### 3. Resource Monitoring

Monitor resource usage during execution:

```python
def _monitor_resources(self, process: subprocess.Popen) -> Dict[str, Any]:
    """Monitor CPU, memory, disk usage."""
    # Track resource consumption
    # Store in execution metadata
```

## References

- [OSS-CRS Integration](./oss-crs-integration.md): oss-bugfind-crs CLI parameters
- [Trial Directory Preparation](./trial-directory-preparation.md): Directory structure
- [CRS Executors](./crs-executors.md): Executor overview
- [Snapshot Module](./snapshot-validation.md): POV validation (to be implemented)
