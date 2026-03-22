# GCE Cloud Orchestration

Guide for provisioning and managing remote orchestrator plus worker VMs for
CRSBench experiments on GCE.

CRSBench uses a provider-neutral top-level `cloud.*` config shape, but the only
implemented managed backend today is GCE through `cloud.providers.gce`. This
guide covers that current backend end to end.

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

| Field | Required | Description |
|---|---|---|
| `cloud.defaults` | no | Provider-agnostic launch/bootstrap defaults merged into every cloud role |
| `cloud.remote.experiment_root` | no | Remote experiment root used by `cloud collect` / `cloud teardown`; defaults to `storage.experiment_filestore` for backward compatibility |
| `cloud.env` | no | Global environment variables merged into every launched cloud role |
| `cloud.providers.gce.project` | yes | GCP project ID used for all referenced GCE resources |
| `cloud.providers.gce.defaults` | no | Provider-specific overrides for `cloud.defaults` |
| `cloud.providers.gce.region` | no | Default GCE region used when orchestrator or placements do not override `region` |
| `cloud.providers.gce.regions` | no | Ordered default candidate regions used for regional placement and regional fallback |
| `cloud.providers.gce.zones` | no | Ordered default candidate zones used when orchestrator or placements do not override `zones` |
| `cloud.providers.gce.fallback` | no | Default policy for retrying later candidate zones after zonal placement failure |
| `cloud.providers.gce.profile_defaults` | no | Default instance-profile fields merged into every named GCE profile |
| `cloud.providers.gce.instance_profiles.<name>` | yes | Reusable machine/image/service-account bundle for orchestrator or workers |
| `cloud.orchestrator.zone` | no | Backward-compatible single preferred orchestrator zone; normalized into `zones` |
| `cloud.orchestrator.region` | no | Optional orchestrator region; enables regional bulk placement |
| `cloud.orchestrator.regions` | no | Ordered candidate regions for regional placement plus runtime fallback |
| `cloud.orchestrator.zones` | no | Ordered candidate zones for the orchestrator VM |
| `cloud.orchestrator.fallback` | no | Override for orchestrator zone retry behavior |
| `cloud.orchestrator.instance_profile` | yes | Instance profile name for the orchestrator VM |
| `cloud.workers.defaults.count` | no | Default number of workers to create per placement |
| `cloud.workers.defaults.instance_profile` | no | Default worker instance profile |
| `cloud.workers.defaults.region` | no | Role-level default region for worker placements |
| `cloud.workers.defaults.regions` | no | Ordered role-level default candidate regions for worker placements |
| `cloud.workers.defaults.zones` | no | Role-level default candidate zones for worker placements |
| `cloud.workers.defaults.fallback` | no | Role-level default fallback policy for worker placements |
| `cloud.workers.placements[].zone` | no | Backward-compatible single preferred worker zone; normalized into `zones` |
| `cloud.workers.placements[].region` | no | Optional worker region for regional bulk placement |
| `cloud.workers.placements[].regions` | no | Ordered candidate regions for one worker placement |
| `cloud.workers.placements[].zones` | no | Ordered candidate zones for one worker placement |
| `cloud.workers.placements[].fallback` | no | Per-placement override for worker zone retry behavior |
| `cloud.workers.placements[].count` | no | Number of workers to create in that placement |
| `cloud.workers.placements[].instance_profile` | no | Instance profile override for that placement |
| `cloud.evaluators.defaults.count` | no | Default number of evaluators to create per placement |
| `cloud.evaluators.defaults.instance_profile` | no | Default evaluator instance profile |
| `cloud.evaluators.defaults.region` | no | Role-level default region for evaluator placements |
| `cloud.evaluators.defaults.regions` | no | Ordered role-level default candidate regions for evaluator placements |
| `cloud.evaluators.defaults.zones` | no | Role-level default candidate zones for evaluator placements |
| `cloud.evaluators.defaults.fallback` | no | Role-level default fallback policy for evaluator placements |
| `cloud.evaluators.placements[].zone` | no | Backward-compatible single preferred evaluator zone; normalized into `zones` |
| `cloud.evaluators.placements[].region` | no | Optional evaluator region for regional bulk placement |
| `cloud.evaluators.placements[].regions` | no | Ordered candidate regions for one evaluator placement |
| `cloud.evaluators.placements[].zones` | no | Ordered candidate zones for one evaluator placement |
| `cloud.evaluators.placements[].fallback` | no | Per-placement override for evaluator zone retry behavior |
| `cloud.evaluators.placements[].count` | no | Number of evaluators to create in that placement |
| `cloud.evaluators.placements[].instance_profile` | no | Instance profile override for that placement |

Provider-neutral configs do not repeat `provider` on orchestrator or
placements. CRSBench resolves the provider from the referenced
`cloud.providers.<provider>.instance_profiles` catalog, and one launch cannot
mix providers across orchestrator, workers, and evaluators.
Instance-profile keys must also be globally unique across provider catalogs, so
the same profile name cannot be reused under multiple `cloud.providers.*`
entries. Today the only supported catalog is `cloud.providers.gce`.

Instance profiles carry the per-VM details such as `machine_type`,
`boot_disk_size_gb`, `image` or `instance_template`, `service_account_email`,
`owner_label`, `labels`, `metadata`, `ssh_via_iap`, and
`assign_external_ip`.

`ssh_via_iap` controls how operators connect. `assign_external_ip` controls
whether GCE attaches an external NAT interface for outbound package installs,
GitHub clone, benchmark downloads, and image pulls. The default is `true`.
Set `assign_external_ip: false` only when your project already provides private
egress, such as Cloud NAT.

When an effective `region` or ordered `regions` list is present, CRSBench uses
GCE regional bulk insert with `ANY_SINGLE_ZONE`. Optional `zones` become an
allowlist inside the effective region set. CRSBench validates that every listed
zone belongs to one of the effective regions before any VM create request is
sent.

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

Fallback policy uses the same precedence, ending with `true` if nothing is
configured. When `fallback: true`, CRSBench retries later declared regions or
zones only for recognized placement failures. When `fallback: false`, the first
placement failure for that logical slot fails the launch and tears down any
instances that were already created.

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

Cloud VMs always run `crsbench prepare`. `download_benchmarks: auto` skips the
VM-side download only when `benchmark_suite: sanity`; other suites download
before the worker joins Redis.

On the remote orchestrator, the startup script binds Valkey on `127.0.0.1` for
the local `crsbench run` path and on the VM's discovered internal address for
workers. It does not expose Redis on `0.0.0.0`.

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
5. Lets the remote orchestrator VM clone CRSBench, run `crsbench prepare`, optionally download benchmarks, start Valkey, rewrite the experiment config to use local Redis, wait for the pre-provisioned workers/evaluators to report ready, and run `crsbench run`

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

### Live Queue Attach

After `cloud launch`, you can attach to the remote orchestrator's live trial
queue from the operator machine:

```bash
uv run crsbench cloud --config config.yaml monitor my-experiment
```

This command requires the config-adjacent launch state written by
`cloud launch`. CRSBench opens a temporary SSH or IAP tunnel to the remote
orchestrator automatically, attaches to the orchestrator-local Redis service,
and renders the same live queue progress view used by `crsbench run`.
If the orchestrator is still finishing bootstrap, `cloud monitor` waits for
the tunneled Redis endpoint to become ready up to `readiness_timeout_sec`
instead of failing on the first connection refusal.

Use `cloud status` when you want a one-shot fleet and job snapshot. For
remote-orchestrator launches it now waits for the tunneled Redis endpoint
during bootstrap, then reports lifecycle records when present or falls back to
the live RQ queue/registry view when lifecycle tracking is still empty. Use
`cloud monitor` when you want the continuously updating queue view.

Bootstrap failures are reported with per-instance evidence, so you can
diagnose issues without SSH-ing into VMs.

`ready` means the whole VM bootstrap finished, not just that GCE reported the
instance as running. Size `readiness_timeout_sec` for package install, repo
checkout, `crsbench prepare`, optional benchmark download, and Redis/queue
listener startup.

Worker bootstrap now polls the configured Redis endpoint before starting the
managed `crsbench worker` process, and evaluator bootstrap uses the same host
bootstrap path before launching a managed `crsbench evaluator` service with the
experiment config embedded in VM metadata. That closes the gap where workers or
evaluators could terminally fail before the remote orchestrator had finished
starting Valkey. Transport-level connection failures are retried until the
readiness timeout, while fatal Redis auth/config errors still fail immediately
with bootstrap evidence.
The same startup scripts also support local rehearsal via file-backed metadata
and a foreground launcher mode for non-`systemd` containers. Startup-time
settings such as `CRSBENCH_TIMEZONE` and `CRSBENCH_GIT_SSH_HOST` can be
overridden through `cloud.env` or the role/profile env layers; when unset,
bootstrap still defaults to `America/New_York` and `github.com`. On real GCE
orchestrators, `CRSBENCH_VALKEY_IMAGE` can be overridden through
`cloud.orchestrator.env` while still defaulting to `valkey/valkey:8.0-alpine`.
On real GCE VMs,
the scripts now create a dedicated `crsbench` user, grant passwordless `sudo`
for disposable-host bootstrap, install the user-session support package needed
for `/run/user/<uid>/bus`, enforce Docker `cgroupfs`, and run the long-lived
orchestrator/worker processes as `systemd --user` services while pre-creating
the delegated `user@<uid>.service/crsbench` and
`user@<uid>.service/oss-crs` cgroup hierarchies expected by the CRSBench
runtime and the `oss-crs` CLI.
The checked-in smoke configs use `cloud.defaults.readiness_timeout_sec: 1200`,
`boot_disk_size_gb: 100` via `cloud.providers.gce.profile_defaults`, and
`runtime.build_timeout: 3600` so fresh-image smoke runs have room to finish
bootstrap plus a real CRS prepare/build cycle. The Atlantis
`atlantis-multilang-given_fuzzer` sample also enables `runtime.pov_early_stop`
plus `inputs.sarif` and `inputs.diff` to match the existing Atlantis sanity
bug-finding preset shape.

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
the cloud VMs back into a local directory on the machine where you run the
command. By default, CRSBench merges worker artifacts into the existing local
experiment directory if one already exists.

`storage.experiment_filestore` is the local destination on your machine.
`cloud.remote.experiment_root` is the remote source root on the VMs used by
`cloud collect` and `cloud teardown`. If you omit
`cloud.remote.experiment_root`, CRSBench falls back to the legacy behavior of
reusing `storage.experiment_filestore` for both.

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

- Uses rsync (via IAP tunnel or direct SSH depending on config)
- For direct SSH, seeds a config-adjacent `.crsbench-cloud/known_hosts` file and reuses the local GCE OS Login username
- Stages worker artifacts in a temporary directory, verifies at least one valid trial exists, then publishes to the experiment filestore
- Continues to remaining worker/evaluator VMs if one fails; exits with code 1 on partial failure
- Evaluator VMs are log-only for collection; they do not rsync `/tmp/crsbench/experiment-data/<experiment>` because build/verify work stays in transient evaluator scratch space instead of a worker-style experiment tree
- Safe to run multiple times (incremental rsync), but when the local destination already exists CRSBench warns before merging into it
- Interactive runs prompt `Continue and merge into the existing destination? [Y/n/t]`
- Press `Enter` or `y` to merge into the existing destination
- Press `n` or `no` to cancel the collect run without changing the local destination
- Press `t` to use the same fresh-sibling behavior as `--timestamp`
- In non-interactive runs, an existing destination causes `cloud collect` to fail unless you pass `--force` or `--timestamp` when the run will publish worker artifacts
- In non-interactive runs, `--force` merges into the existing destination without prompting, while `--timestamp` chooses a fresh artifact destination when worker artifacts are being published
- Successful collect runs that actually publish worker artifact data refresh a hidden local marker at `<local-destination>/.crsbench-collect.json` with the last successful artifact collect time and best-effort experiment start time
- Also collects VM diagnostics under `.crsbench-cloud/remote-logs/<experiment>/`, including:
  - `google-startup-scripts.service` and `google-guest-agent.service` journals
  - `crsbench-worker.service`, `crsbench-evaluator.service`, or `crsbench-orchestrator.service` user journals
  - `runtime-summary.txt` with timezone, Docker cgroup driver, user-bus, linger, and Redis listener state
  - lightweight per-trial observability files such as `worker.log`, `metadata.json`, `.success`, `.failure`, and the orchestrator `trial_matrix.json`
- In remote-orchestrator mode, collects orchestrator logs and control-plane files, but trial artifact publication still comes from workers
- If Redis is unavailable, falls back to the persisted launch state plus live GCE inventory

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
6. Deletes VMs even if some collections fail, to avoid leaking cloud resources
7. Returns a non-zero exit code if any collection or deletion step failed

Use `--force` to skip the confirmation prompt (e.g., in scripts):

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

The worker and evaluator launchers also mirror stdout/stderr into role-specific
files under `/var/lib/crsbench/`, so you can inspect `/var/lib/crsbench/worker.log`
or `/var/lib/crsbench/evaluator.log` directly on the VM. The remote orchestrator
continues to mirror into `/var/lib/crsbench/orchestrator.log`.

During bootstrap, both orchestrator and worker VMs normalize the host timezone
to `CRSBENCH_TIMEZONE` (default `America/New_York`) and configure Docker to use
the `cgroupfs` driver expected by `oss-crs`. On Ubuntu-based GCE images, CRSBench now installs
Docker Engine from Docker's official apt repository rather than the distro
`docker.io` packages. If you inspect a VM manually, verify these with
`timedatectl`, `cat /etc/timezone`, `docker info --format '{{.CgroupDriver}}'`,
and `apt-cache policy docker-ce`.
Bootstrap also installs `iftop`, `rg`, and `fdfind`, and bootstraps Docker
Buildx for the default builder context so ad hoc network and Docker debugging
on the VM uses the same toolchain as CRSBench. Verify that with
`command -v iftop`, `docker buildx ls`, or `docker buildx inspect`.
You can also verify the delegated cgroup setup that both CRSBench and
`oss-crs` depend on with:

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
| `bootstrap_failed` in status | Startup script error | Check the evidence field in `cloud status --json` output; inspect VM serial console via GCP Console |
| Collect fails with rsync error | SSH connectivity issue | Verify OS Login is enabled; check IAP firewall rule if using `ssh_via_iap` |
| Stale Redis entries warning | VMs were manually deleted | Safe to ignore; Redis records from deleted VMs don't affect new runs |
| Teardown returns non-zero | Collection or deletion failed for at least one VM | Check the logged worker/orchestrator errors, then rerun `cloud collect` or `cloud teardown` as needed |
| `use_os_login must be true` | Config validation | OS Login is required; do not set `use_os_login: false` |
| `exactly one of image or instance_template` | Config validation | Provide either `image` + `machine_type` + `boot_disk_size_gb` or `instance_template`, not both |

## See Also

- [Distributed Experiments](./distributed.md) -- full distributed experiment guide
- [Configuration Reference](./config-reference.md) -- all experiment config fields
- [Design: Cloud Orchestration](../../design/distributed/cloud-orchestration.md) -- shared cloud contract
- [Design: GCE Cloud Orchestration](../../design/distributed/gce-cloud-orchestration.md) -- GCE-specific implementation details
- [Design: GCE Cloud Orchestrator Launch](../../design/distributed/gce-cloud-orchestrator.md) -- remote-orchestrator launch contract
