# Multi-Machine Deployment Contract

Audience: contributors changing non-local deployment behavior or distributed runtime assumptions.
Scope: deployment-level contracts for orchestrators, workers, evaluators, and shared queue/storage dependencies.

This document defines the non-local deployment contract for CRSBench.

Operational runbooks and exact commands live in:

- [Distributed Experiments](../../guides/experiments/distributed.md)
- [Benchmark CI Distributed Guide](../../guides/benchmark-ci/distributed.md)

Architecture details live in:

- [Distributed Evaluation](./distributed-evaluation.md)
- [Distributed Job Queue](./distributed-job-queue.md)

## Topology Contract

A distributed CRSBench deployment consists of three process roles sharing one
Redis/Valkey instance:

- orchestrator: owns experiment registration and trial submission
- workers: execute CRS trials
- evaluators: build variants and process verification jobs

Deployment assumptions:

- one orchestrator owns a given experiment run
- all worker/evaluator processes see the same queue backend
- all processes in the same deployment agree on `CRSBENCH_QUEUE_MODEL`
- evaluator presence is optional; verify jobs may accumulate and drain later

## Connectivity Contract

- Remote workers and evaluators may connect to Redis directly or through tunnels.
- Authentication, tunnels, and firewall policy are operational concerns, not protocol concerns.
- CRSBench only requires that each process can reach the configured Redis host.

## Cloud Worker Contract

When a deployment uses managed GCE workers, the experiment config is the source
of truth for worker-fleet shape. The cloud contract is:

- worker fleets are declared through `cloud.providers.gce` plus `cloud.workers.placements`, not in lab-specific host maps
- Phase 1 supports explicit zone placements only; region selectors are not supported
- provisioned workers carry experiment identity plus operator ownership labels
- supported operator access remains OS Login-compatible SSH with host
  verification enabled
- IAP-backed SSH is preferred when workers do not expose public SSH
- worker service accounts must be explicit and least-privileged
- cloud-worker readiness is a control-plane state distinct from raw VM
  `RUNNING` state and from global Redis worker counts
- bootstrapped workers use an experiment-pinned runtime path rather than the
  shared configless worker pool
- readiness records are keyed by cloud `instance_id`, not by instance name
  alone
- startup failure evidence must remain retrievable from the control path
  without interactive VM login

## Remote GCE Orchestrator Contract

When a deployment uses `cloud.orchestrator` plus GCE-backed
`cloud.workers.placements`, the local
operator machine remains the cloud control plane. The deployment contract is:

- the operator machine provisions exactly one orchestrator VM plus the worker fleet
- the orchestrator VM runs `crsbench run` but does not create workers again
- the operator machine generates the Redis/Valkey password for the run
- workers receive the orchestrator VM's worker-reachable Redis host, never `localhost`
- local `cloud` commands reconnect through persisted launch state stored next to
  the submitted config file under `.crsbench-cloud/`
- local `status` and `events` still require Redis reachability from the
  operator machine to the orchestrator VM
- local `collect` and `teardown` may fall back to persisted launch state plus
  GCE inventory when Redis is unavailable

## Cloud Readiness and Evidence

Managed cloud bring-up is successful only when CRSBench sees an explicit ready
fleet for the current experiment:

- provider states such as `PROVISIONING` or VM `RUNNING` map to non-ready
  CRSBench states like `provisioning` and `booting`
- `registering` means the worker runtime can report to Redis but is not yet
  listening for trial work
- workers become schedulable only after the readiness store records `ready`
- `ready` means the worker connected to Redis and is listening on the
  experiment queue; it is not just a VM boot-complete signal
- `bootstrap_failed` and `deleted` are terminal non-ready states during bring-up
- startup evidence must include per-instance detail so operators can diagnose
  failures without manual SSH
- stale readiness is scoped away by experiment name plus `instance_id`
- failed bring-up tears down the matching fleet before the orchestrator returns
- `readiness_timeout_sec` covers clean-image bootstrap, CRSBench install,
  service startup, Redis reachability, and queue-listener registration; it
  must not be sized as a bare GCE boot timeout

## Path and Storage Contract

- The orchestrator serializes experiment metadata into queue jobs.
- Workers and evaluators may need host-local overrides for paths such as
  `benchmarks_root`.
- Heterogeneous clusters are supported only when shared mount points or
  host-local overrides make benchmark and result paths valid on each host.
- Evaluators always resolve OSS-Fuzz from the managed checkout rather than
  from serialized orchestrator-local paths.

## Queue Contract

- Trial jobs and verify jobs are separate queue classes under the active queue model.
- Workers consume trial queues only.
- Evaluators consume build/verify queues only.
- Re-evaluation and delayed verification are valid: queued verify work may be
  processed after the original CRS trial completes.

## Failure and Recovery Contract

- Redis unavailable: no job execution can proceed.
- Worker/evaluator disconnect: queued jobs remain available for later consumers.
- Missing evaluator: verify jobs accumulate until an evaluator or re-eval path drains them.
- Path mismatch on remote hosts: jobs fail unless host-local overrides or shared
  mounts make paths resolvable.
- Queue cleanup must be experiment-scoped in shared flat-queue deployments.
- Cloud worker timeout or bootstrap failure: orchestrator startup fails before
  trial enqueue, tears down the requested fleet, and surfaces per-instance
  readiness evidence.

## Benchmark CI Deployment Contract

- Benchmark CI submitters enqueue build/verify work to the shared CI queues.
- Evaluators in `--ci` mode consume those CI queues.
- Effective build/verify concurrency is owned by evaluator processes.

## Operational Guidance

For startup order, queue inspection commands, SSH tunnel examples, and
troubleshooting, use the user-facing guides:

- [Distributed Experiments](../../guides/experiments/distributed.md)
- [Benchmark CI Distributed Guide](../../guides/benchmark-ci/distributed.md)
