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
├── CRSBugFindingExecutor (bug finding)
└── CRSPatchExecutor (patch generation)
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

## CRSBench Integration with oss-crs CLI

**Important**: CRSBench uses the `oss-crs` and `oss-patch-crs` command-line interfaces with specific parameter management for trial isolation and source code control.

### Key Parameters

CRSBench provides these parameters to oss-crs commands:

1. `--build-dir`: Unique per trial for isolation
2. `--oss-fuzz-dir`: Points to oss-fuzz submodule (shared across trials)
3. `--registry-dir`: Points to `oss-crs-registry/` (the ONLY registry for both testing and production)
4. `--project-path`: Benchmark directory from `benchmarks/`
5. **Source path** (positional arg): Pre-cloned by CRSBench at commit from meta.yaml

**Note**: CRS configurations can come from either `oss-crs-registry/crs/<crs-name>/` (registry) or `crses/<crs-name>/` (local configs following `example_configs/` format). The `--registry-dir` parameter always points to `oss-crs-registry/`.

**See [OSS-CRS Integration](./oss-crs-integration.md) for complete details on parameter mappings, trial isolation strategy, and source code management.**

## OSS-Fuzz Bug Finding Executor

### Purpose

Execute bug finding CRS implementations using the OSS-Fuzz interface.

### Class Definition

```python
class CRSBugFindingExecutor(CRSExecutor):
    """CRS executor for bug finding using OSS-Fuzz interface."""

    def __init__(
        self,
        crs_config_name: str,
        oss_fuzz_path: Path,
        registry_dir: Path,
        benchmarks_root: Path
    ):
        """Initialize executor.

        Args:
            crs_config_name: CRS configuration name (e.g., "ensemble-c")
            oss_fuzz_path: Path to oss-fuzz repository
            registry_dir: Path to CRS registry directory (e.g., crses/ or oss-crs-registry/)
            benchmarks_root: Path to benchmarks directory (for repo manager)
        """
        self.crs_config_name = crs_config_name
        self.oss_fuzz_path = oss_fuzz_path
        self.registry_dir = registry_dir
        self.benchmarks_root = benchmarks_root
        self.config: Dict[str, Any] = {}
        self.built_projects: Set[str] = set()
```

### Build Phase

**Command**:
```bash
oss-crs build \
  --build-dir <build-dir> \
  --oss-fuzz-dir <oss-fuzz-dir> \
  --registry-dir <registry-dir> \
  --project-path <project-path> \
  <crs-config-dir> \
  <project-name> \
  <source-path>
```

**Workflow**:
1. Resolve CRS configuration directory (from `crses/<crs-name>/` or full path)
2. Extract project name from benchmark path or meta.yaml
3. Prepare trial-specific build directory
4. Execute build command with all required parameters
5. Cache successful builds (avoid rebuilding same CRS+project)
6. Handle build failures gracefully

**Note**: CRS config is resolved from `crses/<crs-name>/` directory, NOT from `oss-crs-registry/`. The `crses/` directory follows the same format as `oss-crs/example_configs/`.

**Implementation**:
```python
def _build_crs_if_needed(self, benchmark_path: Path, project_name: str, trial_build_dir: Path) -> None:
    """Build CRS Docker image if not already built."""
    build_key = f"{self.crs_config_name}:{project_name}"

    if build_key in self.built_projects:
        logger.info(f"CRS already built for {build_key}")
        return

    crs_config_dir = self._resolve_crs_config_dir()

    # Ensure source code exists
    from crsbench.migration.repo_manager import ensure_project_repository
    source_path = ensure_project_repository(
        benchmark_dir=str(benchmark_path),
        verbose=self.config.get("verbose", False)
    )

    cmd = [
        "oss-crs", "build",
        "--build-dir", str(trial_build_dir),
        "--oss-fuzz-dir", str(self.oss_fuzz_path),
        "--registry-dir", str(self.registry_dir),
        "--project-path", str(benchmark_path),
        str(crs_config_dir), project_name, str(source_path)
    ]

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=self.config.get("build_timeout", 600)
    )

    if result.returncode != 0:
        raise EvaluationError(f"CRS build failed: {result.stderr}")

    self.built_projects.add(build_key)
```

### Run Phase

**Command**:
```bash
oss-crs run \
  --build-dir <build-dir> \
  --oss-fuzz-dir <oss-fuzz-dir> \
  --registry-dir <registry-dir> \
  <crs-config-dir> \
  <project-name> \
  <harness-name> \
  [--hints <hints-dir>]
```

**Output Directory**: CRS outputs to `{{ build_dir }}/out/{{ crs.name }}/{{ project }}/` (auto-determined from build_dir, CRS name, and project name)

**Note**: No `--output` parameter for oss-crs run command. Output location is derived automatically from build_dir. Future versions may support explicit `--output` parameter.

**Workflow**:
1. Build CRS if not already built
2. Prepare hints directory (if enabled) - copy and filter from benchmark
3. Extract harness name from HarnessFile
4. Execute run command in oss-fuzz directory with optional `--hints` parameter
5. Determine output directory using helper function (derived from build_dir, CRS name, project)
6. Wait for completion (with timeout)
7. Collect outputs from derived directory
8. Return execution result

**Implementation**:
```python
def run_crs(
    self,
    benchmark_path: Path,
    harness: HarnessFile,
    trial_output_dir: Path
) -> CRSResult:
    """Run CRS on specific harness.

    Args:
        benchmark_path: Path to benchmark directory
        harness: Harness configuration
        trial_output_dir: Directory for this trial's outputs

    Returns:
        CRSResult with execution details

    Note:
        Source code is already cloned at the correct commit by
        TrialDirectoryPreparer. The executor does not need commit
        information.
    """
    project_name = self._extract_project_name(benchmark_path)

    # Prepare trial-specific build directory
    trial_build_dir = trial_output_dir / "build"
    trial_build_dir.mkdir(parents=True, exist_ok=True)

    # Build if needed
    self._build_crs_if_needed(benchmark_path, project_name, trial_build_dir)

    # Extract harness name (without extension)
    harness_name = Path(harness.name).stem

    crs_config_dir = self._resolve_crs_config_dir()

    # Build command (no --output parameter for oss-crs)
    cmd = [
        "oss-crs", "run",
        "--build-dir", str(trial_build_dir),
        "--oss-fuzz-dir", str(self.oss_fuzz_path),
        "--registry-dir", str(self.registry_dir),
        str(crs_config_dir), project_name, harness_name
    ]

    start_time = time.time()
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=self.config.get("run_timeout", 3600)
    )
    execution_time = time.time() - start_time

    # Get output directory (derived from build_dir, CRS name, project)
    output_dir = self._get_crs_output_directory(trial_build_dir, project_name)

    # Collect outputs from derived directory
    # (POVs, corpus, etc. are in output_dir/povs/, output_dir/corpus/, etc.)

    return CRSResult(
        harness_name=harness.name,
        execution_time=execution_time,
        success=(result.returncode == 0),
        output=result.stdout,
        error=result.stderr if result.returncode != 0 else None
    )

def _get_crs_output_directory(self, trial_build_dir: Path, project_name: str) -> Path:
    """Get CRS output directory.

    Current implementation: Returns {{ build_dir }}/out/{{ crs.name }}/{{ project }}/
    Future: May support explicit --output parameter if oss-crs adds it.

    Args:
        trial_build_dir: Trial-specific build directory (passed via --build-dir)
        project_name: OSS-Fuzz project name

    Returns:
        Path to CRS output directory
    """
    # TODO: When oss-crs supports --output parameter, check if it's provided
    # and use that instead of the derived path
    return trial_build_dir / "out" / self.crs_config_name / project_name
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

**Hints Preparation** (see `_prepare_hints()` method above for full implementation)

**Updated run_crs Implementation with Hints**:

```python
def run_crs(
    self,
    benchmark_path: Path,
    harness: HarnessFile,
    trial_output_dir: Path
) -> CRSResult:
    """Run CRS on specific harness with optional hints."""
    project_name = self._extract_project_name(benchmark_path)

    # Prepare trial-specific build directory
    trial_build_dir = trial_output_dir / "build"
    trial_build_dir.mkdir(parents=True, exist_ok=True)

    # Build if needed
    self._build_crs_if_needed(benchmark_path, project_name, trial_build_dir)

    # Extract harness name (without extension)
    harness_name = Path(harness.name).stem

    crs_config_dir = self._resolve_crs_config_dir()

    # Build command (no --output parameter)
    cmd = [
        "oss-crs", "run",
        "--build-dir", str(trial_build_dir),
        "--oss-fuzz-dir", str(self.oss_fuzz_path),
        "--registry-dir", str(self.registry_dir),
        str(crs_config_dir), project_name, harness_name
    ]

    # Prepare and add hints if enabled
    hints_path = self._prepare_hints(benchmark_path, harness_name, trial_output_dir)
    if hints_path:
        cmd.extend(["--hints", str(hints_path)])
        logger.info(f"Using prepared hints from {hints_path}")

    start_time = time.time()
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=self.config.get("run_timeout", 3600)
    )
    execution_time = time.time() - start_time

    # Get output directory (derived from build_dir, CRS name, project)
    output_dir = self._get_crs_output_directory(trial_build_dir, project_name)

    # Store execution metadata for reproducibility
    self._store_execution_metadata(
        trial_output_dir=trial_output_dir,
        cmd=cmd,
        hints_path=hints_path,
        povs_path=None,  # Bug finding doesn't use POVs input
        execution_time=execution_time,
        returncode=result.returncode
    )

    # Collect outputs from derived directory
    # (POVs, corpus, etc. are in output_dir/povs/, output_dir/corpus/, etc.)

    return CRSResult(
        harness_name=harness.name,
        execution_time=execution_time,
        success=(result.returncode == 0),
        output=result.stdout,
        error=result.stderr if result.returncode != 0 else None
    )
```

**Hints Directory Structure**:

**Source (in benchmark):**
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

**Prepared (in trial directory):**
```
trial_output_dir/hints/       # Created by _prepare_hints()
├── sarif/                    # Copied from benchmark
│   ├── codeql.sarif
│   └── semgrep.sarif
└── corpus/                   # Filtered based on config (1h or 1d)
    ├── input-001
    ├── input-002
    └── ...
```

**Docker Volume Mapping**:

When `--hints` is provided to the OSS-Fuzz interface:
- Host: `trial_output_dir/hints/` (prepared by CRSBench)
- Container: `/hints/`

Inside the container, CRS can access:
- `/hints/sarif/*.sarif` - Static analysis reports
- `/hints/corpus/*` - Filtered corpus (1h or 1d based on config)

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

**Bug Finding CRS Output Location**:

Current: CRS outputs to `{{ trial_build_dir }}/out/{{ crs.name }}/{{ project }}/`
- Auto-determined from trial-specific build_dir, CRS name, and project name
- Trial build directory is `{{ trial_output_dir }}/build/`
- No --output parameter needed for oss-crs commands
- Use `_get_crs_output_directory(trial_build_dir, project_name)` helper to get the path

**Host directory structure for Bug Finding**:
```
{{ trial_output_dir }}/build/out/{{ crs_config_name }}/{{ project_name }}/
├── povs/                            # CRS writes POVs here (bug finding)
│   ├── pov_001                      # Binary blob
│   ├── pov_002
│   └── pov_003
├── corpus/                          # CRS writes corpus here (optional)
│   ├── input-001
│   ├── input-002
│   └── input-003
└── crs-data/                        # CRS-specific outputs (optional)
    ├── intermediate-results.json
    └── debug-trace.log

trial_output_dir/                    # Provided by BenchmarkRunner (for metadata)
├── hints/                           # Prepared by _prepare_hints(), mounted to /hints/
│   ├── sarif/                       # Filtered SARIF files
│   │   ├── codeql.sarif
│   │   └── semgrep.sarif
│   └── corpus/                      # Filtered corpus (1h or 1d)
│       ├── input-001
│       └── input-002
├── povs/                            # Prepared by _prepare_povs() (patch gen only)
│   ├── pov_0                        # Flattened POV blobs
│   ├── pov_1
│   └── pov_2
├── config.yaml                      # Experiment config (from orchestrator)
├── execution.json                   # Execution metadata (from executor)
├── llm-usage.json                   # CRSBench records LLM metrics
└── crs-output.log                   # CRSBench captures stdout/stderr
```

**Container directory structure**:
```
/out/                                # Mapped from host trial_output_dir/output/
├── povs/                            # CRS writes discovered POVs
│   ├── pov_0
│   ├── pov_1
│   └── pov_2
├── corpus/                          # CRS writes generated corpus
│   ├── input-001
│   ├── input-002
│   └── input-003
└── crs-data/                        # CRS writes custom data
    └── ...
```

**Directory preparation**:

```python
def _prepare_output_directory(self, trial_output_dir: Path) -> None:
    """Prepare output directory before CRS execution.

    Args:
        trial_output_dir: Trial-specific output directory

    Note:
        Only creates the base output directory. CRS is responsible for
        creating subdirectories (povs/, patches/, corpus/, crs-data/)
        according to the naming convention agreement.
    """
    output_dir = trial_output_dir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

def _prepare_hints(
    self,
    benchmark_path: Path,
    harness_name: str,
    trial_output_dir: Path
) -> Optional[Path]:
    """Prepare hints directory with filtered content from benchmark.

    Creates trial-specific hints directory and copies selected content
    based on experiment configuration.

    Args:
        benchmark_path: Path to benchmark directory
        harness_name: Name of the harness
        trial_output_dir: Trial-specific output directory

    Returns:
        Path to prepared hints directory, or None if hints not available

    Process:
        1. Create trial_output_dir/hints/ directory
        2. Copy SARIF files from benchmark .aixcc/<harness>/hints/sarif/
        3. Copy corpus based on config (1h or 1d) from .aixcc/<harness>/hints/corpus/{1h,1d}/
        4. Filter based on experiment configuration

    Example structure created:
        trial_output_dir/hints/
        ├── sarif/
        │   ├── codeql.sarif
        │   └── semgrep.sarif
        └── corpus/
            ├── input-001
            └── input-002
    """
    if not self.hints_enabled:
        return None

    # Source hints from benchmark
    source_hints = benchmark_path / ".aixcc" / harness_name / "hints"
    if not source_hints.exists():
        return None

    # Create trial-specific hints directory
    hints_dir = trial_output_dir / "hints"
    hints_dir.mkdir(parents=True, exist_ok=True)

    # Copy SARIF files
    source_sarif = source_hints / "sarif"
    if source_sarif.exists():
        dest_sarif = hints_dir / "sarif"
        dest_sarif.mkdir(exist_ok=True)
        for sarif_file in source_sarif.glob("*.sarif"):
            shutil.copy2(sarif_file, dest_sarif)

    # Copy corpus based on configured level (1h or 1d)
    corpus_level = self.config.get("hints_corpus_level", "1h")
    source_corpus = source_hints / "corpus" / corpus_level
    if source_corpus.exists():
        dest_corpus = hints_dir / "corpus"
        dest_corpus.mkdir(exist_ok=True)
        for corpus_file in source_corpus.iterdir():
            shutil.copy2(corpus_file, dest_corpus)

    return hints_dir
```

### Execution Metadata Storage

**Purpose**: Store execution details for reproducibility and debugging.

CRS executor records what was actually executed, enabling:
- Full reproducibility of trial execution
- Debugging execution issues
- Understanding what hints/POVs were provided
- Audit trail of CRS runs

**Implementation**:

```python
def _store_execution_metadata(
    self,
    trial_output_dir: Path,
    cmd: List[str],
    hints_path: Optional[Path],
    povs_path: Optional[Path],
    execution_time: float,
    returncode: int
) -> None:
    """Store execution metadata for reproducibility.

    Args:
        trial_output_dir: Trial-specific output directory
        cmd: Command executed
        hints_path: Path to prepared hints (or None)
        povs_path: Path to prepared POVs (or None)
        execution_time: Execution duration in seconds
        returncode: Process exit code

    Writes execution.json with:
        - Timestamp
        - Exact command run
        - Hints preparation details (enabled, path, corpus level, file counts)
        - POVs preparation details (provided, path, count)
        - Execution timing and result
        - CRS configuration
    """
    metadata = {
        "timestamp": datetime.now().isoformat(),
        "command": cmd,
        "crs_config": self.config.copy(),
        "hints": {
            "enabled": hints_path is not None,
            "path": str(hints_path) if hints_path else None,
            "corpus_level": self.config.get("hints_corpus_level") if hints_path else None,
            "sarif_count": len(list((hints_path / "sarif").glob("*.sarif"))) if hints_path and (hints_path / "sarif").exists() else 0,
            "corpus_count": len(list((hints_path / "corpus").iterdir())) if hints_path and (hints_path / "corpus").exists() else 0,
        },
        "povs": {
            "provided": povs_path is not None,
            "path": str(povs_path) if povs_path else None,
            "count": len(list(povs_path.iterdir())) if povs_path and povs_path.exists() else 0,
        },
        "execution": {
            "duration_seconds": execution_time,
            "returncode": returncode,
            "success": returncode == 0,
        },
    }

    execution_file = trial_output_dir / "execution.json"
    with open(execution_file, "w") as f:
        json.dump(metadata, f, indent=2)

    logger.info(f"Stored execution metadata to {execution_file}")
```

**Example execution.json**:

```json
{
  "timestamp": "2025-10-20T15:30:45.123456",
  "command": [
    "oss-crs",
    "run",
    "ensemble-c",
    "json-c",
    "json_array_fuzzer",
    "--output",
    "/tmp/trial-1/output",
    "--hints",
    "/tmp/trial-1/hints"
  ],
  "crs_config": {
    "hints_enabled": true,
    "hints_corpus_level": "1h",
    "run_timeout": 3600
  },
  "hints": {
    "enabled": true,
    "path": "/tmp/trial-1/hints",
    "corpus_level": "1h",
    "sarif_count": 2,
    "corpus_count": 150
  },
  "povs": {
    "provided": false,
    "path": null,
    "count": 0
  },
  "execution": {
    "duration_seconds": 1823.45,
    "returncode": 0,
    "success": true
  }
}
```

**Orchestrator Responsibility**:

The orchestrator stores high-level experiment configuration to `trial_output_dir/config.yaml`:
- Experiment name and parameters
- Benchmark/CRS/harness selection
- Trial number and metadata
- Experiment-level configuration

Together, `execution.json` (from executor) and `config.yaml` (from orchestrator) provide complete reproducibility.

### POV Detection Logic

**Process**:
1. Scan `povs/` directory for POV files discovered by CRS
2. For each POV, replay with sanitizers to verify
3. Parse sanitizer output for error patterns
4. Match error patterns to known POVs from benchmark
5. Map to POVResult status

**Implementation**:
```python
def process_pov_results(
    self,
    crs_result: CRSResult,
    harness: HarnessFile,
    trial_output_dir: Path  # NEW: Trial output directory
) -> List[POVResult]:
    """Process POV output to determine POV detection.

    Args:
        crs_result: CRS execution result
        harness: Harness configuration
        trial_output_dir: Directory containing CRS outputs

    Returns:
        List of POVResult objects for each expected POV
    """
    pov_results = []

    if not crs_result.success or not harness.povs:
        # Mark all POVs as ERROR or return empty
        return self._create_error_results(crs_result, harness)

    # Get POVs directory from trial output
    povs_dir = trial_output_dir / "output" / "povs"

    # Detect which POVs were found
    detected_povs = self._analyze_discovered_povs(povs_dir, harness.povs)

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

def _analyze_discovered_povs(
    self,
    povs_dir: Path,
    expected_povs: List[POV]
) -> Set[str]:
    """Analyze discovered POV files to determine which match expected POVs.

    Args:
        povs_dir: Directory containing POVs discovered by CRS
        expected_povs: List of expected POVs from benchmark

    Returns:
        Set of POV names that were successfully found
    """
    detected = set()

    if not povs_dir.exists():
        return detected

    # Iterate through POV files discovered by CRS
    for pov_file in povs_dir.glob("pov_*"):
        # Replay POV with sanitizer to verify
        pov_log = self._replay_pov_with_sanitizer(pov_file)

        # Match against expected POVs from benchmark
        for expected_pov in expected_povs:
            if self._matches_pov(pov_log, expected_pov):
                detected.add(expected_pov.name)

    return detected

def _matches_pov(self, pov_log: str, expected_pov: POV) -> bool:
    """Check if POV log matches expected POV signature.

    Args:
        pov_log: Sanitizer output from running the POV
        expected_pov: Expected POV configuration from benchmark

    Returns:
        True if POV matches the expected signature
    """
    # Check sanitizer type
    sanitizer_patterns = {
        "address": "AddressSanitizer",
        "memory": "MemorySanitizer",
        "undefined": "UndefinedBehaviorSanitizer",
        "thread": "ThreadSanitizer",
        "leak": "LeakSanitizer"
    }

    expected_pattern = sanitizer_patterns.get(expected_pov.sanitizer)
    if expected_pattern and expected_pattern not in pov_log:
        return False

    # Check error token if provided
    if expected_pov.error_token and expected_pov.error_token not in pov_log:
        return False

    return True

def _replay_pov_with_sanitizer(self, pov_file: Path) -> str:
    """Replay POV file with sanitizer to generate crash log.

    Args:
        pov_file: Path to POV file

    Returns:
        Sanitizer output log
    """
    # Run harness with POV as input
    # Capture sanitizer output
    # Return log for analysis
    # (Implementation depends on harness execution environment)
    pass
```

## OSS-Patch Executor

### Purpose

Execute patch generation CRS implementations using the OSS-Patch interface.

### Class Definition

```python
class CRSPatchExecutor(CRSExecutor):
    """CRS executor for patch generation using OSS-Patch interface."""

    def __init__(
        self,
        crs_config_name: str,
        oss_patch_path: Path,
        oss_fuzz_path: Path,
        litellm_base: str,
        litellm_key: str,
        benchmarks_root: Path
    ):
        """Initialize executor.

        Args:
            crs_config_name: CRS configuration name
            oss_patch_path: Path to oss-patch repository
            oss_fuzz_path: Path to oss-fuzz repository (required for infrastructure)
            litellm_base: LiteLLM API base URL
            litellm_key: LiteLLM API key
            benchmarks_root: Path to benchmarks directory (for finding benchmark dirs)
        """
        self.crs_config_name = crs_config_name
        self.oss_patch_path = oss_patch_path
        self.oss_fuzz_path = oss_fuzz_path
        self.litellm_base = litellm_base
        self.litellm_key = litellm_key
        self.benchmarks_root = benchmarks_root
        self.config: Dict[str, Any] = {}
        self.built_projects: Set[str] = set()
```

### Build Phase

**Command**: `oss-patch-crs build <config> <project> --oss-fuzz $OSS_FUZZ_HOME --project-path <benchmark-dir> --source-path <source-dir>`

**Workflow**:
1. Set OSS_FUZZ_HOME environment variable
2. Resolve CRS configuration
3. **Use repository manager to ensure source code exists**
4. Get benchmark directory path (contains project.yaml)
5. Execute build command with `--project-path` (benchmark) and `--source-path` (pre-cloned source)
6. Cache successful builds

**CRSBench Method**: Uses OSS-Patch Method 2 (External Project + Pre-cloned Source)
- Benchmark directories are out-of-tree (`benchmarks/`)
- Repository manager provides pre-cloned source
- No git clone during build (offline-capable)

**Implementation**:
```python
def _build_crs_if_needed(self, benchmark_path: Path, project_name: str) -> None:
    """Build patch generation CRS if not already built.

    Args:
        benchmark_path: Path to benchmark directory (contains project.yaml)
        project_name: Project name for caching
    """
    build_key = f"{self.crs_config_name}:{project_name}"

    if build_key in self.built_projects:
        return

    # Use repository manager to ensure source code exists
    from crsbench.migration.repo_manager import ensure_project_repository

    source_path = ensure_project_repository(
        benchmark_dir=str(benchmark_path),
        verbose=self.config.get("verbose", False)
    )

    if not source_path:
        raise EvaluationError(
            f"Failed to obtain source code for {project_name}. "
            "Check that project.yaml has valid main_repo or provide source manually."
        )

    logger.info(f"Using source from: {source_path}")

    cmd = [
        "oss-patch-crs", "build",
        self.crs_config_name, project_name,
        "--oss-fuzz", str(self.oss_fuzz_path),
        "--project-path", str(benchmark_path),  # Benchmark dir (OSS-Fuzz compatible)
        "--source-path", str(source_path)        # Pre-cloned source from repo manager
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
    logger.info(f"Successfully built CRS for {project_name}")
```

### Run Phase

**Command**:
```bash
oss-patch-crs run <config> <project> \
  --harness <harness-name> \
  [--pov <pov-file> | --povs <povs-dir>] \
  [--hints <hints-dir>] \
  [--output <output-dir>] \
  --litellm-base <url> \
  --litellm-key <key>
```

**Workflow**:
1. Build CRS if not already built
2. Prepare base output directory (CRS creates subdirectories)
3. Prepare POVs directory - copy and filter from benchmark
4. Prepare hints directory (if enabled) - copy and filter from benchmark
5. Extract harness name
6. Pass LiteLLM configuration
7. Execute run command with `--output`, `--povs`, and optional `--hints` parameters
8. Wait for completion (with timeout)
9. Return execution result

**Implementation**:
```python
def run_crs(
    self,
    benchmark_path: Path,
    harness: HarnessFile,
    trial_output_dir: Path
) -> CRSResult:
    """Run patch generation CRS.

    Args:
        benchmark_path: Path to benchmark directory
        harness: Harness configuration
        trial_output_dir: Directory for this trial's outputs

    Returns:
        CRSResult with execution details

    Note:
        Source code is already cloned at the correct commit by
        TrialDirectoryPreparer. The executor does not need commit
        information.
    """
    project_name = self._extract_project_name(benchmark_path)

    # Build if needed (pass benchmark_path for repo manager integration)
    self._build_crs_if_needed(benchmark_path, project_name)

    # Prepare base output directory (CRS creates subdirectories)
    self._prepare_output_directory(trial_output_dir)

    harness_name = Path(harness.name).stem

    # Build command
    cmd = [
        "oss-patch-crs", "run",
        self.crs_config_name, project_name,
        "--harness", harness_name,
        "--output", str(trial_output_dir / "output"),
        "--litellm-base", self.litellm_base,
        "--litellm-key", self.litellm_key
    ]

    # Prepare and add POVs directory
    povs_path = self._prepare_povs(benchmark_path, harness_name, trial_output_dir)
    if povs_path:
        cmd.extend(["--povs", str(povs_path)])
        logger.info(f"Using prepared POVs from {povs_path}")

    # Prepare and add hints if enabled
    hints_path = self._prepare_hints(benchmark_path, harness_name, trial_output_dir)
    if hints_path:
        cmd.extend(["--hints", str(hints_path)])
        logger.info(f"Using prepared hints from {hints_path}")

    start_time = time.time()
    result = subprocess.run(
        cmd,
        cwd=self.oss_patch_path,
        capture_output=True,
        text=True,
        timeout=self.config.get("run_timeout", 3600)
    )
    execution_time = time.time() - start_time

    # Store execution metadata for reproducibility
    self._store_execution_metadata(
        trial_output_dir=trial_output_dir,
        cmd=cmd,
        hints_path=hints_path,
        povs_path=povs_path,  # Patch generation uses POVs
        execution_time=execution_time,
        returncode=result.returncode
    )

    return CRSResult(
        harness_name=harness.name,
        execution_time=execution_time,
        success=(result.returncode == 0),
        output=result.stdout,
        error=result.stderr if result.returncode != 0 else None
    )
```

### POVs Preparation for Patch Generation

**Process**:
1. Create trial-specific POVs directory
2. Copy POV blobs from benchmark `.aixcc/<harness>/cpv_*/blobs/`
3. Flatten structure (no cpv_* subdirectories)
4. Filter which POVs based on experiment configuration

**Implementation**:
```python
def _prepare_povs(
    self,
    benchmark_path: Path,
    harness_name: str,
    trial_output_dir: Path
) -> Optional[Path]:
    """Prepare POVs directory with filtered POVs from benchmark.

    Creates trial-specific POVs directory and copies selected POV blobs
    based on experiment configuration.

    Args:
        benchmark_path: Path to benchmark directory
        harness_name: Name of the harness
        trial_output_dir: Trial-specific output directory

    Returns:
        Path to prepared POVs directory, or None if no POVs available

    Process:
        1. Create trial_output_dir/povs/ directory
        2. Find all POV blobs from benchmark .aixcc/<harness>/cpv_*/blobs/
        3. Flatten structure (pov_0, pov_1, pov_2 directly in povs/)
        4. Filter based on experiment config (target_povs list)

    Example structure created:
        trial_output_dir/povs/
        ├── pov_0    # From cpv_0/blobs/pov_0.blob
        ├── pov_1    # From cpv_1/blobs/pov_1.blob
        └── pov_2    # From cpv_1/blobs/pov_2.blob
    """
    # Source POVs from benchmark
    source_harness_dir = benchmark_path / ".aixcc" / harness_name
    if not source_harness_dir.exists():
        return None

    # Create trial-specific POVs directory
    povs_dir = trial_output_dir / "povs"
    povs_dir.mkdir(parents=True, exist_ok=True)

    # Collect POVs from all cpv_* directories
    pov_count = 0
    for cpv_dir in sorted(source_harness_dir.glob("cpv_*")):
        blobs_dir = cpv_dir / "blobs"
        if not blobs_dir.exists():
            continue

        for pov_blob in sorted(blobs_dir.glob("*.blob")):
            # Filter based on config if specified
            if self.config.get("target_povs"):
                if pov_blob.stem not in self.config["target_povs"]:
                    continue

            # Copy and flatten: pov_0.blob -> povs/pov_0
            dest_name = pov_blob.stem  # Remove .blob extension
            shutil.copy2(pov_blob, povs_dir / dest_name)
            pov_count += 1

    if pov_count == 0:
        return None

    logger.info(f"Prepared {pov_count} POVs for {harness_name}")
    return povs_dir
```

### CRSBench Integration Strategy

**OSS-Patch Method**: CRSBench uses Method 2 (External Project + Pre-cloned Source) for patch generation CRS builds.

**Why This Method**:
1. **Out-of-tree benchmarks**: Benchmarks are in `benchmarks/` directory, not `oss-fuzz/projects/`
2. **Repository manager**: Centralized source management with caching
3. **Reproducibility**: Exact commit checkout via `meta.yaml` base_commit
4. **Efficiency**: No duplicate clones, no network access during build
5. **Offline builds**: Pre-cloned sources enable offline operation

**Component Integration**:

```
BenchmarkRunner
    ↓ provides benchmark_path
CRSPatchExecutor._build_crs_if_needed()
    ↓ calls
Repository Manager (repo_manager.py)
    ↓ reads config from
benchmark/.aixcc/meta.yaml + project.yaml
    ↓ clones/returns
Pre-cloned Source (cached in PROJECT_REPOS_DIR)
    ↓ passed to
OSS-Patch build command
    ↓ copies (not clones)
Docker Build Context
```

**Repository Manager Benefits**:
- **Clone once, reuse many times**: Same source used for multiple CRS builds
- **Smart caching**: Detects existing git repos, avoids redundant clones
- **Commit checkout**: Ensures reproducible builds at specific commits
- **Configuration extraction**: Reads main_repo from project.yaml automatically

**Build Flow**:
```python
# 1. BenchmarkRunner creates executor
executor = CRSPatchExecutor(
    crs_config_name="multi-retrieval",
    oss_patch_path=Path("/path/to/oss-patch"),
    oss_fuzz_path=Path("/path/to/oss-fuzz"),
    litellm_base="https://api.litellm.com",
    litellm_key="sk-key",
    benchmarks_root=Path("benchmarks")
)

# 2. Executor builds CRS (internally calls repo manager)
executor._build_crs_if_needed(
    benchmark_path=Path("benchmarks/mock-c"),
    project_name="mock-c"
)

# Inside _build_crs_if_needed():
#   - Calls ensure_project_repository(benchmark_path)
#   - Gets source_path from repo manager
#   - Passes both paths to oss-patch-crs build:
#       --project-path benchmarks/mock-c
#       --source-path /repos/mock-c-source
```

**Configuration Requirements**:

**benchmark/.aixcc/meta.yaml**:
```yaml
delta_mode:
  base_commit: abc123...
  ref_commit: def456...
```

**benchmark/project.yaml** (OSS-Fuzz compatible):
```yaml
language: c
main_repo: https://github.com/project/repo.git
homepage: https://github.com/project/repo
```

**Environment**:
```bash
# Optional: Override default repo cache location
export PROJECT_REPOS_DIR=/custom/path/to/repos
```

**Error Handling**:

**Scenario**: Repository manager fails to clone source
```python
source_path = ensure_project_repository(benchmark_dir=str(benchmark_path))
if not source_path:
    raise EvaluationError(
        f"Failed to obtain source code for {project_name}. "
        "Check that project.yaml has valid main_repo or provide source manually."
    )
```

**Scenario**: Network failure during clone (first time)
- Repository manager returns None
- Executor raises EvaluationError
- User can manually clone and set PROJECT_REPOS_DIR

**Scenario**: Source already cloned (subsequent runs)
- Repository manager detects existing .git directory
- Returns path immediately (no clone attempt)
- Build proceeds with cached source

**Design References**:
- OSS-Patch alternative methods: `docs/ossfuzz-crs-interface.md` (Alternative Build Methods)
- Repository manager: `design-docs/migration/repo-manager.md`
- OSS-Patch implementation: `oss-patch/design-docs/alternative-project-sources.md`

### Patch Collection

**Process**:
1. Locate patches directory in trial output
2. Collect generated patch files (*.diff format)
3. Validate patch format
4. Store patch content in results

**Implementation**:
```python
def _collect_patches(self, trial_output_dir: Path) -> Dict[str, str]:
    """Collect generated patches from output directory.

    Args:
        trial_output_dir: Trial-specific output directory

    Returns:
        Dict mapping POV ID to patch content
    """
    patches_dir = trial_output_dir / "output" / "patches"

    patches = {}
    if patches_dir.exists():
        # Collect patches organized by POV ID: patches/<pov_id>/patch.diff
        for pov_dir in patches_dir.iterdir():
            if pov_dir.is_dir():
                patch_file = pov_dir / "patch.diff"
                if patch_file.exists():
                    patches[pov_dir.name] = patch_file.read_text()

    return patches
```

### POV Processing for Patch Generation

For patch generation, POV processing validates patches:

```python
def process_pov_results(
    self,
    crs_result: CRSResult,
    harness: HarnessFile,
    trial_output_dir: Path  # NEW: Trial output directory
) -> List[POVResult]:
    """Process patch generation results.

    Args:
        crs_result: CRS execution result
        harness: Harness configuration
        trial_output_dir: Directory containing CRS outputs

    Returns:
        List of POVResult objects for each expected POV
    """
    pov_results = []

    if not crs_result.success or not harness.povs:
        return self._create_error_results(crs_result, harness)

    # Collect generated patches from output directory
    patches = self._collect_patches(trial_output_dir)

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

CRSBench uses two directories for CRS configurations:

**`crses/`** - CRS configuration directory (following `example_configs/` format):
```
crses/
├── ensemble-c/
│   ├── config-crs.yaml      # CRS runtime config
│   ├── config-litellm.yaml  # LiteLLM config (optional)
│   ├── config-resource.yaml # Resource limits (optional)
│   └── config-worker.yaml   # Worker config (optional)
├── multi-retrieval/
│   ├── config-crs.yaml
│   ├── config-litellm.yaml
│   └── ...
└── ...
```

**Important**: `crses/` is NOT a registry - it's a directory of CRS configurations following the same format as `oss-crs/example_configs/`.

**`oss-crs-registry/`** - The CRS registry (submodule):
- The **ONLY** registry for CRS implementations (used for both testing and production)
- Git submodule from the open-source CRS registry
- Contains `crs/` subdirectory with CRS configurations
- Structure: `oss-crs-registry/crs/<crs-name>/`
- See `oss-crs-registry/crs/` for registry CRS configurations

### Configuration Files

CRS configurations (whether in `crses/` or `oss-crs-registry/crs/`) contain:

**config-crs.yaml**: CRS-specific runtime parameters
```yaml
max_iterations: 10
temperature: 0.7
model: "anthropic/claude-3-sonnet"
```

**config-litellm.yaml**: LiteLLM configuration (optional)
```yaml
api_base: "https://api.litellm.com"
api_key: "${LITELLM_API_KEY}"
```

**config-resource.yaml**: Resource limits (optional)
```yaml
memory_limit: "8GB"
cpu_limit: "4"
```

**config-worker.yaml**: Worker configuration (optional)
```yaml
num_workers: 4
worker_timeout: 3600
```

**Configuration Examples**: See `oss-crs/example_configs/` for reference CRS configuration format (same format used by `crses/`).

### Configuration Loading

```python
def _resolve_crs_config_dir(self) -> Path:
    """Resolve CRS configuration directory.

    Searches for CRS configuration in crses/ directory, which follows
    the same format as oss-crs/example_configs/.

    Note:
        This does NOT search in oss-crs-registry/. The registry is only
        used via --registry-dir parameter for oss-crs CLI.

    Returns:
        Path to CRS config directory (absolute)
    """
    # Check if full path provided
    config_path = Path(self.crs_config_name)
    if config_path.is_absolute() and config_path.exists():
        return config_path

    # Look in crses/ directory (NOT oss-crs-registry/)
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

**Output directory mapping:**
- Host: `trial_output_dir/output/`
- Container: `/out/`
- Purpose: CRS writes POVs/patches/corpus to `/out/` subdirectories

**Hints directory mapping (optional, if hints enabled):**
- Host: `trial_output_dir/hints/` (prepared by `_prepare_hints()`)
- Container: `/hints/`
- Purpose: CRS reads static analysis and corpus hints
- Content: Filtered SARIF files and corpus based on experiment config

**POVs directory mapping (patch generation only):**
- Host: `trial_output_dir/povs/` (prepared by `_prepare_povs()`)
- Container: `/povs/`
- Purpose: CRS reads POVs to generate patches for
- Content: Flattened POV blobs filtered based on experiment config

**Key points:**
- All directories are trial-specific under `trial_output_dir/`
- Hints and POVs are prepared dynamically before CRS execution
- Filtering is based on experiment configuration (corpus level, target POVs, etc.)

### Container Filesystem Layout

**Bug Finding CRS (OSS-Fuzz)**:
```
/
├── out/                         # CRS output (mounted from host trial_output_dir/output/)
│   ├── povs/                    # CRS writes discovered POVs here
│   │   ├── pov_0
│   │   ├── pov_1
│   │   └── pov_2
│   ├── corpus/                  # CRS writes generated corpus here
│   │   ├── input-001
│   │   ├── input-002
│   │   └── input-003
│   └── crs-data/                # CRS-specific outputs (optional)
│       └── ...
├── hints/                       # Optional hints (mounted if --hints provided)
│   ├── sarif/                   # Static analysis reports
│   │   ├── codeql.sarif
│   │   └── semgrep.sarif
│   └── corpus/                  # Pre-fuzzing corpus
│       ├── input-001
│       └── ...
├── src/                         # Project source code
│   └── <project>/
│       └── ...
└── work/                        # Working directory
```

**Patch Generation CRS (OSS-Patch)**:
```
/
├── out/                         # CRS output (mounted from host trial_output_dir/output/)
│   ├── patches/                 # CRS writes generated patches here (organized by POV ID)
│   │   ├── pov_0/
│   │   │   └── patch.diff
│   │   ├── pov_1/
│   │   │   └── patch.diff
│   │   └── pov_2/
│   │       └── patch.diff
│   └── crs-data/                # CRS-specific outputs (optional)
│       └── ...
├── povs/                        # POVs to fix (mounted if --povs provided)
│   ├── pov_0
│   ├── pov_1
│   └── pov_2
├── hints/                       # Optional hints (mounted if --hints provided)
│   ├── sarif/                   # Static analysis reports
│   └── corpus/                  # Pre-fuzzing corpus for validation
├── src/                         # Project source code
│   └── <project>/
│       └── ...
└── work/                        # Working directory
```

**Key points:**
- CRS is responsible for creating subdirectories (`povs/`, `patches/`, `corpus/`, `crs-data/`)
- CRSBench only creates the base `/out/` directory; subdirectory structure is CRS's responsibility
- Naming convention agreement: CRS must use specified subdirectory names for proper evaluation
- Snapshot system periodically captures `/out/` directory contents
- LLM usage and CRS logs are recorded by CRSBench separately (not in `/out/`)

### Harness Path Resolution and Command Arguments

CRS executors can optionally provide harness source file paths to CRS commands via the `--harness-source` argument. The `path_resolver` module handles translation of `$REPO` and `$PROJECT` variables into host filesystem paths.

**Purpose:**
- Provide CRS with harness source code location for optional analysis
- Enable CRS to understand harness structure and API if needed
- Support advanced CRS strategies that analyze harness code

**Variable Semantics:**
- `$REPO/path/to/file`: Path relative to cloned repository (where source code lives)
- `$PROJECT/path/to/file`: Path relative to OSS-Fuzz project directory (containing project.yaml, build.sh)
- `/absolute/path`: Container path (not resolved, used as-is)
- `./relative/path`: Path relative to benchmark directory

**Integration with CRS Executors:**

```python
from crsbench.evaluation.path_resolver import get_harness_source_path

class CRSBugFindingExecutor(CRSExecutor):
    def run_crs(self, benchmark_path: Path, harness: HarnessFile, ...) -> CRSResult:
        """Run CRS with optional harness source path."""
        # Build base command
        cmd = [
            "oss-crs", "run",
            self.crs_config_name,
            project_name,
            harness.name,
            "--output", str(trial_output_dir)
        ]

        # Add optional harness source path
        harness_source = get_harness_source_path(
            harness, benchmark_path, self.repos_dir
        )
        if harness_source:
            cmd.extend(["--harness-source", str(harness_source)])
            logger.info(f"Providing harness source: {harness_source}")
        else:
            logger.warning("Running without harness source code")

        # Add hints if available
        if hints_dir:
            cmd.extend(["--hints", str(hints_dir)])

        # Execute
        result = subprocess.run(cmd, ...)
        return self._parse_result(result)
```

**Argument Passing Example:**

For harness with `path: "$REPO/test/harness.c"`:
```python
# Resolution
host_path = Path("/tmp/repos/json-c/test/harness.c")  # Resolved from $REPO

# Command with argument
cmd = [
    "oss-crs", "run",
    "ensemble-c", "json-c", "json_array_fuzzer",
    "--output", "/tmp/output",
    "--harness-source", "/tmp/repos/json-c/test/harness.c"
]
```

**CRS Implementation Flexibility:**
The CRS implementation receives the host path and decides how to use it:
- Mount it into the container (via Docker -v during execution)
- Copy it into the container during build phase
- Read it from the host before launching the container
- Ignore it if harness source code analysis is not needed

**Error Handling:**
- If harness file doesn't exist: Log warning, continue without --harness-source
- If repository cloning fails: Log warning, continue without --harness-source
- If path format is invalid: Log warning, continue without --harness-source
- CRS must be robust to missing --harness-source argument

**Performance:**
- Path resolution is fast (<1ms) after initial repository clone
- Repository cloning is cached by `repo_manager`
- First resolution per benchmark may be slow (git clone)
- Subsequent resolutions use cached repository

**Design Documentation:**
- Full details in `design-docs/evaluation/path-resolver.md`
- Interface spec in `docs/ossfuzz-crs-interface.md`
- Tests in `tests/test_path_resolver.py`

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
# Bug finding (CRSBench method with all parameters)
oss-crs build \
  --build-dir <trial-build-dir> \
  --oss-fuzz-dir <oss-fuzz-dir> \
  --registry-dir <registry-dir> \
  --project-path <benchmark-dir> \
  <config-dir> \
  <project> \
  <source-path>

oss-crs run \
  --build-dir <trial-build-dir> \
  --oss-fuzz-dir <oss-fuzz-dir> \
  --registry-dir <registry-dir> \
  <config-dir> \
  <project> \
  <harness> \
  [--hints <dir>]
# Note: Output directory is auto-determined as {{ build_dir }}/out/{{ crs.name }}/{{ project }}/

# Patch generation (CRSBench method - with external project + pre-cloned source)
oss-patch-crs build <config> <project> \
  --oss-fuzz $OSS_FUZZ_HOME \
  --project-path <benchmark-dir> \
  --source-path <source-dir>

# Patch generation (Run command - unchanged)
oss-patch-crs run <config> <project> --harness <name> [--pov <file> | --povs <dir>] [--hints <dir>] [--output <dir>] --litellm-base <url> --litellm-key <key>
```

**Notes**:
- Config paths use relative format: `example_configs/ensemble-c` (no `infra/crs/` prefix)
- Commands are installable via pip/uv
- **CRSBench uses trial-specific --build-dir** for isolation between trials
- **CRSBench passes --oss-fuzz-dir and --registry-dir** for proper path resolution
- **CRSBench uses alternative build method** with `--project-path` and `--source-path`
- Run command arguments unchanged for both bug finding and patch generation

### Implementation Notes

**Command Usage**:
- Use `oss-crs` for bug finding CRS execution
- Use `oss-patch-crs` for patch generation CRS execution
- Both commands should be available in the environment
- Config paths are relative (e.g., `example_configs/ensemble-c`)

**Command Construction**:
```python
# Bug finding
cmd = [
    "oss-crs", "build",
    "--build-dir", str(trial_build_dir),
    "--oss-fuzz-dir", str(oss_fuzz_path),
    "--registry-dir", str(registry_dir),
    "--project-path", str(benchmark_path),
    str(crs_config_dir), project_name, str(source_path)
]

cmd = [
    "oss-crs", "run",
    "--build-dir", str(trial_build_dir),
    "--oss-fuzz-dir", str(oss_fuzz_path),
    "--registry-dir", str(registry_dir),
    str(crs_config_dir), project_name, harness_name
]
# Note: Output directory is derived from build_dir/out/crs_name/project/

# Patch generation (CRSBench method)
cmd = ["oss-patch-crs", "build", crs_config_name, project_name,
       "--oss-fuzz", str(oss_fuzz_path),
       "--project-path", str(benchmark_path),
       "--source-path", str(source_path)]

cmd = ["oss-patch-crs", "run", crs_config_name, project_name,
       "--harness", harness_name,
       "--output", str(output_dir),
       "--litellm-base", url,
       "--litellm-key", key]
```

**Repository Manager Integration**:
```python
from crsbench.migration.repo_manager import ensure_project_repository

# Get pre-cloned source path
source_path = ensure_project_repository(
    benchmark_dir=str(benchmark_path),
    verbose=config.get("verbose", False)
)

if not source_path:
    raise EvaluationError("Failed to obtain source code")

# Use in build command
cmd.extend(["--project-path", str(benchmark_path)])
cmd.extend(["--source-path", str(source_path)])
```

## Implementation Checklist

### Phase 1: CRSBugFindingExecutor

- [ ] Create `crsbench/evaluation/oss_fuzz_executor.py`
- [ ] Implement `CRSBugFindingExecutor` class
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

### Phase 2: CRSPatchExecutor

- [ ] Create `crsbench/evaluation/oss_patch_executor.py` (or add to same file)
- [ ] Implement `CRSPatchExecutor` class
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
- [ ] `_prepare_output_directory()` - Create base output directory
- [ ] `_prepare_hints()` - Prepare and filter hints directory from benchmark
- [ ] `_prepare_povs()` - Prepare and filter POVs directory from benchmark
- [ ] `_store_execution_metadata()` - Store execution details to execution.json
- [ ] `_analyze_discovered_povs()` - Analyze POVs discovered by CRS
- [ ] `_replay_pov_with_sanitizer()` - Re-execute POV to verify
- [ ] `_get_command_prefix()` - Future command detection

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
    executor = CRSBugFindingExecutor("ensemble-c", Path("/path/to/oss-fuzz"))
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
    executor = CRSBugFindingExecutor("ensemble-c", Path("/oss-fuzz"))
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
    executor = CRSBugFindingExecutor("ensemble-c", oss_fuzz_path)

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

**Decision**: Create `CRSBugFindingExecutor` and `CRSPatchExecutor` as separate classes.

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

### 4. Assuming Output Directories Exist

**Wrong**:
```python
for pov_file in povs_dir.iterdir():  # Fails if directory missing
    ...
```

**Right**:
```python
if povs_dir.exists():
    for pov_file in povs_dir.iterdir():
        ...
```

### 5. Not Preparing Base Output Directory

**Wrong**:
```python
# Run CRS without creating base output directory
cmd = ["python3", "infra/helper.py", "run_crs", ...]
subprocess.run(cmd)
```

**Right**:
```python
# Prepare base output directory before running CRS
# Note: CRS creates subdirectories (povs/, patches/, etc.)
self._prepare_output_directory(trial_output_dir)
cmd = ["python3", "infra/helper.py", "run_crs", ..., "--output", str(trial_output_dir / "output")]
subprocess.run(cmd)
```

### 6. Creating Subdirectories for CRS

**Wrong**:
```python
# Don't create subdirectories - that's CRS's job
output_dir = trial_output_dir / "output"
(output_dir / "povs").mkdir(parents=True)
(output_dir / "patches").mkdir(parents=True)
```

**Right**:
```python
# Only create base directory - CRS creates subdirectories
output_dir = trial_output_dir / "output"
output_dir.mkdir(parents=True, exist_ok=True)
# CRS will create povs/, patches/, corpus/, crs-data/ as needed
```

## Summary of Key Changes from Legacy Architecture

### What Changed

**1. Output Directory Management**
- **Old**: CRS outputs to `oss-fuzz/build/out/<crs>/<project>/<harness>/crashes/corpus/`
- **New Bug Finding**: CRS outputs to `trial_build_dir/out/<crs>/<project>/povs/corpus/crs-data/` (where `trial_build_dir = trial_output_dir/build/`)
- **New Patch Generation**: CRS outputs to `trial_output_dir/output/povs/patches/corpus/crs-data/`
- **Why**: Trial-based organization enables snapshot system and better isolation

**2. Directory Structure**
- **Old**: Flat `crashes/` and `corpus/` directories
- **New**: Organized subdirectories (`povs/`, `patches/`, `corpus/`, `crs-data/`)
- **Why**: Clear separation of output types, easier to snapshot and evaluate

**3. Command Construction**
- **Old**: Simple positional arguments only
- **New**: Multiple required parameters (`--build-dir`, `--oss-fuzz-dir`, `--registry-dir`, `--project-path`, source path)
- **Why**: Trial isolation, proper path resolution, and external project support

**4. Method Signatures**
- **Old**: `run_crs(benchmark_path, harness, base_commit, ref_commit)`
- **New**: `run_crs(benchmark_path, harness, trial_output_dir)`
- **Why**: Executors need to know where to write trial-specific outputs; commits are handled by TrialDirectoryPreparer

**5. POV/Patch Collection**
- **Old**: Hardcoded paths in `oss-fuzz/build/out/...`
- **New Bug Finding**: Relative to `trial_build_dir/out/<crs>/<project>/`
- **New Patch Generation**: Relative to `trial_output_dir/output/`
- **Why**: Flexible, supports multiple concurrent trials, enables snapshots

**6. Hints/POVs Preparation**
- **Old**: Direct reference to benchmark `.aixcc/<harness>/hints/` and `.aixcc/<harness>/povs/`
- **New**: Prepared on-the-fly in `trial_output_dir/hints/` and `trial_output_dir/povs/`
- **Why**: Filter content based on experiment config, record what hints/POVs were provided, trial isolation

### Responsibilities

**CRS (via Docker container):**
- Create output subdirectories as needed (`/out/povs/`, `/out/patches/`, etc.)
- Write POVs to `/out/povs/`
- Write patches to `/out/patches/`
- Write corpus to `/out/corpus/` (optional)
- Write custom data to `/out/crs-data/` (optional)
- **Naming convention agreement**: CRS must follow the specified directory names for CRSBench to properly evaluate outputs

**CRSBench Executor:**
- Create base trial output directory before CRS execution
- Prepare hints directory (copy and filter from benchmark based on config)
- Prepare POVs directory for patch generation (copy and filter from benchmark)
- Pass `--output`, `--hints`, `--povs` parameters to CRS interface
- Store execution metadata to `execution.json` (command, hints/POVs details, timing)
- Collect and analyze outputs from trial directory
- Do NOT create `/out/` subdirectories - CRS is responsible for directory structure

**CRSBench BenchmarkRunner:**
- Provide `trial_output_dir` to executor
- Record LLM usage to `trial_output_dir/llm-usage.json`
- Capture CRS logs to `trial_output_dir/crs-output.log`
- Manage snapshot system (periodically snapshot `output/` directory)

**CRSBench Orchestrator:**
- Store experiment configuration to `trial_output_dir/config.yaml`
- Record experiment name, parameters, benchmark/CRS/harness selection
- Record trial number and metadata
- Provide high-level reproducibility information

**Snapshot System:**
- Periodically capture `trial_output_dir/output/` directory contents
- Snapshot LLM usage and CRS logs
- Enable progress monitoring and recovery

### Migration Checklist

When updating existing CRS executor code:

**Method Signatures:**
- [ ] Update `__init__()` to accept `registry_dir: Path` and `benchmarks_root: Path` parameters
- [ ] Update `run_crs()` signature: `run_crs(benchmark_path, harness, trial_output_dir)` - remove commit parameters
- [ ] Update `_build_crs_if_needed()` to accept `benchmark_path: Path` and `trial_build_dir: Path` parameters
- [ ] Update `_get_crs_output_directory()` to accept `trial_build_dir: Path` parameter
- [ ] Add `trial_output_dir: Path` parameter to `process_pov_results()` methods

**Command Construction (Bug Finding):**
- [ ] Add `--build-dir` parameter pointing to trial-specific build directory (`trial_output_dir/build/`)
- [ ] Add `--oss-fuzz-dir` parameter pointing to oss-fuzz repository
- [ ] Add `--registry-dir` parameter pointing to CRS registry directory
- [ ] Add `--project-path` parameter for build command (benchmark directory)
- [ ] Add source path as positional argument for build command (from repo manager)
- [ ] Remove `--output` parameter from run command (output is auto-determined from build_dir)
- [ ] Ensure `--hints` parameter uses prepared hints directory

**Command Construction (Patch Generation):**
- [ ] Keep `--output` parameter for oss-patch-crs run command (still required)
- [ ] Add `--povs` parameter to pass prepared POVs directory
- [ ] Add `--hints` parameter if hints are enabled

**Directory Preparation:**
- [ ] Create trial-specific build directory before CRS execution
- [ ] Implement `_prepare_output_directory()` (base directory only)
- [ ] Implement `_prepare_hints()` (copy and filter from benchmark)
- [ ] Implement `_prepare_povs()` (copy and filter from benchmark - patch generation only)
- [ ] Implement `_store_execution_metadata()` (store execution details)
- [ ] Call preparation methods before CRS execution
- [ ] Call `_store_execution_metadata()` after CRS execution

**Output Collection:**
- [ ] Update POV collection to read from `trial_build_dir/out/<crs>/<project>/povs/` (bug finding)
- [ ] Update POV collection to read from `trial_output_dir/output/povs/` (patch generation)
- [ ] Update patch collection to read from `trial_output_dir/output/patches/`
- [ ] Update corpus collection to read from appropriate output directory
- [ ] Remove hardcoded `oss-fuzz/build/out/<crs>/<project>/` paths
- [ ] Use `_get_crs_output_directory()` helper to derive output paths

**Source Code Management:**
- [ ] Integrate with repo manager to obtain pre-cloned source code
- [ ] Pass source path to build command
- [ ] Remove any code that creates CRS subdirectories (povs/, patches/, etc.)
- [ ] Remove hardcoded `.aixcc/<harness>/hints/` and `.aixcc/<harness>/povs/` direct references

**Testing and Documentation:**
- [ ] Update tests to use new directory structure and preparation logic
- [ ] Update tests to mock new command parameters
- [ ] Update documentation and examples
- [ ] Document that CRS is responsible for creating subdirectories
- [ ] Document hints/POVs filtering based on experiment config
- [ ] Document execution metadata storage (execution.json)
- [ ] Document trial-specific build directory isolation
- [ ] Coordinate with orchestrator for config.yaml storage

## References

- [OSS-CRS Integration](./oss-crs-integration.md): **NEW** - Detailed integration with oss-crs CLI, parameter mappings, and trial isolation
- [OSS-Fuzz CRS Interface](../../docs/ossfuzz-crs-interface.md): Interface specification with `--output` parameter
- [Snapshot Design](./snapshots.md): Snapshot system implementation
- [Evaluation Module Design](./evaluation.md): Integration with evaluation
- [Architecture](../architecture.md): Overall CRSBench architecture
- [OSS-Fuzz Documentation](https://google.github.io/oss-fuzz/): OSS-Fuzz details
