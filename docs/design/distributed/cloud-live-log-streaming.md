# Cloud Live Log Streaming

- Audience: contributors changing cloud operator log access, reconnect behavior, or remote log transport
- Scope: cloud operator log-access contract for single-instance follow and explicit multi-instance live fan-in across cloud roles
- Related:
  - [Cloud Orchestration](./cloud-orchestration.md)
  - [Deployment Guide](./deployment-guide.md)
  - [GCE Cloud Orchestration](./gce-cloud-orchestration.md)
  - [Logging Contract](../logging/logging-architecture.md)

## Goals and Non-goals

Goals:

- define a backward-compatible extension of `cloud log` for live fan-in across multiple cloud instances
- preserve low-latency operator visibility without requiring a centralized logging backend
- keep provider-neutral semantics for selection, streaming, ordering, and failure behavior
- make non-local failure and reconnect behavior explicit for SSH- and IAP-backed transport paths

Non-goals:

- replacing `cloud monitor` as the experiment-wide queue/progress surface
- introducing historical search, indexing, retention, or log analytics
- tailing arbitrary remote files as a first-class operator contract
- claiming globally correct total ordering across hosts with independent clocks
- auto-subscribing to runtime-added instances after the attach session has already started

## Constraints

- single-instance usage must remain backward-compatible for both CLI shape and operator expectations
- the command must work from persisted launch state plus live provider inventory, without requiring a resident control-plane daemon on the operator machine
- default live streaming must target the role-appropriate CRSBench user service journal for each selected instance
- non-TTY output must remain readable and free of ANSI escapes, consistent with the shared logging contract
- provider-specific transport differences such as direct SSH and IAP tunneling must stay hidden behind provider-owned transport adapters
- the feature must remain useful when one or more selected streams fail independently of the others

## Context and Boundaries

Current cloud operator surfaces split into three distinct responsibilities:

- `cloud log` follows the primary CRSBench journal for one selected live
  instance by default and supports explicit multi-target fan-in
- `cloud monitor` shows experiment-wide queue state and recovery progress
- `cloud collect` gathers journals and artifacts after or during execution, with bounded parallelism

This design fills the gap between single-instance live follow and offline log
collection. It is intentionally a live operator-view feature, not a replacement
for centralized logging infrastructure.

The canonical scope is live fan-in of service-journal output from a fixed target
set resolved at attach time. Historical retrieval, storage policies, and
cross-run indexing remain owned by collection/reporting workflows rather than by
this contract.

## Contract

### Command Surface

The contract extends `cloud log` rather than introducing a separate command.

Required semantics:

- a single explicit instance selector continues to mean one live target
- multi-target attach must be explicit via operator intent rather than inferred from ambiguous selectors
- multi-target selection must support at least:
  - `--all` for every live instance in the resolved experiment inventory
  - `--role <role>` for all live instances of one role
  - repeated `--instance <selector>` for an explicit set
- duplicate selectors must deduplicate to one logical live target
- when multi-target intent is present, non-interactive sessions must never prompt
- the resolved target set is fixed for the lifetime of the attach session

Out of scope for the initial contract:

- automatic discovery and attachment of workers or evaluators added after the session starts
- arbitrary selector expressions that require a secondary query language

### Source Semantics

The initial live source per target is the role-appropriate CRSBench user service
journal:

- orchestrator: `crsbench-orchestrator.service`
- evaluator: `crsbench-evaluator.service`
- worker: `crsbench-worker.service`

Canonical rules:

- every selected target contributes at most one default live source in the base contract
- additional source families such as startup-script journals or role-local flat files may be added later, but they must remain opt-in and source-labeled
- the merge contract must not depend on parsing CRSBench application log text; ordering metadata must come from the transport/journal layer when timestamp-aware merge is requested

### Stream Record Semantics

Every emitted record in a multi-target session must carry stable operator-visible
source identity.

Required fields in the rendered stream:

- instance identity
- resolved role
- source kind
- event body

The transport layer may use a machine-readable journal format internally, but
the operator-visible output remains line-oriented. Multiline events must remain
readable by either:

- prefixing each rendered line with the same source identity, or
- rendering one framed event block whose continuation lines are visually tied to the same source identity

The command must not rely on hostnames alone as the identity label because
operators already reason about CRSBench instance aliases and role names.

### Merge Semantics

Two merge modes are part of the contract:

- `arrival`: emit each record when the operator process receives it from its source stream
- `timestamp`: use source-carried journal timestamps and a bounded local reordering buffer before rendering

Default behavior:

- default merge mode is `arrival`
- `arrival` optimizes for lowest latency and simplest failure behavior

Timestamp-mode invariants:

- timestamp-aware merge must use journal/transport timestamps rather than parsing the application message body
- the implementation may delay rendering within a bounded window to reduce obvious cross-stream inversions
- the output remains best-effort rather than globally total-ordered because host clocks and network paths may diverge
- rendered records must still include their source identity so operators can reason about skew or delayed delivery

### Failure and Retry Semantics

Each selected target owns an independent live stream state machine.

Canonical states:

- `connecting`
- `streaming`
- `retry_wait`
- `detached`
- `failed`

Required behavior:

- failure to attach one target must not cancel already-streaming targets
- attachment failures must be surfaced as explicit operator-visible control events with the affected target identity
- transient transport failures may retry with bounded backoff against the same resolved target identity
- permanent failures such as target disappearance from live inventory or repeated transport exhaustion transition that target to `failed`
- the session stays alive while at least one target remains in `connecting`, `streaming`, or `retry_wait`
- a user interrupt terminates all active child streams and returns the standard interrupt exit code

Exit semantics:

- return non-zero when no requested target ever reached `streaming`
- return non-zero when the session terminates because every target ended in `failed`
- return success when the operator intentionally terminates a session that had at least one successfully attached stream

### Compatibility Constraints

- single-instance `cloud log` remains the narrowest and simplest operator path
- existing selectors that currently resolve one instance must continue to work unchanged
- the multi-target mode must reuse the same provider inventory and SSH transport abstractions as `cloud ssh` and `cloud exec`
- the feature must not require changes to remote CRSBench runtime logging format beyond the existing centralized logger contract

## Runtime Behavior

### Happy Path

1. resolve experiment context, persisted launch state, and live provider inventory
2. resolve the fixed target set from explicit multi-target intent
3. start one live transport stream per target
4. convert remote stream events into locally labeled records
5. merge records according to the requested mode
6. render a continuous operator-visible stream until interrupted or until all targets terminate

### Partial Failure Path

1. one target disconnects or fails to attach
2. the operator process emits a labeled control event for that target
3. other targets continue streaming
4. the failed target optionally retries with bounded backoff
5. the session ends only when all targets are terminal or the operator interrupts

### Ordering Path

1. each target emits records independently
2. `arrival` mode renders immediately on receipt
3. `timestamp` mode places records into a bounded reorder buffer keyed by source-carried timestamp
4. records older than the watermark render in timestamp order, with best-effort behavior under skew

## Deployment and Distributed Behavior

- the operator machine fan-ins one live transport stream per selected cloud instance
- transport path differences remain provider-owned; the shared contract only requires that a live byte stream be obtainable for each resolved target
- clock skew between VMs is expected and must be visible rather than hidden by false total-order claims
- network asymmetry is expected: operator-to-orchestrator connectivity, operator-to-worker connectivity, and worker-to-runtime-backend connectivity are separate concerns
- large target counts increase operator-side connection load linearly; this feature is for operator visibility, not for fleet-wide centralized log ingestion
- runtime-added capacity does not automatically join an existing attach session; operators re-run `cloud log` to include newly added instances

## Decisions and Tradeoffs

- extend `cloud log` instead of adding a new command: keeps the operator surface compact and preserves existing mental models
- fixed target set per session: avoids background inventory refresh complexity and unclear semantics for late joiners
- one default live source per target: keeps first implementation focused on the same operational signal the single-instance command already exposes
- `arrival` as the default: favors immediacy and robustness over weaker timestamp-order promises across hosts
- best-effort timestamp ordering as opt-in: useful for incident analysis without pretending that distributed clocks are authoritative

## Risks and Validation

Main risks:

- merged output can become noisy without strong source labeling
- timestamp-mode ordering can mislead operators if host clocks drift materially
- many concurrent SSH/IAP sessions can overload the operator machine or hit provider transport limits
- multiline exception output can become unreadable if framing is inconsistent

Validation expectations for implementation work in this area:

- selector-resolution tests for `--all`, `--role`, repeated `--instance`, and deduplication
- merge-behavior tests for `arrival` and timestamp-buffered ordering
- failure-injection tests where one stream fails while others continue
- rendering tests for multiline events in TTY and non-TTY modes
- at least one non-local integration path covering real remote transport semantics, not only local subprocess mocks

## Implementation Pointers

- `crsbench/cloud/cli/_log.py`
- `crsbench/cloud/cli/cloud_command.py`
- `crsbench/cloud/cli/_remote_access.py`
- `crsbench/cloud/transport.py`
- `crsbench/cloud/collection.py`
- `crsbench/utils/logger.py`
