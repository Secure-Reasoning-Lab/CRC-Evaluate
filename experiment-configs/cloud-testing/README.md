# Cloud Smoke Test Configs

This directory holds operator-facing smoke-test configs for real cloud bring-up.

`gce-sanity-1orch-2worker.yaml` is the current GCE smoke config for the
remote-orchestrator flow:

- 1 orchestrator VM in `us-east5-b`
- 1 worker in `us-east5-b`
- 1 worker in `us-east1-b`
- `n2d-standard-16` everywhere

This config is for:

```bash
uv run crsbench cloud launch --config experiment-configs/cloud-testing/gce-sanity-1orch-2worker.yaml
```

It is not intended for:

```bash
uv run crsbench run --experiment-config experiment-configs/cloud-testing/gce-sanity-1orch-2worker.yaml
```

The checked-in `runtime.redis_host` value is only a placeholder for config
validation. In the remote-orchestrator flow, the orchestrator VM rewrites its
own config to `localhost:6379`, and workers are provisioned with the
orchestrator VM's internal Redis address.

## Preflight

Before running the smoke test:

1. Authenticate both GCP credential paths on the operator machine:

```bash
gcloud auth login
gcloud auth application-default login
gcloud config set project aixcc-426805
```

Expected result:

- `gcloud auth login` finishes in the browser and `gcloud auth list` shows your
  email as the active account
- `gcloud auth application-default login` completes without error
- `gcloud config set project aixcc-426805` prints `Updated property [core/project].`

2. Confirm required GCP pieces exist:

```bash
gcloud services list --enabled \
  --filter='config.name=compute.googleapis.com' \
  --format='value(config.name)'

gcloud iam service-accounts describe \
  153298433405-compute@developer.gserviceaccount.com \
  --project aixcc-426805 \
  --format='value(email)'
```

Expected result:

- the first command prints exactly `compute.googleapis.com`
- the second command prints exactly
  `153298433405-compute@developer.gserviceaccount.com`

3. Make sure the repo ref in the config is reachable from the VM install path.

The checked-in config currently uses:

- `crsbench_install_spec: git+https://github.com/sslab-gatech/CRSBench.git`
- `crsbench_git_ref: feat/gcp`

That only works if `feat/gcp` is publicly reachable on GitHub. If it is not,
either:

- push `feat/gcp` to the public repo, or
- switch the config to an authenticated `git+ssh://...` install plus
  `github_deploy_key_file`, or
- change `crsbench_git_ref` to an existing public tag or branch that contains
  the cloud-launch code you want to test

Expected result:

- public HTTPS path: a ref check should succeed instead of returning `404`
- private SSH path: a manual `git clone` / `git ls-remote` with the deploy key
  should succeed

4. Confirm quota is sufficient for this exact layout:

- `us-east5`: 32 `n2d` vCPUs
  because the orchestrator and one worker are both `n2d-standard-16`
- `us-east1`: 16 `n2d` vCPUs
  because one worker is `n2d-standard-16`

## Smoke Test Steps

Launch:

```bash
uv run crsbench cloud launch \
  --config experiment-configs/cloud-testing/gce-sanity-1orch-2worker.yaml
```

Expected result:

- exit code `0`
- log line similar to
  `Cloud launch complete: orchestrator=<name> redis=<internal-ip>:6379 workers=2`
- three VMs visible in GCP:
  one orchestrator, two workers

Watch status:

```bash
uv run crsbench cloud status gce-sanity-1orch-2worker \
  --config experiment-configs/cloud-testing/gce-sanity-1orch-2worker.yaml
```

Expected result:

- both workers eventually show as `ready`
- zones should include `us-east5-b` and `us-east1-b`
- job counts should move from queued/running to completed as the smoke run finishes

Watch recovery events:

```bash
uv run crsbench cloud events gce-sanity-1orch-2worker \
  --config experiment-configs/cloud-testing/gce-sanity-1orch-2worker.yaml
```

Expected result:

- often no output for a healthy smoke run
- if output appears, it should be informational recovery events rather than repeated worker bootstrap failures

Collect results after completion:

```bash
uv run crsbench cloud collect gce-sanity-1orch-2worker \
  --config experiment-configs/cloud-testing/gce-sanity-1orch-2worker.yaml \
  --remote-dir /tmp/crsbench/experiment-data/gce-sanity-1orch-2worker
```

Expected result:

- exit code `0`
- `Collection succeeded:` lines for the two workers and the orchestrator
- local experiment data appears under your configured experiment filestore

Tear everything down:

```bash
uv run crsbench cloud teardown gce-sanity-1orch-2worker \
  --config experiment-configs/cloud-testing/gce-sanity-1orch-2worker.yaml \
  --remote-dir /tmp/crsbench/experiment-data/gce-sanity-1orch-2worker \
  --force
```

Expected result:

- exit code `0`
- log line similar to
  `Teardown complete: 2 workers deleted and orchestrator deleted`
- `gcloud compute instances list` no longer shows the smoke-test VMs

## Current Readiness

Code path readiness:

- remote orchestrator launches Valkey on the orchestrator VM
- workers receive the orchestrator internal Redis address and Redis password
- `cloud collect` and `cloud teardown` can operate from persisted launch state

Operational blockers on this machine at the time of writing:

- Application Default Credentials were missing
- the checked-in GitHub ref `feat/gcp` was not publicly reachable through the
  configured `git+https` install path

Until those are fixed, the checked-in config is not runnable as-is from this
machine.
