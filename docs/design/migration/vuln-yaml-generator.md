# vuln.yaml Generator Design

## Overview
Automated tool to generate `vuln.yaml` files for CPVs by analyzing crash logs, POV files, patches, and source code. Uses Claude Agent SDK to perform intelligent vulnerability analysis.

## Problem Statement
- Manual creation of vuln.yaml is time-consuming and error-prone
- Need accurate CWE classifications and vulnerability descriptions
- Must locate exact vulnerable code locations from crash logs
- Many benchmarks have temporary MOCK vuln.yaml files that need replacement

## Architecture

### Components
1. **VulnAnalyzer Agent**: Analyzes crash logs and locates vulnerable code
2. **YamlGenerator Agent**: Generates structured vuln.yaml from analysis
3. **MOCK Detector**: Identifies temporary files for auto-regeneration
4. **CLI Orchestrator**: Manages bulk generation across benchmarks

### Data Flow
```
Crash Logs + POV + Patches → VulnAnalyzer → Analysis MD → YamlGenerator → vuln.yaml
                                    ↓                          ↓
                             vuln_analysis.md          vuln_agent_log.txt
```

## Implementation

### 1. VulnAnalyzer Agent
**Input**: Project directory, CPV directory, crash logs
**Output**: Markdown document with vulnerability analysis
**Tools**: Read, Grep, Glob, Bash

**Analysis Strategy**:
- Read crash logs from CPV's `logs/` directory (ONLY that CPV's logs)
- Extract error type (heap-buffer-overflow, use-after-free, etc.)
- Identify vulnerable function and file from stack trace
- Locate vulnerable code in project source
- Analyze POV files from `blobs/` directory
- Analyze patches from `patches/` directory (if available)
- Map vulnerability type to CWE classifications

**Critical Rules**:
- ONLY read crash logs from current CPV's `logs/` directory
- DO NOT search for crash logs in other CPVs
- Start analysis with crash logs as primary information source

### 2. YamlGenerator Agent
**Input**: Vulnerability analysis markdown
**Output**: Valid YAML content
**Tools**: Read (for context only)

**Generation Strategy**:
- Extract CWE numbers from analysis
- Create concise vulnerability name
- Format code location with relative paths
- Validate YAML structure
- Remove markdown fences if present

### 3. MOCK Detector
**Purpose**: Auto-detect temporary files
**Detection Rules**:
- Files containing `MOCK:` in content
- Files containing `(TBD)` in content
- These are regenerated WITHOUT requiring `--force` flag

### 4. CLI Orchestrator
**Command**: `python crsbench/migration/generate_vuln_yaml.py`
**Modes**:
- Single CPV: `--benchmark X --harness Y --cpv Z`
- All CPVs in benchmark: `--benchmark X`
- All benchmarks: (no arguments)

**Features**:
- Auto-clone project repos if not found
- Skip existing non-MOCK vuln.yaml files (unless `--force`)
- Batch processing with progress tracking
- Summary report with success/failure counts

## File Locations
- CLI: `crsbench/migration/generate_vuln_yaml.py`
- Generator: `crsbench/migration/vuln_yaml_generator.py`
- Repo manager: `crsbench/migration/repo_manager.py`
- Tests: `tests/test_repo_manager.py`

## vuln.yaml Format

```yaml
id: cpv_0

name: Heap buffer overflow in cr_buf_read

cwes:
- CWE-122
- CWE-787

description: |
  Heap-based buffer overflow occurs in the cr_buf_read function
  when processing HTTP responses. The vulnerability allows reading
  beyond allocated buffer boundaries, potentially leading to
  information disclosure or denial of service.

locations:
- path_from_root: lib/sendf.c
  function_name: cr_buf_read
  startLine: 1298
  startColumn: 5
  endLine: 1298
  endColumn: 20
```

## Usage Examples

### Generate for specific CPV
```bash
python crsbench/migration/generate_vuln_yaml.py \
  --benchmark atlanta-curl-delta-01 \
  --harness curl_fuzzer_http \
  --cpv cpv_0
```

### Generate for all CPVs in a benchmark
```bash
python crsbench/migration/generate_vuln_yaml.py \
  --benchmark atlanta-curl-delta-01
```

### Force overwrite all existing files
```bash
python crsbench/migration/generate_vuln_yaml.py \
  --benchmark atlanta-curl-delta-01 \
  --force
```

### Generate for ALL benchmarks
```bash
python crsbench/migration/generate_vuln_yaml.py
```

## Environment Requirements
- `LITELLM_BASE_URL`: LiteLLM proxy endpoint
- `LITELLM_API_KEY`: API key for LiteLLM
- `PROJECT_REPOS_DIR`: Optional directory for cloned repos

## Generated Artifacts
1. **vuln.yaml**: Structured vulnerability metadata (per CPV)
2. **vuln_analysis.md**: Detailed analysis document (per CPV)
3. **vuln_agent_log.txt**: Agent execution log with tool usage (per CPV)

## CWE Mapping
Common vulnerability types and their CWEs:
- Heap buffer overflow → CWE-122, CWE-787
- Stack buffer overflow → CWE-121, CWE-787
- Use-after-free → CWE-416
- NULL pointer dereference → CWE-476
- Integer overflow → CWE-190
- Out-of-bounds read → CWE-125
- Out-of-bounds write → CWE-787

## Key Design Decisions

### Why Two-Phase Generation?
- **Phase 1 (Analysis)**: Focuses on information extraction from logs/code
- **Phase 2 (YAML)**: Focuses on structured data generation
- Separation allows reviewing analysis before YAML generation
- Easier debugging when generation fails

### Why Auto-Detect MOCK Files?
- Many benchmarks have placeholder vuln.yaml files
- Manual identification is tedious
- Auto-detection enables bulk regeneration
- User doesn't need to remember which files are temporary

### Why Store Analysis MD?
- Provides audit trail of how vuln.yaml was generated
- Allows manual review of agent's reasoning
- Useful for debugging incorrect CWE classifications
- Can be used for future regeneration
