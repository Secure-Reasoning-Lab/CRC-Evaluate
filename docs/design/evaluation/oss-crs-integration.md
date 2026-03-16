# Design: oss-crs Integration
- Audience: maintainers working on CRSBench orchestration of `oss-crs`
- Scope: integration boundaries, parameter contracts, staged inputs, and failure semantics
- Related: [Evaluation](./evaluation.md), [Path Resolver](./path-resolver.md), [Trial Directory Preparation](./trial-directory-preparation.md)

## Goals and Non-goals

### Goals
- define how CRSBench supplies benchmark/trial context to `oss-crs`
- define which inputs are staged by CRSBench versus consumed directly by `oss-crs`
- define integration failure semantics across build and run phases

### Non-goals
- reproducing `oss-crs` CLI documentation
- runnable command tutorials
- copied code from adapters or repository managers

## Integration Boundary

CRSBench is responsible for preparing benchmark- and trial-specific context,
then invoking `oss-crs` with that prepared state. `oss-crs` is responsible for
executing CRS-specific build/run behavior within its own contract.

## Inputs Provided by CRSBench

Depending on workflow, CRSBench may provide:
- benchmark/project path information
- prepared source context
- prepared trial/build/output directories
- resolved harness-source paths
- staged hints or POV inputs
- registry/config references for CRS selection

## Coverage-Specific Contract

Coverage analysis uses `oss-crs` as a build contract, not as the runtime
executor. The coverage path is:

1. Atlantis GHCR `1.0.0` prepare/runtime images, retagged onto the canonical
   local Atlantis image names, or, if unavailable, Atlantis `oss-crs prepare`
   against the repository-local Team Atlanta checkout
2. Atlantis `oss-crs build-target` for the selected benchmark
3. CRSBench-managed warm libCRS/UniAFL coverage sessions for per-seed replay

CRSBench invokes the Team Atlanta Atlantis prepare flow as-is. It must not
mutate Atlantis prepare sources as part of the prepare phase.

When the canonical Atlantis prepare image set is already available locally and
the recorded Atlantis prepare state still matches the current checkout,
coverage builds may skip the Atlantis `prepare` phase and proceed directly to
`build-target`. The runtime contract is still the same Atlantis image set; the
skip only removes redundant image preparation work when the checkout/image
contract remains valid.

Coverage replay runs one warm container per `(benchmark, harness, shard)`. Each
container is pinned to a single CPU, and seeds assigned to that shard are run
sequentially in a fresh libCRS coverage state inside the warm process.
Each session uses its own copied benchmark tree and exported `/out` tree; warm
coverage workers must not share a writable benchmark mount because Atlantis
prepare may generate `.aixcc/config.yaml` inside `/src`, and parallel shard
startup must not race on that file.

Coverage reporting is derived directly from the per-seed replay artifacts that
those warm containers emit. CRSBench must not depend on a separate whole-corpus
summary pass for Atlantis coverage analysis. The timeline y-axis is the
cumulative count of unique covered lines obtained by merging per-seed `.cov`
payloads. Timeline report artifacts are seed-driven: the JSON/CSV outputs store
one row per normalized seed rather than bucketed time windows, and the PNG is
plotted directly from those per-seed rows.

Timeline origin depends on the coverage entry point:
- direct `--seed-dir` analysis uses the first retained seed `mtime` as time
  origin and computes each seed's `relative_time` from that filesystem time
- `--experiment-dir` and `--experiment-config` use
  `pov_store.json.crs_run_start_time` as time origin, preserve POV marker times
  in that same origin, clamp negative seed offsets to `0.0` rather than
  dropping those seeds, and clamp the PNG x-axis to `metadata.json.run_time`

Experiment-backed coverage must fail if either `crs_run_start_time` or
`run_time` is missing. Direct `--seed-dir` mode is the only timeline mode that
uses first-seed `mtime` fallback semantics.

Without a separate denominator pass, total-line percentages are out of
contract for this analysis mode and must remain unknown rather than inferred
from only the subset of source files touched by replay.

The normalized coverage build output must materialize executable runtime
artifacts as real files inside the exported build directory. Host-only symlinks
to Atlantis work directories are out of contract for runtime-mounted `/out`
paths because remote or containerized workers must be able to execute the
exported harness binaries without additional host path context.

If Atlantis emits an empty `.aixcc/config.yaml` during coverage worker startup
but the mounted `/out` directory already contains valid harness binaries,
CRSBench must fall back to binary-derived harness discovery rather than failing
the warm coverage session.

### Prepared Image Publication

The Atlantis prepare phase is complete only after both of the following hold:
- the canonical Atlantis prepare image set exists locally
- CRSBench writes the prepare sentinel for the selected Atlantis checkout

After that point, operators may publish the prepared image set to a remote
registry without rebuilding it. Publication is owned by the Atlantis
repository; CRSBench only consumes the canonical Team Atlanta image contract.

## Invariants

- the prepared trial/build context must correspond to the selected benchmark,
  harness, mode, and sanitizer contract
- staged source and input artifacts must be reproducible from benchmark metadata
- integration failures must remain attributable to either CRSBench staging or
  downstream `oss-crs` execution
- coverage builds must come from the Atlantis/AIxCC `oss-crs` lineage used by
  the warm runtime; mixed `oss-fuzz` lineages are out of contract
- coverage shards must not share a CPU or a warm runner instance

## Failure Semantics

- missing prepared inputs are CRSBench-side integration failures
- invalid CRS registry/config references are integration failures
- downstream `oss-crs` build/run failures must surface distinctly from staging
  failures
- partial outputs must not be mistaken for successful end-to-end trial execution

## Distributed Considerations

In distributed execution, workers and evaluators may consume previously staged or
cached artifacts. The integration contract must not assume a single long-lived
local process with in-memory state.

Coverage-specific distributed implications:

- the normalized Atlantis build output must be sufficient for a different worker
  to start the warm coverage runtime without rerunning `oss-crs build-target`
- shard allocation is local-worker state only; distributed workers must derive
  CPU pinning from their own available CPU set
- post-trial coverage artifacts are trial-level outputs and must remain valid
  even when no snapshot `coverage.json` exists

## Validation

This contract should be covered by:
- adapter integration tests
- staged-input preparation tests
- distributed execution tests that exercise `oss-crs` handoff paths

## Implementation Pointers

- `crsbench/evaluation/adapter/`
- `crsbench/evaluation/`
- integration-focused tests under `tests/`
