# RTS Base Image Plan
- Audience: maintainers working on benchmark packaging, incremental images, and patch verification
- Scope: contract and rollout plan for restoring RTS in CRSBench without modifying `oss-crs`
- Related: [Evaluation](./evaluation/evaluation.md), [Patch Verification](./evaluation/patch-verification.md), [Snapshots](./evaluation/snapshots.md), [oss-crs Integration](./evaluation/oss-crs-integration.md)

## Goals and Non-goals

### Goals
- restore RTS (Regression Test Selection) for CRSBench-managed patch verification
- keep `oss-crs/` unchanged
- move RTS-specific image selection and snapshot preparation into CRSBench-owned paths
- make local and distributed workers use the same RTS image and runtime contracts

### Non-goals
- reintroduce the old `oss-crs-old` implementation verbatim
- require benchmark-by-benchmark permanent Dockerfile rewrites when a centralized CRSBench overlay can enforce the same contract
- define a new public `oss-crs` interface

## Constraints

- `oss-crs` builder-sidecar already chooses its runtime base image from `BASE_IMAGE`, `BASE_IMAGE_{BUILDER}`, and `PROJECT_BASE_IMAGE`; the plan must reuse that contract rather than extend it.
- RTS runs in CRSBench already depend on incremental images; RTS is not a separate build mode.
- Benchmark behavior must remain correct on remote workers that do not share local Docker cache state.
- `docs/design/` must describe durable contracts, not checklist-style implementation history.

## Old System Summary

`oss-crs-old` restored RTS by doing more than swapping a base image. Its snapshot flow:

1. started from a language-specific base image
2. mounted RTS helper scripts into the build container
3. ran RTS initialization during snapshot creation
4. ran `test.sh` once before committing the snapshot so RTS baseline state was baked into the resulting incremental image
5. reused that committed `:inc` image for later patch validation runs

The old helper set was:

- JVM: `rts_init_jvm.py`, `rts_config_jvm.py`, `extensions.xml`
- C/C++: `rts_init_c.py`

The important architectural point is that RTS state was created during snapshot baking, not lazily during the final patch-verification test run.

## Current State Findings

### Benchmarks already contain most RTS hooks

Many CRSBench benchmarks already declare RTS and already branch in `test.sh`:

- JVM benchmarks commonly declare `rts_mode: jcgeks` or `rts_mode: openclover`
- C/C++ benchmarks commonly declare `rts_mode: binaryrts`
- JVM `test.sh` scripts typically execute `python3 /rts_config_jvm.py ...` when `RTS_ON` is set
- C/C++ `test.sh` scripts typically expect BinaryRTS assets under `/opt/pin` and `/opt/binary-rts`

This means the main missing behavior is not broad benchmark authoring; it is the CRSBench-controlled image and runtime path around those benchmarks.

### Current CRSBench gaps

The present CRSBench path does not satisfy the benchmark RTS contract in several places:

- `run_tests()` exports `RTS_MODE=1`, but benchmark scripts check `RTS_ON` and `RTS_TOOL`
- the snapshot bake template performs compile-only baking and does not run RTS initialization or the baseline `test.sh` pass required to generate RTS state
- JVM benchmark scripts call `/rts_config_jvm.py` as an absolute path, but current bake logic only knows how to copy `rts_config_jvm.py` into `/src/`
- many C/C++ RTS scripts require `/tmp/patch.diff`, but the current test container path does not provide it

Because of these gaps, a new RTS base image by itself is insufficient.

## Contract

### Benchmark RTS eligibility

A benchmark is RTS-eligible only when all of the following are true:

- `project.yaml` declares `inc_build: true`
- `project.yaml` declares `rts_mode` with a non-`none` value
- the benchmark provides a `test.sh` or equivalent runtime path that honors the declared RTS mode
- CRSBench can build or pull an incremental image that contains the RTS prerequisites for that mode

If any prerequisite is missing, CRSBench must treat RTS as unavailable rather than silently claiming support.

### RTS image contract

CRSBench-managed incremental images used for RTS must satisfy these language-level contracts.

For JVM:

- `/rts_config_jvm.py` exists in the image filesystem
- the selected RTS tool dependencies are installed
- project `pom.xml` files have already been prepared for the selected tool during snapshot creation
- baseline RTS state created by the first test pass is baked into the image

For C/C++:

- BinaryRTS toolchain assets exist in the image, including `/opt/pin` and `/opt/binary-rts`
- any trace, lookup, or baseline artifacts produced by the initial test pass are baked into the image
- the image is suitable for replaying a patch-specific RTS selection pass with `/tmp/patch.diff`

### Runtime contract

When CRSBench executes RTS verification against an incremental image:

- it must set `RTS_ON=1`
- it must set `RTS_TOOL=<project.yaml rts_mode>`
- it must use the incremental image path, not the clean `:latest` path
- for C/C++ BinaryRTS, it must materialize the patch diff at `/tmp/patch.diff`

For JVM, the patched worktree is sufficient for `git diff HEAD`-based RTS configuration as long as the image already contains the RTS helper and initialized Maven/tool configuration.

### Ownership contract

RTS-specific image choice, helper injection, and snapshot preparation are owned by CRSBench. `oss-crs` remains unchanged and consumes whatever project image CRSBench stages or tags under the existing image-selection interface.

## Runtime Behavior

### Happy path

1. CRSBench resolves benchmark language and `rts_mode`
2. CRSBench stages the benchmark or build context with the appropriate RTS image overlay
3. CRSBench builds or prepares the incremental image
4. snapshot baking performs RTS initialization and a baseline `test.sh` pass
5. CRSBench tags the resulting image as the benchmark's `:inc` image
6. later patch verification retags that `:inc` image per variant and executes tests with `RTS_ON=1` and `RTS_TOOL=<mode>`

### Failure behavior

- if RTS prerequisites cannot be baked into the incremental image, CRSBench must mark RTS unavailable and fall back only to non-RTS verification modes
- if the incremental image exists but does not satisfy the runtime RTS contract, CRSBench must fail the RTS path explicitly rather than reporting a misleading skip or pass
- if distributed workers cannot pull the RTS-enabled image, RTS support is considered unavailable for those workers

## Deployment and Distributed Behavior

The RTS image contract must hold on remote workers, not just on the machine that built the image.

- registry-published incremental images must already contain the RTS baseline state
- worker-local staging must apply the same RTS overlay rules as local execution
- capability probing should eventually confirm not only that an incremental image exists, but that RTS runtime prerequisites are present for the declared mode

This keeps local, queue-based, and cloud workers on the same semantics.

## Preferred Solution Strategy

The preferred design is to keep benchmark sources mostly unchanged and centralize RTS restoration inside CRSBench-owned staging and image-preparation paths.

### Why centralized overlay is preferred

- many benchmarks already contain the necessary `test.sh` RTS branches
- editing dozens of benchmark Dockerfiles creates avoidable drift and maintenance cost
- CRSBench already has central staging hooks for both `oss-crs` execution and OSS-Fuzz helper flows
- centralized overlay keeps local and distributed execution on one implementation path

### Central hook points

RTS restoration should be wired into CRSBench-owned paths that already stage or bake benchmark images:

- benchmark staging for `oss-crs` execution
- benchmark staging into `oss-fuzz/projects/...`
- incremental image creation and snapshot baking
- test container launch for patch verification

The required behavior is:

- choose an RTS-enabled base image or equivalent staged overlay from CRSBench
- inject RTS helper files at the filesystem locations expected by benchmark scripts
- run RTS initialization and baseline tests during snapshot baking
- pass `RTS_ON`, `RTS_TOOL`, and `/tmp/patch.diff` at test time where required

## Decisions and Tradeoffs

### Decision: do not rely on benchmark mass rewrites

Benchmark Dockerfile rewrites are allowed only when a benchmark deviates from the common contract and cannot be handled centrally. The default path is CRSBench-managed overlay and bake logic.

### Decision: snapshot bake is part of RTS, not an optimization detail

The old system depended on snapshot-time initialization and baseline generation. CRSBench must treat that as part of RTS correctness rather than an optional speed optimization.

### Tradeoff: larger images

RTS-enabled images, especially BinaryRTS images, will be larger and slower to build and distribute. This is acceptable because correctness of RTS selection depends on tooling and baseline artifacts being present in the image.

## Risks and Validation

### Primary risks

- false RTS support reporting when an incremental image exists but lacks helper files or baseline state
- drift between benchmark script expectations and CRSBench runtime env vars
- distributed workers pulling a non-RTS or partially baked image

### Required validation

Validation should cover:

- JVM snapshot baking creates an image containing `/rts_config_jvm.py` and RTS-prepared project state
- C/C++ snapshot baking creates an image containing BinaryRTS tooling and baseline trace artifacts
- RTS patch verification passes `RTS_ON` and `RTS_TOOL` correctly
- BinaryRTS test runs receive `/tmp/patch.diff`
- at least one remote-worker or registry-backed path confirms the same RTS image works outside the local builder host

## Immediate Doc Corrections From This Analysis

The previous version of this document assumed:

- C/C++ benchmarks broadly lacked RTS hooks
- base-image replacement alone was enough to restore RTS
- benchmark Dockerfiles were the primary rollout lever

Those assumptions are incorrect for the current repository state. The main work is restoring the CRSBench-owned snapshot and runtime contracts around benchmarks that already declare RTS behavior.

## Implementation Pointers

- [crsbench/builder/infrastructure.py](/home/acorn421/work/crsbench/CRSBench-sidecar/crsbench/builder/infrastructure.py)
- [crsbench/builder/templates/bake_snapshot.sh.tmpl](/home/acorn421/work/crsbench/CRSBench-sidecar/crsbench/builder/templates/bake_snapshot.sh.tmpl)
- [crsbench/evaluation/adapter/oss_crs.py](/home/acorn421/work/crsbench/CRSBench-sidecar/crsbench/evaluation/adapter/oss_crs.py)
- [crsbench/utils/run_helper.py](/home/acorn421/work/crsbench/CRSBench-sidecar/crsbench/utils/run_helper.py)
- [../oss-crs-old/bug_fixing/src/oss_patch/project_builder/__init__.py](/home/acorn421/work/crsbench/CRSBench-sidecar/../oss-crs-old/bug_fixing/src/oss_patch/project_builder/__init__.py)
