# Cloud Distributed Re-Evaluation

- Audience: contributors changing cloud re-eval submission, remote bundle staging, or orchestrator/evaluator responsibilities for non-local verification
- Scope: provider-neutral contract for running `crsbench re-eval` against uploaded trial artifacts on a cloud orchestrator plus evaluator fleet
- Related:
  - [Cloud Orchestration](./cloud-orchestration.md)
  - [GCE Cloud Orchestration](./gce-cloud-orchestration.md)
  - [GCE Cloud Orchestrator Launch](./gce-cloud-orchestrator.md)
  - [Distributed Evaluation](./distributed-evaluation.md)
  - [Multi-Machine Deployment Contract](./deployment-guide.md)

## Goals and Non-goals

Goals:

- support one operator command that submits an existing local experiment tree for remote distributed re-evaluation
- keep authoritative re-eval results on the cloud orchestrator before any later collection back to the operator machine
- reuse the existing distributed `crsbench re-eval` queue, evaluator, and verdict contracts for both POV and patch verification
- allow source experiments that originally ran with `skip_verification: true` to repopulate verification results later from preserved trial artifacts
- avoid uploading benchmark source trees or inventing a second evaluator protocol
- make remote staging, publication, retry, and collection semantics explicit

Non-goals:

- re-running CRS trial execution in the cloud
- uploading full benchmark checkouts, builder images, or other environment state already owned by the cloud VM bootstrap
- silently merging collected re-eval results back into the source experiment tree
- defining provider-specific SSH, rsync, or VM startup details in this document
- per-trial partial resume inside one submitted bundle in v1
- reusing a previously populated remote experiment namespace in v1

## Constraints

- one cloud re-eval session owns exactly one source experiment snapshot and one derived remote experiment name
- cloud re-eval launches an orchestrator and evaluators only; it does not provision CRS trial workers
- the cloud checkout remains the source of benchmark code, OSS-Fuzz checkout, and builder/runtime images
- cloud re-eval relies on normal cloud VM bootstrap for benchmark availability; it does not upload benchmark trees from the operator machine
- the uploaded bundle must contain only the files required for remote `discover_trials` plus verification inputs
- the remote runtime must use the existing `crsbench re-eval` distributed async flows, including `DistributedRuntimeSession.for_reeval`
- the remote experiment name must differ from the source experiment name in v1
- bundle publication must be atomic from the remote runner's point of view: an incomplete upload is never runnable
- acceptance must not occur until both the operator-side reconnect state and the remote-side submission state are durably recorded
- authoritative publication requires compatibility checks between source bundle provenance and the remote runtime environment
- collection failure must not invalidate already completed remote results

## Context and Boundaries

Cloud distributed re-eval adds a submission-and-staging layer around the
existing distributed re-eval runtime:

1. the operator machine discovers re-eval-ready trials from an existing local experiment tree
2. the operator machine builds a self-contained re-eval bundle from those trials plus a config snapshot
3. the cloud control plane provisions or reconnects to one remote orchestrator and the configured evaluator fleet
4. the operator machine publishes the bundle to the orchestrator
5. the orchestrator materializes a synthetic experiment workspace and runs ordinary `crsbench re-eval`
6. evaluators consume the normal distributed build/verify queues for that remote experiment
7. later collection pulls the remote result tree and logs back through the existing cloud control plane

This design is intentionally additive:

- local `crsbench re-eval` remains unchanged
- existing distributed evaluator behavior remains unchanged
- existing cloud trial-execution launch remains unchanged
- the new behavior is the bridge between local existing trials and remote distributed re-eval

This contract introduces new surfaces that the current trial-execution cloud
path does not already provide:

- a dedicated `cloud re-eval` operator command
- a re-eval-specific persisted launch/submission state
- an orchestrator-side remote runner that invokes `crsbench re-eval` instead of `crsbench run`

## Contract

### Command Surface

The cloud entry point is:

- `crsbench cloud re-eval --config <config>`

Provider-neutral command semantics:

- source experiment root defaults to the experiment directory resolved from the supplied config
- the operator may override the source root explicitly for collected or relocated experiments
- if the operator does not provide an explicit remote experiment name, CRSBench derives one as `<source-experiment>-reeval-<utc-timestamp>`
- submission performs one end-to-end control-plane action: resolve source trials, build the bundle, ensure remote capacity, publish the bundle, and start the remote re-eval run
- v1 submission targets a fresh remote experiment namespace; reconnect after submission uses saved state for that namespace rather than reusing a previously populated namespace

For cloud re-eval, "ensure remote capacity" means:

- the orchestrator VM exists and its local Redis/Valkey endpoint is reachable
- the evaluator fleet is provisioned for the remote experiment and may continue reaching readiness after submission acceptance, subject to the bounded drain semantics below

Cloud re-eval launch uses the existing provider-neutral `cloud.*` config surface,
with one role adjustment:

- `cloud.orchestrator` remains required
- `cloud.evaluators` remains the source of evaluator fleet shape
- `cloud.workers` is ignored for re-eval submission and must not cause worker provisioning

This role adjustment is specific to `cloud re-eval`. The existing
worker-centric `cloud launch` contract for CRS trial execution is unchanged.

Local saved state for a cloud re-eval deployment must retain enough information
to reconnect later for collection and teardown, including:

- remote experiment name
- source experiment name
- remote workspace root
- remote published bundle location
- submission mode discriminator for cloud re-eval
- provider/orchestrator identity and evaluator fleet records

That discriminator is how later `cloud collect` and `cloud teardown` distinguish
worker-centric trial-execution launches from orchestrator-authoritative
cloud re-eval launches.

In cloud re-eval mode, reconnect, collect, and teardown must not require
`cloud.workers` in config or persisted worker fleet records in launch state.
They operate from the re-eval submission state, orchestrator identity, and any
persisted evaluator fleet records instead.

Submission must fail closed if that local reconnect state cannot be persisted
before the remote bundle becomes runnable.

### Submission State Contract

Cloud re-eval requires a durable submission state machine shared between the
operator machine and the remote orchestrator.

The remote state record must expose at least these states:

- `uploading`
- `published`
- `materializing`
- `running`
- `succeeded`
- `failed`

This remote state record is owned by the cloud re-eval wrapper layer rather
than by `crsbench re-eval` itself.

Submission is treated as accepted only after all of the following are true:

- operator-side reconnect state is persisted successfully
- the remote bundle has reached `published`
- the remote run-status record exists for that bundle

If local state persistence fails before acceptance, CRSBench must not leave a
new runnable remote submission behind. It must either prevent publication or
revoke the just-published bundle before returning failure.

### Re-Eval Bundle Contract

The logical bundle is a self-contained description of the re-eval inputs for one
source experiment snapshot.

The source experiment may have been produced with `skip_verification: true`.
That source-side setting does not disqualify the run from cloud re-eval as long
as the required artifact inputs are present. Cloud re-eval rebuilds verification
results from preserved trial artifacts rather than requiring prior verification
outputs to exist in the source tree.

Required top-level entries:

- `manifest.json`
- `config/source-config.yaml`
- `trials/...`

`manifest.json` must record at least:

- bundle schema version
- bundle identifier
- source experiment name
- remote experiment name
- source experiment root provenance
- creation timestamp
- a normalized source-config digest
- source runtime provenance when discoverable, such as the local CRSBench revision
- per-mode trial counts
- per-trial relative path and deterministic mapping metadata
- skipped-trial records with explicit reasons

Candidate trial selection is deterministic:

- CRSBench discovers candidates using the same shared trial-discovery and baseline re-eval-readiness classification used by local `crsbench re-eval`
- only trials classified as `valid` and `reeval_ready` are selected into the bundle
- patch-generation submissions additionally require visible `crs-input/povs/`; trials missing those inputs are skipped and recorded explicitly
- invalid, incomplete, or non-ready trials are excluded and recorded in the manifest with their skip reason
- submission fails if zero trials remain after deterministic filtering

The `trials/` tree must preserve each selected trial's path relative to the
source experiment root. The remote materializer reconstructs that same relative
layout under the synthetic remote experiment root so later result collection can
map results back deterministically.

Bundle contents are intentionally minimal:

- every selected trial includes `metadata.json`
- bug-finding trials include `output/povs/` inputs
- bug-finding trials include `povs/pov_store.json` when present so original CRS timing metadata can be preserved
- patch-generation trials include `output/patches/` and `crs-input/povs/`
- terminal `.success` / `.fail` markers are preserved when present as trial provenance
- snapshot archives are outside the v1 bundle contract because they are not required for re-eval correctness
- prior re-eval result outputs are excluded unless the remote runtime explicitly requires them for correctness

Per-trial manifest data must include enough stable identity to recreate and
collect results unambiguously:

- relative path
- benchmark
- harness
- mode
- trial number
- sanitizer when known
- target CPV identifier when known

The bundle must not rely on absolute local paths. Any source-local path from the
original experiment config is advisory provenance only and must be rewritten by
the remote materialization step before execution.

Source-side `skip_verification` is provenance only. The remote `crsbench re-eval`
invocation still performs verification and may repopulate in-trial verification
outputs under the synthetic remote workspace.

Current provider realization may serialize the logical bundle as a tar archive
for upload, but the archive format is not part of the provider-neutral contract.

### Source and Remote Compatibility Contract

Because benchmark code, OSS-Fuzz state, and runtime images remain owned by the
cloud checkout rather than the uploaded bundle, authoritative remote re-eval
requires explicit compatibility checks.

The bundle provenance must carry the identifiers and settings needed to verify
that the remote run is materially equivalent to the source snapshot for
verification purposes, including:

- normalized config digest
- benchmark names selected by the bundle
- source mode and verification-affecting re-eval settings
- source runtime revision or install-spec identity when discoverable

Before a bundle may transition from `published` to `materializing`, CRSBench
must verify that the remote orchestrator/evaluator runtime is compatible with
that provenance. At minimum, compatibility must hold for:

- benchmark names and benchmark-root layout
- source mode
- queue model and evaluator routing model
- per-POV timeout, patch verification mode, and incremental-image settings
- any reserved env-driven runtime knobs that change evaluator or re-eval behavior

In v1, incompatibility is a blocking submission failure. Authoritative remote
results must not be produced from a bundle whose remote runtime compatibility
cannot be established.

If source-side runtime provenance cannot be recovered well enough to establish
that compatibility, v1 must fail closed rather than downgrade the submission to
a weaker authoritative mode.

### Remote Publication and Materialization Contract

Bundle upload and publication are two separate states:

- upload writes into an orchestrator-local staging location that is not yet runnable
- publication happens only after upload completes and the manifest is accepted

Remote re-eval must refuse to start from any bundle that has not reached the
published state.

For a published bundle, the orchestrator creates:

- an immutable bundle directory
- a mutable synthetic workspace directory
- a run-status directory or equivalent durable state record

The synthetic workspace contains one derived experiment config plus one
materialized experiment tree. The derived config must:

- set `experiment` to the remote experiment name
- set `experiment_filestore` to the remote workspace parent
- rewrite both legacy `redis_host` and grouped `runtime.redis.host` to the orchestrator-local Redis endpoint
- rewrite `benchmarks_root` to the cloud checkout-local benchmarks root
- preserve the source snapshot's verification, incremental-image, timeout, queue-model, and evaluator-routing settings that affect evaluator behavior

Cloud bootstrap env may append provider-owned or runtime-managed values, but it
must not silently override verification-affecting settings preserved from the
source snapshot.

Materialization must preserve trial-relative paths from the bundle manifest, but
it must not mutate the immutable uploaded bundle.

### Remote Execution Contract

Once materialization succeeds, the orchestrator runs ordinary distributed
re-evaluation:

- the orchestrator invokes `crsbench re-eval` against the derived config
- the orchestrator registers the remote experiment in Redis using the existing re-eval runtime session
- evaluators consume the normal build and verify queues for that remote experiment
- POV and patch verification continue to use the existing async queue/result semantics

No new evaluator RPC or evaluator-only protocol is introduced for cloud re-eval.
The cloud-specific layer is limited to transport, staging, remote config
derivation, and remote process lifecycle.

The remote re-eval process must outlive the operator's interactive SSH session
once the submission is accepted. Remote execution therefore requires a managed
or detached orchestrator-side process boundary rather than a fragile foreground
SSH command.

### Result and Collection Contract

Authoritative results live on the orchestrator first.

The authoritative remote result set is:

- the synthetic workspace trial directories after `crsbench re-eval` completes
- a run summary record for the bundle
- orchestrator and evaluator logs needed to diagnose failures

The run summary must include:

- bundle identifier
- source and remote experiment names
- started/completed timestamps
- remote wrapper terminal state
- underlying `crsbench re-eval` process exit code
- total trial counts and result counts
- skipped-trial counts
- drain-timeout or undrained-verification indicators

The bundle-level summary artifact is generated by the cloud re-eval wrapper
layer, not by `crsbench re-eval` itself.

Remote wrapper terminal state describes submission lifecycle, not whether every
trial verified cleanly. Per-trial verification failures remain part of the
collected result set and summary data.

Collection uses the existing cloud control plane, but cloud re-eval changes what
must be collected:

- `cloud collect` and `cloud teardown` for a cloud re-eval deployment must collect the orchestrator-hosted re-eval workspace and remote logs
- default local publish destination is a fresh local experiment tree keyed by the remote experiment name
- collection must not silently overwrite the original source experiment tree
- collection is valid only after the remote submission reaches a terminal state unless the operator explicitly requests log-only inspection
- local publication must use the same stage-verify-publish discipline documented for cloud artifact collection so partially collected trees do not become visible results

Repeated collection of the same remote run must remain idempotent from an
operator point of view:

- when a local destination already exists, CRSBench follows explicit CLI collection policy for reusing that destination or publishing a fresh sibling
- it must not merge partial local publishes into the authoritative remote result set implicitly

Later local merge of collected results back into the source experiment is a
separate explicit operation. Remote authoritative results remain valid even when
local collection fails or is deferred.

Collected remote trees are re-eval result trees, not full snapshot-preserving
reconstructions of the original experiment directory.

### Failure and Retry Semantics

Bundle publication:

- incomplete uploads are not runnable
- manifest validation failure leaves the bundle unpublished

Remote execution:

- materialization failure prevents the remote `crsbench re-eval` invocation from starting
- once accepted, operator disconnect does not cancel the remote run
- evaluator absence or slow arrival follows the current distributed `re-eval` drain behavior: queued work is bounded by `verify_timeout`, and remote completion must distinguish clean drain from timed-out or undrained verification through the cloud wrapper summary
- orchestrator-side failures leave the immutable bundle, mutable workspace, and remote logs available for inspection

Retry:

- v1 retry happens at the whole-bundle level
- resubmission targets a fresh remote experiment name in v1
- v1 does not require per-trial in-place resume of a previously failed published bundle or reuse of a previously populated remote namespace

Collection:

- collection failure does not change authoritative remote results
- teardown against a non-terminal remote submission must refuse by default
- an explicit destructive override may first cancel the remote submission, then collect logs best-effort, and only then reclaim the fleet

## Deployment and Distributed Behavior

- cloud re-eval is a non-local deployment mode with an operator machine, one remote orchestrator, zero trial workers, and one or more remote evaluators
- the orchestrator hosts the Redis/Valkey endpoint locally; evaluators connect to the orchestrator's worker-reachable address, while the orchestrator process itself may use `localhost`
- operator reconnect commands may still use SSH and local tunnels independently of evaluator-to-Redis connectivity
- benchmark source is not uploaded; correctness depends on the cloud checkout containing the referenced benchmarks and compatible runtime code
- orchestrator and evaluator VMs both use the shared cloud bootstrap path: `crsbench prepare` plus benchmark download when `cloud.bootstrap.download_benchmarks` resolves true
- when benchmark download is enabled and the selected suite or dataset comes from gated Hugging Face storage, the required token must be passed through cloud env (for example `HF_TOKEN`) before remote re-eval submission
- provider-neutral launch semantics from [Cloud Orchestration](./cloud-orchestration.md) still apply; current runtime realization is GCE-specific

## Decisions and Tradeoffs

- bundle plus synthetic workspace instead of direct remote path references: avoids invalid local path assumptions and keeps remote execution reproducible
- distinct remote experiment name by default: avoids Redis queue collisions and launch-state conflicts with the source experiment
- authoritative remote results first: preserves inspectability and makes failed local collection non-destructive
- reuse existing distributed re-eval runtime: minimizes protocol surface area and keeps evaluator behavior consistent across local, distributed, and cloud re-eval
- no automatic merge into the source experiment tree: prefers correctness and recoverability over convenience when remote collection is partial or repeated

## Risks and Validation

Risks:

- source bundle inputs and cloud checkout contents may drift if the operator submits a bundle built from one experiment snapshot against a different cloud checkout revision
- cloud re-eval without explicit collection may leave useful results stranded on the orchestrator
- reconnect and teardown are easy to underspecify because re-eval results live on the orchestrator, not on workers

Validation expectations:

- bundle builder tests covering minimal required file selection and trial-relative path preservation
- derived-config tests covering remote experiment name, Redis host rewriting, and benchmarks root rewriting
- remote publication tests proving incomplete uploads are not runnable
- integration-style tests covering operator bundle submission -> orchestrator materialization -> remote `crsbench re-eval` -> evaluator verdict persistence
- collection tests covering orchestrator-hosted result-tree retrieval and non-destructive repeated collection

## Implementation Pointers

- `crsbench/evaluation/reeval/cli.py`
- `crsbench/distributed/runtime_session.py`
- `crsbench/cloud/cli/cloud_command.py`
- `crsbench/cloud/launch_state.py`
- `crsbench/cloud/cli/_launch.py`
- `crsbench/cloud/collection.py`
- `crsbench/cloud/gce/transport.py`
