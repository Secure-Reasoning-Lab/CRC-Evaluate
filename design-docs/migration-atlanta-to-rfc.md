# Migration Script Design: Team-Atlanta Format to CRSBench RFC Format

## Overview

This document describes the design of a migration script that converts Team-Atlanta's benchmark format to the CRSBench RFC standard format.

## Goals

- Convert Team-Atlanta benchmark projects to RFC-compliant format
- Preserve all vulnerability information and ground truth data
- Maintain file integrity during migration
- Provide comprehensive logging and reporting
- Support dry-run mode for validation before actual migration

## Input/Output

### Input
- `--source-dir`: Team-Atlanta benchmark repository path (contains `projects/` directory)
- `--target-dir`: CRSBench benchmark repository path (will contain `benchmarks/` directory)
- `--dry-run`: Flag to perform validation without actual file operations
- `--output-csv`: Path to CSV file for migration report

### Output
- Converted benchmark projects in RFC format
- Migration log (console and optionally to file)
- CSV report of successfully converted benchmarks

## Directory Structure Mapping

### Team-Atlanta Format
```
projects/aixcc/{language}/{project-name}/
├── .aixcc/
│   ├── config.yaml           # Benchmark configuration
│   ├── vulns/                # Original vulnerability metadata
│   │   └── {harness}/{cpv_id}.yaml
│   ├── patches/              # Patch files
│   │   └── {harness}/{cpv_id}.diff
│   ├── povs/                 # POV blob files
│   │   └── {harness}/{cpv_id}
│   ├── crash_logs/           # Crash logs
│   │   └── {harness}/{cpv_id}.log
│   └── variants/             # Additional POV variants (optional)
│       └── {cpv_id}/{harness}/*.blob
├── build.sh
├── Dockerfile
├── test.sh
├── {harness}.options
└── project.yaml
```

### CRSBench RFC Format
```
benchmarks/{project-name}/
├── build.sh
├── Dockerfile
├── test.sh
├── (All other root-level files except .aixcc)
└── .aixcc/
    ├── meta.yaml             # Converted from config.yaml
    └── {harness}/
        └── {cpv_id}/         # Vulnerability ID (e.g., cpv_0)
            ├── vuln.yaml     # Vulnerability metadata
            ├── patches/
            │   └── patch_0.patch, patch_1.patch, ...
            ├── blobs/
            │   └── pov_0.blob, pov_1.blob, ...
            └── logs/
                └── pov_0.log, pov_1.log, ...
```

## Conversion Logic

### Language Filtering
- **Only C and Java/JVM projects are processed**
- Projects in other language directories are skipped
- Language is extracted from directory path: `projects/aixcc/{language}/`

### 1. Configuration File Conversion

**Source**: `.aixcc/config.yaml`
**Target**: `.aixcc/meta.yaml`

Uses Pydantic models for type-safe conversion:
- Extract `delta_mode` section (convert list to single entry if needed)
- Extract `full_mode` section
- Transform `harness_files` structure:
  - Keep `name` and `path` fields
  - Convert `cpvs` list to `vulns` structure
  - Map `cpv_id` to `vuln_keyword`
  - Extract POV information from each CPV
- Add `patch_exclude_list` with standard exclusions
- Format with proper line breaks between sections

### 2. Vulnerability Metadata Creation

**Source**: `.aixcc/vulns/{harness}/{cpv_id}.yaml` (original Team-Atlanta format)
**Target**: `.aixcc/{harness}/{cpv_id}/vuln.yaml`

Uses Pydantic models for validation and YAML generation:

#### Reading from Original Files
If original vulnerability file exists:
- Extract `name` from original file
- Extract `cwes` list (no inference, use as-is)
- Extract `description`
- Extract `locations` (path_from_root, startLine, startColumn, endLine, endColumn)
- Parse function name from crash log and populate `function_name` field

#### Function Name Extraction
Parse crash logs to extract function/method names:
- **C/C++ projects**: Parse AddressSanitizer stack traces
  - Pattern: `#0 0x[hex] in <function_name> <file>:<line>:<col>`
  - Example: `#0 0x56470813e7ba in extremelygoodprtcl_sm /src/curl/lib/extremelygoodprtcl.c:306:33`
- **JVM/Java projects**: Parse Java stack traces
  - Pattern: `at [module/]<ClassName>.<methodName>(<FileName>:<line>)`
  - Example: `at org.apache.pdfbox.ocr.OCRStreamEngine.doOCR(OCRStreamEngine.java:190)`
  - Example: `at java.base/java.util.regex.Pattern$BranchConn.match(Pattern.java:4698)`

#### Fallback to MOCK Data
If original vulnerability file not found:
- `name`: "MOCK: {cpv_id} vulnerability in {harness}"
- `cwes`: [] (empty list)
- `description`: Generated from available metadata with "MOCK:" prefix
- `locations`: Single mock location with crash log function name if available
- Mark all mock data with "MOCK:" prefix

#### Excluded Fields
- **author**: Not included in RFC format
- **note**: Not included in location metadata

### 3. File Migration

#### Root Files
- **Copy ALL root-level files and directories EXCEPT .aixcc**
- Includes: `build.sh`, `Dockerfile`, `test.sh`, `project.yaml`
- Includes: `*.options` files, `*.java` files
- Includes: `pkgs/` directory, `README.md`, etc.

#### Patches
- **Source**: `.aixcc/patches/{harness}/{cpv_id}.diff`
- **Target**: `.aixcc/{harness}/{cpv_id}/patches/patch_0.patch`
- Multiple patches from same source get IDs: `patch_0.patch`, `patch_1.patch`, etc.
- **Naming convention**: Underscore format (`patch_0`, not `patch-0`)

#### POV Blobs
- **Source**: `.aixcc/povs/{harness}/{cpv_id}` (file)
- **Target**: `.aixcc/{harness}/{cpv_id}/blobs/pov_0.blob`
- **Source**: `.aixcc/variants/{cpv_id}/{harness}/*.blob` (if exists)
- **Target**: `.aixcc/{harness}/{cpv_id}/blobs/pov_1.blob`, `pov_2.blob`, etc.
- **Naming convention**: Underscore format (`pov_0`, `pov_1`, not `pov-0`)
- Variants are numbered sequentially after the primary POV

#### Crash Logs
- **Source**: `.aixcc/crash_logs/{harness}/{cpv_id}.log`
- **Target**: `.aixcc/{harness}/{cpv_id}/logs/pov_0.log`
- **Source**: `.aixcc/variants/{cpv_id}/{harness}/*.log` (if exists)
- **Target**: `.aixcc/{harness}/{cpv_id}/logs/pov_1.log`, `pov_2.log`, etc.
- Match log files with corresponding blob files by UUID
- Used for parsing function names before migration

## Implementation Architecture

### Modules

#### 1. `atlanta_to_rfc.py` (Main Script)
- CLI argument parsing
- Orchestrates migration process
- Colored logging setup with ANSI codes
- CSV report generation (vulnerability-level detail)
- Language filtering (C and Java/JVM only)
- Repository URL extraction from project.yaml

#### 2. `models.py`
- Pydantic models for type-safe YAML generation
- `VulnerabilityLocation`: Location metadata (without note field)
- `VulnerabilityMetadata`: Complete vulnerability metadata (without author field)
- `MetaConfig`: Meta.yaml configuration
- `POV`, `Vulnerability`, `HarnessFile`, `DeltaMode`, `FullMode`: Supporting models
- Custom YAML formatting with line breaks

#### 3. `config_converter.py`
- Converts `config.yaml` to `meta.yaml` using Pydantic models
- Handles format transformations
- Returns harness information for vulnerability migration

#### 4. `vuln_metadata_generator.py`
- Generates vulnerability YAML files using Pydantic models
- Reads from original Team-Atlanta vulnerability files (`.aixcc/vulns/{harness}/{cpv_id}.yaml`)
- Parses crash logs to extract function names:
  - `_parse_c_stack_trace()`: C/C++ AddressSanitizer traces
  - `_parse_java_stack_trace()`: Java/JVM stack traces
- Creates mock metadata for missing files
- Marks generated fields with "MOCK:" prefix

#### 5. `file_migrator.py`
- Handles file copying and renaming
- Manages directory structure creation
- Supports dry-run mode
- Copies all root files except .aixcc

### Data Structures

#### MigrationContext
```python
@dataclass
class MigrationContext:
    source_dir: Path
    target_dir: Path
    dry_run: bool
    project_name: str
    successful: bool = True
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    num_vulns: int = 0
    num_harnesses: int = 0
    vulns: List[VulnInfo] = field(default_factory=list)
    repo_url: str = ""  # From project.yaml
    language: str = ""  # Extracted from path
    mode: str = ""      # delta or full
    harness_info: Dict = field(default_factory=dict)
```

#### VulnInfo
```python
@dataclass
class VulnInfo:
    """Information about a vulnerability for CSV reporting."""
    project_name: str
    source: str           # "Team-Atlanta"
    repo_url: str         # From project.yaml
    mode: str             # "delta" or "full"
    branch: str           # Default: "master"
    language: str         # "C" or "JVM"
    harness_name: str
    vuln_id: str          # e.g., "cpv_0"
```

## Error Handling

### Fatal Errors (Stop Migration)
- Source directory does not exist
- Missing critical files (config.yaml, build.sh)
- Invalid YAML syntax
- Target directory conflicts

### Non-Fatal Errors (Log and Continue)
- Missing optional files (variants/, regressions/)
- Missing POV blobs or crash logs
- Incomplete vulnerability metadata

### Warnings
- Mock data being generated
- Missing optional metadata fields
- File permission issues

## Logging

### Log Levels
- **DEBUG**: Detailed information (crash log parsing, file operations)
- **INFO**: Normal operation progress
- **WARNING**: Non-critical issues, mock data generation, missing files
- **ERROR**: Non-fatal errors
- **CRITICAL**: Fatal errors that stop migration

### Log Format with Colors
```
[TIMESTAMP] [LEVEL] Message
```

ANSI color codes used:
- **DEBUG**: Cyan
- **INFO**: Green
- **WARNING**: Yellow
- **ERROR**: Red
- **CRITICAL**: Magenta

Special markers:
- `[DRY RUN]`: Magenta/bold
- `Successfully migrated`: Green/bold with ✓
- `Failed`: Red/bold with ✗
- `Processing project:`: Blue/bold with →

### Progress Indicators
- Project-level: "→ Processing project: {project_name}"
- Operation-level: "  Converting config.yaml to meta.yaml"
- Vulnerability-level: "    Processing {harness}/{cpv_id}"
- Debug details: "      Copied: {source} -> {target}"
- Function parsing: "Parsed function name '{name}' from crash log"

## CSV Report Format

**Vulnerability-level detail** (one row per vulnerability):

```csv
Project Name,Source,Vuln. Repo URL,Mode,Branch,Language,Harness Name,Vuln. ID
curl-delta-04,Team-Atlanta,git@github.com:Team-Atlanta/official-afc-curl.git,delta,master,C,curl_fuzzer_http,cpv_0
curl-delta-04,Team-Atlanta,git@github.com:Team-Atlanta/official-afc-curl.git,delta,master,C,curl_fuzzer_ws,cpv_1
apache-commons-compress-delta-01,Team-Atlanta,git@github.com:Team-Atlanta/official-afc-commons-compress.git,delta,master,JVM,CompressTarFuzzer,cpv_0
```

Fields:
- **Project Name**: Project directory name
- **Source**: Always "Team-Atlanta"
- **Vuln. Repo URL**: Repository URL from project.yaml's `main_repo` field
- **Mode**: "delta" or "full"
- **Branch**: Default "master"
- **Language**: "C" or "JVM" (extracted from directory path)
- **Harness Name**: Harness identifier
- **Vuln. ID**: CPV identifier (e.g., cpv_0, cpv_1)

## Implementation Details

### Vulnerability Metadata YAML Structure

```yaml
id: cpv_0

name: HTTP Header-Parsing Stack Buffer Overflow

cwes:
- CWE-121

description: curl will crash if a >64 character string is passed in the HTTP server response method, "X-Powered-By".

locations:
- path_from_root: lib/http.c
  function_name: Curl_doh_close
  startLine: 3011
  startColumn: 1
  endLine: 3020
  endColumn: 1
```

**Key Points**:
- No `author` field
- No `note` field in locations
- `function_name` populated from crash log parsing
- Proper line breaks between major sections
- Empty `cwes` list if no CWEs available

### Naming Conventions

| Item | Format | Examples |
|------|--------|----------|
| Vulnerability ID | `cpv_{N}` | cpv_0, cpv_1, cpv_2 |
| POV ID | `pov_{N}` | pov_0, pov_1, pov_2 |
| Patch ID | `patch_{N}` | patch_0, patch_1 |
| Vulnerability file | `vuln.yaml` | vuln.yaml (not cpv_0.yaml) |

**Note**: Underscore format is used throughout, not hyphen.

## Usage Examples

### Basic Migration
```bash
.venv/bin/python crsbench/migration/atlanta_to_rfc.py \
  --source-dir /path/to/oss-fuzz/projects \
  --target-dir /path/to/CRSBench/benchmarks \
  --output-csv migration_report.csv
```

### Dry Run with Verbose Logging
```bash
.venv/bin/python crsbench/migration/atlanta_to_rfc.py \
  --source-dir /path/to/oss-fuzz/projects \
  --target-dir /path/to/CRSBench/benchmarks \
  --dry-run \
  --verbose
```

### Specific Projects
```bash
.venv/bin/python crsbench/migration/atlanta_to_rfc.py \
  --source-dir /path/to/oss-fuzz/projects \
  --target-dir /path/to/CRSBench/benchmarks \
  --projects curl-delta-04,libxml2-delta-03,apache-commons-compress-delta-01 \
  --output-csv migration_report.csv
```

### Example Output
```
[2025-10-13 23:27:10] [INFO] Found 1 projects to migrate
[2025-10-13 23:27:10] [INFO] → Processing project: curl-delta-04
[2025-10-13 23:27:10] [INFO]   Converting config.yaml to meta.yaml
[2025-10-13 23:27:10] [DEBUG] Migrated from Team-Atlanta format to RFC format: /tmp/test/curl-delta-04/.aixcc/meta.yaml
[2025-10-13 23:27:10] [INFO]   Migrating root files
[2025-10-13 23:27:10] [DEBUG]       Copied: build.sh -> /tmp/test/curl-delta-04/build.sh
[2025-10-13 23:27:10] [INFO]   Migrating vulnerabilities
[2025-10-13 23:27:10] [INFO]     Processing curl_fuzzer_http/cpv_0
[2025-10-13 23:27:10] [DEBUG] Parsed function name 'Curl_doh_close' from crash log
[2025-10-13 23:27:10] [DEBUG] Migrated vulnerability metadata from ... to .../vuln.yaml
[2025-10-13 23:27:10] [INFO] ✓ Successfully migrated curl-delta-04
```
