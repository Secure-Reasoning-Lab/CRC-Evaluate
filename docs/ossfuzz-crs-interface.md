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
    ├── 1h/                  # Corpus from 1 hour of fuzzing
    │   ├── input-001
    │   ├── input-002
    │   └── ...
    └── 1d/                  # Corpus from 1 day of fuzzing
        ├── input-001
        ├── input-002
        └── ...
```

**Container filesystem mapping:**

When `--hints` is provided, the hints directory is mounted in the container:

- Host: `<hints-dir>` (e.g., `benchmarks/json-c/.aixcc/json_array_fuzzer/hints/`)
- Container: `/hints/`

Inside the container, CRS can access:
- `/hints/sarif/*.sarif` - Static analysis reports in SARIF format
- `/hints/corpus/1h/*.blob` - Corpus from 1 hour of fuzzing
- `/hints/corpus/1d/*.blob` - Corpus from 1 day of fuzzing

**Usage notes:**

- Hints are optional - CRS should work without them
- CRS can choose which hints to use (e.g., only SARIF, only corpus, or both)
- CRS can select corpus difficulty level (1h = easier, 1d = harder)
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
│   │           ├── 1h/
│   │           │   ├── input-001
│   │           │   └── ...
│   │           └── 1d/
│   │               └── ...
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

Run command with POV and LiteLLM configuration:

```sh
python3 infra/helper.py run_crs <crs-config-name> <project-name> \
  --harness <harness-name> \
  [--pov <pov-name>] \
  --litellm-base <litellm-api-base> \
  --litellm-key <litellm-api-key>
```

**Example**:
```sh
python3 infra/helper.py run_crs multi-retrieval aixcc/c/mock-c \
  --pov pov1 \
  --harness fuzz_process_input_header \
  --litellm-base https://api.litellm.com \
  --litellm-key sk-your-key-here
```

**Arguments**:
- `<crs-config-name>`: Configuration name for the patch generation CRS
- `<project-name>`: Project name
- `--harness <harness-name>`: Fuzzing harness name (required)
- `--pov <pov-name>`: Specific POV to generate patch for (optional - CRS can generate patches without specific POV)
- `--litellm-base <url>`: LiteLLM API base URL (required)
- `--litellm-key <key>`: LiteLLM API key (required)

### Key Differences from OSS-Fuzz Interface

| Feature          | OSS-Fuzz (Bug Finding)  | OSS-Patch (Patch Generation)                 |
|------------------|-------------------------|----------------------------------------------|
| Repository       | `oss-fuzz`              | `oss-patch`                                  |
| Purpose          | Vulnerability discovery | Program repair                               |
| POV argument     | Not applicable          | Optional (`--pov`)                           |
| Harness argument | Positional              | Named (`--harness`)                          |
| LiteLLM          | Optional                | Required (`--litellm-base`, `--litellm-key`) |
| OSS-Fuzz path    | Not needed              | Required (`--oss-fuzz`)                      |

---

## Future Command Wrappers

The OSS-Fuzz team is developing installable command wrappers:

- **`oss-fuzz-crs`**: Wrapper for bug finding CRS (from `oss-fuzz` repo)
- **`oss-patch-crs`**: Wrapper for patch generation CRS (from `oss-patch` repo)

These will be installable via pip/uv and provide a cleaner interface than direct `infra/helper.py` invocation.
