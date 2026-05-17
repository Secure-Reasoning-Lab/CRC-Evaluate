# Deployment

CRSBench uses a queue-backed model: an **orchestrator** (`crsbench run`)
enqueues trial jobs to Valkey/Redis, **workers** (`crsbench worker`) execute
CRS trials, and an optional **evaluator** (`crsbench evaluator`) processes
build/verify queues. Pick the topology that matches your hardware.

## Single Machine

Orchestrator + worker on one host. The default for first-time users.

```bash
uv run python scripts/valkey-helper.py start
uv run crsbench worker --experiment-config config.yaml         # terminal 1
uv run crsbench run    --experiment-config config.yaml         # terminal 2
```

Add the evaluator only when you want real-time build/verify processing:

```bash
uv run crsbench evaluator --experiment-config config.yaml --jobs 4 --cores-per-job 4
```

Without an evaluator, verification jobs queue in Redis and can be drained later
via `crsbench re-eval` or a later evaluator run.

## Multi-Machine

The orchestrator (and Valkey) runs on one host; workers run on additional
hosts. Start Valkey with password auth, share `.env` across hosts, and let each
worker connect to the orchestrator's Redis:

**Machine A** (orchestrator + Valkey):

```bash
uv run python scripts/valkey-helper.py --password start
uv run crsbench worker --experiment-config config.yaml --cpuset 0-111
uv run crsbench run    --experiment-config config.yaml
```

**Machine B..N** (remote workers): copy `.env`, set `CRSBENCH_REDIS_HOST` to
Machine A, then:

```bash
scp user@machine-a:/path/to/CRSBench/.env /path/to/CRSBench/.env
uv run crsbench worker --experiment-config config.yaml
```

When worker hosts mount benchmarks at different paths, set
`worker.benchmarks_root` in the experiment config to the machine-local path.
CPU placement is operator-side via `--cpuset` / `--skip-cpuset` on the worker
and evaluator commands.

SSH-tunnel alternative, password-auth helpers, queue cleanup behavior, and
re-evaluation: [Distributed Experiments](../deployment/distributed.md).

## Managed Cloud (GCE)

For fleets that should not be operator-managed by hand, declare the fleet in
the experiment config under `cloud.providers.gce` and use the `cloud`
subcommands. CRSBench provisions the orchestrator, workers, and evaluators on
GCE, runs the experiment, and tears the fleet down on completion.

```yaml
cloud:
  providers:
    gce:
      project: example-project
      ssh_via_iap: true
      profile_defaults:
        machine_type: n2d-standard-16
        boot_disk_size_gb: 200
        image: projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64
        service_account_email: crsbench-worker@example-project.iam.gserviceaccount.com
        readiness_timeout_sec: 900
      instance_profiles:
        gce-worker-n2d: {}
  orchestrator:
    zone: us-east5-b
    instance_profile: gce-worker-n2d
  workers:
    defaults:
      instance_profile: gce-worker-n2d
      count: 1
    placements:
      - zone: us-east5-b
        count: 3
```

Lifecycle:

```bash
uv run crsbench cloud preflight --config config.yaml   # validate, no spend
uv run crsbench cloud launch    --config config.yaml
uv run crsbench cloud monitor   --config config.yaml
uv run crsbench cloud collect   --config config.yaml
uv run crsbench cloud teardown  --config config.yaml
```

Regional placement, capacity fallback, OS Login / IAP, env sharding across
worker groups, and per-instance bootstrap evidence:
[GCE Cloud Orchestration](../deployment/gce-cloud-orchestration.md). To rehearse
the cloud bootstrap on a local VM, see
[Local Cloud Rehearsal](../deployment/local-cloud-rehearsal.md).

## Queue and Recovery

Clean queues before re-running an experiment with the same name:

```bash
uv run crsbench queue clean --experiment <experiment-name> --yes
```

`crsbench run` on a TTY prompts `fresh` / `continue` / `quit` when it finds
existing jobs. CI / non-TTY defaults to scoped `continue`. Retry failed jobs
only with explicit opt-in:

```bash
uv run crsbench run --experiment-config config.yaml --queue-mode continue --retry-failed
```

Queue model details, orphaned-job handling, and Valkey administration:
[Queue and Recovery](../operations/queue-and-recovery.md).
