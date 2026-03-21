# GCE Cloud Orchestration

- Audience: contributors changing cloud provisioning, readiness, artifact collection, or CLI behavior
- Scope: GCE orchestrator plus worker/evaluator lifecycle from provisioning through teardown
- Related:
  - [Deployment Guide](./deployment-guide.md)
  - [Configless Runtime](./configless-runtime.md)
  - [Distributed Evaluation](./distributed-evaluation.md)
  - [User Guide: GCE Cloud Orchestration](../../guides/experiments/gce-cloud-orchestration.md)

## Goals and Non-goals

Goals:

- Declarative orchestrator and worker/evaluator provisioning from experiment config
- Explicit readiness gating before trial enqueue
- Safe artifact collection and VM teardown
- Operator visibility into fleet, job, and recovery state

Non-goals:

- automatic placement decisions beyond declared `region` / `regions` / `zones`
- Auto-scaling worker count based on queue depth
- Non-GCE cloud providers

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
│            GCE Worker / Evaluator VM (×N / ×M)           │
│  startup/worker.sh                                       │
│    ├── fetch bootstrap payload from instance metadata    │
│    ├── clone CRSBench checkout as crsbench user          │
│    ├── run prepare/download + oss-crs setup              │
│    ├── start user service for worker or evaluator        │
│    └── on failure: report bootstrap_failed to Redis      │
└─────────────────────┬────────────────────────────────────┘
                      │ workers consume trial queue
                      │ evaluators consume build/verify queue
                      ▼
                 ┌──────────┐
                 │  Redis   │
                 └──────────┘
                      ▲
                      │ operator inspects / monitors / collects / tears down
┌──────────────────────────────────────────────────────────┐
│                  CLI (crsbench cloud)                    │
│    ├── launch / status / monitor / events               │
│    ├── list / ssh(shell) / exec / log                   │
│    └── collect / teardown / keygen                      │
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
| `cloud/runtime.py` | Worker/evaluator-side env and state reporting |
| `cloud/collection.py` | `ArtifactCollector`: rsync staging pipeline with verify-then-publish |
| `cloud/cli/cloud_command.py` | Top-level `cloud` CLI dispatch |
| `cloud/cli/_launch.py` | `launch` sub-action for remote orchestrator bring-up |
| `cloud/cli/_status.py` | `status` sub-action |
| `cloud/cli/_monitor.py` | `monitor` sub-action for live queue attach |
| `cloud/cli/_events.py` | `events` sub-action |
| `cloud/cli/_list.py` | `list` sub-action |
| `cloud/cli/_ssh.py` | `ssh` / `shell` sub-action |
| `cloud/cli/_exec.py` | `exec` sub-action |
| `cloud/cli/_log.py` | `log` sub-action |
| `cloud/cli/_collect.py` | `collect` sub-action |
| `cloud/cli/_teardown.py` | `teardown` sub-action with safety flow |
| `cloud/cli/_keygen.py` | `keygen` sub-action for deploy-key generation |
| `cloud/cli/_config_reconnect.py` | Config loading and Redis/store reconnection |
| `cloud/launch_state.py` | Persisted reconnect state for remote-orchestrator launches |
| `cloud/orchestrator_tunnel.py` | Temporary SSH/IAP Redis tunnel management |

## Contract: Worker / Evaluator State Machine

```
PROVISIONING ──► BOOTING ──► REGISTERING ──► READY
                                          ──► BOOTSTRAP_FAILED
Any state ──► DELETING ──► DELETED
```

Invariants:

- Transitions are forward-only; invalid transitions raise `ValueError`
- `READY`, `BOOTSTRAP_FAILED`, and `DELETED` are terminal during bring-up
- State records are stored in Redis readiness hashes keyed by `instance_id`
- Each record carries `state`, `instance_name`, `zone`, `updated_at`, `ready_at`, and optional `evidence`
- `CloudFleetSnapshot` categorizes workers/evaluators into ready/pending/failed/missing buckets

State semantics:

- `PROVISIONING`: the control plane requested the VM, but the worker/evaluator has not yet reached a provider-observed running state
- `BOOTING`: GCE reports the VM as running, but the startup script may still be installing packages, cloning CRSBench, or writing the managed service
- `REGISTERING`: the worker/evaluator runtime has started far enough to report into the readiness store, but it is not yet counted as schedulable
- `READY`: the worker/evaluator has connected to Redis and is listening on the expected experiment queue; this is the only success state counted by bring-up gating

Timeout contract:

- `readiness_timeout_sec` measures wall-clock time spent waiting for workers/evaluators to reach `READY`, not just time until the VM kernel boots or the GCE provider reports `RUNNING`
- In the create-and-wait flow, the timeout starts after instance creation completes and initial non-ready state is recorded; it therefore includes package installation, checkout/install of CRSBench, systemd launch, Redis reachability, and queue-listener startup
- In the pre-provisioned wait flow used by the remote orchestrator, the timeout starts when the orchestrator begins waiting for the expected worker/evaluator instances and includes both instance discovery and the remaining time until each role reaches `READY`
- Operators should size the timeout for clean-image bootstrap plus first Redis/queue registration, not for bare VM boot alone

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

Rollback:

- If any VM creation fails, all previously created VMs are deleted in reverse order
- Rollback errors are silently caught to avoid masking the original creation error
- Before any VM is created, launch fails fast if the same experiment already has
  a persisted launch-state file or matching live orchestrator/worker/evaluator VMs

## Contract: Bring-up and Readiness Gating

- `CloudFleetStatusManager.bring_up_workers()` clears stale readiness records, creates VMs across all declared placements, records initial state, then polls for readiness
- `CloudFleetStatusManager.wait_for_existing_workers()` gates pre-provisioned workers/evaluators across all declared placements on the same explicit readiness protocol without creating VMs again
- Polling uses `CloudReadinessStore.snapshot()` with configurable `poll_interval_sec` (default 5s)
- Remote launch succeeds only when the orchestrator is reachable and all requested workers/evaluators reach `READY`
- On timeout or any failure: transitions workers/evaluators through `DELETING`/`DELETED`, deletes VMs, raises `CloudFleetBringupError` with the snapshot
- Trial enqueue is gated on successful bring-up

## Contract: VM Bootstrap

The startup script (`cloud/gce/startup/worker.sh`) runs on the VM:

1. Fetches bootstrap payload from GCE instance metadata API
   - local rehearsal may instead mount file-backed metadata and set
     `CRSBENCH_METADATA_ROOT_DIR`; the shell contract prefers that source and
     falls back to the GCE metadata endpoint
2. Requires a `git+...` `crsbench-install-spec`, creates a dedicated `crsbench` user, clones CRSBench into `/opt/crsbench`, and installs the checkout as that user
   - when a deploy key is configured, SSH is scoped to the top-level CRSBench clone only
   - submodules continue to use the URLs declared in `.gitmodules`, so public submodules can remain on HTTPS
3. Normalizes the host timezone, configures Docker to use the `cgroupfs` driver expected by `oss-crs`, and grants passwordless `sudo` for the disposable-host bootstrap
4. On `systemd` hosts, enables linger and delegated controllers for `crsbench`, then runs `oss-crs setup --yes` from the checkout so the user-service cgroup hierarchy is ready
5. Runs `crsbench prepare` from that checkout and optionally downloads benchmarks according to `cloud.bootstrap`
6. Imports resolved first-class cloud env vars plus runtime-managed vars and writes them to a state-scoped env file under `/var/lib/crsbench`
7. Creates and enables the role-appropriate `systemd --user` unit with `WorkingDirectory=/opt/crsbench` (`Restart=always`) when `systemd` is available
   - local rehearsal and other non-`systemd` hosts may set
     `CRSBENCH_SERVICE_MANAGER=foreground`, which skips unit management and
     `exec`s the same launcher directly as `crsbench`
8. `CRSBENCH_STARTUP_MODE=evaluator` switches the shared bootstrap into evaluator mode so it launches `crsbench evaluator` with the embedded experiment config instead of `crsbench worker`
9. The managed launcher polls the configured Redis endpoint up to `readiness_timeout_sec` before starting the role-specific CRSBench runtime; retryable connection timeouts are retried, while fatal Redis auth/config errors fail immediately
10. Only after bootstrap succeeds and Redis becomes reachable does the worker/evaluator connect to Redis and report readiness
11. On failure: ERR trap or launcher timeout calls `report_cloud_worker_state_from_env()` with `bootstrap_failed` and evidence string

Cloud env contract:

- Experiment config may declare `cloud.env`, `cloud.orchestrator.env`, `cloud.workers.defaults.env`, `cloud.workers.placements[].env`, and the evaluator equivalents
- Values may be literals, `os.environ/...`, or `file:...` references and are resolved on the operator before provisioning
- Missing or empty referenced values fail launch before any VM is created
- Reserved runtime-managed names such as `CRSBENCH_REDIS_HOST` and `CRSBENCH_REDIS_PASSWORD` are rejected during config validation
- Resolved env values are encoded into the generic cloud env metadata bundle and exported by the startup scripts on each VM

## Contract: Artifact Collection

`ArtifactCollector` implements a stage-then-publish pattern:

1. **Stage**: rsync from worker into `{experiment_filestore}/.collect-staging/{worker_name}/{experiment_name}/`
2. **Verify**: `discover_trials()` confirms the staging tree contains at least one valid trial (with `metadata.json`)
3. **Publish**: `shutil.copytree` with `dirs_exist_ok=True` into `{experiment_filestore}/{experiment_name}/`
4. **Cleanup**: removes the per-worker staging directory

Default remote source path:

- when `cloud.remote.experiment_root` is set, `collect` / `teardown` read from `<cloud.remote.experiment_root>/<experiment.name>`
- otherwise they fall back to `<storage.experiment_filestore>/<experiment.name>` for backward compatibility

Rsync transport:

- IAP mode: open a temporary local `gcloud compute start-iap-tunnel` to remote
  SSH port 22, then run plain `ssh`/`rsync` against `127.0.0.1:<local-port>`
  with a per-instance host-key alias
- Direct mode: `ssh -o BatchMode=yes -o StrictHostKeyChecking=yes`

Retry: `tenacity` exponential backoff (min 2s, max 30s, 3 attempts) on rsync failure.

## Contract: Teardown Safety Flow

1. Validate GCE instances exist, cross-reference with Redis readiness records
2. Warn about stale Redis entries not matching live VMs
3. Prompt for confirmation (requires TTY unless `--force`)
4. Collect artifacts from ALL workers; collection is best-effort so teardown can still reclaim VMs
5. In remote-orchestrator mode, also collect the orchestrator VM
6. Delete workers and orchestrator even if collection reported failures, and return a non-zero exit code when any collection or deletion step failed

## Contract: CLI Sub-actions

**`cloud --config <yaml> launch`**

- Provisions the orchestrator VM first, then the requested worker/evaluator fleet
- Persists reconnect state under config-adjacent `.crsbench-cloud/`

**`cloud --config <yaml> status <experiment> [--json]`**

- Fleet summary: instance name, state, zone, IP
- Job summary: job_id, trial, state, claimed_by, retries
- Collection summary: total/completed/syncing/pending/failed/completion%
- Last 5 recovery events
- JSON mode outputs structured data

**`cloud --config <yaml> events <experiment> [--type <type>] [--json]`**

- Recovery event timeline from Redis list `crsbench:recovery-events:{experiment}`
- Filterable by event type

**`cloud --config <yaml> monitor [<experiment>]`**

- Reconnects to the remote orchestrator through a temporary SSH/IAP tunnel
- Renders the same live queue progress view used by `crsbench run`

**`cloud --config <yaml> list [--json]`**

- Lists the live cloud inventory inferred from config plus persisted launch state

**`cloud --config <yaml> ssh|shell [<instance>]`**

- Opens an operator shell on a live cloud instance using its launch-time zone

**`cloud --config <yaml> exec [<instance>] -- <command...>`**

- Runs a one-off remote command against a selected cloud instance

**`cloud --config <yaml> log [<instance>]`**

- Follows the primary managed CRSBench journal on the selected instance

**`cloud keygen [--output-dir ...] [--name ...] [--force]`**

- Generates an ed25519 deploy key pair for private `git+ssh` CRSBench clones

**`cloud --config <yaml> collect [<experiment>] [--remote-dir <path>]`**

- Lists live GCE workers, cross-references with Redis
- Runs `ArtifactCollector.collect()` per worker
- Defaults to `experiment.name` and the resolved remote experiment root from
  `cloud.remote.experiment_root` when present, else
  `<storage.experiment_filestore>/<experiment.name>`
- Partial failure: continues to remaining workers, returns exit code 1

**`cloud --config <yaml> teardown [<experiment>] [--remote-dir <path>] [--force]`**

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

- **Explicit declared placement**: keeps placement intent declarative while still allowing quota-driven multi-zone or multi-region splits
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
