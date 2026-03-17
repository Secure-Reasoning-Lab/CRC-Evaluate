# Local Cloud Rehearsal

Use this flow to exercise the cloud startup scripts locally before launching real
VMs.

The rehearsal runs:

- one orchestrator container
- two worker containers
- the same `crsbench/cloud/gce/startup/*.sh` scripts used on GCE
- an Ubuntu 24.04 DinD-based image so the bootstrap path can run Docker-backed
  CRS prep against a base that matches the default GCE VM family more closely

## What It Proves

- startup metadata is decoded correctly
- checkout-first bootstrap works from a real repo clone
- orchestrator and worker bootstrap can run in forced foreground mode inside the
  Docker harness under the non-root `crsbench` user
- the startup scripts work with file-backed metadata, not only the GCE metadata
  endpoint
- the orchestrator binds Valkey on loopback plus the discovered container IP, so
  workers can still reach `orchestrator:6379` without exposing `0.0.0.0`

## Quickstart

From the repo root:

```bash
scripts/cloud-rehearsal/run-local-rehearsal.sh
```

The helper script:

1. renders file-backed metadata trees under `.crsbench-local-rehearsal/`
2. builds the DinD rehearsal image
3. starts `docker compose` for one orchestrator plus two workers

Useful follow-up commands:

```bash
scripts/cloud-rehearsal/run-local-rehearsal.sh ps
scripts/cloud-rehearsal/run-local-rehearsal.sh logs -f orchestrator
scripts/cloud-rehearsal/run-local-rehearsal.sh logs -f worker-1
scripts/cloud-rehearsal/run-local-rehearsal.sh down -v
```

## Inputs

Default rehearsal config:

- [`scripts/cloud-rehearsal/local-experiment.yaml`](../../../scripts/cloud-rehearsal/local-experiment.yaml)

Default topology:

- orchestrator service name: `orchestrator`
- worker compose services: `worker-1`, `worker-2`
- worker runtime names reported to CRSBench: `local-worker-1`, `local-worker-2`
- repo mount inside containers: `/src/CRSBench`

Override knobs:

- `CRSBENCH_LOCAL_REHEARSAL_EXPERIMENT_CONFIG`
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
  - set for both orchestrator and workers; skips `systemd` and `exec`s the
    launcher directly as `crsbench`

The scripts still keep the VM behavior by default:

- GCE metadata remains the fallback when `CRSBENCH_METADATA_ROOT_DIR` is unset
- `systemd --user` under `crsbench` remains the preferred VM runtime when it is
  available

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
