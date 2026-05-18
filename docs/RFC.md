# CRSBench Benchmark Specification

Status: Draft
Canonical Path: `docs/RFC.md`
Last Updated: 2026-05-15

This document defines the benchmark package format used by CRSBench to
evaluate Cyber Reasoning Systems (CRS). It is a data contract for benchmark
authors, validation tools, and evaluators.

CRSBench benchmarks must be reproducible, self-contained, and comparable across
CRS implementations. Implementation details for orchestration, reporting, and
cloud execution belong in the relevant guide or design document, not in this
specification.

## Scope

This specification covers:

- the benchmark directory layout
- source archive packaging
- `.aixcc/meta.yaml`
- harness, POV, patch, and invariant-test requirements
- benchmark suite selection syntax
- validation requirements

It does not define CRS internals, model-provider behavior, experiment
orchestration, distributed scheduling, report formats, or archive/logging
formats.

## Benchmark Goals

CRSBench exists to:

- aggregate AIxCC-derived and CRSBench-created benchmarks into one reproducible
  format
- support fair evaluation across CRS implementations
- evaluate vulnerability discovery and patch generation
- preserve enough provenance for audits and offline rebuilds
- keep benchmark ground truth separate from CRS-generated outputs

The benchmark set evolves over time. For the current local inventory, run:

```bash
uv run crsbench benchmark stats
```

Benchmark source and mode groupings are tracked by benchmark names and
`benchmark-suites/`.

## Benchmark Layout

Each benchmark lives under `benchmarks/<benchmark-name>/`.

```example
benchmarks/<benchmark-name>/
├── build.sh
├── test.sh
├── Dockerfile
├── project.yaml
├── pkgs/
│   ├── <source-name>.tar.gz
│   ├── <dependency-name>.tar.gz
│   └── pkg_refs.txt
├── <other Dockerfile inputs>
└── .aixcc/
    ├── meta.yaml
    ├── ref.diff
    └── <harness-name>/
        └── <cpv-id>/
            ├── vuln.yaml
            ├── <cpv-id>.md
            ├── patches/
            │   └── <patch-id>.diff
            └── <pov-id>/
                ├── <pov-id>.blob
                └── <pov-id>.log
```

Required files:

- `Dockerfile`: builds the OSS-Fuzz-compatible project environment.
- `build.sh`: builds harness artifacts using the OSS-Fuzz convention.
- `test.sh`: validates program invariants after patches are applied.
- `project.yaml`: project metadata used by OSS-Fuzz-compatible tooling.
- `pkgs/`: source and dependency archives needed for offline builds.
- `.aixcc/meta.yaml`: benchmark configuration.

Required ground-truth files for each CPV:

- `vuln.yaml`: structured vulnerability metadata. See
  [Vulnerability Metadata RFC](./reference/vuln-yaml.md).
- `<cpv-id>.md`: human-readable root-cause notes.
- `patches/*.diff`: one or more accepted patches for the CPV.
- `<pov-id>/<pov-id>.blob`: crash-triggering input.
- `<pov-id>/<pov-id>.log`: sanitizer or crash log for the input.

`ref.diff` is required for delta-mode benchmarks and omitted for full-mode
benchmarks unless needed as auxiliary context.

## Source Packaging

Benchmarks must build without network access to upstream source repositories.
The `pkgs/` directory contains the primary source archive and any dependency
archives required by the Dockerfile.

Every Dockerfile must embed source from `pkgs/` using either `COPY` plus
extraction or Docker `ADD` auto-extraction.

```dockerfile
COPY pkgs/<source-name>.tar.gz $SRC/<source-name>.tar.gz
RUN tar -xzf $SRC/<source-name>.tar.gz && rm $SRC/<source-name>.tar.gz

WORKDIR $SRC/<source-name>
```

The source archive name must match the final source directory used by
`WORKDIR`. For example, `WORKDIR $SRC/curl` requires `pkgs/curl.tar.gz`.

JVM benchmarks may use nested source paths:

```dockerfile
COPY pkgs/<source-name>.tar.gz $SRC/src/<source-name>.tar.gz
RUN tar -xzf $SRC/src/<source-name>.tar.gz -C $SRC/src/ && rm $SRC/src/<source-name>.tar.gz

WORKDIR $SRC/src/<source-name>
```

Large source archives are stored with Git LFS. Split archives are not required.

### `pkg_refs.txt`

`pkgs/pkg_refs.txt` records archive provenance and exact revisions:

```text
curl.tar.gz=https://github.com/curl/curl@abc123def456
zlib.tar.gz=https://github.com/madler/zlib@v1.3.1
```

Every dependency archive in `pkgs/` must have a pinned entry.

## `meta.yaml`

`.aixcc/meta.yaml` defines evaluation mode, harnesses, POVs, and patch
constraints.

```yaml
patch_exclude_list:
  - "build.sh"
  - "Dockerfile"
  - ".aixcc/**"
  - "test/**"

delta_mode:
  base_commit: "35af1ffb5dd21ae47332577c2b6c889da302b497"
  ref_commit: "baacf7a0891d4a478c403515f05c2387044a94d0"

harness_files:
  - name: "ossfuzz"
    path: "$REPO/test/ossfuzz.c"
    vulns:
      - vuln_keyword: "cpv_0"
        povs:
          - id: "pov_0"
            sanitizer: "address"
            error_token: "ERROR: AddressSanitizer: heap-buffer-overflow"
```

Full-mode benchmarks use `full_mode` instead of `delta_mode`:

```yaml
full_mode:
  base_commit: "baacf7a0891d4a478c403515f05c2387044a94d0"
```

Exactly one of `delta_mode` or `full_mode` must be present.

### Evaluation Modes

`delta_mode` identifies a base commit and a vulnerable reference commit. The
benchmark must include `.aixcc/ref.diff` containing the bug-inducing diff.

`full_mode` identifies the vulnerable codebase without providing a focused
bug-inducing diff.

### Harnesses

`harness_files` lists fuzzing harnesses available to the CRS.

```yaml
harness_files:
  - name: "customfuzz3"
    path: "$PROJECT/customfuzz3.c"
```

Harness paths may use:

- `$REPO/...`: path relative to the unpacked source repository
- `$PROJECT/...`: path relative to the OSS-Fuzz project directory
- `/...`: absolute container path
- `./...`: relative path from the current working directory

Harnesses without `vulns` are allowed and are treated as non-vulnerable or
distractor harnesses.

### Vulnerabilities and POVs

Each vulnerability entry groups POVs that share one root cause.

```yaml
vulns:
  - vuln_keyword: "cpv_0"
    patch_superset: "cpv_7"
    povs:
      - id: "pov_0"
        sanitizer: "address"
        error_token: "ERROR: AddressSanitizer: heap-buffer-overflow"
```

Rules:

- `vuln_keyword` must match `cpv_N`.
- `vuln_keyword` must match the CPV directory under `.aixcc/<harness>/`.
- Each vulnerability must list at least one POV.
- POV `id` values must be unique within a vulnerability.
- `sanitizer` must be one of `address`, `memory`, `thread`, `undefined`, or
  `leak`.
- `error_token` is optional and, when present, is matched as a substring
  against sanitizer output.
- `patch_superset` is optional. When set, it names a CPV whose patch also fixes
  this CPV.

## Patch Constraints

`patch_exclude_list` defines files and globs that CRS-generated patches may not
modify.

Common exclusions include:

- build files: `build.sh`, `Dockerfile`, `Makefile`, `CMakeLists.txt`
- benchmark metadata: `.aixcc/**`, `*.blob`, `*.log`
- harness files listed in `harness_files`
- tests or validation scripts when modifying them would weaken invariants

Patch validation must reject changes to excluded paths before build or scoring.

## Build Contract

`build.sh` follows the OSS-Fuzz project build convention. It should build the
configured harnesses and place runnable artifacts in `$OUT`.

Patch application is handled by the evaluator outside `build.sh`. Benchmark
build scripts must not rely on local patch state, mutable source mounts, or
network access to fetch project sources.

## Invariant Checking

`test.sh` validates that a patch preserves intended program behavior.

Required behavior:

- exit code `0`: invariants pass
- non-zero exit code: invariants fail

Typical checks include compilation, regression tests, replay of known POVs
against the patched build, differential tests, and targeted fuzzing. A
benchmark may choose the checks appropriate for its project, but `test.sh` must
be deterministic enough for repeated local and non-local evaluation.

If `test.sh` supports optional modes or test selection, those modes must not
change the meaning of pass/fail for the default invariant check.

## CRS Evaluation Configuration

Experiments are run with:

```bash
crsbench run --experiment-config <config.yaml>
```

Experiment configuration is documented in
[`docs/reference/experiment-config.md`](./reference/experiment-config.md).
This RFC only requires that benchmark references resolve to valid benchmark
packages and suites.

## Benchmark Suites

A benchmark suite groups benchmark IDs for experiments.

```yaml
Name: crsbench-c
Description: A benchmark suite for evaluating C/C++ CRS.
Release date: 09.23.2025
benchmark_list:
  - afc-curl-delta-01
  - afc-libxml2-delta-03:
      - html
      - xml
```

`benchmark_list` entries support two forms:

- string: run all harnesses for the benchmark
- mapping: run only the listed harnesses

## Validation Requirements

`crsbench benchmark validate` must check at least:

- required files exist
- `meta.yaml` is valid YAML and matches the schema
- exactly one evaluation mode is configured
- delta-mode benchmarks include `ref.diff`
- Dockerfile source archives are copied or added from `pkgs/`
- referenced `pkgs/` archives exist
- source archive names match final source `WORKDIR` directories
- `pkg_refs.txt` covers dependency archives
- harness paths use supported forms
- CPV directories match `meta.yaml` vulnerability entries
- `vuln.yaml`, POV blobs/logs, and patch files exist for each CPV
- patch exclusion patterns are syntactically valid

Benchmark CI may enforce additional repository hygiene checks, but those checks
must not contradict this specification.

## Anti-Contamination

Benchmarks may use restricted distribution, canaries, and protected archives to
reduce training-data contamination risk. These measures are best-effort only and
do not replace reproducible packaging or validation.