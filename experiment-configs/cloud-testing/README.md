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

This smoke config uses the checkout-first cloud bootstrap path:

- cloud VMs clone CRSBench from `crsbench_install_spec`
- every VM runs `crsbench prepare`
- `download_benchmarks: auto` is set, so this `benchmark_suite: sanity` run
  skips the VM-side benchmark download

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

3. Make sure the private repo install path in the config is usable from the VM bootstrap.

The checked-in config currently uses:

- `crsbench_install_spec: git+ssh://git@github.com/sslab-gatech/CRSBench.git`
- `crsbench_git_ref: feat/gcp`
- `github_deploy_key_file: file:.crsbench-keys/crsbench-deploy`

That only works if:

- `.crsbench-keys/crsbench-deploy` exists on this machine
- the matching public key was added under GitHub `Settings -> Deploy keys`
- `feat/gcp` exists in the repository the VMs will clone

Expected result:

- a manual `git clone` / `git ls-remote` with the deploy key succeeds

Generate the deploy key if you have not already:

```bash
uv run crsbench cloud keygen
```

Expected result:

- `.crsbench-keys/crsbench-deploy` and `.crsbench-keys/crsbench-deploy.pub`
  exist locally
- the command prints the public key you need to add under GitHub
  `Settings -> Deploy keys`

The checked-in config already points at that private key:

```yaml
crsbench_install_spec: "git+ssh://git@github.com/sslab-gatech/CRSBench.git"
github_deploy_key_file: file:.crsbench-keys/crsbench-deploy
```

`file:` paths resolve relative to the directory where you run the launch
command, so run the smoke test from the repo root if you keep the checked-in
relative path.

Optional: if your dataset access requires Hugging Face credentials, export a
token before launch and point the config at `hf_token: os.environ/HF_TOKEN`.

For the checked-in smoke config, `HF_TOKEN` is still required because launch
preflight resolves that env secret before provisioning even though `sanity`
auto-skips the VM-side download step.

Expected result:

- `echo "$HF_TOKEN"` prints a non-empty token when you intend to use gated
  datasets
- the token is not written back into `.crsbench-cloud/*.json`

If your CRS/LLM path also needs operator-side env vars on the remote VMs,
declare them in `cloud.bootstrap.env_passthrough`. Example:

```yaml
cloud:
  bootstrap:
    env_passthrough:
      common:
        - CRSBENCH_LLM_UPSTREAM_BASE_URL
      orchestrator:
        - CRSBENCH_LLM_MASTER_KEY
      workers:
        - OPENAI_API_KEY
```

Expected result:

- missing or empty configured env vars fail launch before any VM is created
- runtime-managed vars such as `CRSBENCH_REDIS_HOST` are rejected in config validation

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
- `ready` only happens after checkout, `crsbench prepare`, the skipped-or-run
  download step, and Redis queue-listener startup complete
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
