# Hint Generation Module

This module generates SARIF format hints at different levels of detail to adjust benchmark difficulty for CRS evaluation.

## Overview

The hint generation system processes vulnerability information from `vuln.yaml` files in benchmarks and generates SARIF (Static Analysis Results Interchange Format) reports at four different hint levels:

- **Level 1**: General vulnerability class (e.g., Memory Safety Issue; CWE-119)
- **Level 2**: Specific vulnerability type (e.g., Out-of-bounds write; CWE-787)
- **Level 3**: Level 2 + Function-level location (e.g., parse_input())
  - Uses SARIF `logicalLocations` field to specify function name
  - Includes `physicalLocation` with file path but no line numbers
- **Level 4**: Level 2 + Line range-level location (e.g., Line 240-245 of input.c)
  - Includes both `logicalLocations` (function name) and `physicalLocation` with line numbers

## Module Structure

```
crsbench/hint_generation/
├── __init__.py
├── README.md                       # This file
├── cwe_mapping.py                  # CWE ID to general class mapping
├── sarif_model.py                  # Auto-generated Pydantic models (legacy)
├── sarif_generator_simple.py       # SARIF hint generator (active)
└── generate_hints.py               # CLI script for hint generation
```

## Components

### 1. CWE Mapping (`cwe_mapping.py`)

Maps specific CWE IDs to general vulnerability classes:

```python
from crsbench.hint_generation.cwe_mapping import get_general_class

general_class, description = get_general_class("CWE-122")
# Returns: ("Memory Safety Issue", "Heap-based buffer overflow")
```

**Supported Categories:**
- Memory Safety Issues (CWE-119, CWE-122, CWE-125, CWE-787, etc.)
- Memory Management Issues (CWE-401, CWE-415, CWE-416, CWE-476, etc.)
- Input Validation Issues (CWE-20, CWE-78, CWE-89, CWE-190, etc.)
- Resource Management Issues (CWE-400, CWE-404, CWE-772, etc.)
- Concurrency Issues (CWE-362, CWE-366, CWE-367, etc.)
- And more...

### 2. SARIF Generator (`sarif_generator_simple.py`)

Core logic for generating SARIF reports at different hint levels:

```python
from crsbench.hint_generation.sarif_generator_simple import (
    SarifHintGenerator,
    VulnInfo,
    HintLevel,
)

# Load vulnerability info
vuln_info = VulnInfo.from_yaml(Path("vuln.yaml"))

# Generate hint at specific level
generator = SarifHintGenerator(vuln_info)
sarif_json = generator.generate(HintLevel.WITH_FUNCTION)
```

### 3. CLI Script (`generate_hints.py`)

Automated script to generate hints for all benchmarks:

```bash
# Generate hints for all benchmarks
python -m crsbench.hint_generation.generate_hints

# Generate for specific benchmark
python -m crsbench.hint_generation.generate_hints --benchmark libxml2-delta-03

# Generate only certain levels
python -m crsbench.hint_generation.generate_hints --levels 1 2 3

# Verbose output
python -m crsbench.hint_generation.generate_hints -v
```

## Usage

### Generating Hints for All Benchmarks

From the repository root:

```bash
uv run python -m crsbench.hint_generation.generate_hints
```

This will:
1. Scan `benchmarks/` directory for all `vuln.yaml` files
2. Generate SARIF hints at all 4 levels for each vulnerability
3. Save hints to `benchmarks/<name>/.aixcc/html/<cpv_id>/hints/` directory

### Programmatic Usage

```python
from pathlib import Path
from crsbench.hint_generation.sarif_generator_simple import (
    generate_hints_for_benchmark,
    HintLevel,
)

vuln_yaml = Path("benchmarks/example/.aixcc/html/cpv_0/vuln.yaml")
output_dir = Path("benchmarks/example/.aixcc/html/cpv_0/hints")

# Generate hints for specific levels
output_files = generate_hints_for_benchmark(
    vuln_yaml_path=vuln_yaml,
    output_dir=output_dir,
    levels=[HintLevel.GENERAL_CLASS, HintLevel.WITH_LINES],
)

for level, file_path in output_files.items():
    print(f"Level {level}: {file_path}")
```

## Input Format

The system expects `vuln.yaml` files with the following structure:

```yaml
id: cpv_0
name: Heap Based Buffer Overflow in UTF-32 implementation
cwes:
  - CWE-122
description: |
  Detailed description of the vulnerability...
locations:
  - path_from_root: xmlIO.c
    function_name: UTF32ToUTF8
    startLine: 2168
    startColumn: 17
    endLine: 2177
    endColumn: 4
```

## Output Format

### Example: Level 1 (General Class)

```json
{
  "$schema": "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/sarif-schema-2.1.0.json",
  "version": "2.1.0",
  "runs": [{
    "tool": {
      "driver": {
        "name": "CRSBench-HintGenerator",
        "version": "1.0.0",
        "rules": [{
          "id": "CWE-122",
          "name": "Memory Safety Issue",
          "shortDescription": {
            "text": "Memory Safety Issue detected"
          }
        }]
      }
    },
    "results": [{
      "ruleId": "CWE-122",
      "level": "warning",
      "message": {
        "text": "Memory Safety Issue: Heap Based Buffer Overflow"
      }
    }]
  }]
}
```

### Example: Level 3 (With Function Name)

```json
{
  "results": [{
    "ruleId": "CWE-122",
    "level": "warning",
    "message": {
      "text": "CWE-122 - Heap-based buffer overflow in function(s): UTF32ToUTF8"
    },
    "locations": [{
      "physicalLocation": {
        "artifactLocation": {
          "uri": "xmlIO.c"
        }
      },
      "logicalLocations": [{
        "name": "UTF32ToUTF8",
        "kind": "function"
      }]
    }]
  }]
}
```

### Example: Level 4 (With Line Numbers)

```json
{
  "results": [{
    "ruleId": "CWE-122",
    "level": "warning",
    "message": {
      "text": "CWE-122 - Heap-based buffer overflow at: xmlIO.c:2168-2177"
    },
    "locations": [{
      "physicalLocation": {
        "artifactLocation": {
          "uri": "xmlIO.c"
        },
        "region": {
          "startLine": 2168,
          "endLine": 2177,
          "startColumn": 17,
          "endColumn": 4
        }
      },
      "logicalLocations": [{
        "name": "UTF32ToUTF8",
        "kind": "function"
      }]
    }]
  }]
}
```

## Integration with CRSBench

When running experiments, users can specify hint levels to provide to CRS systems:

```bash
# Future usage (to be implemented in run_experiment.py)
crsbench run-experiment \
  --experiment-config config.yaml \
  --hint-level 2 \
  --benchmarks libxml2-delta-03
```

The experiment runner will:
1. Check for pre-generated hint files in `benchmarks/<name>/.aixcc/html/<cpv_id>/hints/`
2. Select the appropriate `hint_level_N.sarif` file
3. Provide it to the CRS during the evaluation

## Adding New CWE Mappings

To add support for new CWE IDs, edit `cwe_mapping.py`:

```python
CWE_TO_GENERAL_CLASS: Dict[str, Tuple[str, str]] = {
    # Add new mapping
    "CWE-XXX": ("General Category", "Specific description"),
    # ... existing mappings
}
```

## Testing

```bash
# Generate hints for a specific benchmark
uv run python -m crsbench.hint_generation.generate_hints \
  --benchmark libxml2-delta-03 \
  -v

# Verify generated files
ls benchmarks/libxml2-delta-03/.aixcc/html/cpv_0/hints/

# Validate SARIF format (if sarif-cli is available)
# sarif validate hint_level_1.sarif
```

## SARIF Format Details

### Physical vs Logical Locations

The SARIF specification provides two complementary ways to specify locations:

1. **`physicalLocation`**: File system locations
   - `artifactLocation.uri`: File path (e.g., "xmlIO.c")
   - `region`: Line and column numbers

2. **`logicalLocations`**: Code structure locations
   - `name`: Function/method/class name
   - `kind`: Type of construct ("function", "method", "class", etc.)
   - `fullyQualifiedName`: Full namespace path (optional)

Our hint generator uses:
- **Level 1-2**: No location information
- **Level 3**: `physicalLocation` (file only) + `logicalLocations` (function name)
- **Level 4**: `physicalLocation` (file + lines) + `logicalLocations` (function name)

This provides both machine-parseable (structured) and human-readable (in message) location information.

## Notes

- SARIF files are generated once and stored in the benchmarks directory
- Hint levels are cumulative: higher levels include all information from lower levels
- The system uses a simplified SARIF generator to avoid Pydantic v1/v2 compatibility issues
- Generated hints follow the SARIF 2.1.0 specification
- Function names are included in both the `message` text and structured `logicalLocations` field

## References

- [SARIF Specification](https://docs.oasis-open.org/sarif/sarif/v2.1.0/sarif-v2.1.0.html)
- [CWE List](https://cwe.mitre.org/data/index.html)
- [CRSBench Documentation](../../docs/)
