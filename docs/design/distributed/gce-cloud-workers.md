# GCE Cloud Worker Fleet

- Audience: contributors changing cloud provisioning, readiness, artifact collection, or CLI behavior
- Scope: GCE worker fleet lifecycle from provisioning through teardown
- Related:
  - [Deployment Guide](./deployment-guide.md)
  - [Configless Runtime](./configless-runtime.md)
  - [Distributed Evaluation](./distributed-evaluation.md)
  - [User Guide: GCE Cloud Workers](../../guides/experiments/gce-cloud-workers.md)

## Goals and Non-goals

Goals:

- Declarative worker fleet provisioning from experiment config
- Explicit readiness gating before trial enqueue
- Safe artifact collection and fleet teardown
- Operator visibility into fleet, job, and recovery state

Non-goals:

- Multi-region or multi-zone fleet distribution (Phase 1 is zonal only)
- Auto-scaling worker count based on queue depth
- Non-GCE cloud providers

## Constraints

- Workers must use OS Login SSH (no project-level SSH keys)
- IAP tunneling is the preferred SSH transport for workers without public IPs
- Service accounts must be explicit and least-privileged
- Readiness is a control-plane concept distinct from GCE VM `RUNNING` state
- Cloud workers are experiment-pinned and do not join the shared configless pool

## Architecture

```
Experiment YAML
    │
    ▼
┌──────────────────────────────────────────────────────────┐
│                    Orchestrator                          │
│  CloudFleetStatusManager                                 │
│    ├── GceProvisioner (create/list/delete VMs)           │
│    ├── CloudReadinessStore (Redis state tracking)        │
│    └── wait loop (poll until fleet READY or timeout)     │
└─────────────────────┬────────────────────────────────────┘
                      │ creates VMs with startup metadata
                      ▼
┌──────────────────────────────────────────────────────────┐
│                 GCE Worker VM (×N)                        │
│  startup/worker.sh                                       │
│    ├── fetch bootstrap payload from instance metadata    │
│    ├── install crsbench                                  │
│    ├── create systemd service crsbench-worker.service    │
│    └── on failure: report bootstrap_failed to Redis      │
└─────────────────────┬────────────────────────────────────┘
                      │ workers consume trial queue
                      ▼
                 ┌──────────┐
                 │  Redis   │
                 └──────────┘
                      ▲
                      │ operator inspects / collects / tears down
┌──────────────────────────────────────────────────────────┐
│                  CLI (crsbench cloud)                     │
│    ├── status   (fleet + job + recovery summary)         │
│    ├── events   (recovery event timeline)                │
│    ├── collect  (rsync artifacts from workers)           │
│    └── teardown (collect-then-delete safety flow)        │
└──────────────────────────────────────────────────────────┘
```

## Module Layout

| Module | Responsibility |
|---|---|
| `cloud/gce/models.py` | `GceInstanceRequest`, `GceWorkerInstance` data models |
| `cloud/gce/metadata.py` | Bootstrap payload, label sanitization, startup script bundling |
| `cloud/gce/provisioner.py` | `GceProvisioner`: create, list, delete VMs; `GceApiClient` protocol |
| `cloud/readiness.py` | `CloudReadinessStore`: Redis-backed per-instance state tracking |
| `cloud/status.py` | `CloudFleetStatusManager`: bring-up orchestration with readiness gating |
| `cloud/runtime.py` | `CloudWorkerRuntimeContext`: worker-side env and state reporting |
| `cloud/collection.py` | `ArtifactCollector`: rsync staging pipeline with verify-then-publish |
| `cloud/cli/cloud_command.py` | Top-level `cloud` CLI dispatch |
| `cloud/cli/_status.py` | `status` sub-action |
| `cloud/cli/_events.py` | `events` sub-action |
| `cloud/cli/_collect.py` | `collect` sub-action |
| `cloud/cli/_teardown.py` | `teardown` sub-action with safety flow |
| `cloud/cli/_config_reconnect.py` | Config loading and Redis/store reconnection |

## Contract: Worker State Machine

```
PROVISIONING ──► BOOTING ──► REGISTERING ──► READY
                                          ──► BOOTSTRAP_FAILED
Any state ──► DELETING ──► DELETED
```

Invariants:

- Transitions are forward-only; invalid transitions raise `ValueError`
- `READY`, `BOOTSTRAP_FAILED`, and `DELETED` are terminal during bring-up
- State records are stored in Redis hash `crsbench:cloud:workers:{experiment}`, keyed by `instance_id`
- Each record carries `state`, `instance_name`, `zone`, `updated_at`, `ready_at`, and optional `evidence`
- `CloudFleetSnapshot` categorizes workers into ready/pending/failed/missing buckets

## Contract: VM Provisioning

- Worker names follow the pattern `{prefix}-{NNN}` (zero-padded, e.g., `my-exp-001`)
- Two creation modes: explicit `image` + `machine_type` + `boot_disk_size_gb`, or `instance_template`
- Network: external NAT when `ssh_via_iap=False`; private-only when `ssh_via_iap=True`
- OS Login enabled via metadata (`enable-oslogin=TRUE`, `block-project-ssh-keys=TRUE`)
- Labels always include `owner`, `crsbench-experiment`, `crsbench-role=worker`
- Bootstrap payload delivered as base64-encoded JSON in instance metadata

Rollback:

- If any VM creation fails, all previously created VMs are deleted in reverse order
- Rollback errors are silently caught to avoid masking the original creation error

## Contract: Bring-up and Readiness Gating

- `CloudFleetStatusManager.bring_up_gce_workers()` clears stale readiness records, creates VMs, records initial state, then polls for readiness
- `CloudFleetStatusManager.wait_for_existing_gce_workers()` gates a pre-provisioned fleet on the same explicit readiness protocol without creating VMs again
- Polling uses `CloudReadinessStore.snapshot()` with configurable `poll_interval_sec` (default 5s)
- Bring-up succeeds only when all workers reach `READY`
- On timeout or any failure: transitions workers through `DELETING`/`DELETED`, deletes VMs, raises `CloudFleetBringupError` with the snapshot
- Trial enqueue is gated on successful bring-up

## Contract: Worker Bootstrap

The startup script (`cloud/gce/startup/worker.sh`) runs on the VM:

1. Fetches bootstrap payload from GCE instance metadata API
2. Installs crsbench (using optional `crsbench-install-spec` metadata) or uses a preinstalled `crsbench` CLI on the VM image
3. Writes env vars to `/etc/default/crsbench-worker`
4. Creates and enables `crsbench-worker.service` (systemd, `Restart=always`)
5. On failure: ERR trap calls `report_cloud_worker_state_from_env()` with `bootstrap_failed` and evidence string

## Contract: Artifact Collection

`ArtifactCollector` implements a stage-then-publish pattern:

1. **Stage**: rsync from worker into `{experiment_filestore}/.collect-staging/{worker_name}/{experiment_name}/`
2. **Verify**: `discover_trials()` confirms the staging tree contains at least one valid trial (with `metadata.json`)
3. **Publish**: `shutil.copytree` with `dirs_exist_ok=True` into `{experiment_filestore}/{experiment_name}/`
4. **Cleanup**: removes the per-worker staging directory

Rsync transport:

- IAP mode: `gcloud compute ssh ... --tunnel-through-iap -- -W %h:%p`
- Direct mode: `ssh -o BatchMode=yes -o StrictHostKeyChecking=yes`

Retry: `tenacity` exponential backoff (min 2s, max 30s, 3 attempts) on rsync failure.

## Contract: Teardown Safety Flow

1. Validate GCE instances exist, cross-reference with Redis readiness records
2. Warn about stale Redis entries not matching live VMs
3. Prompt for confirmation (requires TTY unless `--force`)
4. Collect artifacts from ALL workers; collection is best-effort so teardown can still reclaim VMs
5. Delete workers even if collection reported failures, and return a non-zero exit code when any collection or deletion step failed

## Contract: CLI Sub-actions

**`cloud status <experiment> --config <yaml> [--json]`**

- Fleet summary: instance name, state, zone, IP
- Job summary: job_id, trial, state, claimed_by, retries
- Collection summary: total/completed/syncing/pending/failed/completion%
- Last 5 recovery events
- JSON mode outputs structured data

**`cloud events <experiment> --config <yaml> [--type <type>] [--json]`**

- Recovery event timeline from Redis list `crsbench:recovery-events:{experiment}`
- Filterable by event type

**`cloud collect <experiment> --config <yaml> --remote-dir <path>`**

- Lists live GCE workers, cross-references with Redis
- Runs `ArtifactCollector.collect()` per worker
- Partial failure: continues to remaining workers, returns exit code 1

**`cloud teardown <experiment> --config <yaml> --remote-dir <path> [--force]`**

- Full collect-then-delete safety flow described above

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

- **Zonal only (Phase 1)**: simplifies label-based instance listing and avoids cross-zone networking complexity
- **OS Login only**: eliminates project-level SSH key management; IAP adds network-level isolation
- **Sequential VM creation**: enables clean rollback ordering; parallelism is a future optimization
- **Stage-then-publish**: prevents partial artifact trees from appearing in experiment results
- **Collect-before-delete in teardown**: prevents data loss from premature VM deletion
- **experiment-pinned workers**: avoids readiness state leaking across experiments

## Risks and Validation

- Startup script failure on unexpected OS images: mitigated by `bootstrap_failed` reporting with evidence
- Rsync partial transfer: mitigated by staging directory pattern and `--partial-dir`
- Stale Redis state after manual VM deletion: CLI cross-references live GCE state and warns
- GCE API quota limits: sequential creation limits blast radius but may slow large fleets

## Implementation Pointers

- `crsbench/cloud/gce/provisioner.py` -- VM lifecycle
- `crsbench/cloud/readiness.py` -- state machine and Redis store
- `crsbench/cloud/status.py` -- bring-up orchestration
- `crsbench/cloud/collection.py` -- artifact pipeline
- `crsbench/cloud/cli/` -- operator commands
- `crsbench/cloud/gce/startup/worker.sh` -- VM bootstrap script
