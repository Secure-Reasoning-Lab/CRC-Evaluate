# CRS Executors Design

This document provides detailed implementation documentation for concrete CRS executor implementations that integrate with OSS-Fuzz and OSS-Patch interfaces.

## Purpose

Bridge between CRSBench's evaluation module and the standardized OSS-Fuzz/OSS-Patch CRS interfaces. These executors wrap external command-line tools to provide:

- Bug finding CRS execution via OSS-Fuzz interface
- Patch generation CRS execution via OSS-Patch interface
- Result parsing and POV detection
- Docker container lifecycle management

## Architecture Overview

### Class Hierarchy

```
CRSExecutor (abstract)
├── StubCRSExecutor (testing)
├── OSSFuzzBugFindingExecutor (bug finding)
└── OSSPatchExecutor (patch generation)
```

### Integration with Evaluation Module

```
BenchmarkRunner
    ↓ creates and configures
CRSExecutor (concrete implementation)
    ↓ wraps
OSS-Fuzz/OSS-Patch Interface (via infra/helper.py)
    ↓ executes in
Docker Container
    ↓ produces
Output (crashes/patches)
    ↓ parsed into
POVResult objects
```

### Design Philosophy

1. **Separation of Concerns**: Bug finding vs patch generation are separate executors
2. **Command Wrapping**: Thin wrapper around OSS-Fuzz interfaces
3. **Submodule Coordination**: Work with oss-fuzz/ and oss-patch/ submodules
4. **Future-Proof**: Support both current and future command formats

## OSS-Fuzz Bug Finding Executor

### Purpose

Execute bug finding CRS implementations using the OSS-Fuzz interface.

### Class Definition

```python
class OSSFuzzBugFindingExecutor(CRSExecutor):
    """CRS executor for bug finding using OSS-Fuzz interface."""

    def __init__(self, crs_config_name: str, oss_fuzz_path: Path):
        """Initialize executor.

        Args:
            crs_config_name: CRS configuration name (e.g., "ensemble-c")
            oss_fuzz_path: Path to oss-fuzz repository
        """
        self.crs_config_name = crs_config_name
        self.oss_fuzz_path = oss_fuzz_path
        self.config: Dict[str, Any] = {}
        self.built_projects: Set[str] = set()
```

### Build Phase

**Command**: `python3 infra/helper.py build_crs <crs-config-dir> <project-name>`

**Workflow**:
1. Resolve CRS configuration directory from `crses/` or from full path
2. Extract project name from benchmark path or meta.yaml
3. Execute build command in oss-fuzz directory
4. Cache successful builds (avoid rebuilding same CRS+project)
5. Handle build failures gracefully

**Implementation**:
```python
def _build_crs_if_needed(self, project_name: str) -> None:
    """Build CRS Docker image if not already built."""
    build_key = f"{self.crs_config_name}:{project_name}"

    if build_key in self.built_projects:
        logger.info(f"CRS already built for {build_key}")
        return

    crs_config_dir = self._resolve_crs_config_dir()

    cmd = [
        "python3", "infra/helper.py", "build_crs",
        str(crs_config_dir), project_name
    ]

    result = subprocess.run(
        cmd,
        cwd=self.oss_fuzz_path,
        capture_output=True,
        text=True,
        timeout=self.config.get("build_timeout", 600)
    )

    if result.returncode != 0:
        raise EvaluationError(f"CRS build failed: {result.stderr}")

    self.built_projects.add(build_key)
```

### Run Phase

**Command**: `python3 infra/helper.py run_crs <crs-config-dir> <project-name> <harness-name>`

**Workflow**:
1. Build CRS if not already built
2. Extract harness name from HarnessFile
3. Execute run command in oss-fuzz directory
4. Wait for completion (with timeout)
5. Return execution result

**Implementation**:
```python
def run_crs(
    self,
    benchmark_path: Path,
    harness: HarnessFile,
    base_commit: str,
    ref_commit: Optional[str] = None
) -> CRSResult:
    """Run CRS on specific harness."""
    project_name = self._extract_project_name(benchmark_path)

    # Build if needed
    self._build_crs_if_needed(project_name)

    # Extract harness name (without extension)
    harness_name = Path(harness.name).stem

    crs_config_dir = self._resolve_crs_config_dir()

    cmd = [
        "python3", "infra/helper.py", "run_crs",
        str(crs_config_dir), project_name, harness_name
    ]

    start_time = time.time()
    result = subprocess.run(
        cmd,
        cwd=self.oss_fuzz_path,
        capture_output=True,
        text=True,
        timeout=self.config.get("run_timeout", 3600)
    )
    execution_time = time.time() - start_time

    return CRSResult(
        harness_name=harness.name,
        execution_time=execution_time,
        success=(result.returncode == 0),
        output=result.stdout,
        error=result.stderr if result.returncode != 0 else None
    )
```

### Optional Hints Support

CRS implementations can optionally receive hints to improve bug finding effectiveness.

**Configuration**:

```python
def configure_crs(self, config: Dict[str, Any]) -> None:
    """Configure the CRS with hints support."""
    self.config = config.copy()

    # Check if hints are enabled
    self.hints_enabled = config.get("hints_enabled", False)
    self.hints_corpus_level = config.get("hints_corpus_level", "1h")  # "1h" or "1d"
```

**Hints Path Resolution**:

```python
def _get_hints_path(self, benchmark_path: Path, harness_name: str) -> Optional[Path]:
    """Get hints directory path for a specific harness.

    Args:
        benchmark_path: Path to benchmark directory
        harness_name: Name of the harness

    Returns:
        Path to hints directory if it exists, None otherwise
    """
    hints_dir = benchmark_path / ".aixcc" / harness_name / "hints"

    if hints_dir.exists():
        return hints_dir

    return None
```

**Updated run_crs Implementation**:

```python
def run_crs(
    self,
    benchmark_path: Path,
    harness: HarnessFile,
    base_commit: str,
    ref_commit: Optional[str] = None
) -> CRSResult:
    """Run CRS on specific harness with optional hints."""
    project_name = self._extract_project_name(benchmark_path)

    # Build if needed
    self._build_crs_if_needed(project_name)

    # Extract harness name (without extension)
    harness_name = Path(harness.name).stem

    crs_config_dir = self._resolve_crs_config_dir()

    cmd = [
        "python3", "infra/helper.py", "run_crs",
        str(crs_config_dir), project_name, harness_name
    ]

    # Add hints if enabled
    if self.hints_enabled:
        hints_path = self._get_hints_path(benchmark_path, harness_name)
        if hints_path:
            cmd.extend(["--hints", str(hints_path)])
            logger.info(f"Using hints from {hints_path}")
        else:
            logger.warning(f"Hints enabled but not found for {harness_name}")

    start_time = time.time()
    result = subprocess.run(
        cmd,
        cwd=self.oss_fuzz_path,
        capture_output=True,
        text=True,
        timeout=self.config.get("run_timeout", 3600)
    )
    execution_time = time.time() - start_time

    return CRSResult(
        harness_name=harness.name,
        execution_time=execution_time,
        success=(result.returncode == 0),
        output=result.stdout,
        error=result.stderr if result.returncode != 0 else None
    )
```

**Hints Directory Structure**:

The hints directory is expected to follow this structure:
```
benchmarks/<project>/.aixcc/<harness>/hints/
├── sarif/                    # Static analysis reports
│   ├── codeql.sarif
│   ├── semgrep.sarif
│   └── ...
└── corpus/                   # Pre-fuzzing corpus
    ├── 1h/                  # 1 hour fuzzing corpus
    │   ├── input-001
    │   └── ...
    └── 1d/                  # 1 day fuzzing corpus
        ├── input-001
        └── ...
```

**Docker Volume Mapping**:

When `--hints` is provided to the OSS-Fuzz interface:
- Host: `<benchmark-path>/.aixcc/<harness>/hints/`
- Container: `/hints/`

Inside the container, CRS can access:
- `/hints/sarif/*.sarif` - Static analysis reports
- `/hints/corpus/1h/*` - Corpus from 1 hour of fuzzing
- `/hints/corpus/1d/*` - Corpus from 1 day of fuzzing

**Configuration Example**:

```yaml
# In experiment config
crses:
  ensemble-c:
    hints_enabled: true
    hints_corpus_level: "1h"  # Use 1-hour corpus (easier)
```

**Usage Notes**:

- Hints are optional - CRS execution works without hints
- Hints path is only passed if it exists on the filesystem
- CRS implementation decides how to use hints (SARIF, corpus, or both)
- Corpus level selection allows experimenting with different difficulty levels

### Output Directory Structure

OSS-Fuzz produces output in:
```
oss-fuzz/build/out/<crs-name>/<project-name>/<harness-name>/
├── crashes/
│   ├── crash-001
│   ├── crash-002
│   └── ...
└── corpus/
    ├── input-001
    ├── input-002
    └── ...
```

### POV Detection Logic

**Process**:
1. Scan crashes directory for crash files
2. For each crash, replay with sanitizers
3. Parse sanitizer output for error patterns
4. Match error patterns to known POVs
5. Map to POVResult status

**Implementation**:
```python
def process_pov_results(
    self,
    crs_result: CRSResult,
    harness: HarnessFile
) -> List[POVResult]:
    """Process crash output to determine POV detection."""
    pov_results = []

    if not crs_result.success or not harness.povs:
        # Mark all POVs as ERROR or return empty
        return self._create_error_results(crs_result, harness)

    # Get crashes directory
    crashes_dir = self._get_crashes_dir(harness.name)

    # Detect which POVs were found
    detected_povs = self._analyze_crashes(crashes_dir, harness.povs)

    # Create POVResult for each POV
    for pov in harness.povs:
        status = POVStatus.FOUND if pov.name in detected_povs else POVStatus.MISSED

        pov_results.append(POVResult(
            name=pov.name,
            harness_name=harness.name,
            sanitizer=pov.sanitizer,
            error_token=pov.error_token,
            status=status,
            execution_time=crs_result.execution_time / len(harness.povs),
            crs_output=crs_result.output
        ))

    return pov_results

def _analyze_crashes(
    self,
    crashes_dir: Path,
    povs: List[POV]
) -> Set[str]:
    """Analyze crash files to determine which POVs were found."""
    detected = set()

    if not crashes_dir.exists():
        return detected

    for crash_file in crashes_dir.iterdir():
        # Replay crash with sanitizer
        crash_log = self._replay_crash_with_sanitizer(crash_file)

        # Match against known POVs
        for pov in povs:
            if self._matches_pov(crash_log, pov):
                detected.add(pov.name)

    return detected

def _matches_pov(self, crash_log: str, pov: POV) -> bool:
    """Check if crash log matches POV signature."""
    # Check sanitizer type
    sanitizer_patterns = {
        "address": "AddressSanitizer",
        "memory": "MemorySanitizer",
        "undefined": "UndefinedBehaviorSanitizer",
        "thread": "ThreadSanitizer",
        "leak": "LeakSanitizer"
    }

    expected_pattern = sanitizer_patterns.get(pov.sanitizer)
    if expected_pattern and expected_pattern not in crash_log:
        return False

    # Check error token if provided
    if pov.error_token and pov.error_token not in crash_log:
        return False

    return True
```

## OSS-Patch Executor

### Purpose

Execute patch generation CRS implementations using the OSS-Patch interface.

### Class Definition

```python
class OSSPatchExecutor(CRSExecutor):
    """CRS executor for patch generation using OSS-Patch interface."""

    def __init__(
        self,
        crs_config_name: str,
        oss_patch_path: Path,
        oss_fuzz_path: Path,
        litellm_base: str,
        litellm_key: str
    ):
        """Initialize executor.

        Args:
            crs_config_name: CRS configuration name
            oss_patch_path: Path to oss-patch repository
            oss_fuzz_path: Path to oss-fuzz repository (required)
            litellm_base: LiteLLM API base URL
            litellm_key: LiteLLM API key
        """
        self.crs_config_name = crs_config_name
        self.oss_patch_path = oss_patch_path
        self.oss_fuzz_path = oss_fuzz_path
        self.litellm_base = litellm_base
        self.litellm_key = litellm_key
        self.config: Dict[str, Any] = {}
        self.built_projects: Set[str] = set()
```

### Build Phase

**Command**: `python3 infra/helper.py build_crs <config> <project> --oss-fuzz $OSS_FUZZ_HOME`

**Workflow**:
1. Set OSS_FUZZ_HOME environment variable
2. Resolve CRS configuration
3. Execute build command in oss-patch directory
4. Cache successful builds

**Implementation**:
```python
def _build_crs_if_needed(self, project_name: str) -> None:
    """Build patch generation CRS if not already built."""
    build_key = f"{self.crs_config_name}:{project_name}"

    if build_key in self.built_projects:
        return

    cmd = [
        "python3", "infra/helper.py", "build_crs",
        self.crs_config_name, project_name,
        "--oss-fuzz", str(self.oss_fuzz_path)
    ]

    env = os.environ.copy()
    env["OSS_FUZZ_HOME"] = str(self.oss_fuzz_path)

    result = subprocess.run(
        cmd,
        cwd=self.oss_patch_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=self.config.get("build_timeout", 600)
    )

    if result.returncode != 0:
        raise EvaluationError(f"Patch CRS build failed: {result.stderr}")

    self.built_projects.add(build_key)
```

### Run Phase

**Command**:
```bash
python3 infra/helper.py run_crs <config> <project> \
  --harness <harness-name> \
  [--pov <pov-name>] \
  --litellm-base <url> \
  --litellm-key <key>
```

**Workflow**:
1. Build CRS if not already built
2. Extract harness and optional POV name
3. Pass LiteLLM configuration
4. Execute run command
5. Return execution result

**Implementation**:
```python
def run_crs(
    self,
    benchmark_path: Path,
    harness: HarnessFile,
    base_commit: str,
    ref_commit: Optional[str] = None
) -> CRSResult:
    """Run patch generation CRS."""
    project_name = self._extract_project_name(benchmark_path)

    # Build if needed
    self._build_crs_if_needed(project_name)

    harness_name = Path(harness.name).stem

    # Build command
    cmd = [
        "python3", "infra/helper.py", "run_crs",
        self.crs_config_name, project_name,
        "--harness", harness_name,
        "--litellm-base", self.litellm_base,
        "--litellm-key", self.litellm_key
    ]

    # Add POV if specified in config
    if "target_pov" in self.config:
        cmd.extend(["--pov", self.config["target_pov"]])

    start_time = time.time()
    result = subprocess.run(
        cmd,
        cwd=self.oss_patch_path,
        capture_output=True,
        text=True,
        timeout=self.config.get("run_timeout", 3600)
    )
    execution_time = time.time() - start_time

    return CRSResult(
        harness_name=harness.name,
        execution_time=execution_time,
        success=(result.returncode == 0),
        output=result.stdout,
        error=result.stderr if result.returncode != 0 else None
    )
```

### Patch Collection

**Process**:
1. Locate output directory for patches
2. Collect generated patch files
3. Validate patch format
4. Store patch content in results

**Implementation**:
```python
def _collect_patches(self, project_name: str, harness_name: str) -> List[str]:
    """Collect generated patches from output directory."""
    patches_dir = self.oss_patch_path / "build" / "out" / self.crs_config_name / project_name / harness_name / "patches"

    patches = []
    if patches_dir.exists():
        for patch_file in patches_dir.glob("*.patch"):
            patches.append(patch_file.read_text())

    return patches
```

### POV Processing for Patch Generation

For patch generation, POV processing validates patches:

```python
def process_pov_results(
    self,
    crs_result: CRSResult,
    harness: HarnessFile
) -> List[POVResult]:
    """Process patch generation results."""
    pov_results = []

    if not crs_result.success or not harness.povs:
        return self._create_error_results(crs_result, harness)

    # Collect generated patches
    patches = self._collect_patches(
        self._extract_project_name(...),
        harness.name
    )

    # For each POV, check if patch fixes it
    for pov in harness.povs:
        fixed = self._validate_patch_fixes_pov(patches, pov)
        status = POVStatus.FOUND if fixed else POVStatus.MISSED

        pov_results.append(POVResult(
            name=pov.name,
            harness_name=harness.name,
            sanitizer=pov.sanitizer,
            error_token=pov.error_token,
            status=status,
            execution_time=crs_result.execution_time / len(harness.povs),
            crs_output=crs_result.output
        ))

    return pov_results
```

## Configuration Management

### CRS Configuration Directory Structure

```
crses/
├── ensemble-c/
│   ├── pkg.yaml           # Package dependencies
│   ├── config-crs.yaml    # CRS runtime config
│   └── ...                # CRS-specific files
├── multi-retrieval/
│   ├── pkg.yaml
│   ├── config-crs.yaml
│   └── ...
└── ...
```

### Configuration Files

**pkg.yaml**: Package and dependency information
```yaml
name: ensemble-c
version: "1.0"
dependencies:
  - python-packages:
      - litellm>=1.77.5
```

**config-crs.yaml**: CRS-specific runtime parameters
```yaml
max_iterations: 10
temperature: 0.7
model: "anthropic/claude-3-sonnet"
```

### Configuration Loading

```python
def _resolve_crs_config_dir(self) -> Path:
    """Resolve CRS configuration directory.

    Returns:
        Path to CRS config directory (absolute)
    """
    # Check if full path provided
    config_path = Path(self.crs_config_name)
    if config_path.is_absolute() and config_path.exists():
        return config_path

    # Look in crses/ directory
    crses_dir = Path(__file__).parent.parent.parent / "crses"
    config_dir = crses_dir / self.crs_config_name

    if not config_dir.exists():
        raise EvaluationError(f"CRS config not found: {self.crs_config_name}")

    return config_dir.absolute()
```

## Path and Project Mapping

### Benchmark to Project Name

**Strategy 1**: Extract from benchmark directory name
```python
def _extract_project_name(self, benchmark_path: Path) -> str:
    """Extract OSS-Fuzz project name from benchmark path.

    Example: benchmarks/json-c → json-c
    """
    return benchmark_path.name
```

**Strategy 2**: Read from meta.yaml (future)
```yaml
# In meta.yaml
project_name: "json-c"
```

### Docker Volume Mounts

OSS-Fuzz automatically mounts:
- Host: `oss-fuzz/build/out/<crs>/<project>/`
- Container: `/out/`

Benchmark code is available in container at project-specific paths.

### File System Layout

```
/
├── out/                         # Container output
│   └── <harness-name>/
│       ├── crashes/
│       └── corpus/
├── src/                         # Container source code
│   └── <project>/
│       └── ...
└── work/                        # Container workspace
```

## Docker Integration

### Build Caching

**Strategy**:
- Cache successful builds in `self.built_projects` set
- Key: `{crs_config_name}:{project_name}`
- Avoid rebuilding same CRS+project combination
- Clear cache on configuration changes

### Container Lifecycle

1. **Build Phase**: Creates Docker image with CRS and project
2. **Run Phase**: Spawns container, executes CRS, collects output
3. **Cleanup**: Container automatically removed after execution

### Error Handling

**Build Errors**:
- Raise `EvaluationError` immediately
- Include stderr output in error message
- Do not attempt evaluation

**Run Errors**:
- Mark harness as failed
- Capture stderr for debugging
- Continue with remaining harnesses

**Timeout Handling**:
- Configurable via `build_timeout` and `run_timeout`
- Default: 600s for build, 3600s for run
- Kill container on timeout
- Mark as ERROR in results

## POV Detection Logic

### Crash Analysis Strategy

1. **Collect Crashes**: Scan crashes directory
2. **Replay with Sanitizer**: Re-execute crashes with appropriate sanitizer
3. **Parse Output**: Extract sanitizer error type and message
4. **Match POVs**: Compare against known POV signatures

### Sanitizer Output Patterns

**AddressSanitizer (ASAN)**:
```
==12345==ERROR: AddressSanitizer: heap-buffer-overflow on address 0x...
```

**MemorySanitizer (MSAN)**:
```
==12345==WARNING: MemorySanitizer: use-of-uninitialized-value
```

**UndefinedBehaviorSanitizer (UBSAN)**:
```
runtime error: signed integer overflow
```

### Error Token Matching

**Exact Match**: If POV has `error_token`, must appear in crash log
```python
if pov.error_token and pov.error_token not in crash_log:
    return False  # Not a match
```

**Fuzzy Match** (future): Stack trace similarity, function name matching

### Deduplication

**Challenge**: Multiple crashes may trigger same POV

**Strategy**:
- Mark POV as FOUND if any crash matches
- Future: Advanced deduplication using stack trace hashing

## Command Migration Strategy

### Current Format

```bash
# Bug finding
python3 infra/helper.py build_crs <config-dir> <project>
python3 infra/helper.py run_crs <config-dir> <project> <harness>

# Patch generation
python3 infra/helper.py build_crs <config> <project> --oss-fuzz $OSS_FUZZ_HOME
python3 infra/helper.py run_crs <config> <project> --harness <name> --litellm-base <url> --litellm-key <key>
```

### Future Format

```bash
# Bug finding
oss-fuzz-crs build <config-dir> <project>
oss-fuzz-crs run <config-dir> <project> <harness>

# Patch generation
oss-patch-crs build <config> <project>
oss-patch-crs run <config> <project> <harness> [<pov>]
```

### Feature Detection

**Implementation**:
```python
def _get_command_prefix(self, command_type: str) -> List[str]:
    """Get command prefix based on available commands.

    Args:
        command_type: "bug_finding" or "patch_generation"

    Returns:
        Command prefix (either new command or python3 infra/helper.py)
    """
    if command_type == "bug_finding":
        if shutil.which("oss-fuzz-crs"):
            return ["oss-fuzz-crs"]
        else:
            return ["python3", "infra/helper.py"]

    elif command_type == "patch_generation":
        if shutil.which("oss-patch-crs"):
            return ["oss-patch-crs"]
        else:
            return ["python3", "infra/helper.py"]
```

### Compatibility Layer

Maintain backward compatibility during transition:
- Try new command first
- Fall back to old format if not found
- Log which format is being used
- No code changes needed when commands become available

## Implementation Checklist

### Phase 1: OSSFuzzBugFindingExecutor

- [ ] Create `crsbench/evaluation/oss_fuzz_executor.py`
- [ ] Implement `OSSFuzzBugFindingExecutor` class
  - [ ] `__init__` with crs_config_name and oss_fuzz_path
  - [ ] `configure_crs()` method
  - [ ] `_build_crs_if_needed()` helper
  - [ ] `run_crs()` method
  - [ ] `process_pov_results()` method
  - [ ] `_analyze_crashes()` helper
  - [ ] `_matches_pov()` helper
- [ ] Add command execution with subprocess
- [ ] Implement build caching
- [ ] Add timeout handling
- [ ] Add error handling

### Phase 2: OSSPatchExecutor

- [ ] Create `crsbench/evaluation/oss_patch_executor.py` (or add to same file)
- [ ] Implement `OSSPatchExecutor` class
  - [ ] `__init__` with paths and LiteLLM config
  - [ ] `configure_crs()` method
  - [ ] `_build_crs_if_needed()` with OSS_FUZZ_HOME
  - [ ] `run_crs()` with LiteLLM parameters
  - [ ] `process_pov_results()` method
  - [ ] `_collect_patches()` helper
  - [ ] `_validate_patch_fixes_pov()` helper

### Phase 3: Helper Methods

- [ ] `_resolve_crs_config_dir()` - Find CRS config
- [ ] `_extract_project_name()` - Benchmark to project name
- [ ] `_get_crashes_dir()` - Locate crash output
- [ ] `_replay_crash_with_sanitizer()` - Re-execute crash
- [ ] `_get_command_prefix()` - Future command detection
- [ ] `_get_hints_path()` - Locate hints directory for harness

### Phase 4: Integration

- [ ] Update `crsbench/evaluation/__init__.py` exports
- [ ] Add example usage in docstrings
- [ ] Update BenchmarkRunner to accept new executors

### Phase 5: Testing

- [ ] Create `tests/test_oss_fuzz_executor.py`
- [ ] Create `tests/test_oss_patch_executor.py`
- [ ] Unit tests for each method
- [ ] Integration tests with mock OSS-Fuzz
- [ ] Test build caching
- [ ] Test error handling
- [ ] Test timeout behavior

### Phase 6: Documentation

- [ ] Add usage examples to module docstrings
- [ ] Update README.md with executor information
- [ ] Document CRS configuration format

## Testing Strategy

### Unit Tests

**Test Command Building**:
```python
def test_build_command_construction():
    executor = OSSFuzzBugFindingExecutor("ensemble-c", Path("/path/to/oss-fuzz"))
    # Mock subprocess to capture command
    # Assert correct command structure
```

**Test POV Matching**:
```python
def test_matches_pov_with_error_token():
    crash_log = "AddressSanitizer: heap-buffer-overflow"
    pov = POV(id="pov_0", sanitizer="address", error_token="heap-buffer-overflow")
    assert executor._matches_pov(crash_log, pov) == True
```

**Test Configuration Resolution**:
```python
def test_resolve_crs_config_dir():
    executor = OSSFuzzBugFindingExecutor("ensemble-c", Path("/oss-fuzz"))
    config_dir = executor._resolve_crs_config_dir()
    assert config_dir.exists()
    assert config_dir.name == "ensemble-c"
```

### Integration Tests

**Test with Mock OSS-Fuzz**:
```python
def test_run_crs_with_mock_interface():
    # Create mock oss-fuzz directory structure
    # Mock infra/helper.py script
    # Execute CRS
    # Verify output parsing
```

**Test Build Caching**:
```python
def test_build_cache_avoids_rebuild():
    executor = OSSFuzzBugFindingExecutor("ensemble-c", oss_fuzz_path)

    # First build
    executor._build_crs_if_needed("json-c")
    # Mock subprocess to track calls

    # Second build - should skip
    executor._build_crs_if_needed("json-c")
    # Assert subprocess not called again
```

### Test Fixtures

**Mock Crash Directory**:
```
tests/fixtures/crashes/
├── crash-heap-overflow
├── crash-use-after-free
└── crash-null-deref
```

**Mock CRS Configs**:
```
tests/fixtures/crses/
└── test-crs/
    ├── pkg.yaml
    └── config-crs.yaml
```

## Design Decisions

### Why Separate Executors for Bug Finding vs Patch Generation?

**Decision**: Create `OSSFuzzBugFindingExecutor` and `OSSPatchExecutor` as separate classes.

**Rationale**:
- Different interfaces (different arguments, different repos)
- Different output formats (crashes vs patches)
- Different validation logic (crash analysis vs patch testing)
- Clear separation of concerns
- Easier to maintain and test

**Alternative Considered**: Single executor with mode parameter
**Rejected Because**: Too much conditional logic, unclear responsibilities

### Why Wrap Commands Instead of Direct Docker?

**Decision**: Wrap `infra/helper.py` instead of directly managing Docker.

**Rationale**:
- OSS-Fuzz handles Docker complexity
- Consistent with OSS-Fuzz ecosystem
- Future command wrappers will be available
- Reduces maintenance burden
- Leverages OSS-Fuzz's build system

### Why Cache Builds?

**Decision**: Cache successful builds in memory.

**Rationale**:
- Multiple harnesses share same CRS+project build
- Rebuilding is expensive (minutes)
- Evaluation experiments run many trials
- Significant time savings

**Trade-off**: Memory usage (minimal - just set of strings)

### Why Not Persist Build Cache?

**Decision**: Don't save cache to disk between runs.

**Rationale**:
- Docker images persist anyway
- Build system handles layer caching
- Configuration changes require rebuild
- Simpler implementation

### Error Handling Philosophy

**Decision**: Fail fast on build errors, gracefully handle run errors.

**Rationale**:
- Build failure = configuration problem, no point continuing
- Run failure = individual harness issue, others may succeed
- Want to collect maximum results even with failures
- Matches evaluation module's graceful degradation

## Performance Considerations

### Build Time

- First build: 2-10 minutes (Docker image creation)
- Cached build: <1 second (memory check)
- Docker layer caching: Significant speedup on rebuild

### Run Time

- Depends on CRS implementation
- Typically: 10-60 minutes per harness
- Timeout prevents infinite runs

### Parallelization

- Current: Serial execution (one harness at a time)
- Future: Parallel harness execution possible
- Docker allows concurrent containers

## Common Pitfalls

### 1. Incorrect Path Resolution

**Wrong**:
```python
crs_config_dir = Path(self.crs_config_name)  # Might be relative
```

**Right**:
```python
crs_config_dir = self._resolve_crs_config_dir()  # Always absolute
```

### 2. Forgetting OSS_FUZZ_HOME for Patch Generation

**Wrong**:
```python
subprocess.run(cmd, cwd=self.oss_patch_path)
```

**Right**:
```python
env = os.environ.copy()
env["OSS_FUZZ_HOME"] = str(self.oss_fuzz_path)
subprocess.run(cmd, cwd=self.oss_patch_path, env=env)
```

### 3. Not Handling Timeouts

**Wrong**:
```python
subprocess.run(cmd)  # Can hang forever
```

**Right**:
```python
subprocess.run(cmd, timeout=self.config.get("run_timeout", 3600))
```

### 4. Assuming Crashes Directory Exists

**Wrong**:
```python
for crash in crashes_dir.iterdir():  # Crashes if directory missing
    ...
```

**Right**:
```python
if crashes_dir.exists():
    for crash in crashes_dir.iterdir():
        ...
```

## References

- [OSS-Fuzz CRS Interface](../../docs/ossfuzz-crs-interface.md): Interface specification
- [Evaluation Module Design](./evaluation.md): Integration with evaluation
- [Architecture](../architecture.md): Overall CRSBench architecture
- [OSS-Fuzz Documentation](https://google.github.io/oss-fuzz/): OSS-Fuzz details
