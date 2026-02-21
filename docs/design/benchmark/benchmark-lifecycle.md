# Benchmark Lifecycle

This document describes the complete benchmark lifecycle in CRSBench, from creation to evaluation.

## Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         Benchmark Lifecycle                                 │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  [1. GENERATION]        [2. PACKAGING]        [3. PUBLISH]   [4. RUNTIME]   │
│                                                                             │
│  Create benchmark       Bundle for            Distribute     Load for       │
│  from sources           distribution          to users       evaluation     │
│  ─────────────          ────────────          ──────────     ──────────     │
│  • oss-fuzz-vuln        • Source tarball      • Git repo     • Detect pkgs/ │
│  • CVE/NVD              • ref.diff            • Release      • Clone or use │
│  • Bug bounties         • Validation          • Registry     • Provide CRS  │
│  • Manual input         • Provenance                                        │
│                                                                             │
│  Adapters convert       Packaging prepares    Published      Runtime loads  │
│  to standard format     for offline use       benchmarks     for execution  │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Module Structure

```
crsbench/benchmark/
├── __init__.py                    # Top-level exports
│
├── generation/                    # Phase 1: CREATE benchmarks
│   ├── __init__.py
│   ├── README.md                  # Future architecture
│   └── (future: adapters/)        # Convert external sources
│
├── packaging/                     # Phase 2: PACKAGE for distribution
│   ├── __init__.py
│   ├── tarball.py                 # Source tarball creation
│   ├── bundle.py                  # High-level bundling
│   ├── validate.py                # Structure validation
│   ├── workdir_parser.py          # Dockerfile WORKDIR parsing
│   └── cli/                       # CLI commands
│       └── benchmark_command.py
│
└── runtime/                       # Phase 4: LOAD for evaluation
    ├── __init__.py
    ├── loader.py                  # Source loading logic
    └── models.py                  # BenchmarkSource dataclass
```

## Phase 1: Generation (Future)

**Purpose**: Create new benchmarks from external vulnerability sources.

**Status**: Not yet implemented. See `docs/modules/benchmark/generation.md` for planned architecture.

### Planned Adapters

| Adapter | Source | Description |
|---------|--------|-------------|
| `OSSFuzzVulnAdapter` | oss-fuzz-vuln | Google's OSS-Fuzz vulnerability database |
| `CVEAdapter` | NVD/CVE | National Vulnerability Database |
| `BugBountyAdapter` | HackerOne, etc. | Bug bounty platform reports |
| `ManualAdapter` | Interactive | Wizard for manual benchmark creation |

### Workflow

```
External Source ──► Adapter ──► VulnerabilityInfo ──► Generator ──► Benchmark Directory
                                (normalized)                        (ready for packaging)
```

### Future CLI

```bash
crsbench benchmark generate --from ossfuzz-vuln --id OSV-2024-1234
crsbench benchmark generate --from cve --id CVE-2024-12345
crsbench benchmark generate --interactive
```

## Phase 2: Packaging

**Purpose**: Bundle benchmark source into distributable tarballs.

### Components

#### `workdir_parser.py`

Parses Dockerfile to determine expected source directory name.

```python
from crsbench.benchmark.packaging import get_expected_source_dir

# Returns "curl" from: WORKDIR $SRC/curl
source_name = get_expected_source_dir(dockerfile_path)
```

Handles OSS-Fuzz conventions:
- `WORKDIR $SRC/curl` → `curl`
- `WORKDIR ${SRC}/curl` → `curl`
- `WORKDIR /src/curl` → `curl`
- `WORKDIR libtiff` (relative) → `libtiff`

#### `tarball.py`

Creates source tarballs with fresh git history.

```python
from crsbench.benchmark.packaging import create_source_tarball

tarball_path, ref_diff_path = create_source_tarball(
    repo_url="https://github.com/curl/curl",
    base_commit="abc123",
    source_name="curl",
    output_dir=Path("./pkgs"),
    ref_commit="def456",  # Optional, for delta mode
)
```

Features:
- Fresh `git init` with single commit (no history leakage)
- Fixed author/date for reproducibility
- Generates `ref.diff` for delta mode

#### `validate.py`

Validates benchmark structure and format.

```python
from crsbench.benchmark.packaging import validate_benchmark, ValidationResult

result = validate_benchmark(benchmark_path)
if result.valid:
    print("Benchmark is valid")
else:
    print(f"Errors: {result.errors}")
    print(f"Warnings: {result.warnings}")
```

Checks:
- Required files: `Dockerfile`, `project.yaml`, `.aixcc/meta.yaml`
- meta.yaml format (nested or flat)
- pkgs/ structure if exists

Supports two meta.yaml formats:

```yaml
# Nested format (current standard)
delta_mode:
  base_commit: abc123
  ref_commit: def456
full_mode:
  base_commit: def456

# Flat format (legacy)
base_commit: abc123
ref_commit: def456
```

#### `bundle.py`

High-level bundling interface.

```python
from crsbench.benchmark.packaging import bundle_benchmark

pkgs_dir = bundle_benchmark(
    benchmark_path,
    force=True,  # Overwrite existing pkgs/
)
```

Workflow:
1. Validate benchmark structure
2. Extract repo info from project.yaml and meta.yaml
3. Determine source name from Dockerfile WORKDIR
4. Clone repo, checkout base_commit, create tarball
5. Generate ref.diff if delta mode
6. Write pkg_refs.txt for provenance

### CLI Commands

```bash
# Validate benchmark structure
crsbench benchmark validate ./benchmarks/afc-curl-delta-01

# Create pkgs/ tarball
crsbench benchmark bundle ./benchmarks/afc-curl-delta-01

# Generate ref.diff only
crsbench benchmark prepare-delta ./benchmarks/afc-curl-delta-01
```

### Output Structure

After packaging, a benchmark contains:

```
benchmarks/afc-curl-delta-01/
├── Dockerfile
├── project.yaml
├── .aixcc/
│   ├── meta.yaml
│   └── ref.diff              # Generated for delta mode
└── pkgs/
    ├── curl.tar.gz           # Source tarball (matches WORKDIR)
    └── pkg_refs.txt          # Provenance: "https://github.com/curl/curl@abc123"
```

## Phase 3: Publish

**Purpose**: Distribute packaged benchmarks to users.

### Distribution Methods

1. **Git Repository**: Benchmarks committed with pkgs/ included
2. **Releases**: Tagged releases with benchmark archives
3. **Registry**: Centralized benchmark registry (future)

### Provenance

Each packaged benchmark includes `pkgs/pkg_refs.txt`:

```
https://github.com/curl/curl@abc123def456789...
```

This enables:
- Verification of source origin
- Reproducibility audits
- License compliance tracking

## Phase 4: Runtime

**Purpose**: Load benchmarks for CRS evaluation.

### Components

#### `models.py`

Data model for loaded benchmark source.

```python
from crsbench.benchmark.runtime import BenchmarkSource

# Bundled source (from pkgs/)
source = BenchmarkSource(path=None, is_bundled=True)
assert source.requires_source_path is False

# Cloned source
source = BenchmarkSource(path=Path("/tmp/src"), is_bundled=False)
assert source.requires_source_path is True
```

#### `loader.py`

Unified source loading interface.

```python
from crsbench.benchmark.runtime import load_benchmark_source, has_bundled_source

# Check if benchmark has pkgs/
if has_bundled_source(benchmark_path):
    print("Using bundled source")

# Load source (auto-detects pkgs/ vs clone)
source = load_benchmark_source(
    benchmark_path,
    dest_dir=trial_dir / "src",  # Required if cloning
    mode="delta",
    verbose=True,
)

# Use in executor
if source.requires_source_path:
    cmd.extend(["--source-path", str(source.path)])
```

### Flow Diagram

```
                     ┌─────────────────────┐
                     │  load_benchmark_    │
                     │      source()       │
                     └──────────┬──────────┘
                                │
                     ┌──────────▼──────────┐
                     │  has_bundled_       │
                     │     source()?       │
                     └──────────┬──────────┘
                                │
              ┌─────────────────┼─────────────────┐
              │ YES             │                 │ NO
              ▼                 │                 ▼
   ┌──────────────────┐         │      ┌──────────────────┐
   │ BenchmarkSource  │         │      │ Clone via        │
   │ path=None        │         │      │ repo_manager     │
   │ is_bundled=True  │         │      └────────┬─────────┘
   └──────────────────┘         │               │
              │                 │               ▼
              │                 │      ┌──────────────────┐
              │                 │      │ BenchmarkSource  │
              │                 │      │ path=/tmp/src    │
              │                 │      │ is_bundled=False │
              │                 │      └──────────────────┘
              │                 │               │
              └─────────────────┴───────────────┘
                                │
                     ┌──────────▼──────────┐
                     │  Executor uses      │
                     │  source.path or     │
                     │  Docker's built-in  │
                     └─────────────────────┘
```

### Integration with Executors

Executors use the runtime module to load source:

```python
# In crs_patch_executor.py or crs_bug_finding_executor.py

from crsbench.benchmark.runtime import load_benchmark_source

source = load_benchmark_source(
    benchmark_path,
    dest_dir=trial_build_dir / "src",
    verbose=self.config.get("verbose", False),
)

# Build CRS command
cmd = ["oss-crs", "build", ...]

# Only add --source-path if not using bundled source
if source.requires_source_path:
    cmd.extend(["--source-path", str(source.path)])
```

## Design Decisions

### Why Fresh Git Init?

CRS tools often use git commands internally. The tarball includes a fresh git repo with:
- Single commit (no history)
- Fixed author/date for reproducibility
- No sensitive information from original repo

### Why pkgs/ Detection in Runtime?

When `pkgs/` exists with tarballs:
1. Docker image builds with source baked in
2. No need to pass `--source-path` to CRS
3. Prevents overwriting Docker's source with external mount

### Why Separate Generation and Packaging?

- **Generation**: Creates benchmark metadata and structure from vulnerability info
- **Packaging**: Bundles existing benchmark for distribution

A benchmark might be:
- Generated from oss-fuzz-vuln, then packaged
- Manually created, then packaged
- Imported from another format, then packaged

## Future Extensions

### Runtime Module

Could expand to include:
- `runtime/hints.py` - Load SARIF reports and corpus
- `runtime/corpus.py` - Corpus management
- `runtime/pov.py` - POV file handling

### Generation Module

Planned adapters for:
- oss-fuzz-vuln database
- CVE/NVD entries
- Bug bounty reports
- Interactive wizard

## Related Documentation

- `docs/benchmark-spec.md` - RFC for benchmark format
- `docs/ossfuzz-crs-interface.md` - CRS interface specification
- `docs/modules/benchmark/generation.md` - Planned generation architecture
