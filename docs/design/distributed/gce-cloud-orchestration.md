# GCE Cloud Orchestration

- Audience: contributors changing the GCE realization of CRSBench cloud provisioning, bootstrap, access, or placement behavior
- Scope: GCE-specific implementation of the shared cloud orchestration contract for orchestrator, worker, and evaluator VMs
- Related:
  - [Cloud Orchestration](./cloud-orchestration.md)
  - [Deployment Guide](./deployment-guide.md)
  - [Configless Runtime](./configless-runtime.md)
  - [Distributed Evaluation](./distributed-evaluation.md)
  - [User Guide: GCE Cloud Orchestration](../../guides/experiments/gce-cloud-orchestration.md)

## Goals and Non-goals

Goals:

- define how GCE realizes the shared CRSBench cloud contract
- document GCE-specific provisioning, metadata, quota, and operator access behavior
- keep zonal/regional placement and fallback semantics explicit for GCE launches
- document the GCE bootstrap environment expected by CRSBench cloud VMs

Non-goals:

- redefining the provider-neutral `cloud.*` contract owned by [Cloud Orchestration](./cloud-orchestration.md)
- describing non-GCE provider realization
- automatic placement decisions beyond declared `region` / `regions` / `zones`
- auto-scaling worker count based on queue depth

## Constraints

- Workers must use OS Login SSH (no project-level SSH keys)
- IAP tunneling is the preferred SSH transport for workers without public IPs
- Service accounts must be explicit and least-privileged
- Readiness is a control-plane concept distinct from GCE VM `RUNNING` state
- Cloud VMs are experiment-pinned and do not join the shared configless pool
- Duplicate launch is rejected when config-adjacent launch state or matching
  live instances already exist for the same experiment
- Cloud VM bootstrap runs from a cloned CRSBench checkout; non-`git+` install specs are outside this contract
- Operator-selected remote environment passthrough is explicit; runtime-managed vars such as Redis host/password remain owned by the VM bootstrap

## Context and Boundaries

This document is the GCE appendix for the shared cloud contract:

- [Cloud Orchestration](./cloud-orchestration.md) owns provider-neutral config,
  readiness, reconnect, and collection semantics
- this document owns GCE VM creation, metadata payload delivery, OS Login/IAP
  operator access, quota/preflight behavior, and GCE-specific placement
  realization
- [GCE Cloud Orchestrator Launch](./gce-cloud-orchestrator.md) owns the remote
  orchestrator flow on top of these GCE mechanics

## Architecture

```
Experiment YAML
    │
    ▼
┌──────────────────────────────────────────────────────────┐
│             Shared Cloud Control Plane                   │
│  launch plan + readiness + reconnect + collection        │
│    ├── provider resolution                               │
│    ├── launch-state persistence                          │
│    └── control-plane commands                            │
└─────────────────────┬────────────────────────────────────┘
                      │ calls GCE realization layer
                      ▼
┌──────────────────────────────────────────────────────────┐
│                GCE Provider Realization                  │
│  GceProvisioner + metadata + transport + startup         │
│    ├── create/list/delete instances                      │
│    ├── render metadata and startup payloads              │
│    ├── validate GCE quota and region/zone membership     │
│    └── choose OS Login SSH or IAP transport              │
└─────────────────────┬────────────────────────────────────┘
                      │ GCE startup metadata / SSH / rsync
                      ▼
┌──────────────────────────────────────────────────────────┐
│            GCE Orchestrator / Worker / Evaluator VM      │
│  startup/*.sh + managed user services                    │
│    ├── read metadata from GCE                            │
│    ├── install CRSBench checkout and prerequisites       │
│    ├── start role service                                │
│    └── report readiness/evidence through shared control  │
└──────────────────────────────────────────────────────────┘
```

## Module Layout

| Module | Responsibility |
|---|---|
| `cloud/models.py` | provider-neutral launch plan consumed by GCE resolution |
| `cloud/expansion.py` | runtime-added placement request parsing and inherited env resolution |
| `cloud/providers.py` | provider selection that routes launches to GCE today |
| `cloud/transport.py` | provider-neutral transport interface implemented by GCE |
| `cloud/gce/models.py` | `GceInstanceRequest`, `GceWorkerInstance` data models |
| `cloud/gce/metadata.py` | Bootstrap payload, label sanitization, startup script bundling |
| `cloud/gce/provisioner.py` | `GceProvisioner`: create, list, delete VMs; `GceApiClient` protocol |
| `cloud/gce/launch_preflight.py` | GCE quota and launch-input preflight |
| `cloud/gce/transport.py` | OS Login SSH, known-host, and IAP tunnel commands |
| `cloud/readiness.py` | `CloudReadinessStore`: Redis-backed per-instance state tracking |
| `cloud/status.py` | `CloudFleetStatusManager`: bring-up orchestration with readiness gating |
| `cloud/runtime.py` | Worker/evaluator-side env and state reporting |
| `cloud/collection.py` | `ArtifactCollector`: rsync staging pipeline with verify-then-publish |

Shared readiness states and timeout semantics are defined in
[Cloud Orchestration](./cloud-orchestration.md). GCE-specific behavior feeds that
contract by mapping GCE instance status and metadata-driven bootstrap outcomes
into the shared readiness records.

## Contract: VM Provisioning

- Worker names follow the pattern `{prefix}-{NNN}` (zero-padded, e.g., `my-exp-001`)
- Orchestrator names are distinct and labeled with `crsbench-role=orchestrator`
- Two creation modes: explicit `image` + `machine_type` + `boot_disk_size_gb`, or `instance_template`
- Network: `ssh_via_iap` controls operator SSH transport only; outbound internet
  egress is controlled independently by `assign_external_ip` (default `true`,
  set `false` only when private egress such as Cloud NAT is already available)
- OS Login enabled via metadata (`enable-oslogin=TRUE`, `block-project-ssh-keys=TRUE`)
- Labels always include `owner` and `crsbench-experiment`; role-specific labels distinguish orchestrator and workers
- Bootstrap payload delivered as base64-encoded JSON in instance metadata
- Operator-selected remote env vars are delivered separately as base64-encoded JSON metadata after operator-side validation; they are not persisted in launch-state files
- Live quota validation is required before launch begins
- Regional placement uses GCE regional bulk insert with `ANY_SINGLE_ZONE`; optional `zones` are validated as an allowlist inside the resolved region set
- Zonal or regional fallback retries only later declared candidates for recognized GCE capacity failures when fallback is enabled

Rollback:

- If any VM creation fails, all previously created VMs are deleted in reverse order
- Rollback errors are silently caught to avoid masking the original creation error
- Before any VM is created, launch fails fast if the same experiment already has
  a persisted launch-state file or matching live orchestrator/worker/evaluator VMs

## Contract: Runtime Capacity Expansion

- Runtime capacity expansion is operator-driven only; GCE does not autoscale
  CRSBench fleets from queue depth.
- `cloud add-workers` and `cloud add-evaluators` require saved remote launch
  state from a prior `cloud launch`.
- Each command adds exactly one new worker or evaluator placement.
- Runtime overrides are limited to a named instance profile, `count`, and
  `regions` / `zones`; GCE fallback policy, launch defaults, deploy-key path,
  and inherited env layers continue to come from config-owned defaults.
- Delta quota validation happens before any new VM create call.
- `regions` plus `zones` keeps the same GCE semantics as launch-time config:
  regional bulk insert first, with `zones` restricted to the current region on
  each attempt.
- Confirmation is interactive by default; `--force` skips only the prompt.
- Provisioning and readiness waiting apply only to the new placement. Failures
  trigger rollback of only that placement and leave the existing fleet running.
- Successful runtime-added placements are appended to launch state with
  `placement_source=runtime_added`; `cloud status` and `cloud list` surface that
  provenance, and `cloud collect` / `cloud teardown` consume the updated launch
  state automatically.

## Contract: GCE Inputs To Shared Readiness

- `CloudFleetStatusManager.bring_up_workers()` and `wait_for_existing_workers()` use the shared readiness protocol; this GCE contract defines the provider-side inputs they consume
- GCE `RUNNING` is not a success state; it maps to shared non-ready bootstrap states until the runtime reports readiness
- startup-script failures must surface evidence through readiness so operators can diagnose failures without SSH
- remote launch succeeds only when the orchestrator is reachable and all requested workers/evaluators reach shared `READY`
- on timeout or failure, GCE instances are deleted and shared readiness records transition through the deletion path

## Contract: VM Bootstrap

The startup script (`cloud/gce/startup/worker.sh`) runs on the VM:

1. Fetches bootstrap payload from GCE instance metadata API
   - local rehearsal may instead mount file-backed metadata and set
     `CRSBENCH_METADATA_ROOT_DIR`; the shell contract prefers that source and
     falls back to the GCE metadata endpoint
2. Installs the pinned `gitcache` release binary into a CRSBench-managed bin directory on every cloud VM
   - `cloud.bootstrap.gitcache: true` adds a managed `git -> gitcache` wrapper in the CRSBench process PATH
   - `cloud.bootstrap.gitcache: false` leaves the system `git` unchanged while still making `gitcache` available as a command
   - install failure is warning-only when wrapper mode is disabled and bootstrap-fatal when wrapper mode is enabled
3. Requires a `git+...` `crsbench-install-spec`, creates a dedicated `crsbench` user, clones CRSBench into `/opt/crsbench`, and installs the checkout as that user
   - when a deploy key is configured, SSH is scoped to the top-level CRSBench clone only
   - submodules continue to use the URLs declared in `.gitmodules`, so public submodules can remain on HTTPS
4. Normalizes the host timezone, configures Docker to use the `cgroupfs` driver expected by `oss-crs`, and grants passwordless `sudo` for the disposable-host bootstrap
5. On `systemd` hosts, enables linger and delegated controllers for `crsbench`, then runs `oss-crs setup --yes` from the checkout so the user-service cgroup hierarchy is ready
6. Runs `crsbench prepare` from that checkout and optionally downloads benchmarks according to `cloud.bootstrap`
7. Imports resolved first-class cloud env vars plus runtime-managed vars and writes them to a state-scoped env file under `/var/lib/crsbench`
8. Creates and enables the role-appropriate `systemd --user` unit with `WorkingDirectory=/opt/crsbench` (`Restart=always`) when `systemd` is available
   - local rehearsal and other non-`systemd` hosts may set
     `CRSBENCH_SERVICE_MANAGER=foreground`, which skips unit management and
     `exec`s the same launcher directly as `crsbench`
9. `CRSBENCH_STARTUP_MODE=evaluator` switches the shared bootstrap into evaluator mode so it launches `crsbench evaluator` with the embedded experiment config instead of `crsbench worker`
10. The managed launcher polls the configured Redis endpoint up to `readiness_timeout_sec` before starting the role-specific CRSBench runtime; retryable connection timeouts are retried, while fatal Redis auth/config errors fail immediately
11. Only after bootstrap succeeds and Redis becomes reachable does the worker/evaluator connect to Redis and report readiness
12. On failure: ERR trap or launcher timeout calls `report_cloud_worker_state_from_env()` with `bootstrap_failed` and evidence string

Cloud env contract:

- Experiment config may declare `cloud.env`, `cloud.orchestrator.env`, `cloud.workers.defaults.env`, `cloud.workers.placements[].env`, and the evaluator equivalents
- Values may be literals, `os.environ/...`, or `file:...` references and are resolved on the operator before provisioning
- Missing or empty referenced values fail launch before any VM is created
- Reserved runtime-managed names such as `CRSBENCH_REDIS_HOST` and `CRSBENCH_REDIS_PASSWORD` are rejected during config validation
- Resolved env values are encoded into the generic cloud env metadata bundle and exported by the startup scripts on each VM

## Contract: GCE Transport And Collection Realization

Shared collection semantics are owned by
[Cloud Orchestration](./cloud-orchestration.md). GCE realizes them as follows:

- worker artifact transfer uses `rsync` over either direct SSH or an IAP-backed local tunnel
- default remote source path is still derived from `cloud.remote.experiment_root` when present, else the legacy `storage.experiment_filestore` fallback
- the stage-verify-publish pipeline prevents partial worker trees from becoming visible results

Transport details:

- IAP mode: open a temporary local `gcloud compute start-iap-tunnel` to remote
  SSH port 22, then run plain `ssh`/`rsync` against `127.0.0.1:<local-port>`
  with a per-instance host-key alias
- Direct mode: `ssh -o BatchMode=yes -o StrictHostKeyChecking=yes`

Retry: `tenacity` exponential backoff (min 2s, max 30s, 3 attempts) on rsync failure.

## Contract: Teardown Realization

Shared teardown safety semantics are owned by
[Cloud Orchestration](./cloud-orchestration.md). For GCE:

- teardown validates live GCE instances and cross-references readiness records when available
- stale Redis entries that do not match live VMs are warnings, not blockers
- collection remains best-effort so leaked GCE resources can still be reclaimed
- instance deletion uses the realized launch-time zone stored in launch state or live inventory

## External Dependencies

| Dependency | Usage |
|---|---|
| `google-cloud-compute` | GCE API (InstancesClient, ZoneOperationsClient) |
| `redis` | Readiness store, job lifecycle, recovery events |
| `tenacity` | Retry logic for rsync |
| `rsync` (system) | Artifact transfer from workers |
| `ssh` (system) | Direct SSH transport |
| `gcloud` (system) | IAP SSH tunneling, VM listing |
| `systemd` (on VM) | Worker service management |

## Decisions and Tradeoffs

- **Explicit declared placement**: keeps placement intent declarative while still allowing quota-driven multi-zone or multi-region splits
- **OS Login only**: eliminates project-level SSH key management; IAP adds network-level isolation
- **Sequential VM creation**: enables clean rollback ordering; parallelism is a future optimization
- **experiment-pinned workers**: avoids readiness state leaking across experiments

## Risks and Validation

- Startup script failure on unexpected OS images: mitigated by `bootstrap_failed` reporting with evidence
- Rsync partial transfer: mitigated by staging directory pattern and `--partial-dir`
- Stale Redis state after manual VM deletion: CLI cross-references live GCE state and warns
- GCE API quota limits: sequential creation limits blast radius but may slow large fleets

## Implementation Pointers

- `crsbench/cloud/gce/provisioner.py` -- VM lifecycle
- `crsbench/cloud/gce/launch_preflight.py` -- quota and launch-input preflight
- `crsbench/cloud/gce/transport.py` -- operator access and tunnels
- `crsbench/cloud/readiness.py` -- state machine and Redis store
- `crsbench/cloud/status.py` -- bring-up orchestration
- `crsbench/cloud/collection.py` -- artifact pipeline
- `crsbench/cloud/providers.py` -- shared provider resolution
- `crsbench/cloud/gce/startup/worker.sh` -- VM bootstrap script
