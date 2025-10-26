# FuzzBench Docker Build Architecture - Visual Guide

## High-Level Architecture Diagram

```
                                FuzzBench Docker Build Flow
                                ============================

STEP 1: TEMPLATE DEFINITION
━━━━━━━━━━━━━━━━━━━━━━━━━━━
  image_types.yaml
    ├── {benchmark}-project-builder
    ├── {fuzzer}-{benchmark}-builder-intermediate
    ├── {fuzzer}-{benchmark}-builder
    ├── {fuzzer}-{benchmark}-intermediate-runner
    └── {fuzzer}-{benchmark}-runner


STEP 2: INSTANTIATION
━━━━━━━━━━━━━━━━━━━━━
  For each (fuzzer, benchmark) pair:
    docker_images.get_images_to_build(fuzzers, benchmarks)
      └─> Substitutes {fuzzer} and {benchmark} placeholders


STEP 3: MAKEFILE GENERATION
━━━━━━━━━━━━━━━━━━━━━━━━━━
  generate_makefile.py
    └─> Creates build targets:
          build-libfuzzer-libpng_libpng_read_fuzzer
          build-aflplusplus-libpng_libpng_read_fuzzer
          etc.


STEP 4: BUILD EXECUTION
━━━━━━━━━━━━━━━━━━━━━━
  make build-libfuzzer-libpng_libpng_read_fuzzer
    └─> Executes dependency chain (see below)
```

## Dependency Chain for a Single Fuzzer-Benchmark Pair

Example: Building libfuzzer for libpng_libpng_read_fuzzer benchmark

```
┌─────────────────────────────────────────────────────────────────────┐
│                    BUILD DEPENDENCY CHAIN                           │
└─────────────────────────────────────────────────────────────────────┘

Level 1: Base Infrastructure
┌──────────────────────────────────────┐
│      base-image                      │
│  Ubuntu 20.04 + Python 3.10.8       │
│  + Google Cloud SDK                  │
└──────────────────────────────────────┘
            │
            ▼ (depends on)

Level 2: Benchmark Foundation
┌──────────────────────────────────────────────────────────────┐
│  libpng_libpng_read_fuzzer-project-builder                  │
│  (FROM gcr.io/oss-fuzz-base/base-builder)                  │
│                                                              │
│  ✓ Installs benchmark dependencies (zlib, libtool, etc.)  │
│  ✓ Clones benchmark source code                           │
│  ✓ Downloads OSS-Fuzz build.sh                            │
│  ✓ Sets up /src and /out directories                      │
│  ✓ Copies seed corpus                                      │
└──────────────────────────────────────────────────────────────┘
            │
            ▼ (depends on)

Level 3: Fuzzer Integration (Intermediate)
┌──────────────────────────────────────────────────────────────┐
│  libfuzzer-libpng_libpng_read_fuzzer-builder-intermediate   │
│  (FROM previous benchmark builder)                          │
│                                                              │
│  ✓ Inherits benchmark + dependencies                       │
│  ✓ Clones LLVM libfuzzer source                            │
│  ✓ Compiles libFuzzer.a library                            │
│  ✓ Installs to /usr/lib                                    │
└──────────────────────────────────────────────────────────────┘
            │
            ▼ (depends on both previous + base-image)

Level 4: Final Build (Orchestrator)
┌──────────────────────────────────────────────────────────────┐
│  libfuzzer-libpng_libpng_read_fuzzer-builder                │
│  (FROM benchmark-builder/Dockerfile)                        │
│                                                              │
│  ✓ Inherits fuzzer-intermediate image                      │
│  ✓ Copies Python 3.10 from base-image                      │
│  ✓ Reads benchmark.yaml (gets commit hash)                 │
│  ✓ Checks out exact benchmark commit                       │
│  ✓ Calls fuzzer.build() dynamically:                       │
│    - Sets CFLAGS/CXXFLAGS for fuzzer                       │
│    - Sets CC/CXX compilers                                 │
│    - Runs benchmark/build.sh with fuzzer flags             │
│  ✓ Produces compiled binary: /out/libpng_read_fuzzer       │
│  ✓ Produces dictionary & seeds in /out                     │
└──────────────────────────────────────────────────────────────┘
            │
            ▼ (depends on)

Level 5: Runtime Intermediate
┌──────────────────────────────────────────────────────────────┐
│  libfuzzer-libpng_libpng_read_fuzzer-intermediate-runner    │
│  (FROM fuzzers/libfuzzer/runner.Dockerfile)                │
│                                                              │
│  ✓ Minimal runtime environment                             │
│  ✓ Fuzzer-specific runtime setup                           │
└──────────────────────────────────────────────────────────────┘
            │
            ▼ (depends on)

Level 6: Final Runner (Ready for Trials)
┌──────────────────────────────────────────────────────────────┐
│  libfuzzer-libpng_libpng_read_fuzzer-runner                 │
│  (FROM docker/benchmark-runner/Dockerfile)                 │
│                                                              │
│  ✓ Multi-stage: copies /out from builder image             │
│  ✓ Inherits runtime-intermediate                           │
│  ✓ Installs runtime dependencies                           │
│  ✓ Sets up /out as WORKDIR                                 │
│  ✓ Copies benchmarks/, fuzzers/, common/                   │
│  ✓ Creates SEED_CORPUS_DIR and OUTPUT_CORPUS_DIR           │
│  ✓ Sets ENTRYPOINT to startup-runner.sh                    │
│  ✓ Ready to run: docker run image (starts fuzzing)         │
└──────────────────────────────────────────────────────────────┘
```

## Data Flow Through the Build Process

```
Input Data Sources
┌──────────────────────────────────────────────────┐
│                                                  │
│  Fuzzer List:              Benchmark List:      │
│  ├─ libfuzzer              ├─ libpng_...       │
│  ├─ aflplusplus            ├─ vorbis_...       │
│  └─ [others]               └─ [others]         │
│                                                  │
│  benchmark.yaml:           fuzzer.py:           │
│  ├─ commit hash            ├─ build()          │
│  ├─ fuzz_target            └─ fuzz()           │
│  └─ project name                               │
└──────────────────────────────────────────────────┘
            │
            ▼
┌──────────────────────────────────────────────────┐
│      docker_images.py                            │
│      (Image Instantiation)                       │
│                                                  │
│  Reads: image_types.yaml templates              │
│  For each (fuzzer, benchmark) pair:             │
│    - Substitute {fuzzer} → libfuzzer            │
│    - Substitute {benchmark} → libpng_...        │
│  Output: Dictionary of concrete images          │
└──────────────────────────────────────────────────┘
            │
            ▼
┌──────────────────────────────────────────────────┐
│      generate_makefile.py                        │
│      (Build Target Generation)                  │
│                                                  │
│  Takes: Image definitions from docker_images   │
│  Generates: Makefile with rules                 │
│  Example rules:                                 │
│    .libpng_libpng_read_fuzzer-project-builder  │
│    .libfuzzer-libpng_libpng_read_fuzzer-...-int │
│    .libfuzzer-libpng_libpng_read_fuzzer-builder │
│    build-libfuzzer-libpng_libpng_read_fuzzer    │
└──────────────────────────────────────────────────┘
            │
            ▼
┌──────────────────────────────────────────────────┐
│      make build-libfuzzer-libpng_...             │
│      (Docker Build Execution)                   │
│                                                  │
│  1. docker build (benchmark-project-builder)    │
│  2. docker build (fuzzer-intermediate)          │
│  3. docker build (fuzzer-builder)               │
│     └─> Calls fuzzer.build() in Docker         │
│  4. docker build (runner-intermediate)          │
│  5. docker build (runner)                       │
└──────────────────────────────────────────────────┘
            │
            ▼
┌──────────────────────────────────────────────────┐
│      Docker Images                               │
│      (Ready for Trials)                          │
│                                                  │
│  gcr.io/fuzzbench/runners/libfuzzer/            │
│                     libpng_libpng_read_fuzzer   │
│                                                  │
│  Ready to run: docker run [image]               │
└──────────────────────────────────────────────────┘
```

## File Organization

```
FuzzBench Project
│
├── benchmarks/                      [User Input: Benchmark Definitions]
│   ├── libpng_libpng_read_fuzzer/
│   │   ├── Dockerfile              [OSS-Fuzz base + benchmark setup]
│   │   ├── benchmark.yaml           [Metadata: commit, fuzz_target]
│   │   ├── build.sh                 [OSS-Fuzz build script]
│   │   └── seeds/
│   │       └── seed.png
│   │
│   └── vorbis_decode_fuzzer/
│       └── [same structure]
│
├── fuzzers/                         [User Input: Fuzzer Integrations]
│   ├── libfuzzer/
│   │   ├── fuzzer.py                [build() and fuzz() functions]
│   │   ├── builder.Dockerfile       [Fuzzer build environment]
│   │   ├── runner.Dockerfile        [Fuzzer runtime environment]
│   │   └── patch.diff
│   │
│   └── aflplusplus/
│       └── [similar structure]
│
├── docker/                          [Build Infrastructure]
│   ├── image_types.yaml             [CONFIGURATION: Template definitions]
│   ├── generate_makefile.py         [GENERATION: Creates build rules]
│   ├── base-image/
│   │   └── Dockerfile               [Python 3.10 + tools]
│   ├── benchmark-builder/
│   │   ├── Dockerfile               [ORCHESTRATOR: Runs build]
│   │   ├── checkout_commit.py       [Checks out benchmark version]
│   │   └── fuzzer_build             [Calls fuzzer.build()]
│   └── benchmark-runner/
│       ├── Dockerfile               [ORCHESTRATOR: Final packaging]
│       └── startup-runner.sh        [Fuzzing entrypoint]
│
└── experiment/build/
    └── docker_images.py             [INSTANTIATION: Template substitution]
```

## Build Stages in Detail

### Stage 1: Benchmark Project Builder

```
Input:  benchmarks/libpng_libpng_read_fuzzer/Dockerfile
Output: Docker image with benchmark source + dependencies

Dockerfile Content:
  FROM gcr.io/oss-fuzz-base/base-builder
  RUN apt-get install make autoconf automake libtool zlib1g-dev
  RUN git clone https://github.com/madler/zlib.git
  RUN git clone https://github.com/glennrp/libpng.git
  RUN cp libpng/contrib/oss-fuzz/build.sh $SRC
  ADD seeds /opt/seeds

Environment:
  - $SRC = /src (source directory)
  - $OUT = /out (output directory)
  - $WORK = /work (working directory)
```

### Stage 2: Fuzzer Intermediate Builder

```
Input:  fuzzers/libfuzzer/builder.Dockerfile
        (parent: benchmark builder image)
Output: Image with fuzzer runtime libraries

Dockerfile Content:
  ARG parent_image
  FROM $parent_image
  RUN git clone https://github.com/llvm/llvm-project.git
  RUN cd llvm-project && git checkout [commit]
  RUN compile fuzzer source files
  RUN ar r libFuzzer.a *.o
  RUN cp libFuzzer.a /usr/lib

Result: /usr/lib/libFuzzer.a available for linking
```

### Stage 3: Final Builder (Compilation)

```
Input:  docker/benchmark-builder/Dockerfile
        (parent: fuzzer intermediate image)
        benchmark.yaml
Output: Compiled fuzz target binary

Key Steps:
  1. ARG parent_image (from fuzzer-intermediate)
  2. COPY Python 3.10 from base-image
  3. COPY benchmarks/{benchmark}/benchmark.yaml
  4. RUN checkout_commit.py (checks out specific commit)
  5. COPY docker/benchmark-builder/fuzzer_build
  6. RUN fuzzer_build

fuzzer_build Script Execution:
  PYTHONPATH=$SRC python3 -u -c \
    "from fuzzers.libfuzzer import fuzzer; \
     fuzzer.build()"

fuzzer.build() does:
  - Sets CFLAGS = "-fsanitize=fuzzer-no-link"
  - Sets CXXFLAGS = "-fsanitize=fuzzer-no-link"
  - Sets CC = "clang", CXX = "clang++"
  - Sets FUZZER_LIB = "/usr/lib/libFuzzer.a"
  - Calls utils.build_benchmark()
  - Runs benchmarks/libpng/build.sh with flags

Result: /out/libpng_read_fuzzer (executable)
```

### Stage 4: Runner Intermediate

```
Input:  fuzzers/libfuzzer/runner.Dockerfile
Output: Minimal runtime base image

Typically minimal, sets up:
  - Required runtime libraries
  - Environment variables
  - Does NOT include build tools or source
```

### Stage 5: Final Runner (Packaging)

```
Input:  docker/benchmark-runner/Dockerfile
Output: Complete runtime image

Key Steps:
  1. Multi-stage FROM: Copy artifacts from builder
  2. FROM fuzzer-runner-intermediate (minimal runtime base)
  3. Install runtime dependencies
  4. COPY --from=builder /out/ ./
  5. COPY benchmarks/, fuzzers/, common/
  6. Set ENTRYPOINT to startup-runner.sh

Result: Ready-to-run image for fuzzing trials
```

## Makefile Rule Example

```makefile
# Generated rule for libfuzzer + libpng benchmark

libpng_libpng_read_fuzzer-fuzz-target=libpng_read_fuzzer

# Step 1: Build benchmark foundation
.libpng_libpng_read_fuzzer-project-builder:
	docker build \
	--tag gcr.io/fuzzbench/builders/benchmark/libpng_libpng_read_fuzzer \
	--file benchmarks/libpng_libpng_read_fuzzer/Dockerfile \
	benchmarks/libpng_libpng_read_fuzzer

# Step 2: Add fuzzer (intermediate)
.libfuzzer-libpng_libpng_read_fuzzer-builder-intermediate: \
  .libpng_libpng_read_fuzzer-project-builder
	docker build \
	--tag gcr.io/fuzzbench/builders/libfuzzer/libpng_libpng_read_fuzzer-intermediate \
	--build-arg parent_image=gcr.io/fuzzbench/builders/benchmark/libpng_libpng_read_fuzzer \
	--file fuzzers/libfuzzer/builder.Dockerfile \
	fuzzers/libfuzzer

# Step 3: Compile (final builder)
.libfuzzer-libpng_libpng_read_fuzzer-builder: \
  .libfuzzer-libpng_libpng_read_fuzzer-builder-intermediate \
  base-image
	docker build \
	--tag gcr.io/fuzzbench/builders/libfuzzer/libpng_libpng_read_fuzzer \
	--build-arg benchmark=libpng_libpng_read_fuzzer \
	--build-arg fuzzer=libfuzzer \
	--build-arg parent_image=gcr.io/fuzzbench/builders/libfuzzer/libpng_libpng_read_fuzzer-intermediate \
	--file docker/benchmark-builder/Dockerfile \
	.

# Step 4: Runtime setup
.libfuzzer-libpng_libpng_read_fuzzer-intermediate-runner: \
  .libfuzzer-libpng_libpng_read_fuzzer-builder
	docker build \
	--tag gcr.io/fuzzbench/runners/libfuzzer/libpng_libpng_read_fuzzer-intermediate \
	--file fuzzers/libfuzzer/runner.Dockerfile \
	fuzzers/libfuzzer

# Step 5: Final runner image
.libfuzzer-libpng_libpng_read_fuzzer-runner: \
  .libfuzzer-libpng_libpng_read_fuzzer-intermediate-runner
	docker build \
	--tag gcr.io/fuzzbench/runners/libfuzzer/libpng_libpng_read_fuzzer \
	--build-arg benchmark=libpng_libpng_read_fuzzer \
	--build-arg fuzzer=libfuzzer \
	--file docker/benchmark-runner/Dockerfile \
	.

# Final target
build-libfuzzer-libpng_libpng_read_fuzzer: \
  .libfuzzer-libpng_libpng_read_fuzzer-runner

# Run the image
run-libfuzzer-libpng_libpng_read_fuzzer: \
  .libfuzzer-libpng_libpng_read_fuzzer-runner
	docker run \
	--cpus=1 \
	--shm-size=2g \
	-e FUZZER=libfuzzer \
	-e BENCHMARK=libpng_libpng_read_fuzzer \
	-e FUZZ_TARGET=$(libpng_libpng_read_fuzzer-fuzz-target) \
	gcr.io/fuzzbench/runners/libfuzzer/libpng_libpng_read_fuzzer
```

## Key Insights

1. **Composition over Monolith**: Each Dockerfile has a single responsibility
2. **Template Reuse**: One template definition generates rules for all fuzzer-benchmark pairs
3. **Build Isolation**: Each stage is a separate Docker build, enabling caching and parallelization
4. **Dynamic Integration**: Python dynamic imports avoid per-fuzzer code duplication
5. **Reproducibility**: Metadata in benchmark.yaml ensures exact version pinning
6. **Multi-Stage Optimization**: Builder and runner images separate concerns
7. **Dependency Tracking**: Makefile rules automatically capture image dependencies
