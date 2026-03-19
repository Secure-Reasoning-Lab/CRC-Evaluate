# Cloud Smoke Test Configs

This directory holds operator-facing smoke-test configs for real cloud bring-up.

This directory currently ships four GCE smoke configs for the
remote-orchestrator flow:

- `gce-sanity-1orch-2worker-1eval.yaml`
  - uses `crs-libfuzzer`
  - checked-in experiment identifier: `gce-sanity-1o2w1e`
- `gce-sanity-1orch-2worker-1eval-multilang-given-fuzzer.yaml`
  - uses `atlantis-multilang-given_fuzzer`
  - checked-in experiment identifier: `gce-sanity-mgf-1o2w1e`
- `gce-hf-download-1orch-2worker-1eval.yaml`
  - uses `crs-libfuzzer`
  - benchmark suite: `smoke-test-bug-finding-hf-download`
  - checked-in experiment identifier: `gce-hf-download-1o2w1e`
- `gce-sanity-zone-fallback-1orch-1worker-1eval.yaml`
  - uses `crs-libfuzzer`
  - benchmark suite: `sanity`
  - exercises provider-level ordered zones plus placement-level `fallback: false`
  - checked-in experiment identifier: `gce-sanity-fallback-1o1w1e`

The first three samples use the same fixed-topology GCE layout:

- 1 orchestrator VM in `us-east5-b`
- 1 worker in `us-east5-b`
- 1 worker in `us-east1-b`
- 1 evaluator in `us-east5-b`
- `n2d-standard-16` everywhere

The fallback sample uses:

- provider-level `zones: [us-east5-b, us-east1-b]`
- provider-level `fallback: true`
- 1 orchestrator that inherits the provider zone order
- 1 worker that inherits the provider zone order
- 1 evaluator pinned to `us-east1-b` with `fallback: false`

All checked-in experiment identifiers stay under the 63-character GCE name
limit by using compact role suffixes: `orch`, `work`, and `eval`. Generated
instance names are zone-independent, for example
`crsbench-gce-sanity-fallback-1o1w1e-work-001`.

The command examples below use the `crs-libfuzzer` sanity sample config. For the
Atlantis preset, replace:

- the config path with
  `experiment-configs/cloud-testing/gce-sanity-1orch-2worker-1eval-multilang-given-fuzzer.yaml`
- the experiment name `gce-sanity-1o2w1e` with `gce-sanity-mgf-1o2w1e`
- the remote dir `/tmp/crsbench/experiment-data/gce-sanity-1o2w1e` with
  `/tmp/crsbench/experiment-data/gce-sanity-mgf-1o2w1e`

For the HF-download preset, replace:

- the config path with
  `experiment-configs/cloud-testing/gce-hf-download-1orch-2worker-1eval.yaml`
- the experiment name `gce-sanity-1o2w1e` with `gce-hf-download-1o2w1e`
- the remote dir `/tmp/crsbench/experiment-data/gce-sanity-1o2w1e` with
  `/tmp/crsbench/experiment-data/gce-hf-download-1o2w1e`

For the fallback preset, replace:

- the config path with
  `experiment-configs/cloud-testing/gce-sanity-zone-fallback-1orch-1worker-1eval.yaml`
- the experiment name `gce-sanity-1o2w1e` with `gce-sanity-fallback-1o1w1e`
- the remote dir `/tmp/crsbench/experiment-data/gce-sanity-1o2w1e` with
  `/tmp/crsbench/experiment-data/gce-sanity-fallback-1o1w1e`

These configs are for:

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

These smoke configs use the checkout-first cloud bootstrap path:

- cloud VMs clone CRSBench from `crsbench_install_spec`
- every VM runs `crsbench prepare`
- `download_benchmarks: auto` is set for all four samples
- the three `benchmark_suite: sanity` samples skip the VM-side benchmark download
- the `gce-hf-download-1orch-2worker-1eval.yaml` sample runs the VM-side
  Hugging Face dataset download for
  `benchmark_suite: smoke-test-bug-finding-hf-download`
- `cloud.defaults.readiness_timeout_sec: 1200` gives the whole fleet a
  20-minute bring-up window for cold-image bootstrap and Redis startup

The fallback sample is the reference when you want CRSBench to try zones in
order at runtime. If `us-east5-b` is exhausted, the orchestrator and inherited
worker placement retry `us-east1-b`. The evaluator does not retry because its
placement sets `fallback: false`.

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

- `cloud.defaults.crsbench_install_spec: git+ssh://git@github.com/sslab-gatech/CRSBench.git`
- `cloud.defaults.crsbench_git_ref: feat/gcp`
- `cloud.defaults.github_deploy_key_path: .crsbench-keys/crsbench-deploy`

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
cloud:
  defaults:
    crsbench_install_spec: "git+ssh://git@github.com/sslab-gatech/CRSBench.git"
    github_deploy_key_path: .crsbench-keys/crsbench-deploy
```

Relative deploy-key paths resolve from the directory where you run the launch
command, so run the smoke test from the repo root if you keep the checked-in
relative path.

For the checked-in `gce-hf-download-1orch-2worker-1eval.yaml` `crs-libfuzzer`
sample, `HF_TOKEN` is required because the worker bootstrap performs a real
VM-side Hugging Face download for the
`smoke-test-bug-finding-hf-download` benchmark suite. The Atlantis
`gce-sanity-1orch-2worker-1eval-multilang-given-fuzzer.yaml` sample leaves
`cloud.env` empty because that CRS does not need LLM credentials and the
checked-in `sanity` bring-up skips VM-side benchmark download.

The checked-in `gce-sanity-1orch-2worker-1eval.yaml` sample still declares
`HF_TOKEN`, so launch preflight resolves it before provisioning even though
that `sanity` run auto-skips the VM-side download step.

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

The checked-in `gce-sanity-1orch-2worker-1eval.yaml` `crs-libfuzzer` sanity
sample includes:

- `CRSBENCH_LLM_UPSTREAM_BASE_URL`
- `CRSBENCH_LLM_MASTER_KEY`
- `HF_TOKEN`
- `worker.jobs: 1` so each VM runs one trial job at a time
- `evaluator.jobs: 1` so the evaluator runs one job at a time
- `runtime.build_timeout: 3600` to leave more headroom for cold-cloud prepare
  and build phases

So the operator must export those before launch. Using a `.env` file is also
fine when you launch through the CRSBench CLI.

The Atlantis
`gce-sanity-1orch-2worker-1eval-multilang-given-fuzzer.yaml` sample does not
declare any `cloud.env` secrets, so it does not require `CRSBENCH_LLM_*` or
`HF_TOKEN` exports for launch preflight.

The `gce-hf-download-1orch-2worker-1eval.yaml` sample declares only:

- `HF_TOKEN`

That keeps the operator-side contract focused on the VM-side dataset-download
path this sample is intended to validate.

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
