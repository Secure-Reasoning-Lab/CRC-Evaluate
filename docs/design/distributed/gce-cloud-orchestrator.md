# GCE Cloud Orchestrator Launch

- Audience: contributors changing GCE deployment flow, remote orchestration, or cloud control-plane behavior
- Scope: local-machine launch of one orchestrator VM plus a worker fleet for a single experiment run
- Related:
  - [Deployment Guide](./deployment-guide.md)
  - [GCE Cloud Workers](./gce-cloud-workers.md)
  - [Configless Runtime](./configless-runtime.md)
  - [User Guide: GCE Cloud Workers](../../guides/experiments/gce-cloud-workers.md)

## Goals and Non-goals

Goals:

- allow an operator machine to launch a full GCE-backed experiment without running the orchestrator locally
- keep worker lifecycle owned by the operator machine, not by the remote orchestrator VM
- give workers a stable, worker-reachable Redis endpoint hosted on the orchestrator VM
- preserve the existing local-orchestrator workflow as a separate mode

Non-goals:

- moving worker fleet ownership to the orchestrator VM
- adding autoscaling or self-healing worker creation from the orchestrator VM
- changing non-GCE deployment paths

## Constraints

- the operator machine may have GCP credentials; the orchestrator VM must not require them
- the orchestrator VM may run `crsbench run`, but must not provision workers itself
- workers and the orchestrator share one Redis/Valkey instance hosted on the orchestrator VM
- worker bootstrap must receive both a reachable Redis address and Redis authentication material
- local-machine `cloud` commands remain the operational surface for status, collection, and teardown

## Context and Boundaries

The current GCE worker-fleet contract assumes that `crsbench run` executes on the
operator machine and provisions workers directly. This contract adds a second,
explicit flow:

1. the operator machine creates an orchestrator VM
2. the operator machine discovers the orchestrator VM's internal address
3. the operator machine creates the worker fleet with metadata pointing at that orchestrator-hosted Redis
4. the orchestrator VM runs the experiment against the pre-created worker fleet

This flow is additive. The existing local-orchestrator path remains valid.

## Contract

### Launch Ownership

- one local-machine launch command owns orchestrator creation, worker creation, and the initial bootstrap material for both roles
- the orchestrator VM is a runtime host, not a cloud control-plane authority
- all GCE create/delete/list operations remain callable from the local machine
- successful launch persists local reconnect state so later `cloud status`,
  `cloud collect`, and `cloud teardown` commands can target the remote
  orchestrator queue and worker fleet

### Redis Contract

- the local machine generates the Redis password for the run
- the orchestrator VM receives that password as metadata and starts Valkey with it
- workers receive the same password plus the orchestrator VM's worker-reachable Redis host
- worker metadata must never use `localhost` as the remote Redis endpoint

### Experiment Config Contract

- the orchestrator VM runs a config derived from the operator-supplied experiment config
- the derived config must rewrite both the compatibility `redis_host` key and the grouped `runtime.redis.host` key to the orchestrator-local Redis endpoint
- the derived orchestrator config must not trigger worker provisioning again
- worker fleet declarations remain the source of truth for the remote worker shape

### Role Discovery Contract

- orchestrator and workers are labeled distinctly in GCE
- local-machine status and teardown commands must be able to find both roles for one experiment
- worker readiness remains keyed by experiment plus cloud `instance_id`
- orchestrator lifecycle state is tracked separately from worker readiness

## Runtime Behavior

Happy path:

1. operator runs a local `cloud launch` command with an experiment config
2. local control plane validates config and creates an orchestrator VM
3. local control plane waits until the orchestrator VM has a usable internal address
4. local control plane creates the worker fleet with Redis host/password metadata targeting the orchestrator VM
5. orchestrator VM bootstraps CRSBench, starts Valkey, rewrites/derives the experiment config for remote-orchestrator mode, waits for the pre-provisioned worker fleet to report ready, and runs `crsbench run`
6. workers bootstrap, connect to the orchestrator-hosted Redis, and process trial jobs
7. operator uses local `cloud status`, `cloud collect`, and `cloud teardown`

Failure behavior:

- orchestrator create failure: no workers are created
- worker create failure after orchestrator creation: local control plane tears down created workers and the orchestrator VM
- orchestrator bootstrap failure: status must surface evidence without requiring SSH
- worker bootstrap failure: readiness gating remains explicit and per-instance
- Redis auth mismatch: workers fail bootstrap and surface evidence
- operator reconnect failure: `cloud status` and `cloud events` fail fast, while `cloud collect` and `cloud teardown` continue from persisted launch state plus GCE inventory

## Deployment and Distributed Behavior

- the orchestrator VM must expose Redis on a worker-reachable address, typically an internal VPC address
- workers are expected to use private connectivity to the orchestrator VM
- operator collection/teardown may use IAP or direct SSH independently of worker-to-Redis connectivity
- heterogeneous worker filesystems remain governed by the existing worker override contract

## Decisions and Tradeoffs

- separate launch path instead of overloading `crsbench run`: keeps current local mode stable and makes remote-orchestrator intent explicit
- local-machine cloud ownership instead of orchestrator-managed workers: avoids requiring GCP credentials inside the orchestrator VM
- operator-generated Redis secret: keeps both roles synchronized without requiring the orchestrator VM to publish secrets after boot

## Risks and Validation

- race between orchestrator creation and worker launch: mitigated by waiting for a usable orchestrator address before creating workers
- double-provisioning workers from the orchestrator VM: mitigated by deriving an orchestrator-only runtime config
- job enqueue before workers are actually available: mitigated by explicit readiness waiting for the pre-provisioned worker fleet on the orchestrator VM
- stale cloud resources after partial launch failure: mitigated by local rollback of both orchestrator and workers
- secret drift between orchestrator and workers: validated by an end-to-end launch test covering shared Redis auth

Validation requirements:

- unit tests for orchestrator metadata rendering and launch sequencing
- integration-style tests for launch rollback and derived config generation
- at least one smoke path covering local-machine launch -> orchestrator VM bootstrap -> worker readiness -> teardown

## Implementation Pointers

- `crsbench/cloud/gce/startup/orchestrator.sh`
- `crsbench/cloud/gce/startup/worker.sh`
- `crsbench/cloud/gce/provisioner.py`
- `crsbench/cloud/cli/cloud_command.py`
- `crsbench/cloud/status.py`
