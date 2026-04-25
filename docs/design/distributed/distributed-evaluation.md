# Design: Distributed Evaluation
- Audience: maintainers working on queue-backed CRS execution and evaluator behavior
- Scope: distributed evaluation topology, evaluator contracts, verification payload semantics, and result handling
- Related: [Distributed Job Queue](./distributed-job-queue.md), [Unified Build & Verify](./unified-build-verify.md), [Trial-Fair Evaluator Scheduling](./evaluator-trial-fair-scheduling.md)

## Goals and Non-goals

### Goals
- define the evaluator's role in distributed CRSBench execution
- define verification payload, queue, and result semantics
- define non-local behavior across orchestrator, worker, and evaluator processes

### Non-goals
- replacing worker-based CRS trial execution
- runnable deployment walkthroughs
- copied implementation of supervisor/job functions

## Topology Contract

Distributed evaluation extends the queue-backed model with a dedicated evaluator
process family. The roles are:
- orchestrator: submits and monitors experiment work
- worker: runs CRS trials and discovers verification candidates
- evaluator: builds variants and executes verification workloads

## Evaluator Runtime Modes

The evaluator may operate in:
- config-pinned mode for one experiment
- configless mode for registry-discovered experiments
- CI compatibility mode for legacy CI build/verify queues

These are queue-selection/runtime-discovery differences. Verification verdict
payloads stay stable across modes, but async POV verification may use different
execution plumbing under the shared-queue and dispatcher-routed models.

Startup behavior differs between the first two modes:

- focused worker/evaluator CLI modes default to dispatcher routing when
  `CRSBENCH_EVALUATOR_ROUTING_MODEL` is unset
- shared routing remains available as an explicit override via
  `CRSBENCH_EVALUATOR_ROUTING_MODEL=shared`; in that mode, config-pinned
  evaluator CLI startup performs the legacy shared pre-build enqueue phase
- configless mode skips startup pre-build enqueue and relies on lazy build-queue
  consumption
- dispatcher routing (`CRSBENCH_EVALUATOR_ROUTING_MODEL=dispatcher`) is
  currently supported only in config-pinned evaluator mode
- dispatcher routing now starts one evaluator-local claim loop plus one
  evaluator-local advisory warmup feeder over evaluator-local build and verify
  queues
- the evaluator-local claim loop may materialize multiple logical verify
  requests per poll; intake is capped by local evaluator execution width rather
  than a fixed one-request-per-evaluator limit
- workers submit logical verify requests only; evaluators derive the necessary
  local build DAG after claim rather than consuming a global logical build queue
- async POV verification still uses explicit build prerequisites, but the
  evaluator localizes those build job IDs to its own queue namespace before
  enqueue and attaches local `depends_on` only for unfinished local builds
- async patch verification uses the same logical claim flow: once an evaluator
  claims the request it enqueues one local patch build job and one local verify
  wrapper that depends on that local build
- warmup uses the same evaluator-local build namespace as claimed work and
  pauses whenever that evaluator still has required local builds pending

## Verification Payload Contract

Verification jobs must carry enough benchmark/trial/harness/target context for
an evaluator on another machine to execute verification without assuming a
shared trial-working directory on the worker.

Embedded or staged payload data must be sufficient for non-local verification.

## Queue Semantics

Distributed evaluation distinguishes trial queues from verify/build queues.
Workers may enqueue verification work while trials continue, and evaluators may
process those jobs independently.

Async distributed verification now has two runtime realizations for both POV
and patch validation:

- shared routing: workers enqueue physical build and verify RQ jobs directly
  onto shared evaluator queues
- dispatcher routing: workers submit logical verify requests into an
  experiment-scoped Redis claim store, evaluators claim those requests with
  owner-fair rotation, and each evaluator materializes one evaluator-local
  build/verify DAG after claim
- there is no global logical build queue, no dispatcher leader lease, and no
  lineage-owner placement state in the runtime path; build locality is now
  purely evaluator-local
- the worker-visible handle remains the stable logical `request_id`; physical
  evaluator-local RQ job IDs are internal and may include evaluator-specific
  suffixes so different evaluators can rebuild the same variant independently
- evaluator-local build IDs must preserve the variant name in the third
  slash-delimited segment so local disk-context hydration can still reconstruct
  build results for downstream verify jobs
- claimed POV verify requests rebuild or reuse local benchmark variants on the
  claiming evaluator and then enqueue one local verify wrapper that publishes
  the terminal verdict by logical `request_id`
- claimed patch verify requests enqueue one local patch build plus one local
  verify wrapper that carries the localized patch-build job ID into patch
  verification
- dispatcher warmup is evaluator-local and advisory: evaluators may enqueue
  cache-priming local build jobs before the first required local build demand,
  but once any claimed request on that evaluator still has unmet local build
  prerequisites the evaluator stops issuing new warmup jobs until that required
  demand clears
- warmup honors `inc_build_enabled`: when enabled it plans incremental
  build variants, and when disabled it still prebuilds benchmark variants for
  configured experiment benchmarks but does so via clean/full builds instead of
  preparing incremental images
- already queued or running warmup jobs are not canceled when required build
  demand appears; only new warmup dispatch is suppressed

Dispatcher routing preserves the worker-facing contract around verdict payloads,
but workers poll stable logical `request_id` values rather than depending on
physical evaluator RQ job IDs. Physical local RQ job IDs are internal to the
evaluator runtime and may change across reclaim after evaluator death.

Async POV and patch verification must preserve build/verify queue separation:

- build prerequisites are enqueued to the build queue
- shared routing verify jobs declare explicit RQ dependencies on those build jobs
- dispatcher routing verify requests do not become physical evaluator jobs until
  an evaluator has claimed them and materialized the local DAG
- dispatcher routing patch and POV verify both become evaluator-local verify
  wrappers that depend on unfinished local build prerequisites only
- if a claim expires or the evaluator dies, another evaluator may reclaim the
  logical request and rebuild the same variants locally; no shared build-result
  row needs to be advanced first
- if an evaluator claims a logical request but fails before it can fully
  materialize the evaluator-local DAG, it releases that claim immediately
  instead of burning the full lease window; materialization failure must not
  stall owner-fair intake behind an idle lease
- scheduler fairness applies only to runnable queued work; build-before-verify
  correctness remains a dependency contract rather than a blanket build-priority
  rule
- dispatcher warmup never satisfies correctness by itself; only claimed local
  build completion makes a claimed local verify wrapper runnable

Build jobs that rely on incremental benchmark images must carry the resolved
inc-image runtime settings needed by the evaluator worker (`inc_image_policy`,
registry, pull limits, timeout, and local image prefix). Remote evaluators must
not silently fall back to unrelated local defaults when the originating
experiment resolved non-default image settings.

Current incremental-image prepare order is:

- local image first
- then remote pull if policy allows
- then local image build fallback if policy allows

If `inc_image_max_pull_bytes` is set, evaluators try to read the remote image
size before pulling and skip pulls whose known remote size exceeds the cap.
`inc_image_pull_timeout_sec` is propagated to the `docker pull` timeout.

## Result Contract

Verification results returned by evaluators must be attributable to:
- the originating trial
- the benchmark and harness
- the verification candidate(s)
- the resulting verdict/error classification

Workers and orchestrators may poll or aggregate those results later, but the
stored result contract must remain stable. In dispatcher mode, the authoritative
result identity is the logical `request_id`; physical evaluator attempt IDs are
not part of the worker-visible correctness contract.

## Failure Semantics

- evaluator-side build failures are distinct from worker-side trial failures
- stale/missing queue metadata must surface as infrastructure failures
- non-local verification must not assume shared filesystem state unless that
  state is explicitly part of the deployment contract
- delayed polling/early-stop is acceptable; silent result loss is not
- if an async verification drain times out after the worker has already
  preserved trial artifacts, the worker may still publish `.success` for the
  trial, but it must also persist `.verification-undrained.json` at the trial
  root with the verification kind (`pov` or `patch`) and missing-result counts;
  CI/smoke validation must treat that marker as a hard failure and operators may
  recover later via `crsbench re-eval`
- dispatcher-routed stale local verify wrappers must be fenced from publishing
  terminal logical results after claim expiry or reclaim by another evaluator
- the async final drain budget is `runtime.verify_timeout`; it covers both
  queued build prerequisites and queued POV verification work

## Distributed Constraints

Configless multi-experiment evaluation requires compatible shared runtime
assumptions, including queue discovery, benchmark roots, and selected evaluator
resource/runtime settings. Incompatible experiments must not be merged into one
ambiguous evaluator runtime silently.

## Validation

This contract should be covered by:
- distributed evaluator tests
- verify-payload/result serialization tests
- verify-claim store, claim-worker, and evaluator-local warmup tests
- registry-driven configless evaluator tests
- CI-compatibility queue tests

## Implementation Pointers

- distributed evaluator modules under `crsbench/distributed/`
- distributed evaluation and CI-related tests under `tests/`
