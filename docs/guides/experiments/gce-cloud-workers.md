# GCE Cloud Workers

Guide for provisioning and managing GCE worker fleets in CRSBench experiments.

## Prerequisites

1. **GCP project** with Compute Engine API enabled
2. **gcloud CLI** authenticated (`gcloud auth login`)
3. **Service account** for workers with minimal permissions:
   - `roles/logging.logWriter` (optional, for Cloud Logging)
   - Access to Redis host (firewall rules or VPC)
   - Access to any shared storage mounts
4. **OS Login** enabled on the GCP project (`gcloud compute project-info add-metadata --metadata enable-oslogin=TRUE`)
5. **IAP** configured if using `ssh_via_iap: true` (firewall rule allowing TCP port 22 from IAP range `35.235.240.0/20`)
6. **Redis/Valkey** reachable from worker VMs
7. **rsync** installed on the operator machine (for artifact collection)

## Configuration

Add a `cloud.gce` block to your experiment config YAML:

```yaml
cloud:
  gce:
    project: my-gcp-project
    zone: us-central1-a
    worker_count: 4
    machine_type: e2-standard-16
    boot_disk_size_gb: 200
    image: projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64
    service_account_email: crsbench-worker@my-gcp-project.iam.gserviceaccount.com
    owner_label: my-team
    use_os_login: true
    ssh_via_iap: true
    readiness_timeout_sec: 900
```

To launch the orchestrator in GCE as well, add a sibling `cloud.orchestrator`
block. The local operator machine still owns provisioning; the remote
orchestrator VM only runs `crsbench run` and hosts the Redis/Valkey queue.

```yaml
cloud:
  orchestrator:
    project: my-gcp-project
    zone: us-central1-a
    machine_type: e2-standard-16
    boot_disk_size_gb: 200
    image: projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64
    service_account_email: crsbench-orchestrator@my-gcp-project.iam.gserviceaccount.com
    owner_label: my-team
    use_os_login: true
    ssh_via_iap: true
    crsbench_install_spec: "git+ssh://git@github.com/your-org/CRSBench.git"
    github_deploy_key_file: .crsbench-keys/crsbench-deploy
  gce:
    project: my-gcp-project
    zone: us-central1-a
    worker_count: 4
    machine_type: e2-standard-16
    boot_disk_size_gb: 200
    image: projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64
    service_account_email: crsbench-worker@my-gcp-project.iam.gserviceaccount.com
    owner_label: my-team
    use_os_login: true
    ssh_via_iap: true
```

### Configuration Fields

| Field | Required | Default | Description |
|---|---|---|---|
| `project` | yes | -- | GCP project ID |
| `zone` | yes | -- | GCE zone for all workers |
| `worker_count` | no | `1` | Number of worker VMs to create |
| `machine_type` | conditional | -- | Required when using `image` |
| `boot_disk_size_gb` | conditional | -- | Required when using `image` (min 10) |
| `image` | conditional | -- | VM image; mutually exclusive with `instance_template` |
| `instance_template` | conditional | -- | GCE instance template; mutually exclusive with `image` |
| `service_account_email` | yes | -- | Service account for worker VMs |
| `owner_label` | yes | -- | Owner label applied to all VMs (or set via `labels.owner`) |
| `network` | no | default | VPC network |
| `subnetwork` | no | -- | VPC subnetwork |
| `labels` | no | `{}` | Additional GCE labels |
| `metadata` | no | `{}` | Additional instance metadata key-value pairs |
| `worker_name_prefix` | no | auto | Name prefix for VM instances |
| `startup_script_uri` | no | bundled | Custom startup script URL (overrides built-in bootstrap) |
| `use_os_login` | no | `true` | Must be `true` (enforced by validation) |
| `ssh_via_iap` | no | `false` | Use IAP tunnel for SSH and rsync |
| `readiness_timeout_sec` | no | `900` | Max seconds to wait for all workers to report ready |
| `crsbench_install_spec` | no | -- | How to install crsbench on workers (`git+ssh://...` for private repo clone, or pip spec). Optional when the VM image already has `crsbench` installed. |
| `crsbench_git_ref` | no | `main` | Git branch, tag, or commit to checkout after cloning (only used with `git+ssh://` install spec) |
| `github_deploy_key_file` | no | -- | Path to SSH private key for GitHub deploy key access |
| `hf_token` | no | -- | HuggingFace token for gated dataset access |

### Using Instance Templates

Instead of specifying `image` + `machine_type` + `boot_disk_size_gb`, you can reference a pre-configured instance template:

```yaml
cloud:
  gce:
    project: my-gcp-project
    zone: us-central1-a
    worker_count: 8
    instance_template: projects/my-gcp-project/global/instanceTemplates/crsbench-worker-v1
    service_account_email: crsbench-worker@my-gcp-project.iam.gserviceaccount.com
    owner_label: my-team
    ssh_via_iap: true
```

## Private Repository & Dataset Access

Worker VMs need to install CRSBench from source and optionally download
benchmarks from HuggingFace. When the repo or dataset is private, the
provisioner injects credentials via GCE instance metadata so the startup
script can authenticate automatically.

<!-- TODO: When the CRSBench repo and HuggingFace dataset become public,
     these fields become optional. Keep them supported for adopters who
     fork to a private repo or host a private dataset mirror. -->

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
  gce:
    # Install crsbench from private repo via git clone + uv sync
    crsbench_install_spec: "git+ssh://git@github.com/your-org/CRSBench.git"

    # Path to the private key generated by `crsbench cloud keygen`
    github_deploy_key_file: .crsbench-keys/crsbench-deploy

    # HuggingFace token for gated dataset downloads (optional)
    hf_token: "hf_..."
```

The provisioner reads the private key file at provision time, base64-encodes
it, and sets it as `crsbench-github-deploy-key` instance metadata. The
startup script writes it to `/root/.ssh/id_ed25519` and configures git SSH
access. The `hf_token` is passed as `crsbench-hf-token` metadata and exported
as `HF_TOKEN` in the worker environment.

## Launching an Experiment

### Local Orchestrator + GCE Workers

When `cloud.gce` is present and `cloud.orchestrator` is absent, `crsbench run`
provisions the worker fleet from the local machine:

```bash
uv run crsbench run --experiment-config config.yaml
```

The local orchestrator will:

1. Create `worker_count` VMs in the specified zone
2. Wait for each VM to bootstrap and report `ready` (up to `readiness_timeout_sec`)
3. Enqueue trial jobs only after the full fleet is ready
4. If any VM fails to become ready, tear down the entire fleet and exit with an error

### Remote Orchestrator + GCE Workers

When both `cloud.orchestrator` and `cloud.gce` are present, use `cloud launch`
from the local operator machine:

```bash
uv run crsbench cloud launch --config config.yaml
```

This path:

1. Provisions one orchestrator VM
2. Waits for the orchestrator VM to have an internal address
3. Provisions the worker fleet with Redis host/password metadata targeting that orchestrator VM
4. Lets the remote orchestrator VM start Valkey, rewrite the experiment config to use local Redis, wait for the pre-provisioned workers to report ready, and run `crsbench run`

`cloud launch` persists local launch state next to the config file under
`.crsbench-cloud/<experiment>.json`. Later `cloud status`, `cloud collect`, and
`cloud teardown` commands reuse that state automatically. `cloud status` and
`cloud events` still reconnect to the remote orchestrator's Redis; `cloud collect`
and `cloud teardown` can fall back to the persisted VM inventory if Redis is
unavailable.

Bootstrap failures are reported with per-instance evidence, so you can
diagnose issues without SSH-ing into VMs.

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

For direct access to worker VMs (debugging):

```bash
# IAP mode
gcloud compute ssh my-experiment-001 \
    --project my-gcp-project \
    --zone us-central1-a \
    --tunnel-through-iap

# Direct mode (if workers have public IPs)
ssh my-experiment-001
```

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
- [Design: GCE Cloud Workers](../../design/distributed/gce-cloud-workers.md) -- architecture and contracts
- [Design: GCE Cloud Orchestrator Launch](../../design/distributed/gce-cloud-orchestrator.md) -- remote-orchestrator launch contract
