# CRSBench Migration Module

## Script-Based Atlanta to RFC Migration

In addition to the LangGraph-based migration system described above, this module includes a direct script-based implementation for migrating Team-Atlanta format to CRSBench RFC format.

### Implementation

The script-based migrator consists of:

#### **atlanta_to_rfc.py**
Main CLI script for orchestrating migrations.

#### **config_converter.py**
Converts `config.yaml` to `meta.yaml` format.

#### **vuln_metadata_generator.py**
Generates vulnerability YAML files with mock data.

#### **file_migrator.py**
Handles file operations with dry-run support.

### Quick Start

```bash
# Dry run to validate
.venv/bin/python -m crsbench.migration.atlanta_to_rfc \
  --source-dir /path/to/oss-fuzz/projects \
  --target-dir /path/to/CRSBench/benchmarks \
  --dry-run

# Actual migration
.venv/bin/python -m crsbench.migration.atlanta_to_rfc \
  --source-dir /path/to/oss-fuzz/projects \
  --target-dir /path/to/CRSBench/benchmarks \
  --projects curl-delta-04
```

### Features

- ✅ Complete directory structure transformation
- ✅ Config.yaml to meta.yaml conversion
- ✅ Vulnerability metadata generation with mock data
- ✅ Multiple POV variant support
- ✅ Dry-run mode for validation
- ✅ Comprehensive logging
- ✅ CSV migration reports

### Design Documentation

See [migration design document](../../design-docs/migration/migration-atlanta-to-rfc.md) for implementation details.

---

## Test.sh Generator

Automated tool to generate `test.sh` functional test scripts for benchmarks using Claude Agent SDK.

### Overview

Security patch validation requires functional testing beyond vulnerability triggering. The test.sh generator automatically creates test scripts by:
1. Analyzing project repositories to find unit tests
2. Identifying build systems and test frameworks
3. Generating appropriate test.sh scripts for Docker environments

### Quick Start

#### Option 1: Using .env file (Recommended)

```bash
# Create .env file in project root
cat > .env << 'EOF'
LITELLM_BASE_URL=http://localhost:4000
LITELLM_API_KEY=your-api-key-here
PROJECT_REPOS_DIR=/home/acorn421/work/team-atlanta/afc-repos
EOF

# .env is automatically loaded by the tool
```

#### Option 2: Set environment variables manually

```bash
export LITELLM_BASE_URL="http://localhost:4000"
export LITELLM_API_KEY="your-api-key"
export PROJECT_REPOS_DIR="/home/acorn421/work/team-atlanta/afc-repos"
```

#### Generate test.sh for a benchmark

**Option 1: Auto-clone (simplest!)**
```bash
# Repository is automatically cloned if not found
python crsbench/migration/generate_test_sh.py \
  --benchmark apache-commons-compress-delta-01 \
  --verbose

# Cloned to PROJECT_REPOS_DIR/commons-compress
```

**Option 2: Specify existing project directory**
```bash
python crsbench/migration/generate_test_sh.py \
  --benchmark apache-commons-compress-delta-01 \
  --project-dir /path/to/commons-compress \
  --verbose
```

### Features

- ✅ **Auto-clone repositories**: Automatically clones project repos from benchmark config
- ✅ Automatic unit test discovery using Claude Agent SDK
- ✅ Multi-build system support (Maven, Make, CMake, Gradle)
- ✅ Docker environment handling (test exclusions, permission issues)
- ✅ Generates both test.sh and analysis documentation

### Testing the Agent

Two test scripts are provided:

#### Minimal Test (No Dependencies)
```bash
python crsbench/migration/test_agent_minimal.py
```
Basic structure validation without requiring Claude Agent SDK.

#### Full Test (Requires LiteLLM)
```bash
# Set up environment first
export LITELLM_BASE_URL="http://localhost:4000"
export LITELLM_API_KEY="your-api-key"

# Run full test suite
python crsbench/migration/test_agent_simple.py
```
Tests actual agent functionality including:
- Simple queries
- Tool usage (Read, Grep, Glob)
- Agent initialization

### Programmatic Usage

```python
from crsbench.migration.test_sh_generator import generate_test_sh_for_benchmark

result = generate_test_sh_for_benchmark(
    benchmark_name="apache-commons-compress-delta-01",
    benchmark_dir="benchmarks/apache-commons-compress-delta-01",
    project_dir="/path/to/commons-compress",
    verbose=True
)

if result["success"]:
    print(f"✅ Generated: {result['test_sh_path']}")
    print(f"📄 Analysis: {result['analysis_md_path']}")
```

### Design Documentation

See [test.sh generator design document](../../design-docs/migration/test-sh-generator.md) for architecture details.

---

## MCP-Enhanced test.sh Generator

An optional iterative approach to test.sh generation using Model Context Protocol (MCP) with Claude Agent SDK.

### Overview

The MCP-enhanced generator adds Docker testing capabilities to the standard test.sh generator:
1. Build Docker images and analyze build logs
2. Test generated test.sh scripts in containers
3. Iteratively refine based on test failures
4. Automatic retry until test.sh works

### Quick Start

```python
from crsbench.migration.test_sh_generator import generate_test_sh_for_benchmark

# Standard two-phase generation
result = generate_test_sh_for_benchmark(
    benchmark_name="curl-delta-01",
    benchmark_dir="benchmarks/curl-delta-01",
    project_dir="/path/to/curl",
    verbose=True
)

# With Docker testing (MCP-enabled)
result = generate_test_sh_for_benchmark(
    benchmark_name="curl-delta-01",
    benchmark_dir="benchmarks/curl-delta-01",
    project_dir="/path/to/curl",
    with_docker_testing=True,  # Enable MCP tools
    verbose=True
)
```

### Features

- ✅ **Integrated approach**: MCP tools work alongside Claude Agent SDK
- ✅ **Docker integration**: Builds images and tests scripts in containers
- ✅ **Iterative refinement**: Tests and improves test.sh until it works
- ✅ **Multi-language**: Supports C, C++, Java, Python, Go
- ✅ **No separate client**: MCP server managed by SDK automatically

### Available Tools

**Claude Agent SDK tools** (always available):
- `Read`, `Grep`, `Glob` - File operations
- `WebSearch`, `WebFetch` - Research capabilities
- `TodoWrite` - Task tracking

**MCP tools** (when `with_docker_testing=True`):
- `mcp__crsbench__build_benchmark` - Build Docker image
- `mcp__crsbench__get_build_logs` - Retrieve build logs
- `mcp__crsbench__check_test_sh` - Run and validate test.sh
- `mcp__crsbench__get_benchmark_info` - Get metadata

### Comparison: Standard vs MCP-Enhanced

| Feature | Standard | MCP-Enhanced |
|---------|----------|--------------|
| Approach | Two-phase (analyze → generate) | Iterative (generate → test → refine) |
| Testing | Post-generation (manual) | Integrated (automatic) |
| Docker | Not used | Build and test in container |
| Iteration | Single pass | Multiple attempts until success |
| Setup | Simpler | Requires Docker |

### Detailed Documentation

See [MCP_README.md](./MCP_README.md) for:
- Architecture details
- MCP server implementation
- Tool descriptions
- Advanced usage examples

---

## vuln.yaml Generator

Automatically generates vuln.yaml files for CPVs by analyzing crash logs, POV files, and source code.

### Features
- Analyzes crash logs (pov_*.log) to extract vulnerability information
- Identifies CWE classifications based on vulnerability type
- Locates vulnerable code in source files
- Generates accurate vuln.yaml with proper metadata
- **Auto-detects temporary MOCK files** and regenerates them automatically

### Usage

```bash
# Generate vuln.yaml for ALL CPVs in a benchmark
python crsbench/migration/generate_vuln_yaml.py \
  --benchmark atlanta-curl-delta-01

# Generate vuln.yaml for a specific CPV
python crsbench/migration/generate_vuln_yaml.py \
  --benchmark atlanta-curl-delta-01 \
  --harness curl_fuzzer_http \
  --cpv cpv_0

# Force overwrite ALL existing vuln.yaml files
python crsbench/migration/generate_vuln_yaml.py \
  --benchmark atlanta-curl-delta-01 \
  --force
```

### MOCK File Auto-Detection

The generator automatically detects and replaces temporary vuln.yaml files:
- Files containing `MOCK:` are considered temporary
- Files containing `(TBD)` are considered temporary
- These files are automatically regenerated **without** needing `--force`

Example:
```yaml
# This will be auto-detected and replaced:
name: 'MOCK: cpv_0 vulnerability in curl_fuzzer_http'
description: 'MOCK: ... (TBD) ...'
```

### Generated Files
1. **vuln.yaml** - Vulnerability metadata (per CPV)
2. **vuln_analysis.md** - Detailed analysis document (per CPV)
3. **vuln_agent_log.txt** - Agent execution log (per CPV)

### Requirements
- LITELLM_BASE_URL and LITELLM_API_KEY environment variables
- Claude Agent SDK
- Project source code (auto-cloned if not present)

### Design Documentation

See [vuln.yaml generator design document](../../design-docs/migration/vuln-yaml-generator.md) for architecture details.

---

## Repository Manager

Core utility for automatic cloning and checkout of project repositories needed during migration and test generation workflows.

### Overview

The repository manager (`repo_manager.py`) provides:
- **Automatic repository cloning** from benchmark configuration
- **Smart repository caching** to avoid redundant clones
- **Commit checkout** for reproducible testing
- **Configuration extraction** from benchmark metadata

### Key Features

- ✅ **Reads repository info** from `project.yaml` and `.aixcc/meta.yaml`
- ✅ **Auto-clones repositories** if they don't exist locally
- ✅ **Caches repositories** in `PROJECT_REPOS_DIR` for reuse
- ✅ **Checks out specific commits** for reproducibility
- ✅ **Smart reuse** - detects existing git repositories
- ✅ **Graceful error handling** with detailed logging

### Usage

#### Basic Usage
```python
from crsbench.migration.repo_manager import find_or_clone_project

# Automatically clone if needed
project_dir = find_or_clone_project(
    benchmark_name="json-c",
    benchmarks_root="benchmarks",
    verbose=True
)
```

#### With Custom Cache Location
```python
import os
from crsbench.migration.repo_manager import ensure_project_repository

# Set custom cache
os.environ['PROJECT_REPOS_DIR'] = '/mnt/ssd/git-cache'

project_dir = ensure_project_repository(
    benchmark_dir="benchmarks/curl",
    verbose=True
)
```

#### Using Existing Clone
```python
from crsbench.migration.repo_manager import ensure_project_repository

# Use pre-cloned repository
project_dir = ensure_project_repository(
    benchmark_dir="benchmarks/json-c",
    project_dir="/home/user/projects/json-c",
    verbose=True
)
```

### Environment Variables

**`PROJECT_REPOS_DIR`** (optional)
- Default cache location for cloned repositories
- Default: `/home/acorn421/work/team-atlanta/afc-repos`
- Override to customize cache location

### Integration

The repository manager is used by:
- **test.sh generator** - Needs project source for test discovery
- **vuln.yaml generator** - Needs source code for vulnerability analysis
- **Migration scripts** - Can pre-clone repositories for batch operations

### Design Documentation

See [repository manager design document](../../design-docs/migration/repo-manager.md) for full implementation details including:
- Architecture and component relationships
- Function workflows and algorithms
- Error handling strategies
- Configuration requirements
- Usage examples and testing approaches

---

## Build Script Splitter

Automatically splits `build.sh` into `build-pre.sh` and `build-apply.sh` for incremental build systems.

### Overview

For patch generation CRS, incremental builds significantly reduce compile time:
1. **build-pre.sh**: Full build before patch application (creates build cache)
2. **build-apply.sh**: Incremental rebuild after patch (reuses cached artifacts)

This is inspired by OSS-Fuzz's `replay_build.sh` system which enables fast rebuilds by caching object files and only recompiling changed sources.

### Quick Start

```bash
# Set up environment
export LITELLM_BASE_URL="http://localhost:4000"
export LITELLM_API_KEY="your-api-key"

# Single benchmark
python -m crsbench.migration.split_build \
  --benchmark libxml2-delta-01 \
  --benchmark-dir benchmarks/libxml2-delta-01 \
  --project-dir /path/to/libxml2 \
  --verbose

# Multiple benchmarks with auto-discovery
python -m crsbench.migration.split_build \
  --benchmarks libxml2-delta-01,curl-delta-01 \
  --benchmarks-root benchmarks/ \
  --projects-root /path/to/projects/ \
  --verbose

# Process all benchmarks
python -m crsbench.migration.split_build \
  --all \
  --benchmarks-root benchmarks/ \
  --projects-root /path/to/projects/ \
  --verbose
```

### Features

- ✅ **Automatic build analysis**: Analyzes build.sh to understand build process
- ✅ **Smart script splitting**: Separates configuration from compilation
- ✅ **OSS-Fuzz compatible**: Based on OSS-Fuzz replay_build.sh patterns
- ✅ **Multi-build system support**: Make, CMake, Meson, Maven, Gradle, etc.
- ✅ **Reference-driven**: Uses OSS-Fuzz examples as templates
- ✅ **Batch processing**: Process multiple benchmarks at once

### How It Works

The splitter performs two-phase analysis and generation:

#### Phase 1: Build Analysis
1. Reads original `build.sh` and referenced scripts
2. Identifies build system (Make, CMake, Maven, etc.)
3. Categorizes build steps (configuration, compilation, installation)
4. Determines which steps can be cached
5. References similar OSS-Fuzz replay_build.sh examples

#### Phase 2: Script Generation
1. **build-pre.sh**: Exact copy of original build.sh
2. **build-apply.sh**: Simplified script that:
   - Skips configuration (./configure, cmake, meson setup)
   - Skips clean operations (make clean, git clean)
   - Runs only incremental compilation
   - Copies updated fuzzers to $OUT

### OSS-Fuzz Replay Build Reference

The agent uses real OSS-Fuzz replay_build.sh examples as few-shot learning:

**Example 1: PHP (Ultra-fast, 62s → 2s)**
```bash
#!/bin/bash -eu
make -j$(nproc)

FUZZERS="php-fuzz-json php-fuzz-exif php-fuzz-parser"
for fuzzerName in $FUZZERS; do
    cp sapi/fuzzer/$fuzzerName $OUT/
done
```

**Example 2: OpenSSL (Function-based)**
```bash
#!/bin/bash -eu
function build_fuzzers() {
    make -j$(nproc) LDCMD="$CXX $CXXFLAGS"
    fuzzers=$(find fuzz -executable -type f)
    for f in $fuzzers; do
        cp $f $OUT/$(basename $f)
    done
}
cd $SRC/openssl/
build_fuzzers ""
```

**Example 3: FFmpeg (Complex with args)**
```bash
#!/bin/bash -eux
cd $SRC/ffmpeg
make -j$(nproc) install

if [ "$#" -lt 1 ]; then exit 0; fi
make_target=$($SRC/name_mappings.py build_target_name "$1")
make tools/${make_target}
```

These examples are **embedded in the agent's prompt** as few-shot examples.

### Example Output

**build-pre.sh** (Full build - exact copy of build.sh):
```bash
#!/bin/bash -eu
# Original build.sh content (no changes)
./configure --enable-fuzz
make -j$(nproc)
cp fuzzer $OUT/
```

**build-apply.sh** (Incremental):
```bash
#!/bin/bash -eu
# Incremental rebuild after patch application
# Configuration already done in build-pre.sh

cd $SRC/project
make -j$(nproc)  # Reuses cached .o files
cp fuzzer $OUT/
```

### Generated Files

For each benchmark, the splitter generates:
1. **build-pre.sh** - Full build script
2. **build-apply.sh** - Incremental rebuild script
3. **build_analysis.md** - Build process analysis (in `.aixcc/`)
4. **build_split_log.txt** - Agent execution log (in `.aixcc/`)

### Programmatic Usage

```python
from crsbench.migration.build_script_splitter import split_build_script

result = split_build_script(
    benchmark_name="curl-delta-01",
    benchmark_dir="benchmarks/curl-delta-01",
    project_dir="/path/to/curl",
    verbose=True
)

if result["success"]:
    print(f"✅ build-pre.sh: {result['build_pre_path']}")
    print(f"✅ build-apply.sh: {result['build_apply_path']}")
    print(f"📄 Analysis: {result['analysis_md_path']}")
```

### Requirements

- LITELLM_BASE_URL and LITELLM_API_KEY environment variables
- Claude Agent SDK
- Project source code (for build system analysis)
- Existing build.sh in benchmark directory

### Design Principles

1. **build-pre.sh is authoritative**: Matches original build.sh behavior
2. **build-apply.sh is minimal**: Only essential incremental steps
3. **Cache-aware**: Leverages build system's incremental compilation
4. **Idempotent**: build-apply.sh can run multiple times safely
5. **Fast**: Reduces rebuild time from minutes to seconds

