# CRSBench Builder Module

## Overview

The builder module provides infrastructure for building and testing projects within CRSBench. It supports multiple project types with specialized builders for different environments, including OSS-Fuzz projects.

## Attribution

The OSS-Fuzz builder implementation is adapted from [PatchAgent](https://github.com/cla7aye15I4nd/PatchAgent) under Apache 2.0 license, with significant extensions for CRSBench integration.

## Components

### Core Classes

- **`Builder`** (base.py): Abstract base class for all builders
- **`OSSFuzzBuilder`** (ossfuzz.py): OSS-Fuzz specific implementation
- **`POC`** (poc.py): Proof of Concept handling classes
- **Utilities** (utils.py): Helper functions and error handling
- **Integration** (integration.py): Bridge to existing CRSBench modules

### Key Features

1. **Multi-language Support**: C/C++, Java with extensible architecture
2. **Sanitizer Integration**: AddressSanitizer, MemorySanitizer, UBSan, TSan
3. **Docker Support**: Seamless Docker-based builds for OSS-Fuzz
4. **Patch Testing**: Apply and validate patches against POVs
5. **Build Caching**: Efficient caching for repeated builds
6. **CRSBench Integration**: Native integration with POV validation system

## Quick Start

```python
from crsbench.builder import create_builder_from_config
from pathlib import Path

# Auto-detect and create appropriate builder
builder = create_builder_from_config(Path("path/to/benchmark"))

# Build the project
result = builder.build()

# Test a POV
poc_data = Path("poc.bin").read_bytes()
test_result = builder.test_pov(poc_data, "target_harness")
```

## Architecture

```
Builder (Abstract)
├── OSSFuzzBuilder
│   ├── Docker Integration
│   ├── Multi-Sanitizer Support
│   └── OSS-Fuzz Helper Scripts
└── [Future: Other Builder Types]

POC System
├── FilePOC
├── BinaryPOC
├── StdinPOC
└── OSSFuzzPOC

Integration Layer
├── Config Parsing
├── Format Conversion
└── CRSBench Bridge
```

## Usage Patterns

### Basic Building

```python
from crsbench.builder.ossfuzz import OSSFuzzBuilder
from crsbench.builder.base import Sanitizer

builder = OSSFuzzBuilder(
    project="libpng",
    source_path=Path("/src/libpng"),
    ossfuzz_path=Path("/oss-fuzz"),
    sanitizers=[Sanitizer.AddressSanitizer]
)

result = builder.build()
```

### Patch Testing

```python
patch = """
diff --git a/file.c b/file.c
--- a/file.c
+++ b/file.c
@@ -1,1 +1,1 @@
-buggy_line();
+fixed_line();
"""

# Test patch effectiveness
result = builder.test_pov(poc_data, "harness", patch=patch)
if not result["triggered"]:
    print("Patch successfully fixed the vulnerability")
```

### POV Management

```python
from crsbench.builder.poc import create_poc_from_file

# Create POC from file
poc = create_poc_from_file(
    Path("testcase.bin"),
    target_harness="fuzz_target",
    expected_sanitizer="address"
)

# Test POC
result = builder.test_pov(poc.data, poc.target_harness)
```

## Error Handling

```python
from crsbench.builder.utils import (
    BuilderProcessError,
    BuilderTimeoutError,
    DockerUnavailableError
)

try:
    result = builder.build()
except DockerUnavailableError:
    print("Docker not available")
except BuilderTimeoutError:
    print("Build timed out")
except BuilderProcessError as e:
    print(f"Build failed: {e.message}")
```

## Configuration

### OSS-Fuzz Projects

```yaml
# project.yaml
language: c++
sanitizers:
  - address
  - memory
fuzzing_engines:
  - libfuzzer
```

### CRSBench Integration

```yaml
# .aixcc/config.yaml
harness_files:
- name: fuzz_target
  path: $PROJECT/fuzz_target.c
  cpvs:
  - name: pov1
    sanitizer: address
    error_token: 'AddressSanitizer: heap-buffer-overflow'
```

## Dependencies

- `pexpect>=4.9.0`: Interactive shell sessions
- `gitpython>=3.1.44`: Git operations
- `pyyaml>=6.0.2`: Configuration parsing
- Docker: Required for OSS-Fuzz builds

## Extending the System

### Adding New Builder Types

```python
from crsbench.builder.base import Builder, BuildResult

class CustomBuilder(Builder):
    @property
    def language(self):
        return Language.RUST

    @property
    def supported_sanitizers(self):
        return [Sanitizer.AddressSanitizer]

    def build(self, patch="", sanitizer=None):
        # Implement custom build logic
        return BuildResult(status=BuildStatus.SUCCESS)

    def test_pov(self, poc_data, harness_name, patch="", sanitizer=None):
        # Implement custom POV testing
        return {"triggered": False}
```

### Custom POC Types

```python
from crsbench.builder.poc import POC, POCType

class NetworkPOC(POC):
    def __init__(self, host, port, payload):
        super().__init__()
        self.host = host
        self.port = port
        self.payload = payload

    @property
    def data(self):
        return self.payload

    @property
    def poc_type(self):
        return POCType.NETWORK
```

## Testing

```bash
# Run builder tests
pytest crsbench/builder/tests/

# Test with specific OSS-Fuzz project
python -m crsbench.builder.ossfuzz --project libpng --test
```

## Performance

- **Build Caching**: Builds are cached by patch hash and sanitizer
- **Parallel Builds**: Multiple sanitizers can be built concurrently
- **Workspace Management**: Efficient workspace cleanup and reuse

## Troubleshooting

1. **Docker Issues**: Ensure Docker daemon is running and accessible
2. **OSS-Fuzz Path**: Set `OSS_FUZZ_PATH` environment variable
3. **Permissions**: Add user to docker group for OSS-Fuzz builds
4. **Timeouts**: Increase timeout values for large projects

## See Also

- [OSS-Fuzz Builder Documentation](../../docs/ossfuzz-builder.md)
- [CRSBench Architecture](../../docs/architecture.md)
- [PatchAgent Original Implementation](https://github.com/cla7aye15I4nd/PatchAgent)