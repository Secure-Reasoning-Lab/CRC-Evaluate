# Benchmark Snapshot Generator (bench_snapgen) Design

## Overview and Motivation

### Purpose

The `bench_snapgen` module generates realistic trial snapshots from CRSBench benchmark ground truth data. Unlike runtime snapshot generation (which captures actual CRS execution), this module simulates realistic trial progression for testing, demonstration, and validation purposes.

### Key Differences

**vs. Runtime Snapshot Generation (SnapshotManager):**
- **Runtime**: Captures actual CRS outputs during live trials
- **bench_snapgen**: Simulates trial progression from ground truth
- **Use case**: Testing evaluation pipelines, demonstration, format validation

**vs. Example Snapshot Generator (snapshot-examples/):**
- **Examples**: Hardcoded dummy data with fake timings
- **bench_snapgen**: Reads actual benchmark ground truth with realistic timing models
- **Use case**: Production-quality simulated trials vs. format examples

### Use Cases

1. **Testing**: Validate snapshot processing pipelines without running expensive CRS trials
2. **Demonstration**: Show snapshot format and structure with real data
3. **Development**: Test deduplication, POV validation, patch testing without CRS execution
4. **Fault Injection**: Test evaluation robustness with intentionally invalid data

## Architecture

### Module Structure

```
crsbench/bench_snapgen/
├── __init__.py                    # Public API exports
├── generator.py                   # Main BenchmarkSnapshotGenerator
├── timeline.py                    # Discovery timing models
├── fault_injection.py             # Invalid POV/patch generation
├── builder.py                     # Snapshot archive creation
└── README.md                      # Usage documentation
```

### Core Components

#### 1. BenchmarkSnapshotGenerator (generator.py)

**Responsibilities:**
- Read benchmark ground truth from `.aixcc/` directory
- Parse `meta.yaml` configuration
- Load POVs, patches, hints from filesystem
- Orchestrate snapshot generation across configured intervals
- Support both bug-finding and patch-generation modes

**Key Methods:**
```python
class BenchmarkSnapshotGenerator:
    def __init__(self, benchmark_path, output_dir, config)
    def generate_trial_snapshots(mode, difficulty_level) -> Path
    def _create_discovery_timeline() -> DiscoveryTimeline
    def _generate_snapshot(cycle, elapsed_time, timeline)
```

#### 2. DiscoveryTimeline (timeline.py)

**Responsibilities:**
- Model realistic POV/patch discovery timing
- Implement difficulty-based discovery patterns
- Cluster POVs from same root cause
- Generate patch discovery after POV discovery

**Key Classes:**
```python
class DiscoveryEvent:
    timestamp: float          # Seconds since trial start
    event_type: str          # 'pov' or 'patch'
    data: bytes              # POV blob or patch diff
    metadata: Dict           # Source info, validity
    is_valid: bool          # For fault injection tracking

class DiscoveryTimeline:
    events: List[DiscoveryEvent]
    def add_pov(time, pov_blob, **metadata)
    def add_patch(time, patch_diff, **metadata)
    def get_events_before(time) -> List[DiscoveryEvent]

class POVDiscoveryModel:
    def get_discovery_times(difficulty, max_time) -> List[float]

class PatchGenerationModel:
    def get_patch_time(pov_time, difficulty) -> float
```

#### 3. FaultInjector (fault_injection.py)

**Responsibilities:**
- Generate invalid POVs that don't trigger crashes
- Generate invalid patches with various fault types
- Track valid vs invalid data for testing

**Fault Types:**
```python
class FaultInjector:
    def inject_invalid_povs(valid_povs) -> List[POV]
    def inject_invalid_patches(valid_patches) -> List[Patch]

    # Invalid POV: random data that won't crash
    def _create_invalid_pov() -> POV

    # Invalid patch types:
    def _create_syntax_error_patch()      # Malformed diff
    def _create_wrong_file_patch()        # Patches unrelated code
    def _create_incomplete_patch()        # Doesn't fix all POVs
    def _create_build_breaking_patch()    # Introduces syntax errors
```

#### 4. SnapshotBuilder (builder.py)

**Responsibilities:**
- Create incremental snapshot archives
- Track captured POVs, patches, corpus
- Generate full logs and configs
- Compress to tar.gz with completion markers

**Key Methods:**
```python
class SnapshotBuilder:
    captured_povs: Set[str]          # Incremental tracking
    captured_patches: Set[str]       # Incremental tracking
    last_corpus_mtime: float         # Incremental tracking

    def build_snapshot(cycle, elapsed, timeline) -> Path
    def _write_incremental_povs(temp_dir, events)
    def _write_incremental_patches(temp_dir, events)
    def _write_full_logs(temp_dir, events)
    def _create_tar_gz(source_dir, archive_path)
```

### Data Flow

```
Benchmark Ground Truth (.aixcc/)
    ↓
Read meta.yaml, POVs, patches, hints
    ↓
Create DiscoveryTimeline (difficulty-based timing)
    ↓
Optional: Inject invalid POVs/patches
    ↓
Generate snapshots at intervals
    ↓
For each snapshot cycle:
  - Get events before elapsed time
  - Write incremental POVs/patches (only new)
  - Write full logs/configs
  - Compress to tar.gz
  - Create completion marker
    ↓
Output: trial-N/snapshot-NNNN.tar.gz files
```

## Benchmark Ground Truth Reading

### Directory Structure

```
benchmarks/[benchmark-name]/.aixcc/
├── meta.yaml                        # Benchmark configuration
├── [harness-name]/                  # Per-harness directories
│   └── [vuln-keyword]/              # Per-vulnerability directories
│       ├── blobs/                   # POV binary files
│       │   ├── pov_0.blob
│       │   ├── pov_1.blob
│       │   └── ...
│       ├── logs/                    # Sanitizer/crash logs
│       │   ├── pov_0.log
│       │   ├── pov_1.log
│       │   └── ...
│       ├── patches/                 # Bug-fixing patches
│       │   ├── patch_0.diff
│       │   ├── patch_1.diff
│       │   └── ...
│       ├── hints/                   # Progressive difficulty hints
│       │   ├── level_1.sarif
│       │   ├── level_2.sarif
│       │   └── ...
│       ├── vuln.yaml               # Structured metadata
│       └── vuln_analysis.md        # Human-readable description
```

### meta.yaml Parsing

```python
class BenchmarkMetadata:
    """Parsed from .aixcc/meta.yaml"""

    # Mode configuration (one of these present)
    delta_mode: Optional[DeltaMode]
    full_mode: Optional[FullMode]

    # Harness specifications
    harness_files: List[HarnessSpec]

    # Patch exclusion rules
    patch_exclude_list: List[str]

class HarnessSpec:
    name: str                    # e.g., "CompressTarFuzzer"
    path: str                    # e.g., "$REPO/test/fuzz.c"
    vulns: List[VulnSpec]        # Vulnerabilities for this harness

class VulnSpec:
    vuln_keyword: str            # e.g., "cpv_0"
    difficulty_level: int        # 1-5
    povs: List[POVSpec]          # POV variants

class POVSpec:
    id: str                      # e.g., "pov_0"
    sanitizer: str               # e.g., "address"
    error_token: Optional[str]   # For matching/deduplication
```

### Reading Ground Truth Files

```python
def load_benchmark_ground_truth(benchmark_path: Path) -> BenchmarkData:
    """Load all ground truth data from benchmark."""

    # Parse meta.yaml
    meta = parse_meta_yaml(benchmark_path / ".aixcc" / "meta.yaml")

    # Load POVs for each vulnerability
    povs = {}
    for harness in meta.harness_files:
        for vuln in harness.vulns:
            vuln_dir = benchmark_path / ".aixcc" / harness.name / vuln.vuln_keyword

            # Read POV blobs
            for pov_spec in vuln.povs:
                blob_path = vuln_dir / "blobs" / f"{pov_spec.id}.blob"
                log_path = vuln_dir / "logs" / f"{pov_spec.id}.log"

                povs[(harness.name, vuln.vuln_keyword, pov_spec.id)] = POVData(
                    blob=blob_path.read_bytes(),
                    log=log_path.read_text(),
                    sanitizer=pov_spec.sanitizer,
                    error_token=pov_spec.error_token
                )

            # Read patches
            patch_files = list((vuln_dir / "patches").glob("*.diff"))
            for patch_file in patch_files:
                patches[(harness.name, vuln.vuln_keyword, patch_file.stem)] = \
                    patch_file.read_text()

    return BenchmarkData(meta=meta, povs=povs, patches=patches)
```

## Discovery Timeline Model

### Difficulty-Based POV Discovery Timing

```python
class POVDiscoveryModel:
    """Model realistic POV discovery based on difficulty."""

    DIFFICULTY_TIMING = {
        1: (0.10, 0.30),  # Easy: 10-30% of trial time
        2: (0.25, 0.50),  # Medium-Low: 25-50%
        3: (0.30, 0.60),  # Medium: 30-60%
        4: (0.50, 0.75),  # Hard: 50-75%
        5: (0.50, 0.90),  # Very Hard: 50-90%
    }

    def get_discovery_times(
        self,
        difficulty: int,
        pov_count: int,
        max_time: float
    ) -> List[float]:
        """Generate realistic discovery times for POVs.

        Multiple POVs for same root cause are clustered with jitter.
        """
        min_pct, max_pct = self.DIFFICULTY_TIMING[difficulty]

        # Base discovery time (when first POV found)
        base_time = max_time * random.uniform(min_pct, max_pct)

        # Cluster other POVs around base time (±5-10%)
        times = []
        for i in range(pov_count):
            if i == 0:
                times.append(base_time)
            else:
                jitter = max_time * random.uniform(-0.05, 0.10)
                clustered_time = base_time + jitter
                times.append(max(0, min(clustered_time, max_time)))

        return sorted(times)
```

**Rationale:**
- **Difficulty → Timing**: Harder bugs discovered later in trial
- **POV Clustering**: Multiple POVs from same root cause found close together
- **Realistic Spread**: Some randomness within difficulty range
- **Never exceed max_time**: All discoveries within trial duration

### Patch Generation Timing

```python
class PatchGenerationModel:
    """Model patch generation after POV discovery."""

    DIFFICULTY_DELAYS = {
        1: (300, 900),      # Easy: 5-15 minutes after POV
        2: (600, 1200),     # Medium-Low: 10-20 minutes
        3: (900, 1800),     # Medium: 15-30 minutes
        4: (1200, 2400),    # Hard: 20-40 minutes
        5: (1800, 3600),    # Very Hard: 30-60 minutes
    }

    def get_patch_time(
        self,
        first_pov_time: float,
        difficulty: int,
        max_time: float
    ) -> float:
        """Generate patch time after POV discovery.

        Patch generated after analyzing POVs, with delay based on difficulty.
        """
        min_delay, max_delay = self.DIFFICULTY_DELAYS[difficulty]
        delay = random.uniform(min_delay, max_delay)

        patch_time = first_pov_time + delay
        return min(patch_time, max_time)  # Don't exceed trial duration
```

**Rationale:**
- **Delay after POV**: CRS needs time to analyze and generate patch
- **Difficulty → Delay**: Harder bugs take longer to patch
- **Bounded**: Patch time never exceeds trial duration

### Timeline Construction

```python
def create_discovery_timeline(
    benchmark_data: BenchmarkData,
    difficulty_level: int,
    max_time: float,
    mode: str = 'bug-finding'
) -> DiscoveryTimeline:
    """Create timeline of POV/patch discoveries."""

    timeline = DiscoveryTimeline()
    pov_model = POVDiscoveryModel()
    patch_model = PatchGenerationModel()

    for harness in benchmark_data.meta.harness_files:
        for vuln in harness.vulns:
            # Get POV count for this vulnerability
            pov_ids = [pov.id for pov in vuln.povs]

            # Generate discovery times
            discovery_times = pov_model.get_discovery_times(
                difficulty=difficulty_level,
                pov_count=len(pov_ids),
                max_time=max_time
            )

            # Add POV discoveries to timeline
            for pov_id, time in zip(pov_ids, discovery_times):
                pov_data = benchmark_data.povs[(harness.name, vuln.vuln_keyword, pov_id)]
                timeline.add_pov(
                    time=time,
                    pov_blob=pov_data.blob,
                    harness=harness.name,
                    vuln=vuln.vuln_keyword,
                    pov_id=pov_id,
                    is_valid=True
                )

            # Add patch generation (for patch mode or after POVs)
            if mode == 'patch-generation' and discovery_times:
                first_pov_time = min(discovery_times)
                patch_time = patch_model.get_patch_time(
                    first_pov_time,
                    difficulty_level,
                    max_time
                )

                # Get patch (usually one patch fixes all POV variants)
                patch_key = (harness.name, vuln.vuln_keyword, 'patch_0')
                if patch_key in benchmark_data.patches:
                    timeline.add_patch(
                        time=patch_time,
                        patch_diff=benchmark_data.patches[patch_key],
                        harness=harness.name,
                        vuln=vuln.vuln_keyword,
                        patch_id='patch_0',
                        is_valid=True
                    )

    return timeline
```

## Snapshot Generation Strategy

### Incremental vs Full Capture

Following the established snapshot format:

| Data Type | Strategy | Tracking Method | Rationale |
|-----------|----------|-----------------|-----------|
| POVs | Incremental | Filename set | File-based, clear IDs |
| Patches | Incremental | Filename set | POV-organized structure |
| Corpus | Incremental | mtime | New/modified files |
| LLM logs | Full | N/A | Complex JSON, easier full copy |
| CRS logs | Full | N/A | Users want complete log |
| Config | Full | N/A | Static, copy once |

### Snapshot Archive Structure

```
snapshot-0001.tar.gz contains:
├── metadata.json              # Cycle, timestamp, elapsed_time, snapshot_period
├── povs/                      # Incremental: new POVs only
│   ├── pov_001               # Binary blob (no extension)
│   └── pov_002
├── patches/                   # Incremental: organized by POV ID
│   └── pov_0/
│       └── patch.diff
├── corpus/                    # Incremental: new/modified files (optional)
│   └── input-001
├── config.yaml                # Full: experiment configuration
├── execution.json             # Full: execution metadata
├── llm-usage.json            # Full: cumulative LLM metrics
└── crs-output.log            # Full: complete CRS log from start
```

### Incremental Tracking Logic

```python
class SnapshotBuilder:
    """Build snapshots with incremental tracking."""

    def __init__(self):
        self.captured_povs: Set[str] = set()
        self.captured_patches: Set[str] = set()
        self.last_corpus_mtime: float = 0.0

    def _write_incremental_povs(
        self,
        temp_dir: Path,
        events: List[DiscoveryEvent]
    ):
        """Write only new POVs not in previous snapshots."""
        pov_dir = temp_dir / "povs"
        pov_dir.mkdir(exist_ok=True)

        for event in events:
            if event.event_type != 'pov':
                continue

            pov_id = event.metadata['pov_id']
            if pov_id in self.captured_povs:
                continue  # Already captured in previous snapshot

            # Write new POV
            pov_file = pov_dir / pov_id
            pov_file.write_bytes(event.data)
            self.captured_povs.add(pov_id)

    def _write_incremental_patches(
        self,
        temp_dir: Path,
        events: List[DiscoveryEvent]
    ):
        """Write only new patches organized by POV ID."""
        patches_dir = temp_dir / "patches"

        for event in events:
            if event.event_type != 'patch':
                continue

            vuln_id = event.metadata['vuln']
            patch_id = event.metadata['patch_id']
            patch_key = f"{vuln_id}/{patch_id}"

            if patch_key in self.captured_patches:
                continue  # Already captured

            # Create POV subdirectory (patches organized by vulnerability)
            pov_patch_dir = patches_dir / vuln_id
            pov_patch_dir.mkdir(parents=True, exist_ok=True)

            # Write patch
            patch_file = pov_patch_dir / f"{patch_id}.diff"
            patch_file.write_text(event.data)
            self.captured_patches.add(patch_key)
```

### Full Capture Logic

```python
def _write_full_logs(self, temp_dir: Path, events: List[DiscoveryEvent]):
    """Write full logs (complete at each snapshot)."""

    # LLM usage (cumulative)
    self._write_llm_usage(temp_dir, events)

    # CRS log (complete from start)
    self._write_crs_log(temp_dir, events)

    # Config (static)
    self._write_config(temp_dir)

    # Execution metadata (static)
    self._write_execution_metadata(temp_dir)

def _write_llm_usage(self, temp_dir: Path, events: List[DiscoveryEvent]):
    """Generate realistic cumulative LLM usage metrics."""

    # Correlate LLM usage with POV/patch discoveries
    pov_count = len([e for e in events if e.event_type == 'pov'])
    patch_count = len([e for e in events if e.event_type == 'patch'])

    # Realistic token counts
    base_tokens = 10000  # Base analysis
    tokens_per_pov = 5000  # POV analysis
    tokens_per_patch = 15000  # Patch generation

    total_tokens = base_tokens + (pov_count * tokens_per_pov) + \
                   (patch_count * tokens_per_patch)

    llm_usage = {
        "total_api_calls": pov_count * 10 + patch_count * 20,
        "total_input_tokens": int(total_tokens * 0.7),
        "total_output_tokens": int(total_tokens * 0.3),
        "total_cached_tokens": int(total_tokens * 0.4),
        "total_cost_usd": round(total_tokens * 0.00003, 4),
        # ... model breakdown
    }

    (temp_dir / "llm-usage.json").write_text(json.dumps(llm_usage, indent=2))

def _write_crs_log(self, temp_dir: Path, events: List[DiscoveryEvent]):
    """Generate realistic CRS log showing discovery progression."""

    log_lines = [
        "[2025-01-15 10:00:00] INFO: CRS starting up",
        "[2025-01-15 10:00:05] INFO: Initializing fuzzing engine",
    ]

    # Add log entries for each POV discovery
    for event in sorted(events, key=lambda e: e.timestamp):
        if event.event_type == 'pov':
            time_str = self._format_timestamp(event.timestamp)
            pov_id = event.metadata['pov_id']
            log_lines.extend([
                f"{time_str} INFO: Generated 1000 test cases",
                f"{time_str} INFO: Found crash: {event.metadata.get('sanitizer', 'unknown')}",
                f"{time_str} INFO: Analyzing crash with LLM",
                f"{time_str} INFO: Generated POV candidate {pov_id}",
            ])
        elif event.event_type == 'patch':
            time_str = self._format_timestamp(event.timestamp)
            patch_id = event.metadata['patch_id']
            log_lines.extend([
                f"{time_str} INFO: Analyzing vulnerability root cause",
                f"{time_str} INFO: Generating patch with LLM",
                f"{time_str} INFO: Created patch {patch_id}",
            ])

    (temp_dir / "crs-output.log").write_text('\n'.join(log_lines) + '\n')
```

## Fault Injection

### Purpose

Test evaluation pipeline robustness by injecting invalid POVs/patches:
- **POV validation**: Does deduplication work correctly?
- **Patch validation**: Are invalid patches rejected?
- **Scoring**: Is scoring affected by invalid data?
- **Robustness**: Can evaluation handle bad data gracefully?

### Invalid POV Generation

```python
class FaultInjector:
    """Inject invalid POVs/patches for testing."""

    def __init__(self, fault_rate: float = 0.1):
        self.fault_rate = fault_rate
        self.invalid_pov_counter = 0
        self.invalid_patch_counter = 0

    def should_inject(self) -> bool:
        """Probabilistically decide if fault should be injected."""
        return random.random() < self.fault_rate

    def create_invalid_pov(self) -> DiscoveryEvent:
        """Create POV that won't trigger crash.

        Invalid POV characteristics:
        - Random data (not crafted to trigger vulnerability)
        - Won't match any sanitizer error patterns
        - Should be rejected by POV validation
        """
        self.invalid_pov_counter += 1

        # Generate random data
        blob = random.randbytes(random.randint(64, 512))

        return DiscoveryEvent(
            timestamp=0,  # Set by caller
            event_type='pov',
            data=blob,
            metadata={
                'pov_id': f'invalid_pov_{self.invalid_pov_counter}',
                'harness': 'unknown',
                'vuln': 'unknown',
                'is_valid': False,  # Track for testing
                'fault_type': 'invalid_pov'
            },
            is_valid=False
        )
```

### Invalid Patch Generation

```python
def create_invalid_patch(self, fault_type: str) -> DiscoveryEvent:
    """Create invalid patch with specified fault type."""
    self.invalid_patch_counter += 1

    if fault_type == 'syntax_error':
        # Malformed diff (invalid unified diff format)
        patch_diff = """--- a/src/main.c
+++ b/src/main.c
@ -10,5 +10,5  # Invalid hunk header
-    old line
+    new line
"""

    elif fault_type == 'wrong_file':
        # Patches a file that doesn't exist or unrelated to vulnerability
        patch_diff = """--- a/nonexistent_file.c
+++ b/nonexistent_file.c
@@ -1,3 +1,3 @@
-old code
+new code
"""

    elif fault_type == 'incomplete':
        # Patches only some vulnerable code, doesn't fix all POVs
        patch_diff = """--- a/src/parser.c
+++ b/src/parser.c
@@ -45,7 +45,7 @@
 void parse_input(char *input, size_t len) {
-    // Missing bound check fix here
+    if (len > MAX_SIZE) return;  // Only partial fix
 }
"""

    elif fault_type == 'breaks_build':
        # Introduces syntax error in code
        patch_diff = """--- a/src/parser.c
+++ b/src/parser.c
@@ -45,7 +45,7 @@
 void parse_input(char *input, size_t len) {
-    char buffer[256];
+    char buffer[512]  // Missing semicolon
 }
"""

    return DiscoveryEvent(
        timestamp=0,  # Set by caller
        event_type='patch',
        data=patch_diff,
        metadata={
            'patch_id': f'invalid_patch_{self.invalid_patch_counter}',
            'vuln': 'unknown',
            'is_valid': False,
            'fault_type': fault_type
        },
        is_valid=False
    )
```

### Fault Injection Strategy

```python
def inject_faults_into_timeline(
    timeline: DiscoveryTimeline,
    fault_injector: FaultInjector,
    max_time: float
):
    """Inject invalid POVs/patches into timeline."""

    # Count valid events
    valid_povs = [e for e in timeline.events if e.event_type == 'pov' and e.is_valid]
    valid_patches = [e for e in timeline.events if e.event_type == 'patch' and e.is_valid]

    # Inject invalid POVs (randomly scattered)
    invalid_pov_count = int(len(valid_povs) * fault_injector.fault_rate)
    for _ in range(invalid_pov_count):
        invalid_pov = fault_injector.create_invalid_pov()
        invalid_pov.timestamp = random.uniform(0, max_time)
        timeline.events.append(invalid_pov)

    # Inject invalid patches (randomly scattered)
    invalid_patch_count = int(len(valid_patches) * fault_injector.fault_rate)
    fault_types = ['syntax_error', 'wrong_file', 'incomplete', 'breaks_build']
    for _ in range(invalid_patch_count):
        fault_type = random.choice(fault_types)
        invalid_patch = fault_injector.create_invalid_patch(fault_type)
        invalid_patch.timestamp = random.uniform(0, max_time)
        timeline.events.append(invalid_patch)

    # Re-sort timeline by timestamp
    timeline.events.sort(key=lambda e: e.timestamp)
```

## CLI Interface Design

### Command Structure

```bash
python -m crsbench.bench_snapgen.generator [OPTIONS]
```

### Arguments

```
Required:
  --benchmark PATH              Path to benchmark directory
  --output PATH                 Output directory for generated snapshots

Optional:
  --duration SECONDS            Trial duration in seconds (default: 7200)
  --snapshot-period SECONDS     Snapshot interval in seconds (default: 900)
  --mode {bug-finding,patch-generation}
                               Generation mode (default: bug-finding)
  --difficulty LEVEL            Difficulty level 1-5 (default: 1)

Timing Control:
  --discovery-model {probabilistic,uniform,early,late}
                               Discovery timing model (default: probabilistic)
  --pov-jitter FLOAT           POV clustering jitter factor (default: 0.05)

Fault Injection:
  --fault-injection-rate FLOAT  Rate of invalid data injection (default: 0)
  --fault-types TYPE[,TYPE]     Comma-separated fault types to inject

Output Control:
  --compress / --no-compress   Compress snapshots to tar.gz (default: compress)
  --include-corpus             Generate synthetic corpus files
  --llm-simulation             Generate realistic LLM usage (default: True)

Multiple Trials:
  --trials COUNT               Generate N independent trials (default: 1)
  --difficulty-range START-END  Generate trials at each difficulty level
```

### Usage Examples

```bash
# Basic usage - single trial, bug-finding mode
python -m crsbench.bench_snapgen.generator \
    --benchmark benchmarks/apache-commons-compress-delta-01 \
    --output /tmp/simulated-trial

# Patch generation mode with medium difficulty
python -m crsbench.bench_snapgen.generator \
    --benchmark benchmarks/atlanta-bcel-delta-01 \
    --output /tmp/patch-trial \
    --mode patch-generation \
    --difficulty 3

# With fault injection (10% invalid data)
python -m crsbench.bench_snapgen.generator \
    --benchmark benchmarks/apache-poi-full-01 \
    --output /tmp/trial-with-faults \
    --fault-injection-rate 0.10 \
    --fault-types syntax_error,incomplete

# Generate multiple trials at different difficulties
python -m crsbench.bench_snapgen.generator \
    --benchmark benchmarks/wireshark-delta-01 \
    --output /tmp/multi-difficulty-trials \
    --trials 3 \
    --difficulty-range 1-5

# Custom timing: late discovery with high jitter
python -m crsbench.bench_snapgen.generator \
    --benchmark benchmarks/curl-delta-01 \
    --output /tmp/late-discovery \
    --discovery-model late \
    --pov-jitter 0.15
```

### Configuration File Support

```yaml
# snapgen-config.yaml
benchmark: benchmarks/apache-commons-compress-delta-01
output: /tmp/simulated-trials
duration: 7200
snapshot_period: 900
mode: bug-finding
difficulty: 2

# Timing
discovery_model: probabilistic
pov_jitter: 0.05

# Fault injection
fault_injection_rate: 0.10
fault_types:
  - syntax_error
  - incomplete

# Output
compress: true
include_corpus: false
llm_simulation: true

# Multiple trials
trials: 3
```

```bash
# Use configuration file
python -m crsbench.bench_snapgen.generator --config snapgen-config.yaml
```

## Testing Strategy

### Unit Tests

```python
# tests/test_bench_snapgen.py

class TestBenchmarkReading:
    def test_read_meta_yaml():
        """Test parsing meta.yaml from real benchmark."""

    def test_load_pov_blobs():
        """Test loading POV binary files."""

    def test_load_patches():
        """Test loading patch diffs."""

    def test_handle_delta_mode():
        """Test delta_mode benchmark."""

    def test_handle_full_mode():
        """Test full_mode benchmark."""

class TestDiscoveryTimeline:
    def test_difficulty_based_timing():
        """Test POV discovery varies by difficulty."""

    def test_pov_clustering():
        """Test multiple POVs clustered together."""

    def test_patch_timing_after_pov():
        """Test patch comes after POV with realistic delay."""

    def test_timeline_sorting():
        """Test events sorted by timestamp."""

class TestFaultInjection:
    def test_invalid_pov_generation():
        """Test invalid POV creation."""

    def test_invalid_patch_types():
        """Test all invalid patch types."""

    def test_fault_injection_rate():
        """Test fault rate is approximately correct."""

    def test_fault_tracking():
        """Test is_valid flag tracks faults."""

class TestSnapshotBuilder:
    def test_incremental_pov_tracking():
        """Test POVs only written once."""

    def test_incremental_patch_tracking():
        """Test patches only written once."""

    def test_full_log_capture():
        """Test logs are complete at each snapshot."""

    def test_snapshot_format():
        """Test snapshot format matches expected structure."""

    def test_compression():
        """Test tar.gz compression works."""

    def test_completion_markers():
        """Test .complete files created."""
```

### Integration Tests

```python
class TestGeneratorIntegration:
    def test_generate_from_real_benchmark():
        """Test complete generation from actual benchmark."""
        benchmark_path = Path("benchmarks/atlanta-bcel-delta-01")
        output_dir = tmp_path / "trial"

        generator = BenchmarkSnapshotGenerator(
            benchmark_path=benchmark_path,
            output_dir=output_dir,
            trial_duration=3600,
            snapshot_period=600
        )

        generator.generate_trial_snapshots(
            mode='bug-finding',
            difficulty_level=2
        )

        # Verify snapshots created
        snapshots = list(output_dir.glob("snapshot-*.tar.gz"))
        assert len(snapshots) == 6  # 3600s / 600s

        # Verify completion markers
        for snapshot in snapshots:
            marker = snapshot.parent / f"{snapshot.stem}.complete"
            assert marker.exists()

    def test_snapshot_validation():
        """Test generated snapshots pass existing validators."""
        # Generate snapshots
        output_dir = generate_snapshots(...)

        # Validate using existing snapshot validator
        from crsbench.evaluation.snapshot import validate_snapshot_structure

        for snapshot in output_dir.glob("snapshot-*.tar.gz"):
            assert validate_snapshot_structure(snapshot)

    def test_bug_finding_mode():
        """Test bug-finding mode generates POVs only."""
        generator.generate_trial_snapshots(mode='bug-finding')

        # Check final snapshot
        snapshot = extract_final_snapshot(output_dir)
        assert len(list(snapshot.glob("povs/*"))) > 0
        assert len(list(snapshot.glob("patches/*"))) == 0

    def test_patch_generation_mode():
        """Test patch mode generates both POVs and patches."""
        generator.generate_trial_snapshots(mode='patch-generation')

        # Check final snapshot
        snapshot = extract_final_snapshot(output_dir)
        assert len(list(snapshot.glob("povs/*"))) > 0
        assert len(list(snapshot.glob("patches/*"))) > 0
```

## Implementation Plan

### Phase 1: Core Infrastructure (Est: 2-3 days)

**Goal**: Read benchmark ground truth and generate basic snapshots

**Tasks**:
1. Create module structure: `crsbench/bench_snapgen/`
2. Implement `BenchmarkMetadata` parsing from `meta.yaml`
3. Implement ground truth file loading (POVs, patches, hints)
4. Implement basic `DiscoveryTimeline` (uniform timing, no models yet)
5. Implement `SnapshotBuilder` (reuse logic from snapshot-examples)
6. Generate basic snapshots without realistic timing

**Deliverables**:
- Can read actual benchmark ground truth
- Can generate snapshot archives at configured intervals
- Basic incremental tracking works
- Snapshots pass format validation

### Phase 2: Realistic Timing Models (Est: 2-3 days)

**Goal**: Implement difficulty-based discovery timing

**Tasks**:
1. Implement `POVDiscoveryModel` with difficulty-based timing
2. Implement POV clustering logic (same root cause)
3. Implement `PatchGenerationModel` with delays
4. Generate realistic LLM usage correlated with discoveries
5. Generate realistic CRS logs showing timeline
6. Add timing configuration options

**Deliverables**:
- POVs discovered at realistic times based on difficulty
- Multiple POVs clustered appropriately
- Patches appear after POVs with realistic delay
- LLM usage and logs correlate with discoveries

### Phase 3: Fault Injection (Est: 1-2 days)

**Goal**: Support invalid POV/patch injection for testing

**Tasks**:
1. Implement `FaultInjector` class
2. Implement invalid POV generation
3. Implement invalid patch generation (all fault types)
4. Add fault injection to timeline
5. Track valid vs invalid data for verification
6. Add fault injection configuration options

**Deliverables**:
- Can inject configurable % of invalid POVs
- Can inject different types of invalid patches
- Fault tracking allows verification
- Configuration controls fault injection

### Phase 4: Testing and Polish (Est: 2-3 days)

**Goal**: Comprehensive testing and production-ready

**Tasks**:
1. Write comprehensive unit tests (>80% coverage)
2. Write integration tests with real benchmarks
3. Test with multiple benchmark types (delta, full, C, Java)
4. Implement CLI interface with argument parsing
5. Add configuration file support
6. Write README with usage examples
7. Add logging and progress reporting

**Deliverables**:
- All tests passing
- CLI tool works with real benchmarks
- Documentation complete
- Ready for production use

## Summary

The `bench_snapgen` module provides ground truth-based snapshot generation for:
- **Testing**: Validate evaluation pipelines without expensive CRS runs
- **Demonstration**: Show snapshot format with real benchmark data
- **Fault injection**: Test robustness with invalid data
- **Development**: Rapid iteration on snapshot processing

**Key features**:
- Reads actual benchmark ground truth from `.aixcc/` directories
- Generates realistic discovery timelines based on difficulty
- Supports both bug-finding and patch-generation modes
- Configurable fault injection for testing
- Compatible with existing snapshot format and validators
- CLI tool for easy integration

**Implementation**: ~8-11 days for complete, production-ready module with comprehensive testing.
