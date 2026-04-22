# Cloud Orchestration

- Audience: contributors changing cloud launch config, readiness semantics, reconnect behavior, or cloud control-plane commands
- Scope: provider-neutral cloud orchestration contracts shared across launch, monitoring, collection, and teardown
- Related:
  - [Deployment Guide](./deployment-guide.md)
  - [Cloud Live Log Streaming](./cloud-live-log-streaming.md)
  - [Configless Runtime](./configless-runtime.md)
  - [Distributed Evaluation](./distributed-evaluation.md)
  - [GCE Cloud Orchestration](./gce-cloud-orchestration.md)
  - [GCE Cloud Orchestrator Launch](./gce-cloud-orchestrator.md)
  - [User Guide: GCE Cloud Orchestration](../../guides/experiments/gce-cloud-orchestration.md)

## Goals and Non-goals

Goals:

- define the canonical provider-neutral cloud contract for experiment launches
- keep config ownership, readiness semantics, reconnect behavior, and artifact collection stable across provider implementations
- make control-plane behavior explicit for non-local operator workflows
- preserve one shared launch/config surface even when provider adapters differ underneath

Non-goals:

- requiring all providers to use the same native VM APIs or placement mechanics
- supporting one launch that mixes multiple cloud providers
- defining provider-specific quota names, API payloads, or transport commands in this doc
- autoscaling or provider-managed worker creation after launch

## Constraints

- one cloud launch resolves to exactly one provider across orchestrator, workers, and evaluators
- the experiment config owns fleet shape; cloud providers do not discover extra workers implicitly
- readiness is a CRSBench control-plane state and is distinct from raw provider VM status
- cloud VMs stay pinned to the declaring experiment and do not join the shared configless worker/evaluator pool
- the operator machine remains the control plane for launch-state persistence and later reconnect commands
- provider-neutral config contracts may exist before every provider implementation exists; current runtime support is GCE only
- runtime-managed variables such as Redis connection material remain owned by CRSBench bootstrap and cannot be overridden by user env layers

## Context and Boundaries

The shared cloud contract sits above provider-specific provisioning code and below
user-facing guides:

- validation owns the provider-neutral config schema under `cloud.*`
- launch planning resolves that config into a provider-neutral launch plan
- provider resolution chooses the backing provisioner, adapter, and transport
- control-plane commands reuse persisted launch state plus live provider inventory
- provider-specific docs own native API behavior, access methods, quota checks, and placement realization details

This split keeps canonical ownership stable even when the current implementation
is still GCE-only. New providers should extend the provider-owned layers without
changing the shared contract unless behavior visible to operators or configs
actually changes.

## Contract

### Config Ownership

The canonical provider-neutral cloud surface is:

- `cloud.bootstrap`: VM bootstrap policy shared by cloud roles
- `cloud.remote.experiment_root`: remote source root used by standalone `cloud collect` and `cloud teardown`
- `cloud.defaults`: provider-agnostic launch/bootstrap defaults
- `cloud.env`: global cloud env map merged into every launched role
- `cloud.providers.<provider>`: provider-native backing details and reusable instance profiles
- `cloud.orchestrator`: provider-neutral orchestrator placement plus orchestrator-only env
- `cloud.workers.defaults` and `cloud.workers.placements[]`: worker fleet defaults and explicit placements
- `cloud.evaluators.defaults` and `cloud.evaluators.placements[]`: evaluator fleet defaults and explicit placements

The provider-neutral surface owns placement intent, bootstrap policy, reconnect
semantics, and artifact path semantics. Provider-specific docs own how one
provider realizes those declarations.

### Provider Resolution

- orchestrator, worker, and evaluator placements reference named instance profiles
  from `cloud.providers.<provider>.instance_profiles`
- CRSBench resolves the owning provider from the referenced profile catalog
- instance-profile names must remain globally unique across provider catalogs
- one launch may not mix providers; cross-provider launches are invalid even if
  the schema knows multiple provider names
- `CloudProvider` may reserve future providers such as AWS or Azure before their
  runtime backends exist; that does not imply they are launchable yet

Current implementation status:

- the shared config and type surface is provider-neutral
- the only implemented launch backend today is GCE

### Merge and Precedence

Launch/bootstrap defaults merge as:

`cloud.defaults -> cloud.providers.<provider>.defaults`

Cloud env layers merge in this order:

`cloud.env -> profile_defaults.env -> instance_profile.env -> role defaults.env -> placement.env`

Role-specific behavior:

- the orchestrator uses the same chain without `role defaults.env` / `placement.env`,
  ending with `cloud.orchestrator.env`
- runtime-managed variables are applied after user-configured env layers and win last
- reserved runtime-managed names must be rejected during validation

### Launch Plan and State

- the validated experiment config is normalized into one provider-neutral launch plan
- that plan carries provider, resolved placement intent, launch defaults, and merged env payloads for each role
- the operator machine owns create/list/delete actions, duplicate-launch guards, and launch-state persistence
- persisted launch state must record enough realized provider data to reconnect later without parsing instance names
- reconnect state must store the actual provider and realized location chosen for each created instance

### Readiness and Lifecycle

Readiness is the canonical bring-up contract for managed cloud roles.

Provider-observed states such as VM creation or `RUNNING` are only inputs to
CRSBench readiness. A role becomes schedulable only when CRSBench records
explicit readiness for that experiment and `instance_id`.

For pre-provisioned remote-orchestrator mode, that schedulable signal is used
for operator visibility and failure evidence, not as a hard requirement that
every declared worker/evaluator reach `ready` before the orchestrator enqueues
jobs.

Shared readiness invariants:

- readiness is keyed by experiment plus provider instance identity
- `ready` means bootstrap completed and the role-specific runtime is listening on the expected queue/backend
- `bootstrap_failed` is terminal for bring-up and must retain operator-visible evidence
- failed bring-up tears down the matching requested fleet before control returns
- `readiness_timeout_sec` measures end-to-end bootstrap time, not bare VM boot time

### Control-Plane Commands

The shared cloud control plane includes:

- `cloud launch`: create the declared fleet and persist reconnect state
- `cloud add-workers`, `cloud add-evaluators`: append one runtime placement to
  an already launched experiment without mutating the checked-in config
- `cloud preflight`: resolve the launch plan, duplicate-launch guard, provider launch-input preflight, and quota checks without provisioning or mutating launch state
- `cloud status`: return a one-shot fleet, job, and recovery snapshot
- `cloud events`: return recovery-event history from the active experiment control plane
- `cloud monitor`: attach to the launched experiment's live queue view; when
  Apprise URLs are configured in the operator environment, it may emit one
  operator-side notification after the first observed non-empty -> empty queue
  transition during that attach session; failed jobs at that drain point flip
  the terminal message into a failure report; an initial idle state does not
  notify, but a later active -> idle transition in the same session still can
- `cloud list`: show the resolved live inventory for the experiment
- `cloud ssh`, `cloud shell`, and `cloud exec`: operator access to one live
  cloud instance; selectors may use the full name, resolved alias, or any other
  unambiguous filtered short form such as `eval-001`, and role shorthands like
  `eval` resolve when they match exactly one live instance; when a selector
  still matches multiple live instances, interactive sessions must prompt from
  the narrowed match set while non-interactive sessions fail
- `cloud serial`: operator access to one live cloud instance's guest serial
  console using the same selector rules; serial-console login uses guest-local
  credentials and remains distinct from OS Login-backed SSH access
- `cloud log`: operator access to one live cloud instance by default, and to
  multiple live cloud instances when explicit multi-target intent is provided
  with `--all`, `--role`, or repeated `--instance`; ambiguous selectors must
  not implicitly widen to multiple streams
- `cloud collect`: retrieve worker artifacts plus role diagnostics
- `cloud teardown`: collect first, then reclaim the fleet

Shared reconnect semantics:

- `status`, `events`, and `monitor` are control-plane reconnect commands and may
  require runtime/backend reachability in addition to launch state
- `monitor` uses the operator session as the notification boundary; if
  operator-side `cloud monitor` Apprise and orchestrator-side Apprise are both
  enabled through the cloud launch env, terminal notifications can duplicate
  because the operator-side monitor and the orchestrator-side cleanup path are
  independent emitters
- `add-workers` and `add-evaluators` require saved launch state plus backend
  reachability because they must gate the new placement on shared readiness
- `preflight` is read-only and must not write or refresh persisted launch state
- `list`, `ssh`, `exec`, `log`, `collect`, and `teardown` must be able to reuse
  persisted launch state plus live provider inventory
- `collect` and `teardown` should continue from persisted state when the runtime
  backend is unavailable, subject to provider inventory still being resolvable

Shared runtime-expansion semantics:

- runtime expansion is operator-driven, not automatic scaling
- each add-capacity command appends exactly one new worker or evaluator placement
- omitting `--count` adds exactly one new instance instead of inheriting
  `cloud.<role>.defaults.count`
- omitting `--instance-profile` inherits the matching role default instance
  profile
- omitting location selectors inherits matching role defaults first, then
  provider defaults
- explicit CLI `instance_profile`, `count`, and location selectors override the
  inherited values for the new placement; all other behavior remains inherited
  from config defaults
- quota validation applies to the delta placement before any provider create call
- failures during provisioning or readiness roll back only the new placement and
  leave the existing fleet untouched
- persisted launch state records runtime-added placements explicitly so later
  status, list, collect, and teardown commands include them automatically

Shared preflight semantics:

- `preflight` resolves the same provider-neutral launch plan that `launch` would use
- duplicate-launch conflicts, provider launch-input failures, and quota failures are blocking checks
- warning checks may still return success by default, but `--strict` upgrades warnings to a non-zero exit code
- preflight output is provider-neutral even when the underlying implementation is provider-specific today

### Artifact Collection

Artifact collection is a shared contract even when transport and inventory come
from a specific provider.

Canonical semantics:

- local destination defaults to `storage.experiment_filestore/<experiment>`
- remote source defaults to `<cloud.remote.experiment_root>/<experiment>` when
  `cloud.remote.experiment_root` is set
- when `cloud.remote.experiment_root` is unset, standalone cloud operations fall
  back to the legacy remote path derived from `storage.experiment_filestore`
- worker artifacts are staged, verified, then published so partial trees do not
  become the visible experiment result
- evaluator and orchestrator collection may be log-only even when workers publish artifacts
- repeated collect runs may merge or publish to a fresh sibling destination according to CLI policy

### Failure and Retry Semantics

- fallback policy is config-driven, not provider-autonomous
- recognized provider placement failures may retry later declared regions or zones only when fallback is enabled for that logical slot
- quota/preflight checks are provider-owned, but launch must fail before partial provisioning when preflight proves the request unsatisfiable
- rollback and duplicate-launch safety remain mandatory control-plane behavior

## Deployment and Distributed Behavior

- cloud orchestration is a non-local deployment mode with an operator machine, cloud VMs, and a runtime backend that may not be reachable from every network path
- remote orchestrators may host the runtime backend for workers/evaluators, but they do not automatically become the cloud control plane
- worker/evaluator runtime connectivity, operator SSH connectivity, and operator reconnect connectivity are separate contracts and may use different paths
- cloud workers and evaluators remain outside the shared configless runtime pool even when they use the same Redis technology

## Decisions and Tradeoffs

- shared contract plus provider appendices: keeps future provider work additive instead of cloning a GCE-only contract
- single-provider-per-launch: simplifies config validation, launch planning, readiness, and reconnect state
- explicit env precedence: keeps operator-controlled secret distribution predictable across roles and placements
- persisted realized locations: avoids brittle reconnect logic based on naming conventions

## Risks and Validation

- docs may drift back toward provider-specific ownership if future provider work edits only implementation docs
- provider-neutral terminology can become misleading if it claims support for backends that validation or runtime still reject
- reconnect semantics are easy to underspecify; docs must keep the distinction between control-plane commands and provider-inventory commands explicit

Validation expectations for changes in this area:

- update this doc when changing the shared `cloud.*` contract or reconnect semantics
- update the relevant provider appendix when changing native provider realization details
- keep at least one user-facing guide aligned with the shared contract wording

## Implementation Pointers

- `crsbench/cloud/models.py`
- `crsbench/cloud/providers.py`
- `crsbench/cloud/transport.py`
- `crsbench/cloud/types.py`
- `crsbench/validation/schemas.py`
