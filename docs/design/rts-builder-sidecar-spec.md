# RTS Builder-Sidecar Spec

- Audience: maintainers working on oss-crs builder-sidecar integration and RTS base images
- Scope: contract for RTS support in the oss-crs builder-sidecar for CRSBench benchmarks
- Related: [RTS Base Image Plan](./rts-base-image-plan.md), [oss-crs Integration](./evaluation/oss-crs-integration.md)

## Goals and Non-goals

### Goals

- Support RTS (Regression Test Selection) for CRSBench benchmarks via the oss-crs builder-sidecar
- Provide RTS base Docker images (`crsbench-rts-jvm`, `crsbench-rts-c`) with pre-installed RTS toolchains
- Dynamically swap benchmark Dockerfile `FROM` to RTS base images when RTS is enabled
- Pass `RTS_ON` and `RTS_TOOL` as runtime env vars (not baked into images)
- Bake RTS baseline state into oss-crs test snapshots for efficient subsequent runs

### Non-goals

- CRSBench patch verification RTS (handled separately, not in this spec)
- Modifying oss-crs core logic (`crs_compose.py`, `crs.py`, `env_policy.py`)
- Modifying benchmark `test.sh` scripts
- Introducing a separate `:inc` image tagging scheme (oss-crs uses `oss-crs-snapshot:test-{build_id}`)

## Constraints

- oss-crs core code is unchanged; only `oss-crs-infra/builder-sidecar/` receives minimal modifications
- Benchmark `test.sh` scripts are not modified; they already contain RTS hooks (`RTS_ON`, `RTS_TOOL` checks)
- `RTS_ON` and `RTS_TOOL` must be dynamic at runtime, not hardcoded in images
- RTS base images must be compatible with `gcr.io/oss-fuzz/base-builder-jvm` and `gcr.io/oss-fuzz/base-builder`

## Architecture Overview

```
CRSBench (RTS ON)
  1. Read project.yaml -> rts_mode, inc_build
  2. Swap Dockerfile FROM -> crsbench-rts-{language}:latest
  3. Build project image (RTS tools + /src/run_tests.sh wrapper in base)

oss-crs build-target --incremental-build
  Build snapshot: compile (unchanged)
  Test snapshot:
    oss-crs run_tests.sh -> finds /src/run_tests.sh (wrapper) -> execs it
    RTS_ON=1 in env -> wrapper runs rts_init_project.py -> execs test.sh
    Committed as oss-crs-snapshot:test-{build_id} (RTS baseline baked)

oss-crs run (patch verification via sidecar)
  get_test_image() -> test snapshot (has baked RTS state)
  run_ephemeral_test():
    RTS_ON=1, RTS_TOOL=<mode> in container env (from sidecar env)
    /usr/local/bin/run_tests.sh -> /src/run_tests.sh (wrapper)
    Sentinel exists -> skip init -> test.sh runs with RTS
```

## Component 1: RTS Base Docker Images

CRSBench maintains two base images, one per language family.

### JVM: `crsbench-rts-jvm:latest`

```dockerfile
FROM gcr.io/oss-fuzz/base-builder-jvm

# Project-independent RTS dependencies
# JcgEks, Ekstazi, OpenClover Maven artifacts pre-installed to local repo
COPY rts_artifacts/ /opt/rts_artifacts/
RUN install_jvm_rts_artifacts.sh

# Helper scripts at well-known absolute paths
COPY rts_init_project.py /rts_init_project.py
COPY rts_config_jvm.py /rts_config_jvm.py

# Wrapper: env-driven, not hardcoded
COPY run_tests_rts_wrapper.sh /src/run_tests.sh
RUN chmod +x /src/run_tests.sh
```

Contents:
- `/rts_init_project.py`: project-specific lazy init (pom.xml modification, idempotent)
- `/rts_config_jvm.py`: per-run RTS config generation (jcg_config/, java_modify.txt, etc.)
- `/src/run_tests.sh`: wrapper script (see Component 3)
- Maven local repository with RTS tool artifacts pre-installed

### C/C++: `crsbench-rts-c:latest`

```dockerfile
FROM gcr.io/oss-fuzz/base-builder

# Project-independent RTS dependencies
# Intel Pin tool
COPY pin/ /opt/pin/
# universal-ctags (pre-compiled)
COPY ctags /usr/local/bin/ctags
# BinaryRTS
COPY binary-rts/ /opt/binary-rts/

# Helper scripts
COPY rts_init_project.py /rts_init_project.py
COPY run_tests_rts_wrapper.sh /src/run_tests.sh
RUN chmod +x /src/run_tests.sh
```

Contents:
- `/opt/pin/`: Intel Pin tool (pre-built pintools-rts)
- `/opt/binary-rts/`: BinaryRTS CLI
- `/usr/local/bin/ctags`: universal-ctags with macrodef support
- `/rts_init_project.py`: project-specific lazy init (BinaryRTS baseline trace)
- `/src/run_tests.sh`: wrapper script

## Component 2: Dockerfile FROM Dynamic Swap

CRSBench reads `project.yaml` for each benchmark. When `inc_build: true` and `rts_mode` is a non-`none` value, CRSBench replaces the `FROM` line in the benchmark Dockerfile at build time.

### FROM mapping

| Language | Original FROM | RTS FROM |
|----------|--------------|----------|
| jvm | `gcr.io/oss-fuzz/base-builder-jvm` | `crsbench-rts-jvm:latest` |
| c, c++ | `gcr.io/oss-fuzz/base-builder` | `crsbench-rts-c:latest` |

### Mechanism

CRSBench generates a modified Dockerfile in a temporary location before invoking oss-crs `build-target`. The modification is a single-line replacement of the first `FROM` instruction.

## Component 3: `/src/run_tests.sh` Wrapper

Baked into the RTS base image at `/src/run_tests.sh`. The oss-crs `run_tests.sh` (mounted at `/usr/local/bin/run_tests.sh`) discovers `/src/run_tests.sh` and execs it.

```bash
#!/bin/bash
set -euo pipefail

# RTS_ON and RTS_TOOL are provided via container env at runtime
if [ -n "${RTS_ON:-}" ]; then
    # Lazy project-specific init (idempotent, guarded by sentinel)
    if [ ! -f /rts_project_initialized ]; then
        echo "[RTS] Running project init with tool: ${RTS_TOOL}..."
        python3 /rts_init_project.py "$(pwd)" --tool "${RTS_TOOL}"
        touch /rts_project_initialized
    fi
fi

exec bash /src/test.sh
```

Behavior:
- `RTS_ON` not set: wrapper is a transparent pass-through to `test.sh`
- `RTS_ON=1`: runs `rts_init_project.py` once (sentinel guard), then execs `test.sh`
- `test.sh` already contains `if [ -n "${RTS_ON}" ]` checks for `rts_config_jvm.py` and BinaryRTS

## Component 4: `docker_ops.py` Minimal Modification

File: `oss-crs/oss-crs-infra/builder-sidecar/docker_ops.py`

Change `_oss_fuzz_env()` to also pass RTS env vars from the sidecar environment to ephemeral test containers:

```python
_OSS_FUZZ_ENV_KEYS = ("FUZZING_ENGINE", "SANITIZER", "ARCHITECTURE", "FUZZING_LANGUAGE")
_RTS_ENV_KEYS = ("RTS_ON", "RTS_TOOL")

def _oss_fuzz_env() -> dict[str, str]:
    keys = _OSS_FUZZ_ENV_KEYS + _RTS_ENV_KEYS
    return {k: v for k in keys if (v := os.environ.get(k)) is not None}
```

This is the only oss-crs code change. It is in `oss-crs-infra/builder-sidecar/` (infra code), not in core oss-crs logic.

## Component 5: Compose Template Update

File: `oss-crs/oss_crs/src/templates/run-crs-compose.docker-compose.yaml.j2`

Add RTS env vars to the builder-sidecar service environment:

```yaml
oss-crs-builder-sidecar:
  environment:
    # existing vars...
    - RTS_ON={{ target_env.rts_on | default('') }}
    - RTS_TOOL={{ target_env.rts_tool | default('') }}
```

When `target_env` does not contain `rts_on`/`rts_tool`, the template must omit the keys entirely (not set empty strings), because `_oss_fuzz_env()` uses `(v := os.environ.get(k)) is not None` and an empty string passes this check.

Template conditional:

```yaml
{%- if target_env.rts_on is defined and target_env.rts_on %}
    - RTS_ON={{ target_env.rts_on }}
    - RTS_TOOL={{ target_env.rts_tool }}
{%- endif %}
```

### How `target_env` gets `rts_on`/`rts_tool`

CRSBench reads `project.yaml` (`rts_mode`, `inc_build`) and injects `rts_on`/`rts_tool` into the target configuration before invoking oss-crs. The exact injection point depends on how CRSBench prepares the oss-crs target:

- If CRSBench controls `target.get_target_env()` output: add `rts_on` and `rts_tool` keys
- If CRSBench stages a project-level config that oss-crs reads: add the keys to that config

This is CRSBench-owned logic and does not require oss-crs core changes.

## Component 6: Test Snapshot RTS Baking

### Snapshot creation path

`oss-crs` `_create_incremental_snapshots()` creates the test snapshot with `extra_env=oss_fuzz_env`. The `oss_fuzz_env` is:

```python
oss_fuzz_env = {k: target_env[v] for k, v in OSS_FUZZ_TARGET_ENV.items()}
oss_fuzz_env["SANITIZER"] = sanitizer
```

For `RTS_ON` and `RTS_TOOL` to flow into the test snapshot, they must be in `oss_fuzz_env`. This requires adding them to `OSS_FUZZ_TARGET_ENV` in `env_policy.py`:

```python
OSS_FUZZ_TARGET_ENV = {
    "FUZZING_ENGINE": "engine",
    "SANITIZER": "sanitizer",
    "ARCHITECTURE": "architecture",
    "FUZZING_LANGUAGE": "language",
    "RTS_ON": "rts_on",
    "RTS_TOOL": "rts_tool",
}
```

Alternatively, `_create_incremental_snapshots()` can be modified to merge RTS env into `oss_fuzz_env` before passing to `_snapshot_one()`.

### Snapshot content

When `RTS_ON=1` during test snapshot creation:
1. oss-crs `run_tests.sh` -> `/src/run_tests.sh` (wrapper)
2. Wrapper: `RTS_ON` set -> `rts_init_project.py` runs -> sentinel created
3. `test.sh` runs with RTS (baseline pass)
4. Container committed as `oss-crs-snapshot:test-{build_id}`

The snapshot contains:
- JVM: modified pom.xml, RTS tool configuration, baseline RTS state (e.g., `.jcg/`, `.ekstazi/`)
- C/C++: BinaryRTS baseline traces, function-level dependency maps

### Subsequent runs from snapshot

Sentinel `/rts_project_initialized` exists in the snapshot. Wrapper skips init, `test.sh` runs with RTS selection using baked baseline state.

## Decisions and Tradeoffs

### Decision: RTS env vars are runtime, not baked

`RTS_ON` and `RTS_TOOL` are passed via env at container creation time. The same snapshot can be used with or without RTS (though a snapshot created without RTS will need init on first RTS run in an ephemeral container).

### Decision: minimal oss-crs-infra changes

Only `docker_ops.py` (env pass-through) and the compose template (env var wiring) are modified. No core logic changes.

### Decision: `env_policy.py` modification for snapshot creation

`_create_incremental_snapshots()` builds `oss_fuzz_env` from `OSS_FUZZ_TARGET_ENV`. For RTS env to flow into test snapshots, one of:

1. **Add to `OSS_FUZZ_TARGET_ENV`** in `env_policy.py` (core change, consistent with existing pattern)
2. **Merge separately** in `_create_incremental_snapshots()` (isolated, but ad-hoc)

Option 1 is preferred for consistency. This is the only oss-crs core change besides the builder-sidecar infra modifications.

### Tradeoff: C/C++ snapshot bake time

BinaryRTS baseline trace generation during test snapshot creation adds significant time. This is acceptable because the trace is needed for correct RTS selection and is amortized over all subsequent runs.

## Risks and Validation

### Risks

- Dockerfile FROM swap may fail if benchmarks use non-standard base images
- `rts_init_project.py` may fail on some projects (pom.xml structure, BinaryRTS compatibility)
- Empty `RTS_ON=""` vs unset `RTS_ON` distinction in shell and Python

### Required validation

- JVM: snapshot baking creates image with `/rts_config_jvm.py`, modified pom.xml, baseline state
- C/C++: snapshot baking creates image with BinaryRTS tools and baseline traces
- `RTS_ON=1` + `RTS_TOOL=<mode>` correctly flow: compose -> sidecar -> ephemeral container
- `RTS_ON` unset: wrapper is transparent pass-through, no RTS init
- Test snapshot with baked RTS state: subsequent runs skip init and use baseline

## Implementation Pointers

- `oss-crs/oss-crs-infra/builder-sidecar/docker_ops.py` — env pass-through
- `oss-crs/oss_crs/src/templates/run-crs-compose.docker-compose.yaml.j2` — compose template
- `oss-crs/oss_crs/src/env_policy.py` — OSS_FUZZ_TARGET_ENV (if adding RTS keys here)
- `oss-crs/oss_crs/src/crs_compose.py:297-373` — snapshot creation with oss_fuzz_env
- `oss-crs-old/bug_fixing/base_images/oss_patch_runner/rts_init_jvm.py` — reference JVM init
- `oss-crs-old/bug_fixing/base_images/oss_patch_runner/rts_config_jvm.py` — reference JVM config
- `oss-crs-old/bug_fixing/base_images/oss_patch_runner/rts_init_c.py` — reference C init
- CRSBench benchmark Dockerfile swap logic — new code in CRSBench builder
