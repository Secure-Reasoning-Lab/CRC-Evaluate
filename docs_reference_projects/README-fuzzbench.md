# FuzzBench Reference Documentation

This directory contains documentation summarizing the FuzzBench reference project at `claude_reference_projects/fuzzbench/`.

## Contents

### 1. fuzzbench-docker-build-process.md

**Comprehensive guide** to how FuzzBench converts benchmarks into Docker images.

Covers:
- Directory structure of benchmarks, docker, and fuzzers directories
- Key files involved in the build process
- Detailed explanation of each Dockerfile type
- Step-by-step build process from benchmark definition to runtime image
- Code snippets showing key components
- Design patterns and philosophy

**Use this when**: You need to understand the actual mechanics of how docker images are created from benchmark specifications.

### 2. fuzzbench-build-architecture.md

**Visual and conceptual guide** to the FuzzBench build architecture.

Covers:
- High-level architecture diagrams
- Dependency chains with visual ASCII art
- File organization
- Data flow through the build process
- Build stages in detail
- Makefile rule examples
- Key insights and design patterns

**Use this when**: You need a visual understanding of how the pieces fit together or want to see the dependency graph.

## Quick Reference: The Build Flow

```
Benchmarks (user input)
  ↓
Templates (image_types.yaml)
  ↓
Instantiation (docker_images.py)
  ↓
Makefile Generation (generate_makefile.py)
  ↓
Docker Builds (make build-...)
  ↓
Runtime Images (ready for trials)
```

## Key Concepts

### Benchmarks
Located in `benchmarks/{project}_{fuzz_target}/`:
- `Dockerfile`: Sets up benchmark source using OSS-Fuzz base-builder
- `benchmark.yaml`: Stores critical metadata (commit hash, fuzz target name)
- `build.sh`: OSS-Fuzz compatible build script
- `seeds/`: Optional initial corpus

### Fuzzers
Located in `fuzzers/{fuzzer_name}/`:
- `fuzzer.py`: Contains `build()` and `fuzz()` functions
- `builder.Dockerfile`: Fuzzer-specific build environment
- `runner.Dockerfile`: Fuzzer-specific runtime environment

### Docker Infrastructure
Located in `docker/`:
- `image_types.yaml`: Template definitions for all image types and their dependencies
- `docker_images.py`: Instantiates templates for fuzzer-benchmark pairs
- `generate_makefile.py`: Generates Makefile build rules
- `base-image/Dockerfile`: Python 3.10 + tools
- `benchmark-builder/Dockerfile`: Orchestrates the compilation
- `benchmark-runner/Dockerfile`: Packages final artifacts

## Build Process in 6 Steps

1. **Template Definition**: `image_types.yaml` defines reusable image types with placeholders
2. **Instantiation**: For each (fuzzer, benchmark) pair, substitute placeholders
3. **Makefile Generation**: Generate build targets with dependency tracking
4. **Benchmark Builder**: Use OSS-Fuzz base-builder to set up benchmark source
5. **Fuzzer Integration**: Add fuzzer-specific tools and compile
6. **Runtime Packaging**: Multi-stage build creates minimal runtime image

## Multi-Stage Build Advantage

```
Builder Stage (large)              Runner Stage (minimal)
├─ Compilers                       ├─ Runtime libraries
├─ Source code                     └─ Compiled artifacts
├─ Build tools
└─ Compilation artifacts (copied to runner)
```

This approach keeps runtime images small while enabling efficient building.

## How to Read the Docs

**If you have 5 minutes:**
- Read this README
- Look at the architecture diagrams in fuzzbench-build-architecture.md

**If you have 15 minutes:**
- Read the overview section of fuzzbench-docker-build-process.md
- Study the "Build Process Step-by-Step" section
- Scan the directory structure diagrams

**If you have 30+ minutes:**
- Read fuzzbench-docker-build-process.md completely
- Study fuzzbench-build-architecture.md in detail
- Review code snippets from actual files

## Applying This to CRSBench

Key lessons from FuzzBench for CRSBench:

1. **Template-Based System**: Use YAML templates to define image types once, instantiate many times
2. **Modular Dockerfiles**: Each Dockerfile has a single responsibility
3. **Dynamic Integration**: Use Python dynamic imports instead of per-component build scripts
4. **Metadata Driven**: Store critical information (commit hash, fuzzer config) in YAML files
5. **Multi-Stage Optimization**: Separate builder and runner images
6. **Makefile Generation**: Automatically generate build rules from configuration

## Files in FuzzBench Repository

### Key Build Files
```
docker/
├── image_types.yaml               # Configuration: Image templates
├── generate_makefile.py           # Generation: Creates build rules
├── base-image/Dockerfile          # Base infrastructure
├── benchmark-builder/Dockerfile   # Orchestrates compilation
├── benchmark-runner/Dockerfile    # Packages artifacts
└── benchmark-builder/
    ├── checkout_commit.py         # Reproducibility
    └── fuzzer_build               # Dynamic fuzzer invocation

experiment/build/
└── docker_images.py               # Instantiation: Template substitution
```

### Benchmark Examples
```
benchmarks/libpng_libpng_read_fuzzer/
├── Dockerfile                     # OSS-Fuzz base + benchmark setup
├── benchmark.yaml                 # Metadata
├── build.sh                        # Build script
└── seeds/                          # Seed corpus
```

### Fuzzer Examples
```
fuzzers/libfuzzer/
├── fuzzer.py                       # build() and fuzz() functions
├── builder.Dockerfile             # Compile environment
└── runner.Dockerfile              # Runtime environment
```

## References

- FuzzBench Repository: `/Users/fuyu0425/aixcc/CRSBench/claude_reference_projects/fuzzbench/`
- FuzzBench Docs: `/docker/`, `/docs/`
- CRSBench Project Instructions: `/CLAUDE.md`

## See Also

- CRSBench Benchmark Specification: `/docs/benchmark-spec.md`
- CRS Interface Design: `/docs/ossfuzz-crs-interface.md`
