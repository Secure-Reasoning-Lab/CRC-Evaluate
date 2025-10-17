# Interface to run standardized CRS interface

## NOTE
The following interfaces are still not finalized and subject to change.

## Overview

This document describes two standardized CRS interfaces:

1. **OSS-Fuzz CRS Interface**: For bug finding / vulnerability discovery CRS (from `oss-fuzz` repository)
2. **OSS-Patch CRS Interface**: For patch generation / program repair CRS (from `oss-patch` repository)

Both use `infra/helper.py` but from different repositories with different capabilities.

---

## OSS-Fuzz CRS Interface (Bug Finding)

Repository: `oss-fuzz`

### Building CRS docker image
It starts with `build_crs` command with following arguments:

- configuration files directory for a CRS `ensemble-c`: `infra/crs/example_configs/ensemble-c`
- project name: `json-c`

```sh
python3 infra/helper.py build_crs infra/crs/example_configs/ensemble-c json-c
```

## Running CRS for specific fuzzing harnesses
`run_crs` accepts the same arguments as `build_crs`, it additionally accept the
following arguments:

- fuzzing harness name: `json_array_fuzzer`

```sh
python3 infra/helper.py run_crs infra/crs/example_configs/ensemble-c json-c json_array_fuzzer
```

### Filesystem mapping between host and docker container
On host, it is `build/out/<crs-name>/<project-name>/<harness-name>/{crashes, corpus}`.

In the docker container, the directory will be mapped to `/out/<harness-name>/{crashes, corpus}`.

### Optional Hints Support

CRS implementations can optionally receive hints to guide vulnerability discovery. Hints include:

- **SARIF reports**: Static analysis results pointing to potential bug locations
- **Pre-fuzzing corpus**: Seed inputs from short fuzzing runs to bootstrap fuzzing

**Command-line interface:**

```sh
python3 infra/helper.py run_crs <config> <project> <harness> --hints <hints-dir>
```

**Example:**

```sh
python3 infra/helper.py run_crs ensemble-c json-c json_array_fuzzer \
  --hints /path/to/benchmarks/json-c/.aixcc/json_array_fuzzer/hints
```

**Hints directory structure:**

```
hints/
├── sarif/                    # Static analysis reports
│   ├── codeql.sarif         # CodeQL analysis results
│   ├── semgrep.sarif        # Semgrep analysis results
│   └── ...                  # Other SARIF-format reports
└── corpus/                   # Pre-fuzzing corpus
    ├── input-001.blob
    ├── input-002.blob
    └── ...
```

**Container filesystem mapping:**

When `--hints` is provided, the hints directory is mounted in the container:

- Host: `<hints-dir>` (e.g., `benchmarks/json-c/.aixcc/json_array_fuzzer/hints/`)
- Container: `/hints/`

Inside the container, CRS can access:
- `/hints/sarif/*.sarif` - Static analysis reports in SARIF format
- `/hints/corpus/*.blob` - Pre-fuzzing corpus files

**Usage notes:**

- Hints are optional - CRS should work without them
- CRS can choose which hints to use (e.g., only SARIF, only corpus, or both)
- Multiple SARIF files from different tools can be provided
- SARIF format: [SARIF v2.1.0 specification](https://docs.oasis-open.org/sarif/sarif/v2.1.0/sarif-v2.1.0.html)

**Typical benchmark structure:**

```
benchmarks/json-c/
├── .aixcc/
│   ├── meta.yaml
│   ├── json_array_fuzzer/
│   │   ├── cpv_0/
│   │   │   └── vuln.yaml
│   │   └── hints/              # Hints for this harness
│   │       ├── sarif/
│   │       │   ├── codeql.sarif
│   │       │   └── semgrep.sarif
│   │       └── corpus/
│   │           ├── input-001.blob
│   │           ├── input-002.blob
│   │           └── ...
│   └── json_parse_fuzzer/
│       └── hints/              # Separate hints per harness
│           └── ...
└── ...
```

---

## OSS-Patch CRS Interface (Patch Generation)

Repository: `oss-patch`

**Important**: This is a different repository from `oss-fuzz`, but also uses `infra/helper.py` with extended functionality for patch generation.

### Environment Setup

Set the OSS-Fuzz home directory (required for patch generation):

```sh
export OSS_FUZZ_HOME=/path/to/oss-fuzz
```

### Building CRS docker image

Build command with OSS-Fuzz path:

```sh
python3 infra/helper.py build_crs <crs-config-name> <project-name> --oss-fuzz $OSS_FUZZ_HOME
```

**Example**:
```sh
python3 infra/helper.py build_crs multi-retrieval aixcc/c/mock-c --oss-fuzz $OSS_FUZZ_HOME
```

**Arguments**:
- `<crs-config-name>`: Configuration name for the patch generation CRS (e.g., `multi-retrieval`)
- `<project-name>`: Project name (e.g., `aixcc/c/mock-c`)
- `--oss-fuzz`: Path to OSS-Fuzz home directory

### Running CRS for patch generation

Run command with POV(s), hints, and LiteLLM configuration:

```sh
python3 infra/helper.py run_crs <crs-config-name> <project-name> \
  --harness <harness-name> \
  [--pov <pov-file> | --povs <povs-dir>] \
  [--hints <hints-dir>] \
  --litellm-base <litellm-api-base> \
  --litellm-key <litellm-api-key>
```

**Example - With single POV**:
```sh
python3 infra/helper.py run_crs multi-retrieval aixcc/c/mock-c \
  --harness fuzz_process_input_header \
  --pov /path/to/benchmarks/mock-c/.aixcc/fuzz_process_input_header/povs/pov_0 \
  --litellm-base https://api.litellm.com \
  --litellm-key sk-your-key-here
```

**Example - With POVs directory**:
```sh
python3 infra/helper.py run_crs multi-retrieval aixcc/c/mock-c \
  --harness fuzz_process_input_header \
  --povs /path/to/benchmarks/mock-c/.aixcc/fuzz_process_input_header/povs \
  --litellm-base https://api.litellm.com \
  --litellm-key sk-your-key-here
```

**Example - Without POV specification (process all available POVs)**:
```sh
python3 infra/helper.py run_crs multi-retrieval aixcc/c/mock-c \
  --harness fuzz_process_input_header \
  --litellm-base https://api.litellm.com \
  --litellm-key sk-your-key-here
```

**Example - With single POV and hints**:
```sh
python3 infra/helper.py run_crs multi-retrieval aixcc/c/mock-c \
  --harness fuzz_process_input_header \
  --pov /path/to/benchmarks/mock-c/.aixcc/fuzz_process_input_header/povs/pov_0 \
  --hints /path/to/benchmarks/mock-c/.aixcc/fuzz_process_input_header/hints \
  --litellm-base https://api.litellm.com \
  --litellm-key sk-your-key-here
```

**Example - With POVs directory and hints**:
```sh
python3 infra/helper.py run_crs multi-retrieval aixcc/c/mock-c \
  --harness fuzz_process_input_header \
  --povs /path/to/benchmarks/mock-c/.aixcc/fuzz_process_input_header/povs \
  --hints /path/to/benchmarks/mock-c/.aixcc/fuzz_process_input_header/hints \
  --litellm-base https://api.litellm.com \
  --litellm-key sk-your-key-here
```

**Arguments**:
- `<crs-config-name>`: Configuration name for the patch generation CRS
- `<project-name>`: Project name
- `--harness <harness-name>`: Fuzzing harness name (required)
- `--pov <pov-file>`: Path to a single POV file (optional, mutually exclusive with `--povs`)
  - Specifies a single POV test case file
  - Mounted to `/pov` in the container (single file)
  - Use when targeting a specific vulnerability
- `--povs <povs-dir>`: Path to directory containing POVs (optional, mutually exclusive with `--pov`)
  - Directory contains multiple POV files: `povs/pov_0`, `povs/pov_1`, etc.
  - Mounted to `/povs/` in the container (directory)
  - Use when processing multiple POVs
  - **CRSBench generates this directory** based on experiment configuration
- If neither `--pov` nor `--povs` is specified, CRS can attempt to generate patches for all available vulnerabilities (implementation-dependent)
- `--hints <hints-dir>`: Path to hints directory (optional)
  - Provides SARIF reports and pre-fuzzing corpus to guide patch generation
  - Same structure as OSS-Fuzz bug finding hints (see above)
  - **CRSBench generates this directory** based on experiment configuration
- `--litellm-base <url>`: LiteLLM API base URL (required)
- `--litellm-key <key>`: LiteLLM API key (required)

### Filesystem mapping between host and docker container

OSS-Patch mounts POV file(s) to provide vulnerability information to the CRS.

**Single POV file mapping (--pov):**

When `--pov <pov-file>` is provided:
- Host: `<pov-file>` (e.g., `benchmarks/mock-c/.aixcc/fuzz_process_input_header/povs/pov_0`)
- Container: `/pov` (single file, not a directory)

**POVs directory mapping (--povs):**

When `--povs <povs-dir>` is provided:
- Host: `<povs-dir>` (e.g., `benchmarks/mock-c/.aixcc/fuzz_process_input_header/povs/`)
- Container: `/povs/` (directory)

The host povs directory structure is flattened and mounted directly to `/povs/` in the container.

**Container filesystem structure:**

Inside the container, CRS can access POV test case(s):

**With --pov (single file):**
```
/pov                     # Single POV test case file
```

**With --povs (directory):**
```
/povs/
├── pov_0                # Test case that triggers a vulnerability
├── pov_1                # Test case
├── pov_2                # Test case
└── ...
```

**Usage notes:**

- CRSBench generates the `povs/` directory based on experiment configuration
- **Single POV mode (`--pov`):**
  - Mount a single POV file to `/pov` in the container
  - Use when targeting a specific vulnerability or for focused patch generation
  - CRS runs `/pov` to generate crash log and generate a patch
- **Multiple POVs mode (`--povs`):**
  - Mount a directory of POVs to `/povs/` in the container
  - Each POV is a file directly in the directory (e.g., `/povs/pov_0`)
  - CRS must iterate through all POVs in `/povs/`
  - **CRS must determine which POVs share the same root cause** (grouping is CRS's responsibility)
  - POVs are not pre-grouped by vulnerabilities - they are flat files
- **No POV specification:**
  - If neither `--pov` nor `--povs` is specified, CRS may process all available POVs (implementation-dependent)
- **CRS is responsible for running each POV to generate crash logs**
- CRS must analyze crash logs (generated by running POVs) to understand the vulnerability
- No vulnerability metadata, crash logs, or reference patches are provided
- POVs are used to:
  1. Generate crash logs for analysis
  2. Verify that generated patches fix the vulnerabilities

**Typical benchmark structure on host:**

```
benchmarks/mock-c/
├── .aixcc/
│   ├── meta.yaml
│   └── fuzz_process_input_header/
│       ├── cpv_0/                     # Ground truth vulnerability groups
│       │   └── blobs/
│       │       └── pov_0.blob
│       │   └── logs/
│       │       └── pov_0.log         # For evaluation only
│       ├── cpv_1/
│       │   └── blobs/
│       │       ├── pov_1.blob
│       │       └── pov_2.blob
│       │   └── logs/
│       │       ├── pov_1.log         # For evaluation only
│       │       └── pov_2.log         # For evaluation only
│       └── povs/                       # Generated by CRSBench for CRS
│           ├── pov_0                  # POV file (no subdirectory)
│           ├── pov_1                  # POV file
│           └── pov_2                  # POV file
```

**How it works:**

1. **Ground truth**: CPV directories (`cpv_*/`) contain the actual vulnerabilities with POV blobs and logs (for evaluation)
2. **CRSBench generates**: The `povs/` directory from CPVs based on experiment configuration (which POVs to include)
3. **POVs are flattened**: Individual POVs are extracted as flat files (not grouped by CPV, no subdirectories)
4. **Only POV files provided**: The `povs/` directory contains only POV test case files, not logs
5. **Mounted to container**: The `povs/` directory is mounted to `/povs/` in the container
6. **CRS generates logs**: CRS runs each POV file to generate crash logs for analysis
7. **CRS discovers grouping**: CRS must determine which POVs (pov_0, pov_1, pov_2) share the same root cause

### Optional Hints Support for Patch Generation

Like the OSS-Fuzz bug finding interface, OSS-Patch CRS can optionally receive hints to improve patch generation.

**Hints directory mapping:**

When `--hints` is provided:
- Host: `<hints-dir>` (e.g., `benchmarks/mock-c/.aixcc/fuzz_process_input_header/hints/`)
- Container: `/hints/`

**Hints directory structure (same as bug finding):**

```
hints/
├── sarif/                    # Static analysis reports
│   ├── codeql.sarif         # May identify vulnerability locations
│   ├── semgrep.sarif        # Pattern-based bug detection
│   └── ...
└── corpus/                   # Pre-fuzzing corpus
    ├── input-001.blob
    ├── input-002.blob
    └── ...
```

**Container access:**

Inside the container, CRS can access:
- `/hints/sarif/*.sarif` - Static analysis reports pointing to bug locations
- `/hints/corpus/*.blob` - Pre-fuzzing corpus to verify patch correctness

**Usage for patch generation:**

- **SARIF reports**: May help identify vulnerability root causes and suggest fix locations
- **Pre-fuzzing corpus**: Can be used as regression tests to ensure patches don't break functionality
- **Combination with POVs**: Use POV blobs from `/povs/` to verify vulnerability is fixed, and corpus from `/hints/` to verify no regressions

**Complete container filesystem for patch generation:**

**With --pov (single POV):**
```
/
├── pov                      # Single POV test case (if --pov provided)
├── hints/                   # Optional hints (if --hints provided)
│   ├── sarif/
│   └── corpus/
├── out/                     # Output directory for generated patches
│   └── patches/
├── src/                     # Project source code
└── work/                    # Working directory
```

**With --povs (multiple POVs):**
```
/
├── povs/                    # POV test cases (if --povs provided)
│   ├── pov_0               # POV file
│   ├── pov_1               # POV file
│   ├── pov_2               # POV file
│   └── ...
├── hints/                   # Optional hints (if --hints provided)
│   ├── sarif/
│   └── corpus/
├── out/                     # Output directory for generated patches
│   └── patches/
├── src/                     # Project source code
└── work/                    # Working directory
```

**Example workflow (single POV with --pov):**

1. CRS runs `/pov` to generate crash log
2. CRS analyzes the crash log to understand the vulnerability (sanitizer output, stack trace)
3. CRS reads `/hints/sarif/*.sarif` to find potential bug locations (optional)
4. CRS generates patch candidates for the vulnerability
5. CRS tests patch with `/pov` (must fix the POV)
6. CRS validates with `/hints/corpus/*.blob` (must not break existing functionality)
7. CRS outputs final patch to `/out/patches/`

**Example workflow (multiple POVs with --povs):**

1. CRS iterates through POVs in `/povs/` (e.g., pov_0, pov_1, pov_2)
2. For each POV, CRS runs `/povs/pov_0` to generate crash log
3. CRS analyzes the crash log to understand the vulnerability (sanitizer output, stack trace)
4. CRS reads `/hints/sarif/*.sarif` to find potential bug locations (optional)
5. CRS determines which POVs share the same root cause (e.g., pov_0 and pov_1 might be the same bug)
6. CRS generates patch candidates for each unique vulnerability
7. CRS tests patches with `/povs/pov_0`, `/povs/pov_1`, etc. (must fix all related POVs)
8. CRS validates with `/hints/corpus/*.blob` (must not break existing functionality)
9. CRS outputs final patches to `/out/patches/`

### Key Differences from OSS-Fuzz Interface

| Feature          | OSS-Fuzz (Bug Finding)  | OSS-Patch (Patch Generation)                 |
|------------------|-------------------------|----------------------------------------------|
| Repository       | `oss-fuzz`              | `oss-patch`                                  |
| Purpose          | Vulnerability discovery | Program repair                               |
| POV argument     | Not applicable          | Optional (`--pov <pov-file>`)                |
| POVs argument    | Not applicable          | Optional (`--povs <povs-dir>`)               |
| Hints argument   | Optional (`--hints`)    | Optional (`--hints`)                         |
| Harness argument | Positional              | Named (`--harness`)                          |
| LiteLLM          | Optional                | Required (`--litellm-base`, `--litellm-key`) |
| OSS-Fuzz path    | Not needed              | Required (`--oss-fuzz`)                      |
| Container mounts | `/hints/` (optional)    | `/pov` or `/povs/`, `/hints/` (all optional) |

---

## Future Command Wrappers

The OSS-Fuzz team is developing installable command wrappers:

- **`oss-fuzz-crs`**: Wrapper for bug finding CRS (from `oss-fuzz` repo)
- **`oss-patch-crs`**: Wrapper for patch generation CRS (from `oss-patch` repo)

These will be installable via pip/uv and provide a cleaner interface than direct `infra/helper.py` invocation.
