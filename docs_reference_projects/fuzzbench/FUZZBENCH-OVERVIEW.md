# FuzzBench Docker Build System - Complete Overview

This directory contains comprehensive documentation of the FuzzBench reference project's Docker image build process.

## Quick Navigation

For different levels of detail, start with:

1. **5-minute overview**: Read this file, then skim the README-fuzzbench.md
2. **15-minute understanding**: Read README-fuzzbench.md + Quick Reference sections
3. **Complete technical deep dive**: Read all three documents in order

## Documentation Files

### 1. README-fuzzbench.md
High-level guide and quick reference. Start here to understand:
- What each document covers
- The 6-step build process
- Key concepts (benchmarks, fuzzers, docker infrastructure)
- How to apply lessons to CRSBench

### 2. fuzzbench-docker-build-process.md
Technical details of the complete build process. Learn:
- Full directory structure of benchmarks/, docker/, fuzzers/
- How each Dockerfile works with code snippets
- The complete 6-step build process with detailed explanations
- Design patterns and philosophy
- Environment variables used throughout

### 3. fuzzbench-build-architecture.md
Visual and conceptual architecture guide. See:
- ASCII diagrams of the complete build flow
- Dependency chains illustrated
- Data flow through the system
- Build stages broken down
- Real Makefile rule examples
- Key architectural insights

## The Build System at a Glance

FuzzBench uses a sophisticated template-based system to build Docker images for fuzzing:

```
                    CONFIGURATION
                    ══════════════
                         │
    image_types.yaml ────┤── Templates for image types
    benchmarks/ ─────────┤── Benchmark definitions
    fuzzers/ ────────────┤── Fuzzer implementations
                         │
                         ▼
                  INSTANTIATION
                  ══════════════
                         │
        docker_images.py ├── Instantiates templates for each
                         │   fuzzer-benchmark pair
                         │
                         ▼
                   GENERATION
                   ════════════
                         │
       generate_makefile.py ├── Creates Makefile with
                            │   build rules & dependencies
                            │
                            ▼
                      BUILD EXECUTION
                      ═══════════════
                            │
                      make build-X ├── Docker builds images
                                   │   following dependency chain
                                   │
                                   ▼
                          FINAL IMAGES
                          ════════════
                            Ready for fuzzing trials
```

## Core Concepts Explained

### Benchmarks
Each benchmark is an OSS-Fuzz project with:
- **Dockerfile**: Inherits from OSS-Fuzz base-builder, sets up benchmark source
- **benchmark.yaml**: Metadata (commit hash, fuzz target name) for reproducibility
- **build.sh**: OSS-Fuzz standard build script
- **seeds/**: Initial test cases for fuzzing

### Fuzzers
Each fuzzer implements the FuzzBench standard interface:
- **fuzzer.py**: Contains `build()` and `fuzz()` functions
- **builder.Dockerfile**: Fuzzer-specific build environment
- **runner.Dockerfile**: Fuzzer-specific runtime environment

### Docker Infrastructure
FuzzBench provides the glue that connects benchmarks and fuzzers:
- **image_types.yaml**: Template definitions (single source of truth)
- **docker_images.py**: Instantiation engine (substitutes placeholders)
- **generate_makefile.py**: Makefile generator (creates build rules)
- **Orchestrator Dockerfiles**: benchmark-builder/ and benchmark-runner/

## The Build Pipeline

When you run `make build-libfuzzer-libpng_libpng_read_fuzzer`:

1. **Level 1**: base-image
   - Ubuntu 20.04 + Python 3.10.8 + cloud tools

2. **Level 2**: benchmark-project-builder
   - OSS-Fuzz base-builder + benchmark dependencies
   - Source code cloned to /src

3. **Level 3**: fuzzer-intermediate-builder
   - Adds fuzzer-specific libraries (e.g., libFuzzer.a)

4. **Level 4**: Final builder
   - Reads benchmark.yaml to get commit hash
   - Checks out exact benchmark version
   - Calls fuzzer.build() to compile
   - Produces /out/{fuzz_target} binary

5. **Level 5**: Runner intermediate
   - Minimal runtime environment for the fuzzer

6. **Level 6**: Final runner
   - Multi-stage copy of /out artifacts
   - Complete runtime image with framework code
   - Ready to execute fuzzing trials

## Key Design Decisions

### 1. Template-Based Instantiation
**Why**: Avoid code duplication for N fuzzer × M benchmark combinations

**How**: 
- image_types.yaml defines templates with {fuzzer} and {benchmark} placeholders
- docker_images.py substitutes placeholders for each pair
- generate_makefile.py creates build rules automatically

**Benefit**: Adding new fuzzer/benchmark only requires new files, no orchestration code changes

### 2. Multi-Stage Builds
**Why**: Keep runtime images small while maintaining build ability

**How**:
- Large builder image with compilers, source, tools
- Small runner image with only artifacts + runtime deps
- `COPY --from=builder` transfers compiled binaries

**Benefit**: Runtime images are 5-10x smaller, faster to deploy

### 3. Dynamic Python Integration
**Why**: Avoid per-fuzzer shell scripts and build scaffolding

**How**: Single fuzzer_build script uses Python dynamic imports:
```bash
python3 -c "from fuzzers.$FUZZER import fuzzer; fuzzer.build()"
```

**Benefit**: All fuzzers implement standard interface, easy to add new ones

### 4. Metadata-Driven Reproducibility
**Why**: Ensure builds are reproducible across time

**How**: benchmark.yaml stores exact commit hash, checkout_commit.py checks out that version

**Benefit**: Different benchmarks can use different versions of same project

### 5. Configuration Over Code
**Why**: Single source of truth for system structure

**How**: image_types.yaml defines all image types and dependencies in one place

**Benefit**: Dependency graph is explicit and reviewable

## File Organization

```
FuzzBench Root
├── benchmarks/
│   ├── libpng_libpng_read_fuzzer/
│   │   ├── Dockerfile (OSS-Fuzz base + setup)
│   │   ├── benchmark.yaml (commit, target, metadata)
│   │   ├── build.sh (OSS-Fuzz build script)
│   │   └── seeds/ (test corpus)
│   └── [more benchmarks...]
│
├── fuzzers/
│   ├── libfuzzer/
│   │   ├── fuzzer.py (build() and fuzz() functions)
│   │   ├── builder.Dockerfile (build environment)
│   │   └── runner.Dockerfile (runtime environment)
│   └── [more fuzzers...]
│
├── docker/
│   ├── image_types.yaml (CONFIGURATION: Templates)
│   ├── generate_makefile.py (GENERATION: Creates rules)
│   ├── base-image/
│   │   └── Dockerfile (Python 3.10 + tools)
│   ├── benchmark-builder/
│   │   ├── Dockerfile (ORCHESTRATOR: Compilation)
│   │   ├── checkout_commit.py (Reproducibility)
│   │   └── fuzzer_build (Dynamic invocation)
│   └── benchmark-runner/
│       ├── Dockerfile (ORCHESTRATOR: Packaging)
│       └── startup-runner.sh (Entrypoint)
│
├── experiment/build/
│   └── docker_images.py (INSTANTIATION: Templates → images)
│
└── [other directories...]
```

## How Each Part Works Together

### image_types.yaml
**Role**: Define templates for all image types

**Contains**: Image definitions with {fuzzer} and {benchmark} placeholders

**Used by**: docker_images.py

### docker_images.py
**Role**: Instantiate templates for each fuzzer-benchmark pair

**Process**:
1. Read image_types.yaml
2. For each (fuzzer, benchmark) pair:
   - Substitute placeholders
   - Create concrete image definition

**Output**: Dictionary of image objects with resolved tags, dockerfiles, build args

**Used by**: generate_makefile.py

### generate_makefile.py
**Role**: Create Makefile rules from image definitions

**Process**:
1. Call docker_images.get_images_to_build()
2. For each image:
   - Extract dockerfile, context, build args, dependencies
   - Generate make rule with proper targets

**Output**: Makefile with rules like:
```makefile
.libfuzzer-libpng-builder: .libpng-project-builder
	docker build --tag ... [args] ...
```

**Used by**: make command

### Benchmark Dockerfile
**Role**: Prepare benchmark source with OSS-Fuzz setup

**Inherits from**: gcr.io/oss-fuzz-base/base-builder

**Sets up**:
- Benchmark dependencies
- Source code in /src
- Dictionary files in /out
- Seed corpus

**Used by**: fuzzer-intermediate-builder as parent image

### Fuzzer builder.Dockerfile
**Role**: Add fuzzer-specific compilation tools

**Inherits from**: Benchmark project builder image

**Sets up**:
- Fuzzer runtime libraries
- Fuzzer-specific compilers/tools
- Fuzzer configurations

**Used by**: benchmark-builder/Dockerfile as parent_image

### benchmark-builder/Dockerfile
**Role**: Orchestrate the actual compilation

**Inherits from**: Fuzzer intermediate builder

**Does**:
1. Copy Python 3.10 from base-image
2. Read benchmark.yaml for commit hash
3. Check out exact benchmark version
4. Call fuzzer.build() dynamically
5. Compile fuzz target with fuzzer flags
6. Produce /out/{fuzz_target} binary

**Output**: Built binary and artifacts ready to run

**Used by**: benchmark-runner/Dockerfile (as builder stage)

### benchmark-runner/Dockerfile
**Role**: Package final runtime image

**Multi-stage**:
1. Copy artifacts from builder image
2. Inherit from fuzzer runner intermediate
3. Install runtime dependencies
4. Set up execution environment

**Output**: Ready-to-run image for fuzzing trials

## Environment Variables

Key env vars used in the build:

| Variable | Set by | Used in | Purpose |
|----------|--------|---------|---------|
| `$SRC` | OSS-Fuzz base | build.sh | Source directory |
| `$OUT` | OSS-Fuzz base | build.sh | Output artifacts |
| `$WORK` | OSS-Fuzz base | build.sh | Working directory |
| `$FUZZER` | benchmark-builder | fuzzer_build | Fuzzer name |
| `$BENCHMARK` | benchmark-builder | checkout_commit.py | Benchmark name |
| `$FUZZ_TARGET` | runtime | fuzzing loop | Target binary name |
| `$PYTHONPATH` | benchmark-builder | fuzzer_build | Python import path |
| `$ASAN_OPTIONS` | benchmark-builder | compiler | Sanitizer settings |

## Applying to CRSBench

FuzzBench's architecture provides a proven template for CRSBench:

1. **Use YAML for configuration** → crs-types.yaml template system
2. **Dynamic Python integration** → crs.build() and crs.run()
3. **Separate builder/runner** → Build artifacts in builder, execute in runner
4. **Metadata storage** → crs.yaml for CRS config, reproducibility
5. **Auto-generated rules** → Generate Makefile from crs-types.yaml
6. **Multi-stage builds** → Minimize runtime image size

## Document Reading Tips

### If you're new to FuzzBench:
1. Start with this file (FUZZBENCH-OVERVIEW.md)
2. Read README-fuzzbench.md for concepts
3. Look at diagrams in fuzzbench-build-architecture.md
4. Read the overview section of fuzzbench-docker-build-process.md

### If you want implementation details:
1. Read fuzzbench-docker-build-process.md completely
2. Study code snippets in the "Key Files" section
3. Reference the architecture diagrams

### If you want to understand specific components:
- Benchmarks: See "Directory Structure" in fuzzbench-docker-build-process.md
- Dockerfiles: See "Key Files" → "4. Dockerfiles" section
- Image instantiation: Read "Step 3: Image Instantiation" in build process doc
- Build orchestration: Read "Step 5: Makefile Generation" in build process doc

## Files Analyzed

**Core Infrastructure**:
- docker/image_types.yaml
- docker/generate_makefile.py
- experiment/build/docker_images.py
- docker/base-image/Dockerfile
- docker/benchmark-builder/Dockerfile (2 versions)
- docker/benchmark-runner/Dockerfile
- docker/benchmark-builder/checkout_commit.py
- docker/benchmark-builder/fuzzer_build

**Examples**:
- benchmarks/libpng_libpng_read_fuzzer/Dockerfile
- benchmarks/libpng_libpng_read_fuzzer/benchmark.yaml
- fuzzers/libfuzzer/fuzzer.py (excerpt)
- fuzzers/libfuzzer/builder.Dockerfile
- fuzzers/libfuzzer/runner.Dockerfile

## Total Documentation

- 3 comprehensive markdown documents
- 2,000+ lines of detailed technical content
- 25+ code snippets
- 8+ ASCII diagrams
- Complete architecture explanation

## Questions to Test Understanding

1. Why does FuzzBench use templates in image_types.yaml?
2. How does dynamic Python import reduce code duplication?
3. What are the 6 build levels and what does each do?
4. Why separate builder and runner Dockerfiles?
5. How is reproducibility maintained across builds?
6. What files does a new benchmark need?
7. What files does a new fuzzer need?
8. How does generate_makefile.py know what to build?
9. Where is the actual build() function called?
10. How is the benchmark commit hash used?

(Answers in the detailed documents)

## Additional References

- FuzzBench Repository: `/Users/fuyu0425/aixcc/CRSBench/claude_reference_projects/fuzzbench/`
- FuzzBench Docs: `/docs/` in the repository
- CRSBench Architecture: `/docs/architecture.md` or design-docs/architecture.md
- CRS Interface: `/docs/ossfuzz-crs-interface.md`
