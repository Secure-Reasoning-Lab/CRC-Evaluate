# Local Cloud Rehearsal

Use this flow to exercise the cloud startup scripts locally before launching real
VMs.

The cloud config shape is provider-neutral, but this rehearsal harness exercises
the current managed backend implementation: GCE startup scripts and metadata
layout.

The rehearsal runs:

- one orchestrator container
- two worker containers
- one evaluator container
- the same `crsbench/cloud/gce/startup/*.sh` scripts used on GCE
- an Ubuntu 24.04 DinD-based image so the bootstrap path can run Docker-backed
  CRS prep against a base that matches the default GCE VM family more closely

## What It Proves

- startup metadata is decoded correctly
- checkout-first bootstrap works from a real repo clone
- orchestrator, worker, and evaluator bootstrap can run in forced foreground
  mode inside the Docker harness under the non-root `crsbench` user
- the startup scripts work with file-backed metadata, not only the GCE metadata
  endpoint
- provider-neutral cloud env layering resolves the same way it does for a real
  managed launch
- the orchestrator binds Valkey on loopback plus the discovered container IP, so
  workers/evaluators can still reach `orchestrator:6379` without exposing
  `0.0.0.0`

## Notification Rehearsal

Use the cloud notification rehearsal to validate that the checked-in
`CRSBENCH_NOTIFY_APPRISE_URLS` `cloud.env` passthrough reaches the orchestrator
runtime before a managed-style launch.

```bash
export CRSBENCH_NOTIFY_APPRISE_URLS='discord://token/chat-id'
scripts/cloud-rehearsal/test-notification-rehearsal.sh
scripts/cloud-rehearsal/test-notification-rehearsal.sh --send
```

Notes:

- default mode is dry-run
- this checks cloud-env injection into the orchestrator container only; it does
  not imply workers or evaluators send notifications in this flow
- `scripts/test_notification.py` on its own validates a non-cloud operator
  environment, whether inherited from the local shell or loaded from `.env`
  unless `--no-dotenv` is used

## Quickstart

From the repo root:

```bash
scripts/cloud-rehearsal/run-local-rehearsal.sh
```

The helper script:

1. renders file-backed metadata trees under `.crsbench-local-rehearsal/`
2. builds the DinD rehearsal image
3. starts `docker compose` for one orchestrator plus two workers plus one evaluator

Useful follow-up commands:

```bash
scripts/cloud-rehearsal/run-local-rehearsal.sh ps
scripts/cloud-rehearsal/run-local-rehearsal.sh logs -f orchestrator
scripts/cloud-rehearsal/run-local-rehearsal.sh logs -f worker-1
scripts/cloud-rehearsal/run-local-rehearsal.sh logs -f evaluator-1
scripts/cloud-rehearsal/run-local-rehearsal.sh down -v
```

## Inputs

Default rehearsal config:

- [`scripts/cloud-rehearsal/local-experiment.yaml`](../../../scripts/cloud-rehearsal/local-experiment.yaml)
  - mirrors the checked-in GCE smoke path with `crs-libfuzzer` and the same
    longer build window used for cloud testing
- [`scripts/cloud-rehearsal/local-experiment-sanity-always.yaml`](../../../scripts/cloud-rehearsal/local-experiment-sanity-always.yaml)
  - forces `download_benchmarks: always` for the `sanity` suite so the startup
    scripts rehearse the explicit-download override
- [`scripts/cloud-rehearsal/local-experiment-hf-download.yaml`](../../../scripts/cloud-rehearsal/local-experiment-hf-download.yaml)
  - uses the 3-benchmark
    [`smoke-test-bug-finding-hf-download`](../../../benchmark-suites/smoke-test-bug-finding-hf-download.yaml)
    suite with `download_benchmarks: auto` plus `cloud.env.HF_TOKEN` to rehearse
    gated Hugging Face benchmark download
- the checked-in cloud rehearsal configs also set `cloud.remote.experiment_root`
  explicitly so remote collect/teardown inference stays aligned with the
  container-side experiment tree

Default topology:

- orchestrator service name: `orchestrator`
- worker compose services: `worker-1`, `worker-2`
- worker runtime names reported to CRSBench: `local-worker-1`, `local-worker-2`
- evaluator compose service: `evaluator-1`
- evaluator runtime name reported to CRSBench: `local-evaluator-1`
- repo mount inside containers: `/src/CRSBench`

Override knobs:

- `CRSBENCH_LOCAL_REHEARSAL_EXPERIMENT_CONFIG`
- `HF_TOKEN`
  - required when the selected config declares `cloud.env.HF_TOKEN`, such as the
    non-`sanity` download smoke config above
- `CRSBENCH_LOCAL_REHEARSAL_GIT_REF`
  - defaults to the current local `HEAD`; set it explicitly to bypass host-side
    `HEAD` autodetection or pin a different ref that already exists in the
    mounted checkout. The rehearsal still requires that checkout to be a
    clonable Git repository or worktree.
- `CRSBENCH_LOCAL_REHEARSAL_STATE_DIR`

## Startup Script Overrides Used By The Harness

The local rehearsal depends on these startup-script env overrides:

- `CRSBENCH_METADATA_ROOT_DIR`
  - reads metadata from mounted files instead of `metadata.google.internal`
- `CRSBENCH_SERVICE_MANAGER=foreground`
  - set for orchestrator, workers, and evaluator; skips `systemd` and `exec`s
    the launcher directly as `crsbench`
- `CRSBENCH_STARTUP_MODE=evaluator`
  - set only on the evaluator compose service so the shared `worker.sh`
    bootstrap launches `crsbench evaluator`

The scripts still keep the VM behavior by default:

- GCE metadata remains the fallback when `CRSBENCH_METADATA_ROOT_DIR` is unset
- `systemd --user` under `crsbench` remains the preferred VM runtime when it is
  available
- when a rehearsal config includes provider-neutral `cloud.env`, the harness now
  resolves the same secret references on the operator side and injects the
  resolved values into the file-backed metadata tree before container startup

## Notes

- The compose topology is a local rehearsal harness, not a replacement for real
  cloud launch/collect/teardown commands.
- The DinD image is there so the bootstrap path has a real Docker daemon inside
  each rehearsal container. The current Dockerfile uses
  `cruizba/ubuntu-dind:noble-latest` to stay on Ubuntu 24.04 like the GCE
  startup target.
- `oss-crs` still expects privileged Docker behavior. Keep the compose services
  privileged if you want the local rehearsal to match the VM bootstrap
  assumptions.
