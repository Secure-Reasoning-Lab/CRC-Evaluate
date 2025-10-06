---
author: Yu-Fu Fu
---

# RFC for Standard Benchmark Specification for Cyber Reasoning Systems (CRS) v0.1

This specification defines a standardized benchmark format for
evaluating Cyber Reasoning Systems (CRS) developed during AIxCC competition.

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
├── (Other files used in the Dockerfile)
└── .aixcc/
    ├── meta.yaml                # Configuration metadata
    ├── ref.diff                 # Delta mode reference diff
    ├── test.sh                  # Invariant checking script
    └── [harness_name]/
        └── [pov-keyword]/
            ├── {pov-keyword}.md         # Vulnerability description
            ├── {pov-variant-id}.blob    # Input data/payload
            ├── {pov-variant-id}.log     # Sanitizer/crash outputs
            └── patches/
                ├── {patch-variant-id}.patch  # Bug-fixing patch
                └── {patch-variant-id}.patch  # Bug-fixing patch
```


### Configuration File (meta.yaml)

The `meta.yaml` file defines the benchmark configuration, specifying
evaluation modes, harness files, and vulnerability detection criteria.

```yaml
# Patch exclusion list - files that patches cannot modify (global setting)
patch_exclude_list:
  - "build.sh"                    # Build scripts are immutable
  - "Makefile"                    # Build configuration files
  - "CMakeLists.txt"             # Build system files
  - "configure*"                  # Configuration scripts
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
    path: "$REPO/test/customfuzz3.c"
    # Proof of Vulnerability configurations for customfuzz3
    povs:
      - name: "pov_0"
        sanitizer: "address"
        error_token: "ERROR: AddressSanitizer: stack-buffer-overflow"
        requires_clean_build: true
      - name: "pov_1"
        sanitizer: "memory"
        error_token: "ERROR: MemorySanitizer: use-of-uninitialized-value"
        requires_clean_build: false
```

## Configuration Fields

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
- Dynamic analyis enabled.

### Patch Exclusion List

The `patch_exclude_list` configuration defines files and patterns that CRS-generated patches are forbidden from modifying. This ensures critical infrastructure remains unchanged during patch evaluation.

#### Purpose and Rationale

- **Build System Protection**: Prevents modification of build scripts, makefiles, and configuration files
- **Harness Integrity**: Protects fuzzing harnesses from being altered or disabled
- **Metadata Security**: Safeguards benchmark configuration and ground truth data
- **Test Isolation**: Prevents patches from modifying test files to artificially pass validation
- **Fair Evaluation**: Ensures all CRS systems operate under identical constraints

#### Supported Patterns

The exclusion list supports various file matching patterns:

- **Exact filenames**: `"build.sh"`, `"Makefile"`
- **Glob patterns**: `"*.ac"`, `"configure*"`
- **Directory patterns**: `"docs/**"`, `".aixcc/**"`
- **Complex patterns**: `"**/*test*.c"` (any file containing "test" in name)

#### Common Exclusion Categories

- **Build Infrastructure**: `build.sh`, `Makefile`, `CMakeLists.txt`, `configure*`, `*.ac`, `*.am`
- **Benchmark Files**: `.aixcc/**`, `meta.yaml`, `*.patch`, `*.blob`
- **Harness Files**: Fuzzing harness source files as specified in harness_files configuration
- **Test Files**: Unit tests, integration tests, and validation scripts
- **Documentation**: `README*`, `docs/**`, `*.md`
- **Version Control**: `.git/**`, `.gitignore`, `.gitmodules`

#### Enforcement

The patch exclusion list is enforced during:

- **Patch Validation**: Before applying patches to source code
- **Build Process**: Docker build fails if excluded files are modified
- **Evaluation Scoring**: Violations result in patch rejection and scoring penalties

### Harness Files

Define the test harnesses used for vulnerability discovery:

- Support multiple harnesses per project
- Link harnesses source code to their filesystem locations within the
  repository
- Harnesses without POV configurations serve as distractor harnesses or baseline
  tests
- CRS systems must analyze all provided harnesses and prioritize those most
  likely to trigger vulnerabilities
- This tests CRS capability to distinguish between productive and
  unproductive fuzzing targets

### Proof of Vulnerability (POV)

Defines how vulnerabilities are detected and verified:

- Multiple POVs allowed per harness to test different vulnerability
  types
- Specifies detection method (sanitizer type: address, memory, etc.)
- Defines error patterns for automated verification and result matching
- Error tokens are matched using substring matching against sanitizer output
- Used for deduplicating discovered vulnerabilities based on error
  signatures
- `requires_clean_build`: Boolean flag indicating whether this POV requires a clean build (e.g., header modifications, template changes)

### Benchmark Components - Ground Truth

Each harness directory contains the following files for each Proof of
Vulnerability (POV). For example, with POVs `pov_0` and `pov_1`:

- `{pov-keyword}.md` - Human-readable vulnerability description and
  analysis (e.g., `pov_0.md`, `pov_1.md`)
- `{pov-keyword}.blob` - Binary input data, payloads, or test cases that
  trigger the vulnerability (e.g., `pov_0.blob`, `pov_1.blob`)
- `{pov-keyword}.patch` - Code patch that fixes the identified
  vulnerability (e.g., `pov_0.patch`, `pov_1.patch`)
- `{pov-keyword}.log` - Sanitizer crash output or exception logs
  demonstrating the vulnerability (e.g., `pov_0.log`, `pov_1.log`)

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

- **Base Image Caching**: Pre-compiled dependencies cached in base images
- **Layer Reuse**: Unchanged Docker layers are reused across different patch evaluations
- **Incremental Compilation**: Build systems (Make, CMake) handle incremental builds within containers
- **Parallel Builds**: Multiple patch evaluations can run simultaneously in separate containers

### Usage Examples

```bash
# Build Docker image for specific patch evaluation
docker build -t project-pov0 .

# Run evaluation with patched image
docker run --name eval-pov0 project-pov0

# Extract build artifacts from container
docker cp eval-pov0:/out ./evaluation-results/pov_0/
```

### Helper Script Integration

The evaluation framework provides automated Docker image management:

```bash
# Build evaluation image for specific POV
helper.py build-image --pov pov_0 --tag project-pov0

# Run complete evaluation workflow
helper.py evaluate --pov pov_0 --output-dir ./results/

# Compare multiple patch evaluations
helper.py compare --baseline project-base --patches pov_0,pov_1,pov_2
```

The helper script automatically:
- Reads patch information from `meta.yaml`
- Builds appropriate Docker images for each evaluation
- Manages container lifecycle and artifact collection
- Provides consistent evaluation environment setup

### Build System Constraints

The Docker-based build system enforces the following constraints to ensure fair evaluation:

- **Immutable build scripts**: CRS cannot modify build.sh or Docker build configuration
- **Codebase-only patches**: CRS can only generate patches that modify source code files
- **Fixed build environment**: Compiler flags, dependencies, and build options are predetermined in base images
- **Reproducible builds**: All builds are deterministic through Docker layer caching and fixed base images
- **Container isolation**: Each patch evaluation runs in a completely isolated container environment
- **Upstreamed location**: build.sh is located at `projects/[name]/build.sh` following OSS-Fuzz convention

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
./test.sh --coverage-guided --patch-file=pov_0.patch

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

- Check whether compiled
- Check crash no longer happens
- Unit tests
- Differential Testing
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

- **POV (Proof of Vulnerability)**: An input that triggers a crash when executed against a fuzzing harness
- **POV Deduplication**: POVs are deduplicated based on root cause analysis, not sanitizer error type
  - Multiple POVs with the same sanitizer error may represent different vulnerabilities (different root causes)
  - Multiple POVs with different stack traces may represent the same vulnerability (same root cause, different code paths)
- **Purpose**: Multiple deduplicated POVs (same root cause) provide different ways to trigger the same vulnerability, aiding root cause analysis for accurate patch generation

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

- **Pattern Recognition**: Identifying common elements across different ways to trigger the same vulnerability
- **Code Path Analysis**: Understanding various paths that lead to the same underlying bug
- **Attack Vector Coverage**: Seeing different input variations that exploit the same root cause
- **Completeness Validation**: Ensuring patches address all known ways to trigger the vulnerability
- **Regression Prevention**: Verifying fixes work across all provided attack scenarios

#### Hint System for Additional Guidance

Beyond POV quantity, the benchmark supports optional hints that provide varying levels of vulnerability guidance:

##### Hint Types and Examples

- **Vulnerability Category Hints**: "The bug is a buffer overflow", "Memory safety issue present", "Use-after-free vulnerability"
- **Location Hints**: "The bug is in the parsing module", "Vulnerability in file processing code", "Issue in network handling functions"
- **Severity Hints**: "Critical security vulnerability", "Exploitable memory corruption", "Remote code execution possible"
- **Technical Hints**: "Missing bounds checking", "Unvalidated input processing", "Race condition in threading code"

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

##### Hint-Based Difficulty Modulation

Hints create additional difficulty dimensions:

- **No Hints + No POVs**: Maximum difficulty - pure vulnerability discovery
- **Basic Hints + No POVs**: High difficulty - guided discovery without crash examples
- **Detailed Hints + Multiple POVs**: Lower difficulty - comprehensive guidance with crash data
- **Progressive Hints**: CRS can request increasingly specific hints with cost penalties

#### Initial Corpus Variations

Beyond POVs and hints, the benchmark supports providing different levels of initial fuzzing corpus to control discovery difficulty:

##### Corpus Quality Levels

- **No Corpus**: Empty starting corpus - CRS must generate inputs from scratch
- **Minimal Corpus**: Basic valid inputs (e.g., empty file, simple valid format)
- **Seed Corpus**: Representative valid inputs covering basic functionality
- **Rich Corpus**: Comprehensive inputs covering various features and edge cases
- **Targeted Corpus**: Inputs specifically designed to exercise vulnerable code paths

##### Corpus Coverage Characteristics

Each corpus level provides different code coverage and fuzzing guidance:

**No Corpus (Highest Difficulty)**
- No initial inputs provided
- CRS must synthesize test inputs from format specifications or examples
- Fuzzer starts with random or grammar-based generation
- Maximum time required to achieve meaningful coverage

**Minimal Corpus (High Difficulty)**
- 1-3 very basic valid inputs (e.g., "hello", empty file, single valid packet)
- Provides basic format understanding
- Limited coverage of input space
- Requires extensive mutation to find interesting paths

**Seed Corpus (Medium Difficulty)**
- 10-50 representative inputs covering normal functionality
- Achieves reasonable baseline code coverage (20-40%)
- Covers main code paths but not edge cases
- Good starting point for mutation-based fuzzing

**Rich Corpus (Lower Difficulty)**
- 100+ diverse inputs covering extensive functionality
- High baseline code coverage (60-80%)
- Includes various input formats, sizes, and structures
- Enables rapid discovery of shallow bugs

**Targeted Corpus (Lowest Difficulty)**
- Inputs specifically crafted to exercise vulnerable functions
- Very high coverage of vulnerability-adjacent code
- May include inputs that are "almost crashes" (near-miss cases)
- Significantly reduces time to vulnerability discovery

##### Corpus Integration with Meta.yaml

```yaml
harness_files:
  - name: "customfuzz3"
    path: "$REPO/test/customfuzz3.c"
    # Corpus configuration for different difficulty levels
    corpus:
      level-0: "corpus-fuzz-level-0"
      level-1: "corpus-fuzz-level-1"
      level-2: "corpus-fuzz-level-2"
      level-3: "corpus-fuzz-level-3"
      level-4: "corpus-fuzz-level-4"
    default_corpus_level: "level-2"
```

##### Corpus-Based Difficulty Impact

Initial corpus quality affects CRS performance in multiple ways:

- **Discovery Time**: Better corpus reduces time to find first crashes
- **Coverage Efficiency**: Rich corpus enables faster code exploration
- **Mutation Effectiveness**: Good seeds provide better mutation starting points
- **Vulnerability Reachability**: Targeted corpus guides fuzzing toward vulnerable code

#### Combined Difficulty Matrix

The final difficulty level combines POV quantity, hint availability, and initial corpus quality. The following is an example multi-dimensional matrix:

##### Base Matrix (POVs + Hints)

| POVs | No Hints | Basic Hints | Detailed Hints |
|------|----------|-------------|----------------|
| 0 POVs | Highest | High | Medium-High |
| 1 POV | High | Medium-High | Medium |
| 2-3 POVs | Medium-High | Medium | Low-Medium |
| 4+ POVs | Medium | Low-Medium | Lowest |

##### Corpus Modulation

Each base difficulty level can be further modified by corpus quality:

- **+ No Corpus**: Increases difficulty by 1 level (e.g., Medium → High)
- **+ Minimal Corpus**: No change to base difficulty
- **+ Seed Corpus**: Decreases difficulty by 0.5 levels
- **+ Rich Corpus**: Decreases difficulty by 1 level (e.g., High → Medium)
- **+ Targeted Corpus**: Decreases difficulty by 1.5 levels

##### Example Combined Scenarios

- **0 POVs + No Hints + No Corpus**: Maximum difficulty - pure vulnerability research
- **1 POV + Basic Hints + Seed Corpus**: Medium difficulty - guided fuzzing with crash example
- **Multiple POVs + Detailed Hints + Rich Corpus**: Lowest difficulty - comprehensive guidance
- **0 POVs + No Hints + Targeted Corpus**: High difficulty - corpus-guided discovery without crash evidence

#### Baseline Fuzzing Difficulty Metric

The benchmark includes a baseline time-to-discovery metric that measures how long a naive fuzzer (like AFL) takes to uncover the vulnerability:

##### Baseline Time-to-Discovery

- **Definition**: Time required for AFL or similar coverage-guided fuzzer to find the first crash
- **Measurement**: Median time across multiple fuzzing runs with standard configuration
- **Purpose**: Provides objective difficulty scoring independent of CRS capabilities
- **Storage**: Recorded in `meta.yaml` for each POV

##### Difficulty Scoring Integration

Baseline fuzzing time contributes to overall difficulty assessment:

- **Fast Discovery (< 1 hour)**: Lower difficulty - easily discoverable bugs
- **Moderate Discovery (1-24 hours)**: Medium difficulty - requires sustained fuzzing
- **Slow Discovery (1-7 days)**: High difficulty - challenging to find
- **Very Slow Discovery (> 7 days)**: Highest difficulty - deep or complex bugs

##### Meta.yaml Integration

```yaml
harness_files:
  - name: "ossfuzz"
    path: "$REPO/test/ossfuzz.c"
    povs:
      - name: "buffer_overflow_main"
        sanitizer: "address"
        error_token: "ERROR: AddressSanitizer: heap-buffer-overflow"
        baseline_fuzzing_time_seconds: 14400  # 4 hours median discovery time
        baseline_fuzzer: "AFL++"
        baseline_config: "default"
        difficulty_factors:
          pov_count: 3
          hint_level: "basic"
          baseline_time: "moderate"
```

#### Real-World Mapping

The combined POV, hint, corpus, and baseline fuzzing levels correspond to different real-world scenarios:

- **0 POVs + No Hints + No Corpus + Slow Baseline**: Maximum difficulty - novel vulnerability research with minimal tooling
- **1 POV + Basic Hints + Minimal Corpus + Fast Baseline**: Medium difficulty - obvious bug with basic fuzzing setup
- **Multiple POVs + Detailed Hints + Rich Corpus + Fast Baseline**: Low difficulty - well-documented vulnerability with comprehensive testing infrastructure
- **0 POVs + No Hints + Targeted Corpus + Moderate Baseline**: High difficulty - guided discovery using sophisticated corpus engineering
- **Progressive Hints + Seed Corpus + Variable Baseline**: Security research with incremental information and standard fuzzing practices

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

#### Usage Integration

```bash
# Generate fuzz introspector report for project
fuzz-introspector --target-dir=$SRC --output-dir=$WORK/introspector

# Analyze specific harness effectiveness
fuzz-introspector --harness=$OUT/harness_binary --coverage-dir=$WORK/coverage

# Compare multiple fuzzing approaches
fuzz-introspector --compare --baseline=$WORK/baseline --current=$WORK/current
```

### LiteLLM Universal Interface

LiteLLM provides a unified interface for interacting with multiple Large Language Model providers, enabling fair comparison between CRS systems regardless of their chosen LLM backend.

#### Multi-Provider Support

LiteLLM supports consistent API access across major LLM providers:

- OpenAI (GPT-3.5, GPT-4, GPT-4-turbo, GPT-o1)
- Anthropic (Claude-3, Claude-3.5-Sonnet, Claude-3.5-Haiku)
- Google (Gemini Pro, Gemini Ultra)
- Open-source models (Llama, CodeLlama, etc.)
- Local model deployments (Ollama, vLLM, etc.)

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
execution across different environments, enabling deterministic replay and
validation of CRS evaluation results.

### Goal

- Enable deterministic replay of CRS evaluation runs
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

#### LLM Interaction Logging

- Request payloads with exact timestamps
- Response data with processing times
- Token usage statistics (input/output/cached)
- API rate limiting and retry information
- Cache hit/miss ratios and patterns

### Validation and Integrity

#### Replay Validation

- Verification that replay produces identical results
- Checkpoints for validating intermediate states
- Error handling for replay divergences

### Replay Capabilities

#### Deterministic Replay

Mock all non-deterministic operations:

- LLM API responses return archived data with original timing
- Fuzzer outputs replay from archived results with timestamps
- Random number generation uses archived seed sequences
- File system operations replay from captured snapshots

#### Partial Replay Support

- Checkpoint-based replay from specific time points
- Fast-forward capabilities for long executions
- Selective replay of specific components (LLM-only, fuzzing-only)
- Differential replay comparing multiple CRS runs

#### Speed Controls

- Real-time replay maintaining original timing
- Accelerated replay for analysis purposes
- Slow-motion replay for detailed examination
- Step-by-step execution for debugging

### Privacy and Security

#### Sensitive Data Handling

- Automatic redaction of API keys and credentials
- Anonymization of personally identifiable information

### Analysis Tools

#### Archive Inspection

- Command-line tools for examining archive contents
- Metadata extraction without full replay
- Timeline visualization of execution events
- Resource usage analysis and graphing

#### Comparative Analysis

- Side-by-side comparison of multiple CRS runs
- Performance difference analysis
- Strategy variation identification
- Success rate comparisons across different scenarios

#### Validation Tools

- Archive integrity verification scripts
- Replay consistency checking
- Performance regression detection
- Format migration utilities for version updates

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



