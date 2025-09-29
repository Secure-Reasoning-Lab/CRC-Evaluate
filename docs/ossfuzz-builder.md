# OSS-Fuzz Builder for CRSBench

## Overview

The OSS-Fuzz builder provides seamless integration between CRSBench and Google's OSS-Fuzz infrastructure. It enables building, testing, and validating vulnerabilities in OSS-Fuzz projects within the CRSBench framework.

## Attribution

This implementation is adapted from [PatchAgent](https://github.com/cla7aye15I4nd/PatchAgent) by Zheng Yu et al., used under the Apache 2.0 license. Key adaptations have been made to integrate with CRSBench's POV validation system and benchmark structure.

**Original PatchAgent Citation:**
```bibtex
@inproceedings{PatchAgent,
  title     = {PatchAgent: A Practical Program Repair Agent Mimicking Human Expertise},
  author    = {Yu, Zheng and Guo, Ziyi and Wu, Yuhang and Yu, Jiahao and
               Xu, Meng and Mu, Dongliang and Chen, Yan and Xing, Xinyu},
  booktitle = {34rd USENIX Security Symposium (USENIX Security 25)},
  year      = {2025}
}
```

## Features

- **Docker-based Building**: Uses OSS-Fuzz's Docker infrastructure for consistent builds
- **Multi-Sanitizer Support**: Supports AddressSanitizer, MemorySanitizer, UndefinedBehaviorSanitizer, ThreadSanitizer
- **Patch Application and Testing**: Apply patches and validate their effectiveness
- **Build Caching**: Efficient caching of successful builds
- **Language Detection**: Auto-detect C/C++/Java from project configuration
- **POV Integration**: Seamless integration with CRSBench's POV validation system

## Prerequisites

### System Requirements

1. **Docker**: OSS-Fuzz builds require Docker
2. **OSS-Fuzz Repository**: Clone of the OSS-Fuzz repository
3. **Python 3.11+**: Required for CRSBench

### Setup OSS-Fuzz

```bash
# Clone OSS-Fuzz repository
git clone https://github.com/google/oss-fuzz.git
cd oss-fuzz

# Set environment variable (optional)
export OSS_FUZZ_PATH=/path/to/oss-fuzz
```

### Install Dependencies

```bash
# Install CRSBench with OSS-Fuzz builder dependencies
uv sync
```

## Usage

### Basic Usage

```python
from crsbench.builder import create_builder_from_config
from pathlib import Path

# Create builder for OSS-Fuzz project
benchmark_path = Path("benchmarks/libpng")
builder = create_builder_from_config(benchmark_path)

# Build the project
result = builder.build()
if result.status == BuildStatus.SUCCESS:
    print("Build successful!")

# Test a POV
with open("poc.bin", "rb") as f:
    poc_data = f.read()

test_result = builder.test_pov(poc_data, "libpng_read_fuzzer")
if test_result["triggered"]:
    print(f"POV triggered: {test_result['summary']}")
```

### Advanced Usage

```python
from crsbench.builder.ossfuzz import OSSFuzzBuilder, OSSFuzzPOC
from crsbench.builder.base import Sanitizer
from pathlib import Path

# Create builder with specific configuration
builder = OSSFuzzBuilder(
    project="libpng",
    source_path=Path("/path/to/libpng/source"),
    ossfuzz_path=Path("/path/to/oss-fuzz"),
    sanitizers=[Sanitizer.AddressSanitizer, Sanitizer.MemorySanitizer],
    timeout=600
)

# Build with specific sanitizer
result = builder.build(sanitizer=Sanitizer.AddressSanitizer)

# Apply patch and test
patch_content = """
diff --git a/src/file.c b/src/file.c
index 1234567..abcdefg 100644
--- a/src/file.c
+++ b/src/file.c
@@ -10,7 +10,7 @@
-    if (size > MAX_SIZE) {
+    if (size >= MAX_SIZE) {
         return -1;
     }
"""

# Test POV with patch
poc = OSSFuzzPOC(Path("testcase.bin"), "libpng_read_fuzzer")
result = builder.test_pov(poc.data, poc.harness_name, patch=patch_content)
```

### Integration with CRSBench Components

```python
from crsbench.builder.integration import (
    create_builder_from_config,
    validate_builder_config,
    load_benchmark_povs
)

# Validate benchmark configuration
validation_result = validate_builder_config(benchmark_path)
if not validation_result["valid"]:
    print(f"Configuration errors: {validation_result['errors']}")

# Load POVs from benchmark
pocs = load_benchmark_povs(benchmark_path)
print(f"Loaded {len(pocs)} POVs")

# Create builder and test all POVs
builder = create_builder_from_config(benchmark_path)
for poc in pocs:
    result = builder.test_pov(poc.data, poc.target_harness)
    print(f"POV {poc.name}: {'TRIGGERED' if result['triggered'] else 'OK'}")
```

## Project Structure

### OSS-Fuzz Project Layout

```
benchmarks/my-ossfuzz-project/
├── project.yaml          # OSS-Fuzz project configuration
├── build.sh              # Build script
├── Dockerfile            # Docker configuration
├── fuzz_target1.c        # Fuzzing harnesses
├── fuzz_target2.c
└── .aixcc/               # CRSBench configuration
    ├── config.yaml       # Benchmark configuration
    └── povs/             # POV files
        ├── pov1.bin
        └── pov2.bin
```

### project.yaml Format

```yaml
homepage: "https://example.com"
main_repo: "https://github.com/user/project.git"
language: c++
sanitizers:
  - address
  - memory
  - undefined
fuzzing_engines:
  - libfuzzer
```

### CRSBench Configuration

```yaml
# .aixcc/config.yaml
harness_files:
- name: fuzz_target1
  path: $PROJECT/fuzz_target1.c
  cpvs:
  - name: pov1
    sanitizer: address
    error_token: 'AddressSanitizer: heap-buffer-overflow'
    file: $PROJECT/.aixcc/povs/pov1.bin

- name: fuzz_target2
  path: $PROJECT/fuzz_target2.c
  cpvs:
  - name: pov2
    sanitizer: memory
    error_token: 'MemorySanitizer: use-of-uninitialized-value'
    file: $PROJECT/.aixcc/povs/pov2.bin
```

## Environment Variables

- `OSS_FUZZ_PATH`: Path to OSS-Fuzz repository (auto-detected if not set)
- `DOCKER_HOST`: Docker daemon connection (if using remote Docker)

## Supported Sanitizers

| Sanitizer | OSS-Fuzz Name | Description |
|-----------|---------------|-------------|
| AddressSanitizer | address | Detects memory errors (buffer overflows, use-after-free) |
| MemorySanitizer | memory | Detects uninitialized memory usage |
| UndefinedBehaviorSanitizer | undefined | Detects undefined behavior |
| ThreadSanitizer | thread | Detects data races |
| LeakAddressSanitizer | address | Detects memory leaks (maps to address) |
| JazzerSanitizer | address | Java fuzzing with Jazzer (maps to address) |

## Error Handling

The builder provides comprehensive error handling:

```python
from crsbench.builder.utils import (
    BuilderProcessError,
    BuilderTimeoutError,
    DockerUnavailableError
)

try:
    result = builder.build()
except DockerUnavailableError:
    print("Docker is not available")
except BuilderTimeoutError as e:
    print(f"Build timed out: {e.message}")
except BuilderProcessError as e:
    print(f"Build failed: {e.stdout}")
```

## Performance Considerations

### Build Caching

The builder automatically caches successful builds based on patch content and sanitizer. This significantly speeds up repeated testing.

### Workspace Management

```python
# Use custom workspace for better control
builder = OSSFuzzBuilder(
    project="libpng",
    source_path=source_path,
    ossfuzz_path=ossfuzz_path,
    sanitizers=[Sanitizer.AddressSanitizer],
    workspace=Path("/tmp/crsbench-builds"),
    clean_up=False  # Preserve builds across sessions
)
```

### Parallel Builds

```python
import concurrent.futures
from crsbench.builder.base import Sanitizer

sanitizers = [Sanitizer.AddressSanitizer, Sanitizer.MemorySanitizer]

with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
    futures = [
        executor.submit(builder.build, sanitizer=san)
        for san in sanitizers
    ]

    for future in concurrent.futures.as_completed(futures):
        result = future.result()
        print(f"Build result: {result.status}")
```

## Troubleshooting

### Common Issues

1. **Docker not available**
   ```bash
   # Check Docker status
   docker info

   # Start Docker daemon
   sudo systemctl start docker
   ```

2. **OSS-Fuzz not found**
   ```bash
   # Set environment variable
   export OSS_FUZZ_PATH=/path/to/oss-fuzz

   # Or clone OSS-Fuzz
   git clone https://github.com/google/oss-fuzz.git
   ```

3. **Build timeouts**
   ```python
   # Increase timeout
   builder = OSSFuzzBuilder(
       project="large-project",
       timeout=1200,  # 20 minutes
       replay_timeout=600  # 10 minutes for POV testing
   )
   ```

4. **Permission issues**
   ```bash
   # Add user to docker group
   sudo usermod -aG docker $USER
   newgrp docker
   ```

### Debug Mode

```python
import logging

# Enable debug logging
logging.basicConfig(level=logging.DEBUG)

# The builder will output detailed information
builder = create_builder_from_config(benchmark_path)
```

## Integration Examples

### With Patch Tester

```python
from crsbench.patch_tester import PatchTester
from crsbench.builder.integration import create_builder_from_config

# Create builder
builder = create_builder_from_config(benchmark_path)

# Integrate with patch tester
patch_tester = PatchTester()

# The patch tester can now use the OSS-Fuzz builder
# for building and testing patches
```

### With Reproducer

```python
from crsbench.reproducer import POVValidator
from crsbench.builder.integration import load_benchmark_povs

# Load POVs
pocs = load_benchmark_povs(benchmark_path)

# Use with reproducer
validator = POVValidator()
for poc in pocs:
    # The reproducer can leverage the builder for validation
    pass
```

## API Reference

See the module docstrings for detailed API documentation:

- `crsbench.builder.base`: Abstract base classes
- `crsbench.builder.ossfuzz`: OSS-Fuzz specific implementation
- `crsbench.builder.poc`: POC/POV handling
- `crsbench.builder.utils`: Utility functions
- `crsbench.builder.integration`: Integration helpers