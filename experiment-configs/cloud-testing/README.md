# Cloud Smoke Test Configs

This directory holds operator-facing smoke-test configs for real cloud bring-up.

`gce-sanity-1orch-2worker-1eval.yaml` is the current GCE smoke config for the
remote-orchestrator flow:

- 1 orchestrator VM in `us-east5-b`
- 1 worker in `us-east5-b`
- 1 worker in `us-east1-b`
- 1 evaluator in `us-east5-b`
- `n2d-standard-16` everywhere

The checked-in experiment identifier inside that file is
`gce-sanity-1o2w1e` so the generated GCE instance names stay under the
63-character limit using compact role suffixes: `orch`, `work`, and `eval`.

This config is for:

```bash
uv run crsbench cloud launch --config experiment-configs/cloud-testing/gce-sanity-1orch-2worker-1eval.yaml
```

It is not intended for:

```bash
uv run crsbench run --experiment-config experiment-configs/cloud-testing/gce-sanity-1orch-2worker-1eval.yaml
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
- all three GCE instance profiles use `readiness_timeout_sec: 1200` so cold-image
  bootstrap and Redis startup have a 20-minute bring-up window

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
- if the main CRSBench repository is private, the deploy key can read that
  repository

The VM bootstrap uses that deploy key only for the top-level CRSBench clone.
Submodules still use their declared URLs. In this repository, the public
`oss-crs` submodule stays on HTTPS and does not need the deploy key.

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
token before launch and point the config at `cloud.env.HF_TOKEN:
os.environ/HF_TOKEN`.

For the checked-in smoke config, `HF_TOKEN` is still required because launch
preflight resolves `cloud.env.HF_TOKEN` before provisioning even though
`sanity` auto-skips the VM-side download step.

If you launch through the normal CRSBench CLI, this can come from either:

- your shell environment
- a `.env` file loaded by CRSBench before launch preflight resolves
  `os.environ/...` references

Expected result:

- `echo "$HF_TOKEN"` prints a non-empty token when you intend to use gated
  datasets
- the token is not written back into `.crsbench-cloud/*.json`

If your CRS/LLM path also needs operator-side env vars on the remote VMs,
declare them in `cloud.env` using the same `os.environ/...` references that
launch preflight already resolves. Example:

```yaml
cloud:
  env:
    CRSBENCH_LLM_UPSTREAM_BASE_URL: os.environ/CRSBENCH_LLM_UPSTREAM_BASE_URL
    CRSBENCH_LLM_MASTER_KEY: os.environ/CRSBENCH_LLM_MASTER_KEY
    OPENAI_API_KEY: os.environ/OPENAI_API_KEY
    HF_TOKEN: os.environ/HF_TOKEN
```

Expected result:

- missing or empty configured env vars fail launch before any VM is created
- runtime-managed vars such as `CRSBENCH_REDIS_HOST` are rejected in config validation

The checked-in `gce-sanity-1orch-2worker-1eval.yaml` now includes:

- `CRSBENCH_LLM_UPSTREAM_BASE_URL`
- `CRSBENCH_LLM_MASTER_KEY`
- `HF_TOKEN`
- `worker.jobs: 1` so each VM runs one trial job at a time
- `evaluator.jobs: 1` so the evaluator runs one job at a time
- `runtime.build_timeout: 3600` to leave more headroom for cold-cloud prepare
  and build phases

So the operator must export those before launch. Using a `.env` file is also
fine when you launch through the CRSBench CLI.

4. Confirm quota is sufficient for this exact layout:

- `us-east5`: 48 `n2d` vCPUs
  because the orchestrator, one worker, and one evaluator are all `n2d-standard-16`
- `us-east1`: 16 `n2d` vCPUs
  because one worker is `n2d-standard-16`

## Smoke Test Steps

Launch:

```bash
uv run crsbench cloud launch \
  --config experiment-configs/cloud-testing/gce-sanity-1orch-2worker-1eval.yaml
```

Expected result:

- exit code `0`
- log line showing the orchestrator name and Redis internal address
- four VMs visible in GCP:
  one orchestrator, two workers, one evaluator

Watch status:

```bash
uv run crsbench cloud status gce-sanity-1o2w1e \
  --config experiment-configs/cloud-testing/gce-sanity-1orch-2worker-1eval.yaml
```

Expected result:

- both workers and the evaluator eventually show as `ready`
- zones should include `us-east5-b` and `us-east1-b`
- `ready` only happens after checkout, `crsbench prepare`, the skipped-or-run
  download step, worker/evaluator-side Redis polling, and queue-listener startup complete
- job counts should move from queued/running to completed as the smoke run finishes

Watch recovery events:

```bash
uv run crsbench cloud events gce-sanity-1o2w1e \
  --config experiment-configs/cloud-testing/gce-sanity-1orch-2worker-1eval.yaml
```

Expected result:

- often no output for a healthy smoke run
- if output appears, it should be informational recovery events rather than repeated worker bootstrap failures

Collect results after completion:

```bash
uv run crsbench cloud collect gce-sanity-1o2w1e \
  --config experiment-configs/cloud-testing/gce-sanity-1orch-2worker-1eval.yaml \
  --remote-dir /tmp/crsbench/experiment-data/gce-sanity-1o2w1e
```

Expected result:

- exit code `0`
- `Collection succeeded:` lines for the two workers, the evaluator, and the orchestrator
- local experiment data appears under your configured experiment filestore

Tear everything down:

```bash
uv run crsbench cloud teardown gce-sanity-1o2w1e \
  --config experiment-configs/cloud-testing/gce-sanity-1orch-2worker-1eval.yaml \
  --remote-dir /tmp/crsbench/experiment-data/gce-sanity-1o2w1e \
  --force
```

Expected result:

- exit code `0`
- log line similar to
  `Teardown complete: 2 workers deleted, 1 evaluator deleted, and orchestrator deleted`
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
