# GCE Cloud Orchestration

Guide for provisioning and managing remote orchestrator plus worker VMs for
CRSBench experiments on GCE.

For a local preflight of the same startup scripts before touching GCE, use
[Local Cloud Rehearsal](./local-cloud-rehearsal.md).

## Prerequisites

1. **GCP project** with Compute Engine API enabled
2. **gcloud CLI** authenticated:
   - `gcloud auth login` for operator CLI use
   - `gcloud auth application-default login` for CRSBench's Python GCE client
3. **Service account** for workers with minimal permissions:
   - `roles/logging.logWriter` (optional, for Cloud Logging)
   - Access to Redis host (firewall rules or VPC)
   - Access to any shared storage mounts
4. **OS Login** available for operator SSH access. Project-level OS Login
   metadata is a common setup (`gcloud compute project-info add-metadata
   --metadata enable-oslogin=TRUE`), and CRSBench also enables OS Login on the
   VMs it provisions.
5. **IAP** configured if using `ssh_via_iap: true`:
   - firewall rule allowing TCP port 22 from IAP range `35.235.240.0/20`
   - operator IAM permissions to open IAP TCP tunnels and log in over SSH
6. **Redis/Valkey** reachable from worker VMs
7. **rsync** installed on the operator machine (for artifact collection)

## Configuration

Declare provider-native GCE details under `cloud.providers.gce`, then reference
those instance profiles from `cloud.orchestrator` and `cloud.workers.placements`:

```yaml
cloud:
  bootstrap:
    prepare_mode: full
    download_benchmarks: auto
  providers:
    gce:
      project: my-gcp-project
      ssh_via_iap: true
      instance_profiles:
        orchestrator-n2d:
          machine_type: n2d-standard-16
          boot_disk_size_gb: 50
          image: projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64
          service_account_email: crsbench-orchestrator@my-gcp-project.iam.gserviceaccount.com
          owner_label: my-team
          readiness_timeout_sec: 900
          crsbench_install_spec: "git+https://github.com/your-org/CRSBench.git"
        worker-n2d:
          machine_type: n2d-standard-16
          boot_disk_size_gb: 50
          image: projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64
          service_account_email: crsbench-worker@my-gcp-project.iam.gserviceaccount.com
          owner_label: my-team
          readiness_timeout_sec: 900
          crsbench_install_spec: "git+https://github.com/your-org/CRSBench.git"
  orchestrator:
    provider: gce
    zone: us-east5-b
    instance_profile: orchestrator-n2d
  workers:
    placements:
      - provider: gce
        zone: us-east5-b
        worker_count: 3
        instance_profile: worker-n2d
      - provider: gce
        zone: us-east1-b
        worker_count: 1
        instance_profile: worker-n2d
```

### Configuration Fields

| Field | Required | Description |
|---|---|---|
| `cloud.providers.gce.project` | yes | GCP project ID used for all referenced GCE resources |
| `cloud.providers.gce.instance_profiles.<name>` | yes | Reusable machine/image/service-account bundle for orchestrator or workers |
| `cloud.orchestrator.provider` | yes | Provider for the remote orchestrator VM (`gce` in v1) |
| `cloud.orchestrator.zone` | yes | Explicit orchestrator zone |
| `cloud.orchestrator.instance_profile` | yes | Instance profile name for the orchestrator VM |
| `cloud.workers.placements[].zone` | yes | Explicit worker placement zone (zone selectors only in v1) |
| `cloud.workers.placements[].worker_count` | no | Number of workers to create in that placement |
| `cloud.workers.placements[].instance_profile` | yes | Instance profile name for that placement |

Instance profiles carry the per-VM details such as `machine_type`,
`boot_disk_size_gb`, `image` or `instance_template`, `service_account_email`,
`owner_label`, `labels`, `metadata`, `ssh_via_iap`, `readiness_timeout_sec`,
`crsbench_install_spec`, `crsbench_git_ref`, `github_deploy_key_file`, and
`hf_token`.

### Bootstrap Policy

`cloud.bootstrap` controls the VM bootstrap steps that run before a worker or
remote orchestrator is counted as ready:

- `prepare_mode: full | skip_base_images`
- `download_benchmarks: auto | always | never`

Cloud VMs always run `crsbench prepare`. `download_benchmarks: auto` skips the
VM-side download only when `benchmark_suite: sanity`; other suites download
before the worker joins Redis.

### Using Instance Templates

Instead of specifying `image` + `machine_type` + `boot_disk_size_gb`, you can reference a pre-configured instance template:

```yaml
cloud:
  providers:
    gce:
      project: my-gcp-project
      ssh_via_iap: true
      instance_profiles:
        worker-template:
          instance_template: projects/my-gcp-project/global/instanceTemplates/crsbench-worker-v1
          service_account_email: crsbench-worker@my-gcp-project.iam.gserviceaccount.com
          owner_label: my-team
  workers:
    placements:
      - provider: gce
        zone: us-central1-a
        worker_count: 8
        instance_profile: worker-template
```

## Private Repository & Dataset Access

Worker VMs need to install CRSBench from source and optionally download
benchmarks from HuggingFace. When the repo or dataset is private, the
provisioner injects credentials via GCE instance metadata so the startup
script can authenticate automatically.

These credential fields stay supported even when you use a public CRSBench
repository or a public dataset mirror, because downstream adopters may still
need private forks or gated datasets.

Cloud secret-bearing fields accept three forms at launch time:

- literal values
- `os.environ/NAME`
- `file:relative/or/absolute/path`

`file:` paths resolve relative to the operator command's current working
directory when they are not absolute.

Cloud orchestration requires `crsbench_install_spec` to use a `git+...`
checkout source. The VM bootstrap clones CRSBench into `/opt/crsbench`,
changes into that checkout, runs `crsbench prepare`, optionally downloads
benchmarks, and only then starts the orchestrator or worker runtime.

If remote VMs need API keys or upstream URLs from the operator environment,
configure `cloud.bootstrap.env_passthrough`:

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

Semantics:

- `common` is copied to both the orchestrator VM and all worker VMs
- `orchestrator` adds orchestrator-only variables
- `workers` adds worker-only variables
- values are resolved from the operator environment before provisioning
- when you launch through the CRSBench CLI, `.env` is loaded first, so
  `os.environ/...` references and `env_passthrough` can come from either the
  shell environment or `.env`
- missing or empty configured variables fail launch before any VM is created
- runtime-managed variables such as `CRSBENCH_REDIS_HOST` and
  `CRSBENCH_REDIS_PASSWORD` are rejected and must not be passed through

### Generate a deploy key

```bash
uv run crsbench cloud keygen
```

This generates an ed25519 SSH key pair in `.crsbench-keys/` and prints the
public key. Add it to your GitHub repository:

1. Go to the repo **Settings > Deploy keys > Add deploy key**
2. Paste the public key (read-only access is sufficient)

### Configure the fleet

```yaml
cloud:
  providers:
    gce:
      instance_profiles:
        orchestrator-n2d:
          # Install CRSBench from a public repo via git clone + uv sync
          crsbench_install_spec: "git+https://github.com/your-org/CRSBench.git"

          # For a private repo, switch to git+ssh://... and provide a deploy key:
          # crsbench_install_spec: "git+ssh://git@github.com/your-org/CRSBench.git"
          # github_deploy_key_file: file:.crsbench-keys/crsbench-deploy

        worker-n2d:
          # Install CRSBench from a public repo via git clone + uv sync
          crsbench_install_spec: "git+https://github.com/your-org/CRSBench.git"

          # For a private repo, switch to git+ssh://... and provide a deploy key:
          # crsbench_install_spec: "git+ssh://git@github.com/your-org/CRSBench.git"
          # github_deploy_key_file: file:.crsbench-keys/crsbench-deploy

          # HuggingFace token for gated dataset downloads (optional)
          hf_token: os.environ/HF_TOKEN
```

If you switch to the private `git+ssh` path, run:

```bash
uv run crsbench cloud keygen
```

Expected result:

- `.crsbench-keys/crsbench-deploy` and `.crsbench-keys/crsbench-deploy.pub`
  exist locally
- the command prints the public key to add under GitHub
  `Settings -> Deploy keys`

Add the public key to the repository that the VMs will clone. Read-only access
is sufficient for smoke testing.

When `github_deploy_key_file` is set, the provisioner reads the private key
file at provision time, base64-encodes it, and sets it as
`crsbench-github-deploy-key` instance metadata. The startup script writes it to
`/root/.ssh/id_ed25519` and uses it only for the top-level CRSBench clone.
Submodules continue to use the URLs declared in `.gitmodules`, so public
submodules such as `oss-crs` can stay on HTTPS and do not require the deploy
key. The `hf_token` is
passed as `crsbench-hf-token` metadata and exported as `HF_TOKEN` in the worker
environment. Secret references are resolved once on the operator before VM
creation; the original experiment config payload sent to the remote orchestrator
is not rewritten with resolved secret values. `cloud.bootstrap.env_passthrough`
uses the same operator-side resolution rule, but copies named environment
variables instead of inline secret refs.

## Launching an Experiment

### Local Orchestrator + GCE Workers

When you run `crsbench run --experiment-config ...`, CRSBench can provision the
declared `cloud.workers.placements` from the local machine:

```bash
uv run crsbench run --experiment-config config.yaml
```

The local orchestrator will:

1. Validate live quota for the requested worker placements
2. Create the requested worker VMs across the configured zones
3. Wait for each VM to bootstrap and report `ready`
4. Enqueue trial jobs only after the full fleet is ready
5. If any VM fails to become ready, tear down the entire fleet and exit with an error

### Remote Orchestrator + GCE Workers

When you use `cloud launch`, the local operator machine provisions the
orchestrator VM and the worker placements declared in the same config:

```bash
uv run crsbench cloud launch --config config.yaml
```

This path:

1. Validates live GCE quota for the orchestrator zone plus all worker placement regions
2. Provisions one orchestrator VM
3. Waits for the orchestrator VM to have an internal address
4. Provisions workers across all `cloud.workers.placements`, passing the orchestrator Redis host/password
5. Lets the remote orchestrator VM clone CRSBench, run `crsbench prepare`, optionally download benchmarks, start Valkey, rewrite the experiment config to use local Redis, wait for the pre-provisioned workers to report ready, and run `crsbench run`

`cloud launch` persists local launch state next to the config file under
`.crsbench-cloud/<experiment>.json`. Later `cloud status`, `cloud collect`, and
`cloud teardown` commands reuse that state automatically. `cloud status` and
`cloud events` still reconnect to the remote orchestrator's Redis; `cloud collect`
and `cloud teardown` can fall back to the persisted VM inventory if Redis is
unavailable.

Bootstrap failures are reported with per-instance evidence, so you can
diagnose issues without SSH-ing into VMs.

`ready` means the whole VM bootstrap finished, not just that GCE reported the
instance as running. Size `readiness_timeout_sec` for package install, repo
checkout, `crsbench prepare`, optional benchmark download, and Redis/queue
listener startup.

Worker bootstrap now polls the configured Redis endpoint before starting the
managed `crsbench worker` process. That closes the gap where workers could
terminally fail before the remote orchestrator had finished starting Valkey.
Transport-level connection failures are retried until the readiness timeout,
while fatal Redis auth/config errors still fail immediately with bootstrap
evidence.
The same startup scripts also support local rehearsal via file-backed metadata
and a foreground worker mode for non-`systemd` containers; the default GCE
runtime path is unchanged.
The checked-in smoke config uses `readiness_timeout_sec: 1200` for both
orchestrator and worker instance profiles to give clean Ubuntu images more room
to finish bootstrap.

## Monitoring

### Fleet and Job Status

```bash
uv run crsbench cloud status my-experiment --config config.yaml
```

Shows:

- Fleet summary: each worker's name, state, zone, and IP
- Job summary: trial progress per job
- Collection summary: artifact sync progress
- Recent recovery events

Add `--json` for machine-readable output:

```bash
uv run crsbench cloud status my-experiment --config config.yaml --json
```

### Recovery Events

```bash
# All events
uv run crsbench cloud events my-experiment --config config.yaml

# Filter by type
uv run crsbench cloud events my-experiment --config config.yaml --type worker_restart

# JSON output
uv run crsbench cloud events my-experiment --config config.yaml --json
```

## Collecting Artifacts

Pull experiment results from all live workers to the local experiment filestore:

```bash
uv run crsbench cloud collect my-experiment \
    --config config.yaml \
    --remote-dir /data/experiments/my-experiment
```

- Uses rsync (via IAP tunnel or direct SSH depending on config)
- Stages artifacts in a temporary directory, verifies at least one valid trial exists, then publishes to the experiment filestore
- Continues to remaining workers if one fails; exits with code 1 on partial failure
- Safe to run multiple times (incremental rsync)
- In remote-orchestrator mode, also collects the orchestrator VM's experiment tree, even if the worker VMs have already been deleted
- If Redis is unavailable, falls back to the persisted launch state plus live GCE inventory

## Teardown

Remove the worker fleet after collecting results:

```bash
uv run crsbench cloud teardown my-experiment \
    --config config.yaml \
    --remote-dir /data/experiments/my-experiment
```

The teardown safety flow:

1. Lists live GCE instances for the experiment
2. Cross-references with Redis readiness records when Redis is reachable (warns about mismatches)
3. Prompts for confirmation (interactive TTY required)
4. Collects artifacts from ALL workers first
5. In remote-orchestrator mode, also collects the orchestrator VM
6. Deletes VMs even if some collections fail, to avoid leaking cloud resources
7. Returns a non-zero exit code if any collection or deletion step failed

Use `--force` to skip the confirmation prompt (e.g., in scripts):

```bash
uv run crsbench cloud teardown my-experiment \
    --config config.yaml \
    --remote-dir /data/experiments/my-experiment \
    --force
```

## Complete Workflow Example

```bash
# 0. Generate deploy key (one-time) and add public key to GitHub
uv run crsbench cloud keygen

# 1. Local-orchestrator mode only: start Valkey accessible from GCE workers
uv run python scripts/valkey-helper.py --password start

# 2. Local-orchestrator mode: run experiment from this machine
uv run crsbench run --experiment-config config.yaml

# 3. Remote-orchestrator mode: provision orchestrator + workers from this machine
uv run crsbench cloud launch --config config.yaml

# 4. Check status during the run
uv run crsbench cloud status my-experiment --config config.yaml

# 5. After completion, collect artifacts
uv run crsbench cloud collect my-experiment \
    --config config.yaml \
    --remote-dir /data/experiments/my-experiment

# 6. Tear down the fleet (and remote orchestrator, if used)
uv run crsbench cloud teardown my-experiment \
    --config config.yaml \
    --remote-dir /data/experiments/my-experiment

# 7. Generate report
uv run python scripts/cpv_report.py /data/experiments/my-experiment --csv
```

## SSH Access

For VM access during debugging:

```bash
# IAP mode
gcloud compute ssh my-experiment-001 \
    --project my-gcp-project \
    --zone us-central1-a \
    --tunnel-through-iap

# Direct mode (only if the VM has a public IP, your firewall allows your source IP,
# and you connect to a routable address or a local SSH alias you created separately)
ssh 203.0.113.10
```

If your firewall only allows SSH from the IAP range `35.235.240.0/20`, direct
`ssh <vm>` from your machine will not work. In that setup, use
`gcloud compute ssh --tunnel-through-iap ...` instead.

The worker process runs as a systemd service:

```bash
# On the worker VM
sudo systemctl status crsbench-worker.service
sudo journalctl -u crsbench-worker.service -f
```

## Operator Connectivity Notes

- Workers must be able to reach the orchestrator VM's Redis/Valkey endpoint.
- `cloud status` and `cloud events` reconnect to Redis using the persisted launch state in remote-orchestrator mode.
- The local operator machine still needs network reachability to that Redis endpoint. If the orchestrator VM exposes only a private address, run these commands from a machine with VPC access or add your own tunnel.
- `cloud collect` and `cloud teardown` can still use the persisted launch state when Redis is temporarily unavailable.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Bring-up timeout | Workers cannot reach Redis | Check firewall rules; verify `redis_host` / `runtime.redis.host` is reachable from the VPC |
| `bootstrap_failed` in status | Startup script error | Check the evidence field in `cloud status --json` output; inspect VM serial console via GCP Console |
| Collect fails with rsync error | SSH connectivity issue | Verify OS Login is enabled; check IAP firewall rule if using `ssh_via_iap` |
| Stale Redis entries warning | VMs were manually deleted | Safe to ignore; Redis records from deleted VMs don't affect new runs |
| Teardown returns non-zero | Collection or deletion failed for at least one VM | Check the logged worker/orchestrator errors, then rerun `cloud collect` or `cloud teardown` as needed |
| `use_os_login must be true` | Config validation | OS Login is required; do not set `use_os_login: false` |
| `exactly one of image or instance_template` | Config validation | Provide either `image` + `machine_type` + `boot_disk_size_gb` or `instance_template`, not both |

## See Also

- [Distributed Experiments](./distributed.md) -- full distributed experiment guide
- [Configuration Reference](./config-reference.md) -- all experiment config fields
- [Design: GCE Cloud Orchestration](../../design/distributed/gce-cloud-orchestration.md) -- architecture and contracts
- [Design: GCE Cloud Orchestrator Launch](../../design/distributed/gce-cloud-orchestrator.md) -- remote-orchestrator launch contract
