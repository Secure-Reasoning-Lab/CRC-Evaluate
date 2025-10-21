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

**Command**: `python3 infra/helper.py run_crs <crs-config-dir> <project-name> <harness-name> --output <output-dir>`

**Workflow**:
1. Build CRS if not already built
2. Prepare base output directory (CRS creates subdirectories)
3. Prepare hints directory (if enabled) - copy and filter from benchmark
4. Extract harness name from HarnessFile
5. Execute run command in oss-fuzz directory with `--output` and optional `--hints` parameters
6. Wait for completion (with timeout)
7. Return execution result

**Implementation**:
```python
def run_crs(
    self,
    benchmark_path: Path,
    harness: HarnessFile,
    trial_output_dir: Path,  # NEW: Trial-specific output directory
    base_commit: str,
    ref_commit: Optional[str] = None
) -> CRSResult:
    """Run CRS on specific harness.

    Args:
        benchmark_path: Path to benchmark directory
        harness: Harness configuration
        trial_output_dir: Directory for this trial's outputs
        base_commit: Base commit for evaluation
        ref_commit: Optional reference commit

    Returns:
        CRSResult with execution details
    """
    project_name = self._extract_project_name(benchmark_path)

    # Build if needed
    self._build_crs_if_needed(project_name)

    # Prepare base output directory (CRS creates subdirectories)
    self._prepare_output_directory(trial_output_dir)

    # Extract harness name (without extension)
    harness_name = Path(harness.name).stem

    crs_config_dir = self._resolve_crs_config_dir()

    # Build command with output directory
    cmd = [
        "python3", "infra/helper.py", "run_crs",
        str(crs_config_dir), project_name, harness_name,
        "--output", str(trial_output_dir / "output")
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

**Hints Preparation** (see `_prepare_hints()` method above for full implementation)

**Updated run_crs Implementation with Hints**:

```python
def run_crs(
    self,
    benchmark_path: Path,
    harness: HarnessFile,
    trial_output_dir: Path,
    base_commit: str,
    ref_commit: Optional[str] = None
) -> CRSResult:
    """Run CRS on specific harness with optional hints."""
    project_name = self._extract_project_name(benchmark_path)

    # Build if needed
    self._build_crs_if_needed(project_name)

    # Prepare base output directory (CRS creates subdirectories)
    self._prepare_output_directory(trial_output_dir)

    # Extract harness name (without extension)
    harness_name = Path(harness.name).stem

    crs_config_dir = self._resolve_crs_config_dir()

    cmd = [
        "python3", "infra/helper.py", "run_crs",
        str(crs_config_dir), project_name, harness_name,
        "--output", str(trial_output_dir / "output")
    ]

    # Prepare and add hints if enabled
    hints_path = self._prepare_hints(benchmark_path, harness_name, trial_output_dir)
    if hints_path:
        cmd.extend(["--hints", str(hints_path)])
        logger.info(f"Using prepared hints from {hints_path}")

    start_time = time.time()
    result = subprocess.run(
        cmd,
        cwd=self.oss_fuzz_path,
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
        povs_path=None,  # Bug finding doesn't use POVs input
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

CRS writes outputs to the trial-specific output directory, which is mounted to `/out/` in the container:

**Host directory structure**:
```
trial_output_dir/                    # Provided by BenchmarkRunner
├── output/                          # Mounted to /out/ in container
│   ├── povs/                        # CRS writes POVs here (bug finding)
│   │   ├── pov_001                  # Binary blob
│   │   ├── pov_002
│   │   └── pov_003
│   ├── corpus/                      # CRS writes corpus here (optional)
│   │   ├── input-001
│   │   ├── input-002
│   │   └── input-003
│   └── crs-data/                    # CRS-specific outputs (optional)
│       ├── intermediate-results.json
│       └── debug-trace.log
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
    "python3",
    "infra/helper.py",
    "run_crs",
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
    trial_output_dir: Path,  # NEW: Trial-specific output directory
    base_commit: str,
    ref_commit: Optional[str] = None
) -> CRSResult:
    """Run patch generation CRS.

    Args:
        benchmark_path: Path to benchmark directory
        harness: Harness configuration
        trial_output_dir: Directory for this trial's outputs
        base_commit: Base commit for evaluation
        ref_commit: Optional reference commit

    Returns:
        CRSResult with execution details
    """
    project_name = self._extract_project_name(benchmark_path)

    # Build if needed
    self._build_crs_if_needed(project_name)

    # Prepare base output directory (CRS creates subdirectories)
    self._prepare_output_directory(trial_output_dir)

    harness_name = Path(harness.name).stem

    # Build command
    cmd = [
        "python3", "infra/helper.py", "run_crs",
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
- **New**: CRS outputs to `trial_output_dir/output/povs/corpus/crs-data/`
- **Why**: Trial-based organization enables snapshot system and better isolation

**2. Directory Structure**
- **Old**: Flat `crashes/` and `corpus/` directories
- **New**: Organized subdirectories (`povs/`, `patches/`, `corpus/`, `crs-data/`)
- **Why**: Clear separation of output types, easier to snapshot and evaluate

**3. Command Construction**
- **Old**: No `--output` parameter
- **New**: `--output` parameter required
- **Why**: Explicit control over where CRS writes outputs

**4. Method Signatures**
- **Old**: `run_crs(benchmark_path, harness, base_commit, ref_commit)`
- **New**: `run_crs(benchmark_path, harness, trial_output_dir, base_commit, ref_commit)`
- **Why**: Executors need to know where to write trial-specific outputs

**5. POV/Patch Collection**
- **Old**: Hardcoded paths in `oss-fuzz/build/out/...`
- **New**: Relative to `trial_output_dir/output/`
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

- [ ] Add `trial_output_dir: Path` parameter to `run_crs()` methods
- [ ] Add `trial_output_dir: Path` parameter to `process_pov_results()` methods
- [ ] Implement `_prepare_output_directory()` (base directory only)
- [ ] Implement `_prepare_hints()` (copy and filter from benchmark)
- [ ] Implement `_prepare_povs()` (copy and filter from benchmark)
- [ ] Implement `_store_execution_metadata()` (store execution details)
- [ ] Call preparation methods before CRS execution
- [ ] Call `_store_execution_metadata()` after CRS execution
- [ ] Add `--output`, `--hints`, `--povs` parameters to command construction
- [ ] Update POV collection to read from `trial_output_dir/output/povs/`
- [ ] Update patch collection to read from `trial_output_dir/output/patches/`
- [ ] Update corpus collection to read from `trial_output_dir/output/corpus/`
- [ ] Remove hardcoded `build/out/<crs>/<project>/<harness>/` paths
- [ ] Remove hardcoded `.aixcc/<harness>/hints/` and `.aixcc/<harness>/povs/` direct references
- [ ] Remove any code that creates CRS subdirectories (povs/, patches/, etc.)
- [ ] Update tests to use new directory structure and preparation logic
- [ ] Update documentation and examples
- [ ] Document that CRS is responsible for creating subdirectories
- [ ] Document hints/POVs filtering based on experiment config
- [ ] Document execution metadata storage (execution.json)
- [ ] Coordinate with orchestrator for config.yaml storage

## References

- [OSS-Fuzz CRS Interface](../../docs/ossfuzz-crs-interface.md): Interface specification with `--output` parameter
- [Snapshot Design](./snapshots.md): Snapshot system implementation
- [Evaluation Module Design](./evaluation.md): Integration with evaluation
- [Architecture](../architecture.md): Overall CRSBench architecture
- [OSS-Fuzz Documentation](https://google.github.io/oss-fuzz/): OSS-Fuzz details
