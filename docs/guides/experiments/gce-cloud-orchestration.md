# GCE Cloud Orchestration

Guide for provisioning and managing remote orchestrator plus worker VMs for
CRSBench experiments on GCE.

CRSBench uses a provider-neutral top-level `cloud.*` config shape, but the only
implemented managed backend today is GCE through `cloud.providers.gce`. This
guide covers that current backend end to end.

For a local preflight of the same startup scripts before touching GCE, use
[Local Cloud Rehearsal](./local-cloud-rehearsal.md).

## TL;DR Smoke Test

If you just want to prove the remote-orchestrator path works end to end, use
the checked-in multilang smoke config below. This assumes the prerequisites in
the next section are already satisfied.

```bash
CONFIG=experiment-configs/cloud-testing/gce-sanity-1orch-2worker-1eval-multilang-given-fuzzer.yaml
EXPERIMENT=gce-sanity-mgf-1o2w1e
```

1. Create the deploy key used by VM bootstrap for the top-level CRSBench clone:

```bash
uv run crsbench cloud keygen
```

Expected result: `.crsbench-keys/crsbench-deploy` and
`.crsbench-keys/crsbench-deploy.pub` exist locally.

2. Add the public key to GitHub under the CRSBench repository's deploy keys:

```bash
cat .crsbench-keys/crsbench-deploy.pub
```

Then go to **Settings > Deploy keys > Add deploy key**, paste that public key,
and leave write access disabled.

3. Launch the checked-in multilang smoke run:

```bash
uv run crsbench cloud launch --config "$CONFIG"
```

This provisions 1 orchestrator, 2 workers, and 1 evaluator, then starts
experiment `gce-sanity-mgf-1o2w1e`.

4. Monitor the live queue from the operator machine:

```bash
uv run crsbench cloud --config "$CONFIG" monitor "$EXPERIMENT"
```

You can omit `"$EXPERIMENT"` here because `cloud monitor` infers it from the
config.

When Apprise URLs are set in the operator environment, `cloud monitor` sends
one operator-side terminal notification after it first sees the queue
transition from non-empty to empty during that attached session. When failed
jobs remain at that drain point, the terminal message reports a failure instead
of a completion. Attaching while the queue is already idle does not emit a
notification for that initial idle state, but a later active-to-idle
transition in the same session still can.

5. After the run finishes, collect artifacts and VM diagnostics back to the
local machine:

```bash
uv run crsbench cloud --config "$CONFIG" collect
```

6. Tear down the fleet so you do not leave GCE resources running:

```bash
uv run crsbench cloud --config "$CONFIG" teardown --force
```

For the detailed preflight checks, config explanation, and troubleshooting
flows, continue with the rest of this guide.

## Prerequisites

1. **GCP project** with Compute Engine API enabled
2. **gcloud CLI** installed and authenticated:
   - Install it for your platform using the official Google Cloud CLI guide:
     <https://cloud.google.com/sdk/docs/install>
   - For archive-based installs on Linux or macOS, download the matching
     package from that guide and run `./google-cloud-sdk/install.sh`
   - Run `gcloud init` after installation to set your default project and
     config
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
     (for example, `gcloud compute firewall-rules create allow-ssh-from-iap
     --project my-gcp-project --network my-vpc --direction=INGRESS
     --action=ALLOW --rules=tcp:22 --source-ranges=35.235.240.0/20`)
   - operator IAM permissions to open IAP TCP tunnels and log in over SSH
6. **Redis/Valkey** reachable from worker VMs
7. **rsync** installed on the operator machine (for artifact collection)

## Notification Preflight

If this deployment will use Apprise notifications, choose the preflight path
that matches where the notification values are coming from.

### Local Shell / `.env` Preflight

Use the operator shell or repo `.env` to verify the notification config before
`cloud launch` or `cloud monitor`:

```bash
uv run python scripts/test_notification.py --dry-run
```

If the dry run looks correct, send a real smoke test to confirm delivery:

```bash
uv run python scripts/test_notification.py
```

This path validates notification settings that come from the operator shell or
repo `.env`. It does not exercise cloud config env injection into the
orchestrator runtime. If the values come from the operator shell, run the
preflight in the same shell environment that will run `cloud launch` or
`cloud monitor`.

If operator-side `cloud monitor` Apprise is enabled and the cloud launch env
also enables orchestrator-side Apprise, the local `cloud monitor` notification
and the orchestrator-side terminal notification can both fire, which
duplicates the terminal alert.

If a checked-in cloud config should inherit the notification target from the
operator shell or `.env`, declare it explicitly under `cloud.orchestrator.env`:

```yaml
cloud:
  orchestrator:
    env:
      CRSBENCH_NOTIFY_APPRISE_URLS: os.environ/CRSBENCH_NOTIFY_APPRISE_URLS
      # Optional:
      # CRSBENCH_NOTIFY_APPRISE_TITLE: os.environ/CRSBENCH_NOTIFY_APPRISE_TITLE
      # CRSBENCH_NOTIFY_APPRISE_TAG: os.environ/CRSBENCH_NOTIFY_APPRISE_TAG
```

### Cloud Env Rehearsal Preflight

Use the stock rehearsal command when you want to rehearse the checked-in
`cloud.orchestrator.env` notification path in
[`scripts/cloud-rehearsal/local-experiment-notification.yaml`](../../../scripts/cloud-rehearsal/local-experiment-notification.yaml):

```bash
export CRSBENCH_NOTIFY_APPRISE_URLS='discord://token/chat-id'
scripts/cloud-rehearsal/test-notification-rehearsal.sh
scripts/cloud-rehearsal/test-notification-rehearsal.sh --send
```

The rehearsal defaults to dry-run and validates that
`cloud.orchestrator.env` injection reaches the orchestrator runtime. It uses the local Docker-based cloud
rehearsal harness described in
[`local-cloud-rehearsal.md`](./local-cloud-rehearsal.md), so the same Docker
prerequisites apply. This stock command validates the checked-in
`CRSBENCH_NOTIFY_APPRISE_URLS` orchestrator passthrough path in
[`scripts/cloud-rehearsal/local-experiment-notification.yaml`](../../../scripts/cloud-rehearsal/local-experiment-notification.yaml).
It is a cloud launch rehearsal, not a worker or evaluator notification path.

If operator-side `cloud monitor` Apprise is enabled and the cloud launch env
also enables orchestrator-side Apprise, expect a duplicate terminal
notification when the queue drains.

## Configuration

Declare provider-native GCE details under `cloud.providers.gce`, then reference
those instance profiles from the provider-neutral `cloud.orchestrator`,
`cloud.workers`, and optional `cloud.evaluators` sections:

```yaml
cloud:
  defaults:
    readiness_timeout_sec: 900
    crsbench_install_spec: "git+https://github.com/sslab-gatech/CRSBench.git"
    crsbench_git_ref: main
  bootstrap:
    prepare_mode: full
    download_benchmarks: auto
  providers:
    gce:
      project: my-gcp-project
      ssh_via_iap: true
      region: us-east5
      zones:
        - us-east5-b
        - us-east1-b
      fallback: true
      profile_defaults:
        machine_type: n2d-standard-16
        boot_disk_size_gb: 100
        image: projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64
        service_account_email: crsbench@my-gcp-project.iam.gserviceaccount.com
        owner_label: my-team
      instance_profiles:
        gce-orchestrator-n2d: {}
        gce-worker-n2d: {}
        gce-evaluator-c3d:
          machine_type: c3d-standard-30
  orchestrator:
    region: us-east5
    instance_profile: gce-orchestrator-n2d
  workers:
    defaults:
      instance_profile: gce-worker-n2d
      count: 1
    placements:
      - region: us-east5
        count: 3
      - region: us-central1
        zones:
          - us-central1-a
        fallback: false
  evaluators:
    defaults:
      instance_profile: gce-evaluator-c3d
      count: 1
    placements:
      - region: us-east1
        zones:
          - us-east1-b
```

### Configuration Fields

#### Shared Cloud Fields

| Field | Required | Description |
|---|---|---|
| `cloud.bootstrap.prepare_mode` | no | How cloud VMs run `crsbench prepare`; `full` or `skip_base_images` |
| `cloud.bootstrap.download_benchmarks` | no | Whether cloud VMs download benchmarks before joining runtime; `auto`, `always`, or `never` |
| `cloud.bootstrap.gitcache` | no | `gitcache` wrapper policy for cloud VMs; CRSBench always installs the binary, and `true` makes CRSBench-managed `git` calls use it |
| `cloud.defaults.readiness_timeout_sec` | no | Max seconds launch waits for VM bootstrap and readiness before failing |
| `cloud.defaults.crsbench_install_spec` | no | CRSBench install source for remote VMs; cloud launch expects a `git+...` spec |
| `cloud.defaults.crsbench_git_ref` | no | Git ref checked out when `crsbench_install_spec` points at a Git source |
| `cloud.defaults.github_deploy_key_path` | no | Optional secret reference for a private Git deploy key used during CRSBench install |
| `cloud.remote.experiment_root` | no | Remote experiment root used by `cloud collect` / `cloud teardown`; defaults to `storage.experiment_filestore` for backward compatibility |
| `cloud.env` | no | Global environment variables merged into every launched cloud role |

#### GCE Provider Fields

| Field | Required | Description |
|---|---|---|
| `cloud.providers.gce.project` | yes | GCP project ID used for all referenced GCE resources |
| `cloud.providers.gce.network` | no | Default VPC network name for GCE resources unless instance profiles override it |
| `cloud.providers.gce.subnetwork` | no | Default VPC subnetwork name for GCE resources unless instance profiles override it |
| `cloud.providers.gce.ssh_via_iap` | no | Default operator SSH transport; when true, operators connect through IAP-backed SSH by default |
| `cloud.providers.gce.assign_external_ip` | no | Default external NAT policy for outbound internet access unless instance profiles override it |
| `cloud.providers.gce.defaults.readiness_timeout_sec` | no | GCE-specific override for the shared launch readiness timeout |
| `cloud.providers.gce.defaults.crsbench_install_spec` | no | GCE-specific override for the shared CRSBench install source |
| `cloud.providers.gce.defaults.crsbench_git_ref` | no | GCE-specific override for the shared CRSBench Git ref |
| `cloud.providers.gce.defaults.github_deploy_key_path` | no | GCE-specific override for the shared Git deploy key secret reference |
| `cloud.providers.gce.region` | no | Default GCE region used when orchestrator or placements do not override `region` |
| `cloud.providers.gce.regions` | no | Ordered default candidate regions used for regional placement and regional fallback |
| `cloud.providers.gce.zones` | no | Ordered default candidate zones used when orchestrator or placements do not override `zones` |
| `cloud.providers.gce.fallback` | no | Default policy for retrying later candidate regions or zones after recognized placement failure |
| `cloud.providers.gce.profile_defaults` | no | Default instance-profile fields merged into every named GCE profile |
| `cloud.providers.gce.profile_defaults.env` | no | Default environment variables merged into every named GCE instance profile |
| `cloud.providers.gce.instance_profiles.<name>` | yes | Reusable machine/image/service-account bundle for orchestrator, workers, or evaluators |

#### GCE Instance Profile Fields

These field suffixes are valid under both
`cloud.providers.gce.profile_defaults` and
`cloud.providers.gce.instance_profiles.<name>`. `profile_defaults` supplies
defaults; `instance_profiles.<name>` defines the effective per-VM contract.

| Field suffix | Required on `instance_profiles.<name>` | Description |
|---|---|---|
| `machine_type` | yes when `image` is used | GCE machine type for image-based instances |
| `boot_disk_size_gb` | yes when `image` is used | Boot disk size in GiB for image-based instances |
| `boot_disk_type` | no | GCE disk type (`pd-ssd`, `pd-balanced`, `pd-standard`); defaults to GCE platform default (`pd-standard`) when omitted |
| `image` | exactly one of `image` or `instance_template` | Image or image-family reference used for VM creation |
| `instance_template` | exactly one of `image` or `instance_template` | Existing GCE instance template to use instead of explicit image and machine settings |
| `network` | no | Optional VPC network override for this profile |
| `subnetwork` | no | Optional VPC subnetwork override for this profile |
| `service_account_email` | yes | Service account email used for instances created with this profile |
| `owner_label` | required unless `labels.owner` is set | Ownership label applied to instances using this profile |
| `labels` | no | Additional GCE labels applied to instances using this profile |
| `metadata` | no | Additional instance metadata applied during bootstrap |
| `env` | no | Environment variables merged into instances using this profile |
| `startup_script_uri` | no | Optional URI for a maintained startup script payload |
| `use_os_login` | must remain `true` | CRSBench supports only OS Login-compatible SSH access in this flow |
| `ssh_via_iap` | no | Whether operators are expected to connect through IAP-backed SSH |
| `assign_external_ip` | no | Whether instances receive an external NAT interface for outbound internet access |

#### Orchestrator Fields

| Field | Required | Description |
|---|---|---|
| `cloud.orchestrator.zone` | no | Backward-compatible single preferred orchestrator zone; normalized into `zones` |
| `cloud.orchestrator.region` | no | Optional orchestrator region; enables regional bulk placement |
| `cloud.orchestrator.regions` | no | Ordered candidate regions for regional placement plus runtime fallback |
| `cloud.orchestrator.zones` | no | Ordered candidate zones for the orchestrator VM |
| `cloud.orchestrator.fallback` | no | Override for orchestrator region-or-zone retry behavior |
| `cloud.orchestrator.instance_profile` | yes | Instance profile name for the orchestrator VM |
| `cloud.orchestrator.env` | no | Environment variables injected only into the orchestrator VM |

#### Worker Fields

| Field | Required | Description |
|---|---|---|
| `cloud.workers.defaults.count` | no | Default number of workers to create per placement |
| `cloud.workers.defaults.instance_profile` | no | Default worker instance profile |
| `cloud.workers.defaults.region` | no | Role-level default region for worker placements |
| `cloud.workers.defaults.regions` | no | Ordered role-level default candidate regions for worker placements |
| `cloud.workers.defaults.zones` | no | Ordered role-level default candidate zones for worker placements |
| `cloud.workers.defaults.fallback` | no | Role-level default fallback policy for worker placements across regions or zones |
| `cloud.workers.defaults.env` | no | Default environment variables merged into each worker placement |
| `cloud.workers.placements[].zone` | no | Backward-compatible single preferred worker zone; normalized into `zones` |
| `cloud.workers.placements[].region` | no | Optional worker region for regional bulk placement |
| `cloud.workers.placements[].regions` | no | Ordered candidate regions for one worker placement |
| `cloud.workers.placements[].zones` | no | Ordered candidate zones for one worker placement |
| `cloud.workers.placements[].fallback` | no | Per-placement override for worker region-or-zone retry behavior |
| `cloud.workers.placements[].count` | no | Number of workers to create in that placement |
| `cloud.workers.placements[].instance_profile` | no | Instance profile override for that placement |
| `cloud.workers.placements[].env` | no | Environment variables injected into every worker VM in that placement |

#### Evaluator Fields

| Field | Required | Description |
|---|---|---|
| `cloud.evaluators.defaults.count` | no | Default number of evaluators to create per placement |
| `cloud.evaluators.defaults.instance_profile` | no | Default evaluator instance profile |
| `cloud.evaluators.defaults.region` | no | Role-level default region for evaluator placements |
| `cloud.evaluators.defaults.regions` | no | Ordered role-level default candidate regions for evaluator placements |
| `cloud.evaluators.defaults.zones` | no | Ordered role-level default candidate zones for evaluator placements |
| `cloud.evaluators.defaults.fallback` | no | Role-level default fallback policy for evaluator placements across regions or zones |
| `cloud.evaluators.defaults.env` | no | Default environment variables merged into each evaluator placement |
| `cloud.evaluators.placements[].zone` | no | Backward-compatible single preferred evaluator zone; normalized into `zones` |
| `cloud.evaluators.placements[].region` | no | Optional evaluator region for regional bulk placement |
| `cloud.evaluators.placements[].regions` | no | Ordered candidate regions for one evaluator placement |
| `cloud.evaluators.placements[].zones` | no | Ordered candidate zones for one evaluator placement |
| `cloud.evaluators.placements[].fallback` | no | Per-placement override for evaluator region-or-zone retry behavior |
| `cloud.evaluators.placements[].count` | no | Number of evaluators to create in that placement |
| `cloud.evaluators.placements[].instance_profile` | no | Instance profile override for that placement |
| `cloud.evaluators.placements[].env` | no | Environment variables injected into every evaluator VM in that placement |

Provider-neutral configs do not repeat `provider` on orchestrator or
placements. CRSBench resolves the provider from the referenced
`cloud.providers.<provider>.instance_profiles` catalog, and one launch cannot
mix providers across orchestrator, workers, and evaluators.
Launchable configs require `cloud.providers`, `cloud.orchestrator`, and
`cloud.workers`. `cloud.evaluators` is optional, but when present it must
declare at least one placement.
Instance-profile keys must also be globally unique across provider catalogs, so
the same profile name cannot be reused under multiple `cloud.providers.*`
entries. Today the only supported catalog is `cloud.providers.gce`.

Instance profiles carry the per-VM details such as `machine_type`,
`boot_disk_size_gb`, `image` or `instance_template`, `network`, `subnetwork`,
`service_account_email`, `owner_label`, `labels`, `metadata`, `env`,
`startup_script_uri`, `ssh_via_iap`, and `assign_external_ip`.

`ssh_via_iap` controls how operators connect. `assign_external_ip` controls
whether GCE attaches an external NAT interface for outbound package installs,
GitHub clone, benchmark downloads, and image pulls. The default is `true`.
Set `assign_external_ip: false` only when your project already provides private
egress, such as Cloud NAT.

When an effective `region` or ordered `regions` list is present, CRSBench uses
GCE regional bulk insert with `ANY_SINGLE_ZONE`. Optional `zones` become an
allowlist inside the effective region set. CRSBench validates that every listed
zone belongs to one of the effective regions before any VM create request is
sent. On each regional attempt, CRSBench first filters that `zones` list down
to only the zones that belong to the current region, then sends that filtered
zone list as the regional bulk-insert location policy.

Launch/bootstrap defaults live outside instance profiles:

- `cloud.defaults.readiness_timeout_sec`
- `cloud.defaults.crsbench_install_spec`
- `cloud.defaults.crsbench_git_ref`
- `cloud.defaults.github_deploy_key_path`

Provider-specific overrides can replace those values through
`cloud.providers.<provider>.defaults`.

Placement selection is ordered. CRSBench resolves the effective candidate list
from the most specific declaration present:

1. placement or orchestrator `regions`
2. placement or orchestrator singular `region`
3. role `defaults.regions`
4. role `defaults.region`
5. `cloud.providers.gce.regions`
6. `cloud.providers.gce.region`

If no effective regions are declared, zonal selection falls back to:

1. placement or orchestrator `zones`
2. role `defaults.zones`
3. `cloud.providers.gce.zones`

In zonal mode, CRSBench tries those effective zones in listed order, one zone
at a time.

Fallback policy uses the same precedence, ending with `true` if nothing is
configured. When `fallback: true`, CRSBench retries later declared regions or
zones only for recognized placement failures. When `fallback: false`, the first
placement failure for that logical slot fails the launch and tears down any
instances that were already created.

Singular `region` and `zone` are just shorthand. CRSBench normalizes them into
the same effective selection flow as `regions` and `zones`, so the real
decision model is: use effective `regions` first; if none are declared, use
effective `zones`.

Example:

```yaml
cloud:
  providers:
    gce:
      project: my-gcp-project
      regions:
        - us-east5
        - us-central1
      zones:
        - us-east5-b
        - us-central1-a
      fallback: true
      instance_profiles:
        gce-worker:
          machine_type: n2d-standard-16
          boot_disk_size_gb: 100
          image: projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64
          service_account_email: crsbench@my-gcp-project.iam.gserviceaccount.com
          owner_label: my-team
  workers:
    defaults:
      instance_profile: gce-worker
    placements:
      - count: 2
        regions:
          - us-east5
          - us-central1
        zones:
          - us-east5-b
          - us-central1-a
        fallback: true
      - count: 1
        zone: us-east5-c
        fallback: false
```

What CRSBench does with this config:

1. For the first placement, effective `regions` are `us-east5`, then
   `us-central1` from `placements[0].regions`.
2. Because effective `regions` exist, CRSBench uses regional bulk insert with
   `ANY_SINGLE_ZONE`; the placement `zones` list is only an allowlist inside
   those regions, not an ordered zonal retry list.
   For the `us-east5` attempt, CRSBench sends only `us-east5-b` to GCE. If it
   falls back to `us-central1`, it sends only `us-central1-a`.
3. If GCE returns a recognized regional capacity failure in `us-east5`,
   `fallback: true` lets CRSBench retry the same logical placement in
   `us-central1`.
4. For the second placement, `zone: us-east5-c` is normalized to
   `zones: [us-east5-c]`. No placement or default `regions` are present for
   that placement, so CRSBench uses zonal selection instead of regional bulk
   insert.
5. Because that placement sets `fallback: false`, a recognized placement
   failure in `us-east5-c` fails that logical slot immediately instead of
   trying another zone.

By default, provisioned instance names sort naturally in the GCP console:
`crsbench-<experiment>-orch`,
`crsbench-<experiment>-work-001`,
and `crsbench-<experiment>-eval-001`. Worker and evaluator suffixes increase
monotonically per experiment and role in config order, even if the actual
chosen zone changes because of fallback. `cloud collect`, `cloud monitor`, and
`cloud teardown` use the persisted launch-state zone, not the instance name, to
reconnect to a launched fleet.

`cloud launch` refuses to provision when the same experiment already has a
config-adjacent launch-state file or matching live orchestrator/worker/evaluator
VMs. Tear down the existing fleet before relaunching instead of mutating the
experiment name to create a second set of instances.

If a generated GCE instance name would violate Compute Engine naming rules,
CRSBench now fails launch before provisioning instead of truncating the name.

### Bootstrap Policy

`cloud.bootstrap` controls the VM bootstrap steps that run before a worker or
remote orchestrator is counted as ready:

- `prepare_mode: full | skip_base_images`
- `download_benchmarks: auto | always | never`
- `gitcache: false | true`

Cloud VMs always run `crsbench prepare`. `download_benchmarks: auto` skips the
VM-side download only when `benchmark_suite: sanity`; other suites download
before the worker joins Redis.

Cloud VMs also always install the pinned `gitcache` binary in a CRSBench-managed
bin directory. `gitcache: false` leaves the normal `git` command unchanged.
`gitcache: true` adds a managed `git -> gitcache` wrapper in the CRSBench PATH,
so CRSBench-managed clone/fetch/update steps use `gitcache` without rewriting
the host-global `git` binary or shell profile.

On the remote orchestrator, the startup script binds Valkey on `127.0.0.1` for
the local `crsbench run` path and on the VM's discovered internal address for
workers. It does not expose Redis on `0.0.0.0`.

### SSD Boot Disks

Use `boot_disk_type` in instance profiles to select SSD storage:

```yaml
cloud:
  providers:
    gce:
      profile_defaults:
        boot_disk_type: pd-ssd   # pd-ssd, pd-balanced, or pd-standard
        boot_disk_size_gb: 1024
      instance_profiles:
        gce-evaluator:
          boot_disk_size_gb: 2048  # evaluator needs more for build cache
          # inherits pd-ssd from profile_defaults
```

### Region Pinning for Quota Management

When the total fleet exceeds a single region's CPU quota, pin placements
to specific regions instead of relying on fallback:

```yaml
cloud:
  workers:
    defaults:
      instance_profile: gce-worker-n2d
      count: 1
    placements:
      # us-central1: 4 workers (quota: 1000 N2D vCPUs)
      - region: us-central1
      - region: us-central1
      - region: us-central1
      - region: us-central1
      # us-south1: 2 workers (quota: 776 N2D vCPUs)
      - region: us-south1
      - region: us-south1
```

Without pinning, all placements attempt the first region in the fallback
list before retrying. CRSBench preflight detects when greedy first-region
demand exceeds that region's quota.

Check quotas with:

```bash
gcloud compute regions describe us-central1 --project=PROJECT \
  --format="table(quotas.filter(metric:N2D_CPUS))"
```

### Using Instance Templates

Instead of specifying `image` + `machine_type` + `boot_disk_size_gb`, you can reference a pre-configured instance template:

```yaml
cloud:
  providers:
    gce:
      project: my-gcp-project
      ssh_via_iap: true
      profile_defaults:
        service_account_email: crsbench-worker@my-gcp-project.iam.gserviceaccount.com
        owner_label: my-team
      instance_profiles:
        gce-worker-template:
          instance_template: projects/my-gcp-project/global/instanceTemplates/crsbench-worker-v1
  workers:
    defaults:
      instance_profile: gce-worker-template
      count: 1
    placements:
      - zone: us-central1-a
        count: 8
```

## Private Repository & Dataset Access

Worker VMs need to install CRSBench from source and optionally download
benchmarks from HuggingFace. When the repo or dataset is private, the
provisioner injects credentials via GCE instance metadata so the startup
script can authenticate automatically.

These credential fields stay supported even when you use a public CRSBench
repository or a public dataset mirror, because downstream adopters may still
need private forks or gated datasets.

### HuggingFace Token for Gated Datasets

When `cloud.bootstrap.download_benchmarks` is `auto` or `always` and the
benchmark dataset is gated, VMs need a HuggingFace token:

```yaml
cloud:
  env:
    HF_TOKEN: os.environ/HF_TOKEN
```

Export the token locally before launching:

```bash
export HF_TOKEN="hf_your_token_here"
uv run crsbench cloud launch --config "$CONFIG"
```

CRSBench preflight warns when `HF_TOKEN` is missing and downloads are
enabled.

### Secret Field Syntax

Cloud secret-bearing fields accept three forms at launch time:

- literal values
- `os.environ/NAME`
- `file:relative/or/absolute/path`

`file:` paths resolve relative to the operator command's current working
directory when they are not absolute.

Cloud orchestration requires `crsbench_install_spec` to use a `git+...`
checkout source. The VM bootstrap clones CRSBench into `/opt/crsbench`,
changes into that checkout, runs `crsbench prepare`, optionally downloads
benchmarks, and only then starts the orchestrator, worker, or evaluator runtime.

If remote VMs need API keys or upstream URLs from the operator environment,
prefer first-class layered `env` maps:

```yaml
cloud:
  env:
    CRSBENCH_LLM_UPSTREAM_BASE_URL: os.environ/LITELLM_BASE_URL
    CRSBENCH_GIT_SSH_HOST: github.example.com
    CRSBENCH_TIMEZONE: America/Los_Angeles

  providers:
    gce:
      profile_defaults:
        env:
          HTTPS_PROXY: os.environ/HTTPS_PROXY
      defaults:
        crsbench_install_spec: "git+https://github.com/sslab-gatech/CRSBench.git"
      instance_profiles:
        gce-orchestrator-n2d:
          env:
            CRSBENCH_LLM_MASTER_KEY: os.environ/LITELLM_ORCH_MASTER_KEY
        gce-worker-n2d:
          env:
            OPENAI_API_KEY: os.environ/DEFAULT_OPENAI_API_KEY

  orchestrator:
    zone: us-east5-b
    instance_profile: gce-orchestrator-n2d
    env:
      CRSBENCH_LLM_MASTER_KEY: os.environ/LITELLM_ORCH_MASTER_KEY
      CRSBENCH_VALKEY_IMAGE: us-docker.pkg.dev/example/platform/valkey:8.0-alpine

  workers:
    defaults:
      instance_profile: gce-worker-n2d
      count: 1
      env:
        OPENAI_API_KEY: os.environ/DEFAULT_OPENAI_API_KEY
    placements:
      - zone: us-east5-b
        env:
          CRSBENCH_LLM_UPSTREAM_BASE_URL: os.environ/LITELLM1_BASE_URL
          CRSBENCH_LLM_MASTER_KEY: os.environ/LITELLM1_MASTER_KEY
      - zone: us-east1-b
        env:
          CRSBENCH_LLM_UPSTREAM_BASE_URL: os.environ/LITELLM2_BASE_URL
          CRSBENCH_LLM_MASTER_KEY: os.environ/LITELLM2_MASTER_KEY
```

Semantics:

- values support literal strings, `os.environ/NAME`, and `file:path`
- values are resolved on the operator before provisioning
- orchestrator merge order is:
  `cloud.env -> profile_defaults.env -> instance_profile.env -> cloud.orchestrator.env`
- worker/evaluator merge order is:
  `cloud.env -> profile_defaults.env -> instance_profile.env -> role defaults.env -> placement.env`
- runtime-managed variables are applied after those user-configured env layers
  and win last
- all VMs in the same placement share the same merged env payload
- when you launch through the CRSBench CLI, `.env` is loaded first, so
  `os.environ/...` references can come from either the
  shell environment or `.env`
- missing or empty referenced values fail launch before any VM is created
- runtime-managed variables such as `CRSBENCH_REDIS_HOST` and
  `CRSBENCH_REDIS_PASSWORD` are rejected and must not be overridden
- startup-time settings such as `CRSBENCH_TIMEZONE` should be set through these
  env layers; for example, `cloud.env.CRSBENCH_TIMEZONE: America/Los_Angeles`
  changes the host timezone on orchestrator and worker/evaluator VMs during
  bootstrap
- startup-time SSH clone settings such as `CRSBENCH_GIT_SSH_HOST` also flow
  through these env layers; use them when `crsbench_install_spec` points at a
  non-`github.com` SSH host
- orchestrator-only startup settings such as `CRSBENCH_VALKEY_IMAGE` should be
  set through `cloud.orchestrator.env`

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
  defaults:
    crsbench_install_spec: "git+https://github.com/sslab-gatech/CRSBench.git"
  env:
    HF_TOKEN: os.environ/HF_TOKEN
  providers:
    gce:
      instance_profiles:
        # Inherit the public git+https install path from cloud.defaults.
        gce-orchestrator-n2d: {}
        gce-worker-n2d: {}
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

When `github_deploy_key_path` is set, the provisioner reads the private key
file at provision time, base64-encodes it, and sets it as
`crsbench-github-deploy-key` instance metadata. The startup script writes it to
`/root/.ssh/id_ed25519` and uses it only for the top-level CRSBench clone.
Submodules continue to use the URLs declared in `.gitmodules`, so public
submodules such as `oss-crs` can stay on HTTPS and do not require the deploy
key. `HF_TOKEN` and other remote env vars should be declared in `cloud.env` or
the role/profile env layers. Secret references are resolved once on the
operator before VM creation; the original experiment config payload sent to the
remote orchestrator is not rewritten with resolved secret values. First-class
`cloud.*.env` values are encoded into the generic env metadata bundle and
exported by the startup scripts after resolution.

## Launching an Experiment

### Local Orchestrator + GCE Workers

When you run `crsbench run --experiment-config ...`, CRSBench can provision the
declared `cloud.workers.placements` and `cloud.evaluators.placements` from the
local machine:

```bash
uv run crsbench run --experiment-config config.yaml
```

The local orchestrator will:

1. Validate live quota for the requested worker/evaluator placements
2. Create the requested worker/evaluator VMs across the configured zones or regions
3. Wait for each VM to bootstrap and report `ready`
4. Enqueue trial jobs only after the full fleet is ready
5. If any VM fails to become ready, tear down the entire fleet and exit with an error

### Remote Orchestrator + GCE Workers

Before you provision anything, use `cloud preflight` to answer two questions:

1. What would CRSBench launch from this config?
2. Will launch fail immediately because of config, duplicate-launch, provider,
   or quota problems?

```bash
uv run crsbench cloud preflight --config config.yaml
```

`cloud preflight` is read-only. It does not create VMs, does not refresh
`.crsbench-cloud/<experiment>.json`, and does not mutate remote state. The
default output is a human-readable report with:

- `Summary`: experiment, provider, and verdict
- `Plan`: orchestrator, worker, and evaluator placements CRSBench would launch
- `Defaults`: resolved launch/bootstrap defaults
- `Environment`: redacted env-layer summary, including runtime-managed vars
- `Checks`: duplicate-launch guard, provider preflight, quota results, and warnings
- `Reconnect Notes`: which later commands need Redis/control-plane reachability

Useful flags:

- `--json`: emit the same report in a machine-readable schema for CI or wrapper tooling
- `--strict`: treat warning-only preflight results as a non-zero exit

Common outcomes:

- `ready`: launch can proceed
- `warning`: launch can proceed, but an operator-visible caveat exists
- `blocked`: fix the reported issue before running `cloud launch`

One common warning is that `cloud.remote.experiment_root` is unset. In that
case, standalone `cloud collect` and `cloud teardown` fall back to the legacy
remote path derived from `storage.experiment_filestore`.

When you use `cloud launch`, the local operator machine provisions the
orchestrator VM and the worker/evaluator placements declared in the same config:

```bash
uv run crsbench cloud launch --config config.yaml
```

This path:

1. Validates live GCE quota for the orchestrator region plus all worker/evaluator placement regions
2. Provisions one orchestrator VM
3. Waits for the orchestrator VM to have an internal address
4. Provisions workers across all `cloud.workers.placements` and evaluators across all `cloud.evaluators.placements`, passing the orchestrator Redis host/password
5. Lets the remote orchestrator VM clone CRSBench, run `crsbench prepare`, optionally download benchmarks, start Valkey, rewrite the experiment config to use local Redis, wait for the pre-provisioned workers/evaluators to exist in GCE inventory, and run `crsbench run` while late workers/evaluators continue booting in the background

`cloud launch` persists local launch state next to the config file under
`.crsbench-cloud/<experiment>.json`. Later `cloud status`, `cloud collect`, and
`cloud teardown` commands reuse that state automatically. `cloud status` and
`cloud events` still reconnect to the remote orchestrator's Redis; `cloud collect`
and `cloud teardown` can fall back to the persisted VM inventory if Redis is
unavailable.
That launch-state file is part of the duplicate-launch guard: if it still
exists for an experiment, CRSBench treats that experiment as already launched
until you tear it down or remove the stale state.
CRSBench also appends every created VM name to
`.crsbench-cloud/created-instances.cache` as local JSONL history so you still
have a garbage-collection ledger even if you forget to tear down a prior run.

### Adding Capacity After Launch

Use `cloud add-workers` or `cloud add-evaluators` when you want to append one
new placement to an already launched remote-orchestrator experiment without
editing the YAML config.

Worker example:

```bash
uv run crsbench cloud add-workers --config config.yaml \
  --regions us-east5,us-east1 \
  --zones us-east5-b,us-east1-b
```

Evaluator example:

```bash
uv run crsbench cloud add-evaluators --config config.yaml \
  --zones us-central1-a
```

Key behavior:

- Each command adds exactly one new placement.
- Omitting `--count` adds exactly one worker or evaluator. Runtime expansion
  does not inherit `cloud.<role>.defaults.count`.
- Omitting `--instance-profile` inherits
  `cloud.workers.defaults.instance_profile` or
  `cloud.evaluators.defaults.instance_profile`.
- Omitting `--regions` / `--zones` inherits the matching role defaults first,
  then falls back to provider defaults.
- When provided, CLI `instance_profile`, `count`, `regions`, and `zones`
  override the inherited values for the new placement.
- The effective placement must still resolve to an existing named
  `cloud.providers.gce.instance_profiles.*` entry and at least one region or
  zone.
- `regions` plus `zones` uses the same semantics as launch-time config:
  regional mode first, with `zones` acting as a per-region allowlist.
- Fallback, bootstrap defaults, deploy-key path, and inherited env layers still
  come from the config.
- CRSBench validates quota for only the requested delta placement before
  creating anything.
- By default CRSBench prints a confirmation summary with the delta placement and
  projected fleet totals; use `--force` to skip the prompt in automation.
- On readiness or provisioning failure, CRSBench rolls back only the new
  placement from that command; the existing fleet stays untouched.
- On success, CRSBench appends the new placement to
  `.crsbench-cloud/<experiment>.json` with source `runtime_added`, so later
  `cloud status`, `cloud list`, `cloud collect`, and `cloud teardown` include it
  automatically.

### Live Queue Attach

After `cloud launch`, you can attach to the remote orchestrator's live trial
queue from the operator machine:

```bash
uv run crsbench cloud --config config.yaml monitor [my-experiment]
```

This command requires the config-adjacent launch state written by
`cloud launch`. CRSBench opens a temporary SSH or IAP tunnel to the remote
orchestrator automatically, attaches to the orchestrator-local Redis service,
and renders the same live queue progress view used by `crsbench run`.
If you omit `my-experiment`, `cloud monitor` infers the experiment name from
the config file.
If the orchestrator is still finishing bootstrap, `cloud monitor` waits for
the tunneled Redis endpoint to become ready up to `readiness_timeout_sec`
instead of failing on the first connection refusal.

Use `cloud status` when you want a one-shot fleet and job snapshot. For
remote-orchestrator launches it now waits for the tunneled Redis endpoint
during bootstrap, then reports lifecycle records when present or falls back to
the live RQ queue/registry view when lifecycle tracking is still empty. Use
`cloud monitor` when you want the continuously updating queue view. When the
Rich monitor needs to paginate running jobs, the caption reports whether
`n`/`p` page hotkeys are active for that session. When they are available you
can switch pages manually; if you leave the monitor idle, page rotation resumes
automatically.
`cloud status --json` and `cloud list --json` include `placement_source` so you
can tell config-declared placements from `runtime_added` ones.

Bootstrap failures are reported with per-instance evidence, so you can
diagnose issues without SSH-ing into VMs.

`ready` means the whole VM bootstrap finished, not just that GCE reported the
instance as running. Size `readiness_timeout_sec` for package install, repo
checkout, `crsbench prepare`, optional benchmark download, and Redis/queue
listener startup.

Bootstrap behavior:

- Worker and evaluator startup waits for Redis before launching managed
  `crsbench worker` / `crsbench evaluator` services.
- Transport-level Redis connection failures retry until the readiness timeout;
  fatal auth or config errors fail immediately with bootstrap evidence.
- The same startup scripts support local rehearsal via file-backed metadata and
  a foreground mode for non-`systemd` containers.
- `CRSBENCH_TIMEZONE` and `CRSBENCH_GIT_SSH_HOST` can be set through
  `cloud.env` or the role/profile env layers; defaults are
  `America/New_York` and `github.com`.
- On real GCE orchestrators, `CRSBENCH_VALKEY_IMAGE` can be overridden through
  `cloud.orchestrator.env`; the default is `valkey/valkey:8.0-alpine`.
- On real GCE VMs, bootstrap creates a dedicated `crsbench` user, configures
  passwordless `sudo` for setup, enforces Docker `cgroupfs`, and runs the
  long-lived services as `systemd --user` units with the delegated cgroup
  hierarchy expected by CRSBench and `oss-crs`.
- The checked-in smoke configs use `cloud.defaults.readiness_timeout_sec: 1200`,
  `boot_disk_size_gb: 100`, and `runtime.build_timeout: 3600` so fresh-image
  runs have time to finish bootstrap plus a real prepare/build cycle.
- The Atlantis `atlantis-multilang-given_fuzzer` sample also enables
  `runtime.pov_early_stop`, `inputs.sarif`, and `inputs.diff` to match the
  existing Atlantis sanity bug-finding preset.

### CPU Pinning

Cloud workers and evaluators automatically use `--cpuset 0-(N-1)` where
N is the VM's core count. This assigns non-overlapping core ranges to
concurrent jobs:

- Worker with `jobs: 14, cores_per_job: 16` on `n2d-standard-224`:
  job 1 gets cores 0-15, job 2 gets 16-31, ..., job 14 gets 208-223
- Evaluator with `jobs: 8, cores_per_job: 16` on `n2d-standard-128`:
  each build/verify job gets a dedicated 16-core slice

Verify on a running worker:

```bash
gcloud compute ssh WORKER --tunnel-through-iap \
  --command="sudo -H -u crsbench docker ps --format '{{.Names}}' | \
    while read c; do
      cpuset=\$(sudo -H -u crsbench docker inspect \
        --format '{{.HostConfig.CpusetCpus}}' \"\$c\")
      echo \"\$c: cpuset=\$cpuset\"
    done"
```

## Monitoring

### Fleet and Job Status

```bash
uv run crsbench cloud --config config.yaml status my-experiment
```

Shows:

- Fleet summary: each worker/evaluator VM's name, role, state, zone, and IP
- Job summary: per-job queue/lifecycle state, including running workers when
  the live queue is the only available source
- Collection summary: one-shot totals for completed, syncing, running, pending,
  failed, and orphaned work
- Recent recovery events

Add `--json` for machine-readable output:

```bash
uv run crsbench cloud --config config.yaml status my-experiment --json
```

### Recovery Events

```bash
# All events
uv run crsbench cloud --config config.yaml events my-experiment

# Filter by type
uv run crsbench cloud --config config.yaml events my-experiment --type worker_restart

# JSON output
uv run crsbench cloud --config config.yaml events my-experiment --json
```

## Collecting Artifacts

Pull experiment results from live workers to the local experiment filestore and
collect runtime logs from workers, evaluators, and the orchestrator:

```bash
uv run crsbench cloud --config config.yaml collect
```

By default, `cloud collect` infers:

- the experiment name from `experiment.name`
- the remote worker artifact directory as
  `<cloud.remote.experiment_root>/<experiment.name>`
  when `cloud.remote.experiment_root` is set
- otherwise, for legacy configs, `<storage.experiment_filestore>/<experiment.name>`

In plain terms, `cloud collect` copies trial artifacts and VM diagnostics from
the cloud VMs back to a local directory on the machine where you run the
command.

Override the local destination with `--dest`:

```bash
uv run crsbench cloud --config config.yaml collect --dest ~/my-results --force
```

Without `--dest`, results go to `storage.experiment_filestore`. With
`--dest`, collect to any writable path regardless of the VM-side
storage configuration.

Key paths:

- `storage.experiment_filestore`: local destination on your machine
- `cloud.remote.experiment_root`: remote source root on the VMs used by
  `cloud collect` and `cloud teardown`
- If `cloud.remote.experiment_root` is unset, CRSBench falls back to the
  legacy behavior of reusing `storage.experiment_filestore` for both

You can still override either value explicitly:

```bash
uv run crsbench cloud --config config.yaml collect my-experiment \
    --remote-dir /data/experiments/my-experiment
```

Use `--force` when you want to merge into an existing local destination without
being prompted, for example in non-interactive automation:

```bash
uv run crsbench cloud --config config.yaml collect my-experiment \
    --remote-dir /data/experiments/my-experiment \
    --force
```

Use `--timestamp` when the run will publish worker artifacts and you want those
artifacts written into a fresh sibling directory instead of merged into the
default local experiment directory. CRSBench names that sibling with a UTC
minute timestamp such as
`/tmp/crsbench/experiment-data/my-experiment-2026-03-21-17-45`; if that path is
already taken, it appends `-02`, `-03`, and so on:

```bash
uv run crsbench cloud --config config.yaml collect my-experiment \
    --timestamp
```

Collection behavior:

- Uses `rsync` via IAP tunnel or direct SSH, depending on config
- For direct SSH, seeds a config-adjacent `.crsbench-cloud/known_hosts` file
  and reuses the local GCE OS Login username
- Stages worker artifacts in a temporary directory, verifies at least one valid
  trial exists, then publishes to the experiment filestore
- Continues to remaining worker and evaluator VMs if one fails; exits with code
  `1` on partial failure
- Evaluator VMs are log-only for collection; they do not rsync
  `/tmp/crsbench/experiment-data/<experiment>`
- In remote-orchestrator mode, collects orchestrator logs and control-plane
  files, but worker artifact publication still comes from workers
- If Redis is unavailable, falls back to the persisted launch state plus live
  GCE inventory

Destination behavior:

- Safe to run multiple times with incremental rsync
- If the local destination already exists, CRSBench warns before merging into it
- Interactive prompt: `Continue and merge into the existing destination? [Y/n/t]`
- `Enter` or `y`: merge into the existing destination
- `n` or `no`: cancel without changing the local destination
- `t`: use the same fresh-sibling behavior as `--timestamp`
- In non-interactive runs, an existing destination fails unless you pass
  `--force` or `--timestamp` when worker artifacts will be published
- `--force` merges without prompting
- `--timestamp` chooses a fresh artifact destination when worker artifacts are
  being published
- Successful publishes refresh
  `<local-destination>/.crsbench-collect.json` with the last successful collect
  time and best-effort experiment start time

### Partial Reruns After `cloud collect`

After collecting artifacts, you can relaunch only unfinished logical trial
keys.

Derive a selector locally, then launch from that selector file:

```bash
uv run crsbench cloud --config config.yaml derive-unfinished-trial-keys
uv run crsbench cloud --config config.yaml launch \
    --only-trial-keys-file ./<experiment-name>-unfinished-trial-keys.txt
```

`derive-unfinished-trial-keys` defaults:

- input (`--from`): `<storage.experiment_filestore>/<experiment.name>`
- output (`--output`): `./<experiment-name>-unfinished-trial-keys.txt`

For the standard `cloud collect` / `cloud teardown` workflow, CRSBench also
normalizes the collected root when the published destination contains one or
more wrapper directories named after the experiment. That means the default
input path works without `--from` for the common collect layout even when the
trial tree lives under nested `<experiment.name>/` wrappers.

You can also skip the intermediate selector file and derive during launch:

```bash
uv run crsbench cloud --config config.yaml launch \
    --only-unfinished-from /path/to/collected/<experiment-name>
```

`--only-trial-keys-file` and `--only-unfinished-from` are mutually exclusive;
choose one selector source per launch.

`--rerun-failed-trials` behavior:

- `derive-unfinished-trial-keys --rerun-failed-trials`: include failed trials
  in the unfinished selector (successful trials remain excluded).
- `cloud launch --only-unfinished-from ... --rerun-failed-trials`: same
  behavior, but derivation happens inside launch. This flag requires
  `--only-unfinished-from`.

Explicit selector files passed to `--only-trial-keys-file` must be
newline-delimited logical trial keys.

Selector keys must match the current config's trial matrix. Unknown or stale
keys are not silently ignored; CRSBench fails with an unknown-trial-key error.

When `cloud collect` used a non-default destination (`--dest` or
`--timestamp`), pass that exact collected root to `--from` (for
`derive-unfinished-trial-keys`) or `--only-unfinished-from` (for launch-time
derivation). The built-in normalization only applies to the default
collect/teardown publish layout; CRSBench cannot infer arbitrary custom
destinations from the config alone.

Selector flags (`--only-trial-keys-file` / `--only-unfinished-from`) constrain
which trial-matrix keys are enqueued and run remotely; they do not change fleet
sizing or placement from the `cloud launch` configuration.

Diagnostics collected under `.crsbench-cloud/remote-logs/<experiment>/`:

- `google-startup-scripts.service` and `google-guest-agent.service` journals
- `crsbench-worker.service`, `crsbench-evaluator.service`, or
  `crsbench-orchestrator.service` user journals
- `runtime-summary.txt` with timezone, Docker cgroup driver, user-bus, linger,
  and Redis listener state
- Lightweight per-trial observability files such as `worker.log`,
  `metadata.json`, `.success`, `.failure`, and the orchestrator
  `trial_matrix.json`

## Listing Instances

Inspect the live VM inventory resolved from the experiment config and persisted
launch state:

```bash
uv run crsbench cloud --config config.yaml list
```

Add `--json` for machine-readable output:

```bash
uv run crsbench cloud --config config.yaml list --json
```

`cloud list` infers the experiment name from `experiment.name`.

## SSH Access

Open an SSH session to a live VM without manually looking up the zone:

```bash
# Exact instance name or short alias like orch, work-001, eval-001
uv run crsbench cloud --config config.yaml ssh orch
# Equivalent alias:
uv run crsbench cloud --config config.yaml shell orch
```

If you omit the instance selector, CRSBench prints the live inventory and lets
you choose interactively:

```bash
uv run crsbench cloud --config config.yaml ssh
```

`cloud ssh` infers the experiment name from `experiment.name` and reuses the
live zone chosen at launch time. The interactive shell immediately runs
`sudo -iu crsbench env -C /opt/crsbench bash -lc ...`, sourcing the
role-specific runtime env file from `/var/lib/crsbench/` first:

- worker: `/var/lib/crsbench/worker.env`
- evaluator: `/var/lib/crsbench/evaluator.env`
- orchestrator: `/var/lib/crsbench/orchestrator.env`

That means `cloud ssh` / `cloud shell` now attach with the same generated
runtime environment that the managed CRSBench service uses, not just the
`crsbench` Unix user and checkout directory.

Selectors accept the full VM name, the resolved alias, or any other
unambiguous filtered short form. For example, `eval-001` can match a longer
evaluator alias, and `eval` works when exactly one evaluator is live. If a
selector still matches multiple live instances, CRSBench shows only the
matching rows and prompts you to choose in interactive sessions.

Open a serial-console session to the same live VM inventory without manually
looking up the realized zone:

```bash
uv run crsbench cloud --config config.yaml serial orch
uv run crsbench cloud --config config.yaml serial work-001 --port 2
```

`cloud serial` reuses the same live selector resolution as `cloud ssh`, but it
attaches through `gcloud compute connect-to-serial-port` instead of OS
Login-backed SSH. CRSBench bootstraps a local guest login for serial access:

- username: `crsbench`
- password: `crsbench`

The startup scripts keep SSH password authentication disabled, so this
credential is for guest console login rather than network SSH. Root does not get
a CRSBench-managed password; log in as `crsbench` and use `sudo -i` when you
need a root shell. Override the default serial-console password by setting
`CRSBENCH_LOCAL_CONSOLE_PASSWORD` through the cloud env layers before launch.

## Remote Command Execution

Run a one-off remote command without opening an interactive shell:

```bash
uv run crsbench cloud --config config.yaml exec work-001 -- hostname
```

If you omit the instance selector, CRSBench prints the live inventory, lets you
pick a VM, and then runs the command there:

```bash
uv run crsbench cloud --config config.yaml exec -- docker ps
```

`cloud exec` infers the experiment name from `experiment.name` and reuses the
live zone chosen at launch time. Unlike `cloud ssh`, it runs as the operator SSH
login user by default, so one-off root or operator diagnostics remain
available.

The same selector rules apply to `cloud exec`, including unambiguous short
forms such as `eval-001` and role shorthands like `eval` when only one
evaluator is live. Ambiguous interactive selectors prompt from the narrowed
match list instead of exiting immediately.

## Log Following

Follow the primary CRSBench `systemd --user` journal for one live VM:

```bash
uv run crsbench cloud --config config.yaml log work-001
```

Role mapping is automatic:

- `orch` follows `crsbench-orchestrator.service`
- `work-*` follows `crsbench-worker.service`
- `eval-*` follows `crsbench-evaluator.service`

If you omit the instance selector, CRSBench prints the live inventory and lets
you choose interactively:

```bash
uv run crsbench cloud --config config.yaml log
```

`cloud log` uses the same selector rules as `cloud ssh` and `cloud exec`,
including narrowed interactive prompting for ambiguous selectors when you are
following a single target. In explicit multi-target fan-in mode, ambiguous
`--instance` selectors are rejected instead of prompting; choose a more
specific selector or use `--role`/`--all`.

For explicit multi-target fan-in, use `--all`, `--role`, or repeated
`--instance` selectors:

```bash
uv run crsbench cloud --config config.yaml log --role worker
uv run crsbench cloud --config config.yaml log --instance orch --instance work-001
uv run crsbench cloud --config config.yaml log --all
```

Multi-target sessions prefix each rendered line with the instance alias, role,
and source kind so interleaved output remains attributable. By default logs are
merged by local arrival time for the lowest latency. Use `--merge-by timestamp`
when you want best-effort journal-timestamp ordering instead:

```bash
uv run crsbench cloud --config config.yaml log --role worker --merge-by timestamp
```

## Teardown

Remove the worker/evaluator fleet after collecting results:

```bash
uv run crsbench cloud --config config.yaml teardown
```

The teardown safety flow:

1. Lists live GCE instances for the experiment
2. Cross-references with Redis readiness records when Redis is reachable (warns about mismatches)
3. Prompts for confirmation (interactive TTY required)
4. Collects artifacts from worker VMs first and log-only diagnostics from evaluator VMs
5. Collects logs from the remote orchestrator VM and all worker/evaluator VMs into `.crsbench-cloud/remote-logs/<experiment>/`
6. Aborts before deletion if any collection failed, to preserve diagnostic data; pass `--force` to delete VMs anyway
7. Returns a non-zero exit code if any collection or deletion step failed

Use `--force` to skip the confirmation prompt and proceed with deletion even
when artifact collection fails (e.g., in scripts, or when you accept the data
loss to free cloud resources):

```bash
uv run crsbench cloud --config config.yaml teardown --force
```

Use `--timestamp` when teardown should publish worker artifacts into a fresh
timestamped sibling directory instead of merging into the default local
experiment directory:

```bash
uv run crsbench cloud --config config.yaml teardown --timestamp --force
```

Teardown now reuses the same local destination safeguards as `cloud collect`:

- When worker artifacts are being published, `--timestamp` chooses a fresh
  sibling destination such as
  `/tmp/crsbench/experiment-data/my-experiment-2026-03-21-17-45`
- Without `--timestamp`, teardown prompts before merging into an existing local
  destination unless `--force` is set
- Successful teardown collections that publish worker artifacts refresh the same
  `<local-destination>/.crsbench-collect.json` marker metadata used by
  standalone `cloud collect`

You can still override the inferred experiment name and remote directory:

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

# 1. Check the managed-cloud plan without provisioning anything
uv run crsbench cloud preflight --config config.yaml

# 2. Local-orchestrator mode only: start Valkey accessible from GCE workers
uv run python scripts/valkey-helper.py --password start

# 3. Local-orchestrator mode: run experiment from this machine
uv run crsbench run --experiment-config config.yaml

# 4. Remote-orchestrator mode: provision orchestrator + workers from this machine
uv run crsbench cloud launch --config config.yaml

# 5. Check status during the run
uv run crsbench cloud status my-experiment --config config.yaml

# 6. After completion, collect artifacts
uv run crsbench cloud collect \
    --config config.yaml

# 7. Tear down the fleet (and remote orchestrator, if used)
uv run crsbench cloud teardown \
    --config config.yaml

# 8. Generate report
uv run python scripts/cpv_report.py /data/experiments/my-experiment --csv
```

For manual VM access during debugging:

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

The worker process now runs as a `crsbench` user service:

```bash
# On the worker VM
sudo -iu crsbench env \
  XDG_RUNTIME_DIR=/run/user/$(id -u crsbench) \
  DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u crsbench)/bus \
  systemctl --user status crsbench-worker.service
sudo -iu crsbench env \
  XDG_RUNTIME_DIR=/run/user/$(id -u crsbench) \
  DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u crsbench)/bus \
  journalctl --user -u crsbench-worker.service -f
```

Useful manual checks:

- Role logs are mirrored under `/var/lib/crsbench/`:
  `/var/lib/crsbench/worker.log`,
  `/var/lib/crsbench/evaluator.log`, and
  `/var/lib/crsbench/orchestrator.log`
- Bootstrap normalizes the host timezone to `CRSBENCH_TIMEZONE` (default
  `America/New_York`) and configures Docker to use the `cgroupfs` driver
  expected by `oss-crs`
- On Ubuntu-based GCE images, CRSBench installs Docker Engine from Docker's
  official apt repository rather than the distro `docker.io` packages
- Bootstrap also installs `iftop`, `rg`, and `fdfind`, and bootstraps Docker
  Buildx for the default builder context

You can verify those with:

- `timedatectl`
- `cat /etc/timezone`
- `docker info --format '{{.CgroupDriver}}'`
- `apt-cache policy docker-ce`
- `command -v iftop`
- `docker buildx ls`
- `docker buildx inspect`

You can also verify the delegated cgroup setup that both CRSBench and `oss-crs`
depend on with:

```bash
sudo ls -ld \
  /sys/fs/cgroup/user.slice/user-$(id -u crsbench).slice/user@$(id -u crsbench).service/crsbench \
  /sys/fs/cgroup/user.slice/user-$(id -u crsbench).slice/user@$(id -u crsbench).service/oss-crs

sudo -iu crsbench env \
  XDG_RUNTIME_DIR=/run/user/$(id -u crsbench) \
  DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u crsbench)/bus \
  /opt/crsbench/.venv/bin/oss-crs setup --check
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
| `bootstrap_failed` in status | Startup script error | Check the evidence field in `cloud status --json` output; attach with `cloud serial <instance>` or inspect the VM serial console in GCP Console |
| Collect fails with rsync error | SSH connectivity issue | Verify OS Login is enabled; check IAP firewall rule if using `ssh_via_iap` |
| Stale Redis entries warning | VMs were manually deleted | Safe to ignore; Redis records from deleted VMs don't affect new runs |
| Teardown returns non-zero | Collection or deletion failed for at least one VM | Check the logged worker/orchestrator errors, then rerun `cloud collect` or `cloud teardown` as needed |
| `use_os_login must be true` | Config validation | OS Login is required; do not set `use_os_login: false` |
| `exactly one of image or instance_template` | Config validation | Provide either `image` + `machine_type` + `boot_disk_size_gb` or `instance_template`, not both |
| Docker network pool exhaustion (`all predefined network addresses are exhausted`) | Too many concurrent Docker compose networks on one VM | CRSBench configures Docker with an expanded address pool (`172.16.0.0/12` with `/24` subnets, up to 4096 networks) automatically via the startup script |
| HF download fails with 401 Unauthorized | Missing `HF_TOKEN` for gated HuggingFace datasets | Add `HF_TOKEN: os.environ/HF_TOKEN` to `cloud.env` and export `HF_TOKEN` locally before launching |
| Quota exceeded on first region | All placements attempt the first region in the fallback list | Pin placements to specific regions using `region:` on each placement entry; CRSBench preflight now warns about greedy first-region overcommit |
| Workers stuck at "registering" | Worker supervisor did not report ready state | Ensure the deployed ref includes the readiness fix (commit `16e4d584`); `main` is the normal launch ref and workers with `--cpuset` now report ready before entering the supervisor loop |
| Collect fails with Permission denied on `/data` | OS Login user cannot read crsbench-owned experiment data | CRSBench uses `--rsync-path="sudo rsync"` on the remote side; ensure the crsbench user has passwordless sudo |

## See Also

- [Distributed Experiments](./distributed.md) -- full distributed experiment guide
- [Configuration Reference](./config-reference.md) -- all experiment config fields
- [Design: Cloud Orchestration](../../design/distributed/cloud-orchestration.md) -- shared cloud contract
- [Design: GCE Cloud Orchestration](../../design/distributed/gce-cloud-orchestration.md) -- GCE-specific implementation details
- [Design: GCE Cloud Orchestrator Launch](../../design/distributed/gce-cloud-orchestrator.md) -- remote-orchestrator launch contract
