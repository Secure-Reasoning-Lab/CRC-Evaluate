# FuzzBench Docker Image Build Process - Deep Dive

## Overview

FuzzBench uses a sophisticated multi-stage Docker image building process to create containerized fuzzing environments. The system separates concerns into:
- Base images
- Benchmark-specific builders
- Fuzzer-specific integrations
- Benchmark runners

This document explains how benchmarks in the `benchmarks/` directory are converted into Docker images.

## Directory Structure

### benchmarks/

The benchmarks directory contains individual benchmark definitions:

```
benchmarks/
├── libpng_libpng_read_fuzzer/
│   ├── Dockerfile                 # OSS-Fuzz base-builder image + benchmark setup
│   ├── benchmark.yaml             # Metadata (commit, fuzz target, unsupported fuzzers)
│   ├── seeds/                     # Initial seed corpus
│   │   └── seed.png
│   └── build.sh                   # OSS-Fuzz compatible build script (cloned from OSS-Fuzz)
│
├── vorbis_decode_fuzzer/
│   ├── Dockerfile
│   ├── benchmark.yaml
│   ├── seeds/
│   └── build.sh
│
└── [other benchmarks...]
```

Each benchmark directory contains:
- **Dockerfile**: Inherits from `gcr.io/oss-fuzz-base/base-builder` and sets up the benchmark source
- **benchmark.yaml**: Stores critical metadata like project commit hash and fuzz target name
- **seeds/**: Optional initial corpus for fuzzing
- **build.sh**: OSS-Fuzz compatible build script

### docker/

The docker directory contains image definitions and build orchestration:

```
docker/
├── base-image/
│   └── Dockerfile                 # Ubuntu 20.04 + Python 3.10.8 + cloud SDK
├── benchmark-builder/
│   ├── Dockerfile                 # Multi-stage builder for fuzzer+benchmark combinations
│   ├── checkout_commit.py         # Script to checkout benchmark repo commit
│   └── fuzzer_build               # Shell script that calls fuzzer.build()
├── benchmark-runner/
│   ├── Dockerfile                 # Multi-stage runner image
│   └── startup-runner.sh           # Entrypoint script
├── generate_makefile.py            # Generates build rules from image_types.yaml
└── image_types.yaml                # Template definitions for all image types
```

### fuzzers/

Each fuzzer has its own subdirectory with integration code:

```
fuzzers/libfuzzer/
├── fuzzer.py                       # Contains build() and fuzz() functions
├── builder.Dockerfile              # Fuzzer-specific build environment
├── runner.Dockerfile               # Fuzzer-specific runtime environment
└── patch.diff                       # Optional patches

fuzzers/aflplusplus/
├── fuzzer.py
├── builder.Dockerfile
└── runner.Dockerfile
```

## Key Files

### 1. image_types.yaml - Image Definition Templates

Located at: `/docker/image_types.yaml`

This YAML file defines templates for all Docker image types and their build dependencies. Key entries:

```yaml
'base-image':
  dockerfile: 'docker/base-image/Dockerfile'
  context: '.'
  tag: 'base-image'
  type: 'base'

'{benchmark}-project-builder':
  dockerfile: 'benchmarks/{benchmark}/Dockerfile'
  context: 'benchmarks/{benchmark}'
  tag: 'builders/benchmark/{benchmark}'
  type: 'builder'

'{fuzzer}-{benchmark}-builder-intermediate':
  build_arg:
    - 'parent_image=gcr.io/fuzzbench/builders/benchmark/{benchmark}'
  depends_on:
    - '{benchmark}-project-builder'
  dockerfile: 'fuzzers/{fuzzer}/builder.Dockerfile'
  context: 'fuzzers/{fuzzer}'
  tag: 'builders/{fuzzer}/{benchmark}-intermediate'
  type: 'builder'

'{fuzzer}-{benchmark}-builder':
  build_arg:
    - 'benchmark={benchmark}'
    - 'fuzzer={fuzzer}'
    - 'parent_image=gcr.io/fuzzbench/builders/{fuzzer}/{benchmark}-intermediate'
  depends_on:
    - '{fuzzer}-{benchmark}-builder-intermediate'
    - 'base-image'
  dockerfile: 'docker/benchmark-builder/Dockerfile'
  context: '.'
  tag: 'builders/{fuzzer}/{benchmark}'
  type: 'builder'

'{fuzzer}-{benchmark}-runner':
  benchmark: '{benchmark}'
  build_arg:
    - 'benchmark={benchmark}'
    - 'fuzzer={fuzzer}'
  depends_on:
    - '{fuzzer}-{benchmark}-intermediate-runner'
  dockerfile: 'docker/benchmark-runner/Dockerfile'
  fuzzer: '{fuzzer}'
  context: '.'
  tag: 'runners/{fuzzer}/{benchmark}'
  type: 'runner'
```

Templates use `{fuzzer}` and `{benchmark}` placeholders that get substituted by the build system.

### 2. docker_images.py - Image Instantiation

Located at: `/experiment/build/docker_images.py`

This module reads `image_types.yaml` and instantiates image definitions for all fuzzer-benchmark pairs:

```python
def _instantiate_image_obj(name_template, obj_template, fuzzer, benchmark):
    """Instantiates an image object from a template for a |fuzzer| - |benchmark| pair."""
    name = _substitute(name_template, fuzzer, benchmark)
    obj = obj_template.copy()
    for key in obj:
        if key in ('build_arg', 'depends_on'):
            obj[key] = [
                _substitute(item, fuzzer, benchmark) for item in obj[key]
            ]
        else:
            obj[key] = _substitute(obj[key], fuzzer, benchmark)
    return name, obj

def get_images_to_build(fuzzers, benchmarks):
    """Returns the set of buildable images."""
    images = {}
    templates = _get_image_type_templates()
    for fuzzer in fuzzers:
        for benchmark in benchmarks:
            for name_templ, obj_templ in templates.items():
                name, obj = _instantiate_image_obj(name_templ, obj_templ,
                                                   fuzzer, benchmark)
                images[name] = obj
    return images
```

### 3. generate_makefile.py - Build Target Generation

Located at: `/docker/generate_makefile.py`

This script generates a Makefile with all build targets:

- Reads fuzzer names and benchmark names
- Calls `docker_images.get_images_to_build()` to get all image definitions
- Generates Makefile rules for building each image
- Creates targets like:
  - `build-libfuzzer-libpng_libpng_read_fuzzer`
  - `run-libfuzzer-libpng_libpng_read_fuzzer`
  - `test-run-libfuzzer-libpng_libpng_read_fuzzer`

### 4. Dockerfiles

#### Benchmark Dockerfile (benchmarks/{benchmark}/Dockerfile)

Example: `benchmarks/libpng_libpng_read_fuzzer/Dockerfile`

```dockerfile
FROM gcr.io/oss-fuzz-base/base-builder@sha256:87ca1e9e19235e731fac8de8d1892ebe8d55caf18e7aa131346fc582a2034fdd

RUN apt-get update && \
    apt-get install -y \
    make \
    autoconf \
    automake \
    libtool \
    zlib1g-dev

RUN git clone \
        --depth 1 \
        --branch v1.2.13 \
        https://github.com/madler/zlib.git

RUN git clone \
        https://github.com/glennrp/libpng.git
RUN cp libpng/contrib/oss-fuzz/build.sh $SRC

WORKDIR libpng

RUN wget --no-check-certificate -qO $OUT/libpng_read_fuzzer.dict \
    https://raw.githubusercontent.com/google/fuzzing/master/dictionaries/png.dict

ADD seeds /opt/seeds
COPY * $SRC/
```

This Dockerfile:
- Uses OSS-Fuzz base builder image
- Installs benchmark dependencies
- Clones the benchmark source code
- Downloads build scripts from OSS-Fuzz
- Copies seed files
- Sets up the environment (SRC and OUT directories)

#### Fuzzer Builder Dockerfile (fuzzers/{fuzzer}/builder.Dockerfile)

Example: `fuzzers/libfuzzer/builder.Dockerfile`

```dockerfile
ARG parent_image
FROM $parent_image

RUN git clone https://github.com/llvm/llvm-project.git /llvm-project && \
    cd /llvm-project && \
    git checkout 5cda4dc7b4d28fcd11307d4234c513ff779a1c6f && \
    cd compiler-rt/lib/fuzzer && \
    (for f in *.cpp; do \
      clang++ -stdlib=libc++ -fPIC -O2 -std=c++11 $f -c & \
    done && wait) && \
    ar r libFuzzer.a *.o && \
    cp libFuzzer.a /usr/lib
```

This Dockerfile:
- Takes parent image (from benchmark builder) as argument
- Installs fuzzer-specific dependencies
- Builds fuzzer runtime libraries

#### Main Benchmark-Builder Dockerfile (docker/benchmark-builder/Dockerfile)

This is the orchestrator that builds the actual fuzzer+benchmark binary:

```dockerfile
ARG parent_image

# Multi-stage: copy Python 3.10 from base-image
FROM gcr.io/fuzzbench/base-image AS base-image
FROM $parent_image

ARG fuzzer
ARG benchmark
ARG debug_builder

ENV FUZZER $fuzzer
ENV BENCHMARK $benchmark
ENV DEBUG_BUILDER $debug_builder

# Install build dependencies
RUN apt-get update && \
    apt-get upgrade -y && \
    apt-get install -y \
    curl \
    xz-utils \
    zlib1g-dev \
    libssl-dev \
    libffi-dev

# Copy latest Python 3.10 from base-image
COPY --from=base-image /usr/local/bin/python3* /usr/local/bin/
COPY --from=base-image /usr/local/bin/pip3* /usr/local/bin/
COPY --from=base-image /usr/local/lib/python3.10 /usr/local/lib/python3.10
COPY --from=base-image /usr/local/include/python3.10 /usr/local/include/python3.10
COPY --from=base-image /usr/local/lib/python3.10/site-packages /usr/local/lib/python3.10/site-packages

# Copy the entire fuzzers directory tree
COPY fuzzers $SRC/fuzzers

# Disable LeakSanitizer since ptrace is unavailable
ENV ASAN_OPTIONS="detect_leaks=0"

# Read benchmark commit from metadata
COPY benchmarks/$benchmark/benchmark.yaml /
RUN mkdir /opt/fuzzbench/
COPY docker/benchmark-builder/checkout_commit.py /opt/fuzzbench/
RUN export CHECKOUT_COMMIT=$(cat /benchmark.yaml | tr -d ' ' | grep 'commit:' | cut -d ':' -f2) && \
    python3 -u /opt/fuzzbench/checkout_commit.py $CHECKOUT_COMMIT $SRC

# Run fuzzer.build()
COPY docker/benchmark-builder/fuzzer_build /usr/bin/fuzzer_build
RUN echo "Run fuzzer_build to build the target" && if [ -z "$debug_builder" ] ; then fuzzer_build; fi
```

Key steps:
1. Takes fuzzer intermediate image as parent
2. Copies Python 3.10 from base-image (multi-stage optimization)
3. Reads benchmark commit from `benchmark.yaml`
4. Checks out the specific commit for reproducibility
5. Calls `fuzzer_build` script which executes `fuzzer.build()`

The `fuzzer_build` script:
```bash
#!/bin/bash
PYTHONPATH=$SRC python3 -u -c "from fuzzers import utils; utils.initialize_env(); from fuzzers.$FUZZER import fuzzer; fuzzer.build()"
```

This dynamically imports and calls the fuzzer's `build()` function.

#### Benchmark-Runner Dockerfile (docker/benchmark-runner/Dockerfile)

Multi-stage image that copies artifacts from builder:

```dockerfile
ARG fuzzer
ARG benchmark

# Copy built artifacts from builder image
FROM gcr.io/fuzzbench/builders/$fuzzer/$benchmark AS builder

# Use intermediate runner image as base
FROM gcr.io/fuzzbench/runners/$fuzzer/$benchmark-intermediate

# Install runtime dependencies
RUN apt-get update -y && \
    DEBIAN_FRONTEND="noninteractive" apt-get install -y \
    libglib2.0-0 \
    libxml2 \
    libarchive13 \
    libgss3

# Set up output directory
ENV OUT /out
ENV WORKDIR /out
RUN mkdir -p $WORKDIR
WORKDIR $WORKDIR

ENV ROOT_DIR=/src

# Copy build artifacts from builder image
COPY --from=builder /out/ ./

# Copy source trees for runtime
COPY benchmarks $ROOT_DIR/benchmarks
COPY fuzzers $ROOT_DIR/fuzzers
COPY common $ROOT_DIR/common
COPY experiment/runner.py $ROOT_DIR/experiment/runner.py
COPY docker/benchmark-runner $ROOT_DIR/docker/benchmark-runner

# Define seed and output corpus directories
ENV SEED_CORPUS_DIR=$WORKDIR/seeds
ENV OUTPUT_CORPUS_DIR=$WORKDIR/corpus

RUN mkdir -p $SEED_CORPUS_DIR $OUTPUT_CORPUS_DIR

# Set up entrypoint
ENV PYTHONPATH=$ROOT_DIR
RUN chmod +x $ROOT_DIR/docker/benchmark-runner/startup-runner.sh
ENTRYPOINT $ROOT_DIR/docker/benchmark-runner/startup-runner.sh
```

## Build Process Step-by-Step

### Step 1: Template Definition

The image_types.yaml file defines templates for all image types. These use `{fuzzer}` and `{benchmark}` placeholders.

### Step 2: Benchmark Registration

A new benchmark is added to `benchmarks/{project}_{fuzz_target}/`:
```
├── Dockerfile          # Inherits from OSS-Fuzz base-builder
├── benchmark.yaml      # Metadata: commit, fuzz_target
├── build.sh            # OSS-Fuzz build script
└── seeds/              # Seed corpus
```

The `benchmark.yaml` stores:
```yaml
commit: cd0ea2a7f53b603d3d9b5b891c779c430047b39a
commit_date: 2023-01-09T13:17:31+00:00
fuzz_target: libpng_read_fuzzer
project: libpng
```

### Step 3: Image Instantiation

When `docker_images.get_images_to_build(fuzzers, benchmarks)` is called:

1. For each fuzzer-benchmark pair, it substitutes placeholders in templates
2. Creates concrete image definitions like:
   - `libpng_libpng_read_fuzzer-project-builder`: The benchmark builder image
   - `libfuzzer-libpng_libpng_read_fuzzer-builder-intermediate`: Fuzzer + benchmark intermediate
   - `libfuzzer-libpng_libpng_read_fuzzer-builder`: Final build image with fuzzer and benchmark
   - `libfuzzer-libpng_libpng_read_fuzzer-runner`: Runtime image for fuzzing trials

### Step 4: Build Dependency Chain

The build follows this dependency chain:

```
base-image (Ubuntu 20.04 + Python 3.10)
    ↓
{benchmark}-project-builder (Benchmark from OSS-Fuzz)
    ↓
{fuzzer}-{benchmark}-builder-intermediate (Fuzzer setup + benchmark)
    ↓ (depends on base-image too for Python 3.10)
{fuzzer}-{benchmark}-builder (Runs fuzzer.build() to compile benchmark)
    ↓
{fuzzer}-{benchmark}-intermediate-runner (Fuzzer runtime setup)
    ↓
{fuzzer}-{benchmark}-runner (Final runtime image with artifacts)
```

### Step 5: Makefile Generation

`generate_makefile.py` generates build rules like:

```makefile
export DOCKER_BUILDKIT := 1

libpng_libpng_read_fuzzer-fuzz-target=libpng_read_fuzzer

.base-image:
	docker pull ubuntu:focal
	docker build \
	--tag gcr.io/fuzzbench/base-image \
	--build-arg BUILDKIT_INLINE_CACHE=1 \
	--cache-from gcr.io/fuzzbench/base-image \
	.

.libpng_libpng_read_fuzzer-project-builder: .base-image
	docker build \
	--tag gcr.io/fuzzbench/builders/benchmark/libpng_libpng_read_fuzzer \
	--build-arg BUILDKIT_INLINE_CACHE=1 \
	--cache-from gcr.io/fuzzbench/builders/benchmark/libpng_libpng_read_fuzzer \
	--file benchmarks/libpng_libpng_read_fuzzer/Dockerfile \
	benchmarks/libpng_libpng_read_fuzzer

.libfuzzer-libpng_libpng_read_fuzzer-builder-intermediate: .libpng_libpng_read_fuzzer-project-builder
	docker build \
	--tag gcr.io/fuzzbench/builders/libfuzzer/libpng_libpng_read_fuzzer-intermediate \
	--build-arg BUILDKIT_INLINE_CACHE=1 \
	--cache-from gcr.io/fuzzbench/builders/libfuzzer/libpng_libpng_read_fuzzer-intermediate \
	--build-arg parent_image=gcr.io/fuzzbench/builders/benchmark/libpng_libpng_read_fuzzer \
	--file fuzzers/libfuzzer/builder.Dockerfile \
	fuzzers/libfuzzer

.libfuzzer-libpng_libpng_read_fuzzer-builder: .libfuzzer-libpng_libpng_read_fuzzer-builder-intermediate .base-image
	docker build \
	--tag gcr.io/fuzzbench/builders/libfuzzer/libpng_libpng_read_fuzzer \
	--build-arg BUILDKIT_INLINE_CACHE=1 \
	--cache-from gcr.io/fuzzbench/builders/libfuzzer/libpng_libpng_read_fuzzer \
	--build-arg benchmark=libpng_libpng_read_fuzzer \
	--build-arg fuzzer=libfuzzer \
	--build-arg parent_image=gcr.io/fuzzbench/builders/libfuzzer/libpng_libpng_read_fuzzer-intermediate \
	--file docker/benchmark-builder/Dockerfile \
	.

.libfuzzer-libpng_libpng_read_fuzzer-runner: .libfuzzer-libpng_libpng_read_fuzzer-builder
	docker build \
	--tag gcr.io/fuzzbench/runners/libfuzzer/libpng_libpng_read_fuzzer \
	--build-arg BUILDKIT_INLINE_CACHE=1 \
	--cache-from gcr.io/fuzzbench/runners/libfuzzer/libpng_libpng_read_fuzzer \
	--build-arg benchmark=libpng_libpng_read_fuzzer \
	--build-arg fuzzer=libfuzzer \
	--file docker/benchmark-runner/Dockerfile \
	.

build-libfuzzer-libpng_libpng_read_fuzzer: .libfuzzer-libpng_libpng_read_fuzzer-runner
```

### Step 6: Building the Image

When `make build-libfuzzer-libpng_libpng_read_fuzzer` is executed:

1. **Benchmark Builder Stage** (`libpng_libpng_read_fuzzer-project-builder`):
   - Uses OSS-Fuzz base-builder image
   - Sets up dependencies (zlib, etc.)
   - Clones benchmark source
   - Results: Prepared benchmark source in `/src`

2. **Fuzzer Intermediate Stage** (`libfuzzer-libpng_libpng_read_fuzzer-builder-intermediate`):
   - Inherits from benchmark builder
   - Installs fuzzer-specific tools (libFuzzer library)
   - Results: Base image with both benchmark and fuzzer ready

3. **Final Builder Stage** (`libfuzzer-libpng_libpng_read_fuzzer-builder`):
   - Inherits from fuzzer intermediate
   - Copies Python 3.10 from base-image
   - Reads `benchmark.yaml` to get commit hash
   - Checks out the specific benchmark commit using `checkout_commit.py`
   - Executes `fuzzer_build` script which calls `fuzzer.build()`
   - The Python script dynamically loads `fuzzers/libfuzzer/fuzzer.py` and calls `build()`
   - Example `build()` function:
     ```python
     def build():
         """Build benchmark."""
         cflags = ['-fsanitize=fuzzer-no-link']
         utils.append_flags('CFLAGS', cflags)
         utils.append_flags('CXXFLAGS', cflags)
         
         os.environ['CC'] = 'clang'
         os.environ['CXX'] = 'clang++'
         os.environ['FUZZER_LIB'] = '/usr/lib/libFuzzer.a'
         
         utils.build_benchmark()
     ```
   - Runs the benchmark's `build.sh` with fuzzer-specific compiler flags
   - Results: Compiled fuzz target binary in `/out` (e.g., `/out/libpng_read_fuzzer`)

4. **Runner Intermediate Stage** (`libfuzzer-libpng_libpng_read_fuzzer-intermediate-runner`):
   - Fuzzer's `runner.Dockerfile`
   - Sets up runtime environment
   - Results: Minimal runtime base image

5. **Final Runner Stage** (`libfuzzer-libpng_libpng_read_fuzzer-runner`):
   - Multi-stage: copies `/out` artifacts from builder image
   - Inherits from runner intermediate
   - Copies source trees and FuzzBench framework code
   - Sets up seed and corpus directories
   - Sets ENTRYPOINT to `startup-runner.sh`
   - Results: Complete runtime image ready for fuzzing trials

## Key Design Patterns

### 1. Multi-Stage Builds

Uses Docker's multi-stage build feature to minimize runtime image size:
- Builders are large (compilers, libraries, source code)
- Runners only include compiled artifacts + runtime dependencies
- Separates concerns: build-time vs. runtime

### 2. Template-Based Instantiation

`image_types.yaml` defines templates that get instantiated for each fuzzer-benchmark pair. Benefits:
- Single source of truth for image dependency graph
- Easy to add new image types
- Automatic Makefile generation

### 3. Modular Responsibility

Each component has a single responsibility:
- **Benchmark Dockerfile**: Provides benchmark source and dependencies
- **Fuzzer builder.Dockerfile**: Installs fuzzer-specific tools
- **Benchmark-builder/Dockerfile**: Orchestrates the actual build
- **Runner Dockerfiles**: Set up execution environment
- **benchmark-runner/Dockerfile**: Packages final artifacts

### 4. Dynamic Python Imports

Instead of per-fuzzer build scripts, FuzzBench uses dynamic imports:
```bash
python3 -u -c "from fuzzers.$FUZZER import fuzzer; fuzzer.build()"
```

This allows adding new fuzzers without modifying FuzzBench code.

### 5. Metadata-Driven Reproducibility

The `benchmark.yaml` stores the exact commit hash:
- `checkout_commit.py` uses this to checkout the specific version
- Ensures reproducible builds across time
- Different benchmarks can use different versions of the same project

## Environment Variables Used

During the build process:

- `$SRC`: Source code directory (set by OSS-Fuzz base-builder)
- `$OUT`: Output directory for artifacts (set by OSS-Fuzz base-builder)
- `$WORK`: Working directory (set by OSS-Fuzz base-builder)
- `$FUZZER`: Fuzzer name (set by benchmark-builder)
- `$BENCHMARK`: Benchmark name (set by benchmark-builder)
- `$FUZZ_TARGET`: Name of the fuzz target binary (set at runtime)
- `$PYTHONPATH`: Points to FuzzBench source for imports
- `$ASAN_OPTIONS`: Sanitizer options (set by benchmark-builder)

## Summary

The FuzzBench Docker build system converts benchmarks into containerized fuzzing environments through:

1. **Template Definition** (`image_types.yaml`): Defines reusable image types and dependencies
2. **Benchmark Registration** (`benchmarks/{name}/`): Adds benchmark-specific metadata and source
3. **Image Instantiation** (`docker_images.py`): Creates concrete image definitions for all fuzzer-benchmark pairs
4. **Makefile Generation** (`generate_makefile.py`): Generates build targets with proper dependencies
5. **Multi-Stage Building**: Separate builder and runner images for efficiency
6. **Dynamic Fuzzer Integration**: Uses Python imports to call fuzzer-specific build functions
7. **Metadata-Driven Reproducibility**: Uses `benchmark.yaml` to pin exact versions

This design ensures:
- Scalability: Adding new benchmarks/fuzzers requires minimal code changes
- Reproducibility: Exact commit hashes stored in metadata
- Efficiency: Multi-stage builds minimize runtime image sizes
- Modularity: Each component has clear responsibilities
- Flexibility: Easy to customize per-fuzzer and per-benchmark
