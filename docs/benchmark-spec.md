---
author: Yu-Fu Fu
---

# RFC for Standard Benchmark Specification for Cyber Reasoning Systems (CRS) v0.1

This specification defines a standardized benchmark format for
evaluating Cyber Reasoning Systems (CRS) developed during the AIxCC competition.

The benchmark ensures reproducibility, prevents LLM training data
contamination, and enables fair evaluation across different CRS
implementations.

## Background

The AIxCC competition evaluates Cyber Reasoning Systems across three core
cybersecurity capabilities:

### Core CRS Capabilities

#### Vulnerability Discovery

- Automated identification of security vulnerabilities in source code
- Generation of Proof of Vulnerability (POV) demonstrations

#### Program Repair

- Automated generation of patches to fix identified vulnerabilities
- Ensuring patches maintain program functionality while addressing security
  issues

## Benchmark Goals

### Primary Objectives

- Aggregate all AIxCC benchmarks into a unified, reproducible format
- Prevent LLM training data contamination
- Enable fair evaluation across different CRS implementations
- Analyze CRS performance on specific challenge types (C/Java,
  full/delta mode, harnessed)
- Classify challenges by difficulty level

### Benchmark Definition

A benchmark consists of a set of projects, each containing vulnerability
discovery challenges and program repair tasks.

### Extensibility Requirements

The framework is designed to be extensible and must support:

- New Proof of Concepts (POCs) from OSS-Fuzz or wild vulnerability
  discoveries
- Zero-day POV discoveries from real-world scenarios
- Future binary-only challenges (without source code access)
- Custom build configurations and compiler flags

## Benchmark Structure and Ground Truth

### Directory Layout

```example
benchmarks/[project-name]/
├── build.sh                     # Unified build script (upstreamed)
├── build-pre.sh                 # Pre-build setup script
├── build-apply.sh               # Patch application script
├── Dockerfile                   # Docker build configuration
├── project.yaml                 # OSS-Fuzz project configuration
├── pkgs/                        # Bundled source tarballs (optional)
│   ├── {source-name}.tar.gz     # Main source tarball
│   └── pkg_refs.txt             # Provenance tracking
├── (Other files used in the Dockerfile)
└── .aixcc/
    ├── meta.yaml                # Configuration metadata
    ├── ref.diff                 # Delta mode reference diff
    ├── test.sh                  # Invariant checking script
    └── [harness_name]/          # One directory for each harness
        └── [vuln-keyword]/      # One subdirectory for each vulnerability
            ├── {vuln-keyword}.md         # description and root cause
            ├── patches/
            │   ├── {patch-variant-id}.diff  # Bug-fixing patch
            │   └── {patch-variant-id}.diff  # Bug-fixing patch
            ├── [pov-variant-id]/        # Multiple POV for this vuln
            │   ├── {pov-variant-id}.blob  # Binary blob trigger crash
            │   └── {pov-variant-id}.log   # Crash log
            └── [pov-variant-id]/
                ├── {pov-variant-id}.blob  # Binary blob trigger crash
                └── {pov-variant-id}.log   # Crash log
```

### Bundled Source Tarballs (pkgs/)

The `pkgs/` directory contains bundled source tarballs for offline benchmark execution. This enables reproducible builds without requiring network access to clone repositories.

#### Tarball Naming Convention

**Critical**: The source tarball name MUST match the Dockerfile's final `WORKDIR` directory name.

```dockerfile
# Example Dockerfile
WORKDIR $SRC/curl    # WORKDIR is "curl"
```

The corresponding tarball must be named `curl.tar.gz` (not `libcurl.tar.gz` or any other name).

#### Why This Matters

- The build system extracts tarballs and expects the directory name to match WORKDIR
- Mismatched names cause build failures when the Dockerfile cannot find the source
- Validation tools enforce this convention to catch errors early

#### Split Tarballs for Large Files

For large source archives that exceed Git LFS limits, tarballs can be split:

```
pkgs/
├── poi.tar.gz.partaa    # First part (80MB)
├── poi.tar.gz.partab    # Second part (80MB)
├── poi.tar.gz.partac    # Third part (remaining)
└── pkg_refs.txt
```

Split tarballs are automatically reassembled during build. Create them with:

```bash
split -b 80M source.tar.gz source.tar.gz.part
```

#### pkg_refs.txt

The `pkg_refs.txt` file tracks tarball provenance:

```
https://github.com/curl/curl@abc123def456
```

This records the repository URL and commit hash used to create the tarball.


### Configuration File (meta.yaml)

The `meta.yaml` file defines the benchmark configuration, specifying
evaluation modes, harness files, and vulnerability detection criteria.

```yaml
# Patch exclusion list - files that patches cannot modify (global setting)
patch_exclude_list:
  - "build.sh"                   # Build scripts are immutable
  - "Makefile"                   # Build configuration files
  - "CMakeLists.txt"             # Build system files
  - "configure*"                 # Configuration scripts
  - "*.ac"                       # Autotools files
  - "*.am"                       # Automake files
  - ".gitignore"                 # Version control files
  - ".aixcc/**"                  # Benchmark metadata files
  - "test/ossfuzz.c"             # Harness files are protected
  - "include/config.h"           # System configuration headers
  - "**/*test*.c"                # Test files cannot be modified
  - "docs/**"                    # Documentation is protected

# Delta mode: provides bug-inducing diff between two commits
delta_mode:
  base_commit: "35af1ffb5dd21ae47332577c2b6c889da302b497"
  ref_commit: "baacf7a0891d4a478c403515f05c2387044a94d0"

# Full mode: only vulnerable repository available
full_mode:
  base_commit: "baacf7a0891d4a478c403515f05c2387044a94d0"

# Harness specifications
harness_files:
  - name: "ossfuzz"
    path: "$REPO/test/ossfuzz.c"

  - name: "customfuzz3"
    path: "$PROJECT/customfuzz3.c"
    # Vulnerability configurations for customfuzz3
    vulns:
      - vuln_keyword: "cpv_0"                # Maps to directory name (must follow cpv_N pattern)
        povs:
          - id: "pov_0"                      # POV variant ID
            sanitizer: "address"
            error_token: "ERROR: AddressSanitizer: heap-buffer-overflow"  # optional field for descriptive purpose
          - id: "pov_1"                      # POV variant ID
            sanitizer: "undefined"
            error_token: "runtime error: index out of bounds"  # optional field for descriptive purpose
      - vuln_keyword: "cpv_1"                # Maps to directory name (must follow cpv_N pattern)
        povs:
          - id: "pov_0"                      # POV variant ID
            sanitizer: "memory"
            error_token: "ERROR: MemorySanitizer: use-of-uninitialized-value"  # optional field for descriptive purpose
```

## Configuration Fields

**Important**: Each benchmark contains either `delta_mode` OR `full_mode` configuration, not both. The presence of `delta_mode` indicates the benchmark provides bug-inducing diffs, while `full_mode` indicates only the vulnerable codebase is provided.

### Evaluation Modes

#### Delta Mode

- Provide the bug-inducing diffs between `base_commit` and `ref_commit`
- Guarantee vulnerabilities triggered because of provided diffs
  - directly (e.g., remove bound checking)
  - indirectly (e.g., a call to vulnerable functions or API misuse)
- Enable focused analysis on specific code changes or their related parts

#### Full Mode

- Provide only the vulnerable code repository
- Require comprehensive analysis of the entire codebase
- Test broader code reasoning capabilities

#### Harness mode

- With provided harness.
- Dynamic analysis enabled.

### Patch Exclusion List

The `patch_exclude_list` configuration defines files and patterns that CRS-generated patches are forbidden from modifying. This ensures critical infrastructure remains unchanged during patch evaluation.

#### Purpose and Rationale

- Build System Protection: Prevents modification of build scripts, makefiles, and configuration files
- Harness Integrity: Protects fuzzing harnesses from being altered or disabled
- Metadata Security: Safeguards benchmark configuration and ground truth data
- Test Isolation: Prevents patches from modifying test files to artificially pass validation
- Fair Evaluation: Ensures all CRS systems operate under identical constraints

#### Supported Patterns

The exclusion list supports various file matching patterns:

- Exact filenames: `"build.sh"`, `"Makefile"`
- Glob patterns: `"*.ac"`, `"configure*"`
- Directory patterns: `"docs/**"`, `".aixcc/**"`
- Complex patterns: `"**/*test*.c"` (any file containing "test" in name)

#### Common Exclusion Categories

- Build Infrastructure: `build.sh`, `Makefile`, `CMakeLists.txt`, `configure*`, `*.ac`, `*.am`
- Benchmark Files: `.aixcc/**`, `meta.yaml`, `*.patch`, `*.blob`
- Harness Files: Fuzzing harness source files as specified in harness_files configuration
- Test Files: Unit tests, integration tests, and validation scripts
- Documentation: `README*`, `docs/**`, `*.md`
- Version Control: `.git/**`, `.gitignore`, `.gitmodules`

#### Enforcement

The patch exclusion list is enforced during:

- Patch Validation: Before applying patches to source code
- Build Process: Docker build fails if excluded files are modified
- Evaluation Scoring: Violations result in patch rejection and scoring penalties

### Harness Files

Define the test harnesses used for vulnerability discovery:

- Support multiple harnesses per project
- Link harnesses source code to their filesystem locations within the
  repository
- Harnesses without vulnerability configurations serve as distractor harnesses or baseline
  tests
- CRS systems must analyze all provided harnesses and prioritize those most
  likely to trigger vulnerabilities
- This tests CRS capability to distinguish between productive and
  unproductive fuzzing targets

#### Harness Path Specification

Harness file paths support variable substitution for flexible location specification:

- `$REPO/path/to/file`: Path relative to the cloned repository directory (where source code lives)
- `$PROJECT/path/to/file`: Path relative to the OSS-Fuzz compatible project directory (containing `project.yaml`, `build.sh`, etc.)
- `/absolute/path/to/file`: Absolute path within the container
- `./relative/path/to/file`: Relative path from current directory

The `$REPO` and `$PROJECT` variables allow harness paths to remain valid across different repository structures and container configurations.

### Proof of Vulnerability (POV)

Defines how vulnerabilities are detected and verified:

- Multiple vulnerabilities allowed per harness, each with multiple POV variants
- Each POV specifies detection method (sanitizer type: address, memory, undefined, etc.)
- Defines error patterns for automated verification and result matching
- Error tokens (optional, for descriptive purposes) are matched using substring matching against sanitizer output
- Used for deduplicating discovered vulnerabilities based on error
  signatures

### Benchmark Components - Ground Truth

Each harness directory contains subdirectories for each vulnerability keyword, which groups related POV variants by their root cause. For example, with vulnerability `cpv_0` having POV variants `pov_0` and `pov_1`:

- `{vuln-keyword}/` - Directory for each vulnerability (e.g., `cpv_0/`)
  - `{vuln-keyword}.md` - Human-readable vulnerability description and root cause analysis (e.g., `cpv_0.md`)
  - `patches/` - Directory containing patches that fix all POV variants of this vulnerability
    - `{patch-variant-id}.diff` - Bug-fixing patch (e.g., `patch_0.diff`, `patch_1.diff`)
  - `{pov-variant-id}/` - Directory for each POV variant (e.g., `pov_0/`, `pov_1/`)
    - `{pov-variant-id}.blob` - Binary blob trigger crash (e.g., `pov_0.blob`)
    - `{pov-variant-id}.log` - Crash log (e.g., `pov_0.log`)

## Build System

### Docker-Based Build Architecture

The benchmark uses a Docker-based build system where each patch evaluation creates a separate Docker image. This approach eliminates the need for patch management within build scripts and ensures complete isolation between different patch evaluations.

#### Build Script Structure

The build.sh script follows the standard OSS-Fuzz format with project-specific build commands. The script is simplified since patch management is now handled at the Docker layer.

#### Docker Image Build Process

```dockerfile
# Example Dockerfile for patch evaluation
FROM project-base:latest

# Copy and apply patch
COPY patch.diff /tmp/
RUN cd $SRC && patch -p1 < /tmp/patch.diff

# Build with patch applied
RUN ./build.sh
```

### Docker Layer Optimization

The Docker-based build system supports efficient builds through:

- Base Image Caching: Pre-compiled dependencies cached in base images
- Layer Reuse: Unchanged Docker layers are reused across different patch evaluations
- Incremental Compilation: Build systems (Make, CMake) handle incremental builds within containers
- Parallel Builds: Multiple patch evaluations can run simultaneously in separate containers

### Usage Examples

```bash
# Build Docker image for specific patch evaluation
docker build -t project-vuln0 .

# Run evaluation with patched image
docker run --name eval-vuln0 project-vuln0

# Extract build artifacts from container
docker cp eval-vuln0:/artifacts ./evaluation-results/cpv_0/
```

### Helper Script Integration

The evaluation framework provides automated Docker image management:

```bash
# Build evaluation image for specific vulnerability
helper.py build-image --vuln cpv_0 --tag project-vuln0

# Run complete evaluation workflow
helper.py evaluate --vuln cpv_0 --output-dir ./results/

# Compare multiple patch evaluations
helper.py compare --baseline project-base --vulns cpv_0,cpv_1,cpv_2
```

The helper script automatically:

- Reads patch information from `meta.yaml`
- Builds appropriate Docker images for each evaluation
- Manages container lifecycle and artifact collection
- Provides consistent evaluation environment setup

### Build System Constraints

The Docker-based build system enforces the following constraints to ensure fair evaluation:

- Immutable build scripts: CRS cannot modify build.sh or Docker build configuration
- Codebase-only patches: CRS can only generate patches that modify source code files
- Fixed build environment: Compiler flags, dependencies, and build options are predetermined in base images
- Reproducible builds: All builds are deterministic through Docker layer caching and fixed base images
- Container isolation: Each patch evaluation runs in a completely isolated container environment
- Upstreamed location: build.sh is located at `projects/[name]/build.sh` following OSS-Fuzz convention

## Docker Container Architecture

### Container Architecture Overview

The Docker-based evaluation system consists of three primary image layers:

1. Base OSS-Fuzz Image: Contains the core build environment and dependencies
2. Project Base Image: Pre-compiled project dependencies and harnesses
3. CRS Evaluation Image: Applied patches and final build artifacts

### OSS-Fuzz Base Image

The foundational layer provides:

- Standard OSS-Fuzz build environment with all required tools
- Compiler toolchain (Clang, GCC) with fuzzing instrumentation support
- Pre-compiled harness binaries stored in `$OUT` directory
- Fuzzing engines and runtime libraries
- Core system dependencies and build tools

This base image remains static across all benchmark evaluations, ensuring consistent build environments.

### Project Base Image

Built upon the OSS-Fuzz base, this layer includes:

- Pre-compiled project libraries and dependencies
- Cached intermediate build artifacts
- Project-specific configuration and setup
- Compiled but unlinked object files for faster incremental builds

The project base image significantly reduces build times by pre-compiling stable components that rarely change during patch evaluation.

### CRS Evaluation Image

The final evaluation layer handles:

- Application of CRS-generated patches to source code
- Incremental compilation of modified components
- Final linking and harness preparation
- Build artifact validation and output preparation

### Docker Inheritance and Patch Application Workflow

```bash
# 1. Start from project base image
FROM project-base-image:latest

# 2. Apply CRS-generated patch
COPY crs-patch.diff /tmp/
RUN cd $SRC && patch -p1 < /tmp/crs-patch.diff

# 3. Run build (incremental building)
RUN ./build.sh

# 4. Validate build artifacts
RUN test -f $OUT/harness_binary
```

### Incremental Build Optimization

The Docker architecture supports several optimization strategies:

- Layer caching: Unchanged base layers are reused across evaluations
- Incremental compilation: Only modified source files require recompilation
- Selective copying: Only new or changed artifacts are copied to `$OUT`
- Parallel builds: Multiple patch evaluations can run simultaneously

### Build State Management

Docker containers maintain build state through:

- Volume mounts for persistent `$WORK` directories
- Cached intermediate files between container runs
- Build markers preserved across container restarts
- Patch application state tracking

This architecture ensures that the same patch can be re-evaluated quickly without full rebuilds, while maintaining isolation between different CRS evaluations.

## Invariant Checking

Invariant checking ensures that patches fix vulnerabilities without
breaking intended program functionality. This prevents over-broad fixes
such as deleting entire codebases or disabling core features.

### Test Script (test.sh)

The `test.sh` script in each benchmark project provides automated invariant
checking capabilities. This script:

- Validates that patches maintain program functionality
- Runs regression tests to ensure no new bugs are introduced
- Checks performance characteristics remain within acceptable bounds
- Verifies API compatibility and interface contracts

The test script should return:
- Exit code 0: All invariants pass, patch is acceptable
- Exit code 1: Invariant violations detected, patch rejected

Example usage:
```bash
./test.sh
```

### Test Optimization and Coverage-Guided Execution

To reduce evaluation time while maintaining thorough validation, the test.sh script supports intelligent test selection based on code coverage analysis and patch impact assessment.

#### Coverage-Based Test Selection

The test script can identify and execute only the subset of tests affected by a given patch:

```bash
# Run coverage-guided test selection
./test.sh --coverage-guided --patch-file=cpv_0/patches/patch_0.diff

# Run specific test categories based on coverage
./test.sh --unit-tests --affected-only
./test.sh --integration-tests --related-modules
```

#### Instrumented Binary Analysis

To determine the mapping between source code changes and relevant unit tests, the system uses instrumented binaries to collect coverage information:

1. Build instrumented version of the project with coverage tracking
2. Execute full test suite to generate coverage maps
3. Analyze coverage data to identify relationships between:
   - Source file modifications and affected functions
   - Function changes and dependent test cases
   - Module boundaries and integration test requirements

#### Patch Impact Analysis

The test script analyzes patch content to determine optimal test selection strategy:

- Static analysis of changed functions and their call graphs
- Identification of modified API surfaces and their test coverage
- Detection of cross-module dependencies requiring broader testing
- Risk assessment based on change complexity and scope

#### Test Execution Modes

The test.sh script supports multiple execution modes for different validation needs:

```bash
# Fast mode: Only directly affected unit tests
./test.sh --mode=fast

# Standard mode: Affected tests plus dependency tests
./test.sh --mode=standard

# Comprehensive mode: Full test suite execution
./test.sh --mode=comprehensive

# Custom mode: User-specified test selection
./test.sh --tests="test_module_a,test_integration_x"
```

#### Coverage Mapping Generation

The coverage mapping process involves:

1. Compile project with coverage instrumentation (gcov, llvm-cov)
2. Execute comprehensive test suite to capture execution paths
3. Generate mapping data linking source locations to test cases
4. Store mapping in efficient lookup format for fast test selection
5. Update mappings when test suite or codebase structure changes

This approach significantly reduces test execution time by focusing validation efforts on code paths most likely to be affected by the applied patches.

### Possible Invariant Checking Methods

Various approaches may be used to verify patch correctness:

- Check whether it compiles
- Check crash no longer happens
- Unit tests
- Differential testing
- Fuzzing after patch
- LLM as a judge

### Runtime Invariants

Verify that program behavior remains consistent during execution after
patches are applied. This includes functional correctness and
performance characteristics.
Functional correctness is generally already tested by the unit tests in the code
repository.

### Code Invariants

Ensure that structural code properties are maintained after patching,
such as API compatibility, interface contracts, and architectural
constraints.

## Evaluation Metrics

The benchmark evaluates CRS performance across multiple dimensions to
provide comprehensive assessment of vulnerability discovery
capabilities.

### Difficulty Classification Based on POV Quantity

The benchmark implements a difficulty classification system based on the number of Proof of Vulnerability (POV) inputs provided to CRS systems. POVs are crash-triggering inputs for fuzzing harnesses that help with vulnerability discovery and root cause analysis.

#### POV Definition and Purpose

- POV (Proof of Vulnerability): An input that triggers a crash when executed against a fuzzing harness
- POV Deduplication: POVs are deduplicated based on root cause analysis, not sanitizer error type
  - Multiple POVs with the same sanitizer error may represent different vulnerabilities (different root causes)
  - Multiple POVs with different stack traces may represent the same vulnerability (same root cause, different code paths)
- Purpose: Multiple deduplicated POVs (same root cause) provide different ways to trigger the same vulnerability, aiding root cause analysis for accurate patch generation

#### Difficulty Levels Based on POV Quantity

Difficulty is determined by how many deduplicated POVs (same root cause) are provided to the CRS:

##### No POVs (0 POVs) - Highest Difficulty
- CRS must discover vulnerabilities without any crash examples
- No existing POVs provided as starting points
- CRS can use any approach: static analysis, fuzzing, dynamic testing, etc.
- CRS may need to synthesize their own fuzzing harnesses and find crashes
- Simulates zero-day vulnerability discovery scenarios
- Requires advanced vulnerability detection capabilities

##### Single POV (1 POV) - High Difficulty
- One crash-triggering input provided for a specific root cause
- Limited information for root cause analysis
- CRS must identify the vulnerability pattern from a single data point
- Represents scenarios with minimal crash evidence for a specific bug

##### Multiple POVs (2-3 POVs) - Medium Difficulty
- Several crash inputs targeting the same root cause vulnerability
- Different ways to trigger the same underlying bug
- Pattern analysis possible across different attack vectors for the same vulnerability
- CRS can compare inputs to better understand the root cause
- Better identification of the specific vulnerability through multiple examples

##### Many POVs (4+ POVs) - Lower Difficulty
- Comprehensive set of crash examples for the same root cause
- Multiple attack vectors and code paths leading to the same vulnerability
- Rich dataset showing various ways to trigger the same bug
- CRS can thoroughly analyze the vulnerability pattern
- Enables high-confidence identification and patch development

#### Root Cause Analysis Benefits

Multiple deduplicated POVs (same root cause) enhance CRS patch generation capabilities by:

- Pattern Recognition: Identifying common elements across different ways to trigger the same vulnerability
- Code Path Analysis: Understanding various paths that lead to the same underlying bug
- Attack Vector Coverage: Seeing different input variations that exploit the same root cause
- Completeness Validation: Ensuring patches address all known ways to trigger the vulnerability
- Regression Prevention: Verifying fixes work across all provided attack scenarios

#### Hint System for Additional Guidance

Beyond POV quantity, the benchmark supports optional hints that provide varying levels of vulnerability guidance:

##### Hint Types and Examples

- Vulnerability Category Hints: "The bug is a buffer overflow", "Memory safety issue present", "Use-after-free vulnerability"
- Location Hints: "The bug is in the parsing module", "Vulnerability in file processing code", "Issue in network handling functions"
- Severity Hints: "Critical security vulnerability", "Exploitable memory corruption", "Remote code execution possible"
- Technical Hints: "Missing bounds checking", "Unvalidated input processing", "Race condition in threading code"

##### Multiple Hints per Vulnerability

Each vulnerability can have multiple hints at different specificity levels:

```yaml
# Example hint progression for a single vulnerability
hints:
  - level: 1
    text: "Memory safety issue detected"
  - level: 2
    text: "Buffer overflow in input processing"
  - level: 3
    text: "Missing bounds check in parse_input() function"
  - level: 4
    text: "Line 245: buffer[i] = input[i] without length validation"
```

#### Initial Corpus Variations

Beyond POVs and hints, the benchmark supports providing different levels of initial fuzzing corpus to control discovery difficulty:

##### Corpus Quality Levels

- **level-0**: Maximum help - inputs specifically designed to exercise vulnerable code paths, may include "almost crashes"
- **level-1**: Significant help - comprehensive inputs covering various features and edge cases
- **level-2**: Moderate help - representative valid inputs covering basic functionality
- **level-3**: Minimal help - basic valid inputs (e.g., empty file, simple valid format)
- **level-4**: No help - empty corpus, CRS must generate inputs from scratch

##### Corpus Integration with meta.yaml

```yaml
harness_files:
  - name: "customfuzz3"
    path: "/src/project/test/customfuzz3.c"
    # Corpus configuration for different difficulty levels
    corpus:
      level-0: "corpus-fuzz-level-0"
      level-1: "corpus-fuzz-level-1"
      level-2: "corpus-fuzz-level-2"
      level-3: "corpus-fuzz-level-3"
      level-4: "corpus-fuzz-level-4"
    default_corpus_level: "level-2"
```

#### Intrinsic Difficulty Level

The benchmark assigns an intrinsic difficulty level to each vulnerability based on characteristics of the vulnerability itself, independent of what assistance (POVs, hints, corpus) is provided to the CRS. The difficulty level is represented as a single numeric value.

##### Difficulty Calculation Factors

The intrinsic difficulty level is calculated based on:

- **Time-to-Discovery by Baseline Fuzzer**: Time required by baseline fuzzers (e.g., AFL++) to discover the vulnerability from scratch
- **Code Complexity**: Complexity of the vulnerable code, nesting depth, control flow complexity, and surrounding codebase characteristics
- **Other Factors**: Additional intrinsic characteristics such as:
  - Vulnerability type and exploitability
  - Code path depth to reach vulnerable code
  - Number of preconditions required to trigger the bug
  - Interaction complexity between components

##### meta.yaml Integration

```yaml
harness_files:
  - name: "ossfuzz"
    path: "/src/project/test/ossfuzz.c"
    vulns:
      - vuln_keyword: "cpv_0"                  # Maps to directory name (must follow cpv_N pattern)
        difficulty_level: 3                   # Intrinsic difficulty level
        povs:
          - id: "pov_0"                       # POV variant ID
            sanitizer: "address"
            error_token: "ERROR: AddressSanitizer: heap-buffer-overflow"  # optional field for descriptive purpose
          - id: "pov_1"                       # POV variant ID
            sanitizer: "undefined"
            error_token: "runtime error: index out of bounds"  # optional field for descriptive purpose
```

The difficulty level is typically represented as an integer (e.g., 1-5, where 1 is easiest and 5 is most difficult), though the exact scale may vary depending on the benchmark implementation.

##### Difficulty Level Mapping

The intrinsic difficulty levels correspond to:

- **Difficulty Level 1**: Easily discoverable vulnerabilities with simple code paths and minimal preconditions
- **Difficulty Level 2-3**: Moderate complexity bugs requiring sustained fuzzing and analysis
- **Difficulty Level 4-5**: Deep or complex vulnerabilities requiring extensive analysis and specialized techniques

#### Scoring System

While the difficulty level represents the intrinsic challenge of a vulnerability, the **scoring system** evaluates CRS performance by considering both the intrinsic difficulty and the assistance provided during evaluation.

##### Score Calculation

The final evaluation score is based on:

**Score = f(difficulty_level, hint_level, corpus_level, pov_count)**

Where:

- **difficulty_level**: The intrinsic difficulty of the vulnerability (1-5)
- **hint_level**: Level of hints provided to the CRS (0 = no hints, 1-4 = increasing specificity)
- **corpus_level**: Quality of initial corpus provided (0 = no corpus, 1-4 = increasing quality)
- **pov_count**: Number of POV examples provided to the CRS (0 = discovery mode, 1+ = varying assistance)

##### Scoring Rationale

Higher scores are awarded for:

- Discovering/patching higher difficulty vulnerabilities
- Achieving success with less assistance (fewer hints, smaller corpus, fewer POVs)
- Finding vulnerabilities in less time with fewer resources

This two-tier system allows fair comparison of CRS capabilities across different assistance levels while maintaining objective difficulty classification.

### Performance Metrics

#### Code Navigation and Symbol Resolution

Evaluates CRS capabilities for code understanding and navigation:

- Code search effectiveness across large codebases
- Symbol lookup and cross-reference resolution
- Function call graph analysis and traversal
- Dependency tracking and import resolution

#### Information Requirements for Vulnerability Discovery

Evaluates CRS performance across two orthogonal dimensions that combine
to create four distinct difficulty levels:

Dimensions:

- Code scope: Full mode (entire codebase) vs Delta mode (focused diff)

This metric helps classify CRS capabilities across different information
availability scenarios.

#### Time to Discovery

Measures the time elapsed from benchmark start until the first POV is
discovered for each vulnerability. This metric evaluates:

- Speed of vulnerability identification
- Efficiency of analysis approaches
- Time-based ranking of CRS systems

#### Cost per POV

Calculates the total resource cost (computing + LLM API) required to
discover each POV, enabling cost-effectiveness comparison between
different CRS approaches.

### Resource Utilization

#### LLM Usage Tracking

- Token consumption (used vs cached tokens)
- Cache utilization efficiency and hit rates
- API cost optimization strategies
- Request patterns and batch processing effectiveness
- Strategic model selection

Effective cache token utilization is crucial for cost optimization, as
major LLM providers (OpenAI, Anthropic) offer significant discounts for
cached tokens compared to new token processing.

Strategic model selection involves choosing task-appropriate models instead of
always using the most advanced (and expensive) models.
For example, using cheaper models for code parsing, documentation reading, or
simple pattern matching tasks, while reserving advanced models for complex
reasoning, vulnerability analysis, or patch generation.
This approach maximizes cost-effectiveness under budget constraints by
allocating expensive model usage to high-value tasks.

#### Computing Resource Usage

- CPU utilization patterns
- Memory consumption profiles
- Storage requirements
- Network bandwidth usage

#### Fuzzing Analysis Metrics

When CRS utilizes fuzzing techniques, additional metrics are collected:

- Fuzz introspector reports for coverage analysis and fuzzing effectiveness
- Fuzzer performance metrics (executions per second, coverage growth)
- Target prioritization strategies and harness selection rationale

### Budget Constraint Scenarios

The benchmark tests CRS performance under different resource limitation
settings:

#### Fixed Total Budget

- Combined budget for computing resources (CPU/RAM/Storage) and LLM API
  costs
- CRS must autonomously decide resource allocation between computation
  and LLM usage
- Test strategic resource management capabilities

#### Separate Budget Categories

- Independent limits for computing resources and LLM API costs
- Allow specialized optimization strategies for each resource type
- Test ability to maximize each resource category independently

### Resource Combination Testing

Different resource availability scenarios to test CRS adaptability:

- High computing resources + High LLM budget
- High computing resources + Low LLM budget
- Low computing resources  + High LLM budget
- Low computing resources  + Low LLM budget

These combinations reveal how CRS systems adapt their strategies based
on available resources and identify optimal operating conditions.

## CRS Evaluation Framework

The benchmark provides a comprehensive framework for running CRS evaluations with standardized experiment configurations, CRS integration, and benchmark suite management.

### Experiment Running Script

A single entry point for running experiments:

```sh
$ crsbench \
  --experiment-config <CONFIG_FILE: e.g. experiment-config.yaml> \
  --benchmarks <BENCHMARK: list of benchmarks or group of benchmark> \
  --experiment-name <EXPERIMENT_NAME> \
  --crses <CRS_LIST: list of crses. e.g atlantis-c, atlantis-multilang>
```

The user can specify:
- Experiment configuration file
- Target benchmarks or benchmark suites
- Experiment name for tracking and reporting
- List of CRS implementations to evaluate

### Experiment Configuration File

A single YAML file provides experiment configurations for each evaluation:

```yaml
# The number of trials.
trials: 1

# The amount of time in seconds that each trial is run for.
max_total_time: 86400

# Difficulty control of the experiment (e.g., corpus level or hint level)
difficulty_level: 1

# The folder that will store most of the experiment data.
experiment_filestore: /tmp/crsbench/experiment-data

# The folder where HTML reports and summary data will be stored.
report_filestore: /tmp/crsbench/report-data
```

Configuration parameters:

- **trials**: Number of trials to run for statistical significance
- **max_total_time**: Maximum time in seconds for each trial
- **difficulty_level**: Controls assistance provided (corpus level, hint level)
- **experiment_filestore**: Directory for experiment data storage
- **report_filestore**: Directory for HTML reports and summary data

### Supported CRSes

All supported CRS implementations are stored in the crses directory of the main repository:

- CRS developers can add their CRS to the benchmark main repository as a submodule
- To test a private CRS, users can add the CRS repo to a subdirectory in the local environment
- All CRS implementations and CRS configurations must follow the CRS RFC format

```example
crses/[crs-name]/
├── pkg.yaml                     # Package manager for CRS installation
└── config-crs.yaml              # CRS configuration
```

#### CRS Directory Structure

- **pkg.yaml**: Defines CRS dependencies, installation requirements, and package management
- **config-crs.yaml**: Specifies CRS-specific configuration, runtime parameters, and evaluation settings

### Benchmark Suite

A benchmark suite is a collection of benchmarks grouped for experimental convenience:

- Used to evaluate CRS for specific languages, input formats, and more
- Users can select the suite through the --benchmarks option of run_experiment.py

```yaml
# Name of the benchmark suite.
Name: crsbench-c
# Simple description of the benchmark group.
Description: A benchmark suite for evaluating C/C++ CRS.
# Release date of benchmark suite.
Release date: 09.23.2025
# List of benchmarks included in the benchmark suite.
benchmark_list:
  # Simple format - runs all harnesses for this benchmark (default)
  - {benchmark_id_1}

  # Extended format - runs only specified harnesses
  - {benchmark_id_2}:
      - {harness_name_1}
      - {harness_name_2}

  - {benchmark_id_3}
```

#### Benchmark Suite Configuration

- **Name**: Unique identifier for the benchmark suite
- **Description**: Purpose and scope of the benchmark collection
- **Release date**: Version tracking for benchmark suite updates
- **benchmark_list**: List of benchmarks with optional harness specification

#### Benchmark List Format

The `benchmark_list` supports two formats for each entry:

1. **Simple format (string)**: Specifies only the benchmark ID. **All harnesses** for this benchmark will be run.
   ```yaml
   benchmark_list:
     - afc-curl-delta-01  # Runs ALL harnesses
   ```

2. **Extended format (dict)**: Specifies both benchmark ID and a list of specific harnesses to include. **Only the listed harnesses** will be run.
   ```yaml
   benchmark_list:
     - afc-curl-delta-01:  # Runs ONLY fuzz_url and fuzz_parser
         - fuzz_url
         - fuzz_parser
   ```

**Important**:
- **Simple format** (no harnesses specified): Runs ALL available harnesses - this is the default behavior
- **Extended format** (harnesses specified): Runs ONLY the specified harnesses - all other harnesses are excluded

This design ensures backward compatibility while allowing fine-grained control over which harnesses to evaluate.

Benchmark suites enable:
- Language-specific evaluation (C/C++, Java, Rust, etc.)
- Domain-specific testing (parsing, cryptography, networking)
- Difficulty-based grouping (easy, medium, hard)
- Feature-specific testing (delta mode, full mode, harnessed)

## Universal Analysis Tools

To ensure fair evaluation and consistent analysis capabilities across different CRS implementations, the benchmark framework integrates standardized analysis tools that provide common functionality and interfaces.

### Fuzz Introspector Integration

Fuzz Introspector provides comprehensive analysis of fuzzing effectiveness and code coverage, enabling CRS systems to make informed decisions about fuzzing strategies and target prioritization.

#### Coverage Analysis Capabilities

- Function-level coverage reporting with detailed execution statistics
- Code reachability analysis to identify untested code paths
- Fuzzing bottleneck identification and optimization recommendations
- Comparative coverage analysis between different fuzzing approaches

#### Target Prioritization Support

Fuzz Introspector helps CRS systems identify the most promising fuzzing targets:

- Complexity-based ranking of functions and code paths
- Dependency analysis to understand code interconnections
- Historical vulnerability pattern analysis for risk assessment
- Integration with existing fuzzing harnesses and test infrastructure


### LiteLLM Universal Interface

LiteLLM provides a unified interface for interacting with multiple Large Language Model providers, enabling fair comparison between CRS systems regardless of their chosen LLM backend.

#### Multi-Provider Support

LiteLLM supports consistent API access across major LLM providers:

- OpenAI GPT
- Anthropic Claude
- Google Gemini
- Open-source models
- Local model deployments

#### Standardized Metrics Collection

The universal interface enables consistent tracking of LLM usage across different providers:

```python
# Standardized LLM usage tracking
from litellm import completion, usage_tracker

response = completion(
    model="claude-3-5-sonnet",
    messages=[{"role": "user", "content": code_analysis_prompt}],
    max_tokens=4000
)

# Automatic usage tracking for fair comparison
usage_stats = usage_tracker.get_current_usage()
# Output: {input_tokens: 1500, output_tokens: 800, cost: 0.045}
```

#### Cost Normalization

LiteLLM enables fair cost comparisons between different model choices:

- Normalized cost-per-token calculations across providers
- Cache utilization tracking and optimization suggestions
- Budget management and spending alerts
- Model performance-to-cost ratio analysis

### Tool Integration Architecture

#### Standardized Tool Interfaces

All analysis tools in the benchmark framework implement consistent interfaces:

- Common command-line argument patterns
- Standardized output formats (JSON)
- Uniform error handling and logging
- Consistent configuration file formats

#### Tool Performance Metrics

The framework tracks effectiveness metrics for all integrated tools:

- Execution time and resource usage for each tool
- Accuracy metrics for vulnerability detection tools
- Coverage effectiveness for fuzzing and testing tools
- Cost-effectiveness ratios for LLM-based analysis

#### Extensibility Framework

The universal tool architecture supports easy integration of new analysis tools:

- Plugin-based architecture for tool registration
- Standardized configuration schema for new tools
- Automatic metric collection for integrated tools
- Version management and compatibility checking

### Fair Evaluation Guarantees

#### Tool Version Control

All CRS evaluations use identical tool versions to ensure fair comparison:

- Locked dependency versions for all analysis tools
- Reproducible tool installation and configuration
- Validation of tool behavior across different environments
- Regression testing for tool updates and changes

#### Usage Monitoring

The framework monitors tool usage patterns to identify potential advantages:

- Tool invocation frequency and timing analysis
- Resource allocation between different analysis approaches
- Success rate correlation with specific tool combinations
- Identification of tool usage best practices

This standardized tooling approach ensures that CRS evaluations focus on reasoning capabilities rather than access to specialized tools or APIs.

## Reproducible Archive Format

The reproducible archive format ensures consistent benchmark distribution and
execution across different environments, providing comprehensive logging and
validation of CRS evaluation results.

### Goals

- Provide comprehensive validation and verification capabilities
- Ensure consistent results across different evaluation environments
- Support comparative analysis between different CRS implementations

### Archive Structure and Format

#### Environment Capture

- Operating system version and architecture
- All dependency versions (compilers, libraries, tools)
- Environment variables relevant to execution
- Hardware specifications (CPU, RAM, storage)

#### Execution Data

- Complete process execution traces with timestamps
- All file system modifications with timing
- Resource utilization patterns (CPU, memory, I/O)
- Random seed values for all non-deterministic operations
- Checkpoint snapshots at key execution milestones

#### CRS Logging

Comprehensive logging of CRS activities and discoveries:

- **Found POVs**: All discovered proof-of-vulnerability inputs with timestamps
- **Generated Patches**: All patches generated during evaluation with metadata
- **Fuzzing Corpus**: Evolution of fuzzing corpus over time
- **Static Analysis Results**: Findings from static analysis tools
- **Dynamic Analysis Results**: Runtime analysis and instrumentation data
- **Vulnerability Reports**: Generated vulnerability descriptions and classifications

#### Custom Logging Interface

The framework provides a custom logging interface for CRS-specific data:

- **Structured Logging Format**: JSON-based format for consistent data storage
- **CRS-Specific Logs**: Custom fields for CRS implementation-specific information
- **Extension Points**: API for CRS to log custom metrics and intermediate results
- **Log Aggregation**: Centralized collection of logs from all CRS components

#### LLM Interaction Logging

Detailed tracking of all LLM API interactions:

- **Request and Response Data**: Complete request payloads and response data with exact timestamps
- **Token Usage Statistics**: Input tokens, output tokens, and cached tokens per request
- **Model Selection**: Which model was used for each request (e.g., GPT-4, Claude-3.5-Sonnet)
- **Cache Efficiency**: Cache hit/miss ratios and patterns
- **API Rate Limiting**: Rate limiting events and retry information
- **Processing Times**: Response latency and processing duration

#### Timestamp and Budget Tracking

Track progress and resource consumption throughout evaluation:

- **Timestamp for Each Finding**: Record exact time when each POV or vulnerability is discovered
- **Remaining Budget**: Track remaining compute and LLM API budget at each checkpoint
- **Resource Consumption**: Cumulative resource usage over time
- **CRS State Proxy**: Timestamps and budgets serve as proxy for CRS internal state
- **Continuation Support**: Enable CRS to resume from saved state with accurate resource tracking
- **Ensembling Support**: Allow state synchronization between multiple CRS instances for ensemble approaches

### Validation and Integrity

Archive integrity validation ensures consistent and reliable evaluation data:

- **Archive Completeness**: Verify all required logs and data are present
- **Data Consistency**: Check for consistency between different log sources
- **Timestamp Validation**: Ensure timeline consistency across all logged events
- **Budget Verification**: Validate resource consumption matches logged activities

### Privacy and Security

#### Sensitive Data Handling

- Automatic redaction of API keys and credentials
- Anonymization of personally identifiable information

### Analysis Tools

The framework provides comprehensive analysis tools to dissect CRS performance across different aspects of vulnerability discovery and patching.

#### Archive Inspection

- **Command-line Tools**: Utilities for examining archive contents and extracting specific data
- **Metadata Extraction**: Extract evaluation metadata, configuration, and summary statistics
- **Timeline Visualization**: Visual representation of CRS activities over time
- **Resource Usage Analysis**: Detailed graphs and charts of resource consumption patterns
- **Log Querying**: Query interface for searching and filtering logged events

#### Performance Dissection

Tools to analyze CRS performance across different dimensions:

- **Vulnerability Discovery Analysis**: Time-to-discovery, coverage metrics, fuzzing effectiveness
- **Patch Generation Analysis**: Patch quality, correctness rate, iteration count
- **LLM Usage Analysis**: Token efficiency, model selection patterns, cache utilization
- **Resource Efficiency Analysis**: Cost per finding, resource allocation strategies
- **Strategy Effectiveness**: Success rates for different CRS approaches and techniques

#### Comparative Analysis

- **Side-by-Side Comparison**: Compare multiple CRS runs with synchronized timeline views
- **Performance Benchmarking**: Rank different CRS implementations across various metrics
- **Strategy Variation Identification**: Identify different approaches used by different CRS systems
- **Success Rate Comparisons**: Compare success rates across different difficulty levels and scenarios
- **Cost-Effectiveness Analysis**: Compare resource usage and costs relative to success rates

#### Validation Tools

- **Archive Integrity Verification**: Scripts to verify archive completeness and consistency
- **Data Validation**: Ensure logged data matches evaluation requirements
- **Performance Regression Detection**: Detect performance changes across different versions
- **Format Migration Utilities**: Tools for migrating archives between format versions

## Anti-Contamination Measures

To prevent benchmark data from being used in LLM training sets:

### Protective Measures

- Password-protected compressed archives
- Static canary insertions in code
- Random canary insertions
- Restricted distribution channels

### Limitations

- Cannot prevent manual extraction and re-upload by third parties
- Determined attackers can still access and redistribute benchmark data

### Objective

Prevent LLMs from providing direct answers from memorized training data.

## Related Works

- [CyberGym](https://www.cybergym.io/)
- [BountyBench](https://bountybench.github.io/)
- [Cybench](https://cybench.github.io/)
- [XBOW Validation Benchmarks](https://github.com/xbow-engineering/validation-benchmarks)
- [CyberSecEval](https://github.com/meta-llama/PurpleLlama/tree/main/CybersecurityBenchmarks)
- [CVE-Bench](https://github.com/uiuc-kang-lab/cve-bench)



