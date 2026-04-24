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
- dispatcher routing replaces the shared startup pre-build fanout with an
  evaluator-local advisory warmup feeder that only tops up spare local build
  capacity
- async POV verification enqueues a benchmark-local build DAG on first POV
  discovery and each verify job depends on those build jobs before execution

## Verification Payload Contract

Verification jobs must carry enough benchmark/trial/harness/POV context for an
evaluator on another machine to execute verification without assuming a shared
trial-working directory on the worker.

Embedded or staged payload data must be sufficient for non-local verification.

## Queue Semantics

Distributed evaluation distinguishes trial queues from verify/build queues.
Workers may enqueue verification work while trials continue, and evaluators may
process those jobs independently.

Async POV verification now has two runtime realizations:

- shared routing: workers enqueue physical build and verify RQ jobs directly
  onto shared evaluator queues
- dispatcher routing: workers submit logical build/verify requests into
  experiment-scoped Redis state, one dispatcher leader applies global owner
  fairness over those ready requests, and the dispatcher then enqueues physical
  attempts onto evaluator-local build/verify queues
- dispatcher warmup is separate from that logical request flow: evaluators may
  enqueue local cache-priming build jobs before the first blocked verify demand,
  but once any blocked verify still has unmet build prerequisites the evaluator
  stops issuing new warmup jobs until that required build demand clears
- already queued or running warmup jobs are not canceled when required build
  demand appears; only new warmup dispatch is suppressed

Dispatcher routing preserves the worker-facing contract around verdict payloads,
but workers poll stable logical `request_id` values rather than depending on
physical evaluator RQ job IDs. Physical `attempt_id` values are internal to the
dispatcher/evaluator contract and may change across retries or evaluator-death
recovery.

Async POV verification must preserve build/verify queue separation:

- build prerequisites are enqueued to the build queue
- verify jobs declare explicit queue dependencies on those build jobs
- verify workers only hydrate prebuilt artifacts from evaluator disk for
  dependency-backed POV jobs; they must not hide fresh variant builds inside the
  verify worker
- scheduler fairness applies only to runnable queued work; build-before-verify
  correctness remains a dependency contract rather than a blanket build-priority
  rule
- dispatcher warmup never satisfies correctness by itself; workers still wait on
  logical dispatcher build request completion before a verify request becomes
  runnable

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
- dispatcher-routed stale attempts must be fenced from publishing terminal
  logical results after lineage reassignment or newer attempt issuance
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
- dispatcher logical-request and evaluator-locality tests
- registry-driven configless evaluator tests
- CI-compatibility queue tests

## Implementation Pointers

- distributed evaluator modules under `crsbench/distributed/`
- distributed evaluation and CI-related tests under `tests/`
