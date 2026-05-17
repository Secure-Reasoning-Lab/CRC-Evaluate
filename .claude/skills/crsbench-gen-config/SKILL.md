---
name: crsbench-gen-config
description: Generate CRSBench experiment config YAML files conversationally. Use this skill whenever the user mentions experiment configs, wants to set up a CRSBench run, asks to create or scaffold a YAML config, describes an experiment they want to run (mentioning CRS names, benchmark suites, bugfinding/bugfixing), or needs help writing configuration for crsbench. Even partial descriptions like "run crs-libfuzzer on sanity" should trigger this skill.
---

# CRSBench Gen-Config Skill

Generate experiment configuration YAML files from natural language descriptions. The user describes what they want to run, and you produce a validated config file using the existing Python API.

## Workflow

### 1. Discover available options first

Before asking the user anything, check what's available in this repo. Benchmark suites are nested by category (`afc/`, `asc/`, `atlanta/`, `crsbench/`, `smoke/`); CRS registry is flat.

```bash
ls benchmark-suites/*/*.yaml    # available suites (nested by category)
ls oss-crs/registry/*.yaml      # available CRS
```

The suite identifier passed to `experiment.benchmark_suite` is the path under `benchmark-suites/` without the `.yaml` extension — e.g. `afc/final`, `smoke/sanity`, `crsbench/all`. See `benchmark-suites/README.md` for the full layout and counts.

### 2. Gather missing information with AskUserQuestion

Extract what you can from the user's message, then use **AskUserQuestion** to collect anything missing. Skip questions the user already answered.

**Important**: AskUserQuestion supports max 4 options per question, but there are many CRS and benchmark suites. For selections with more than 4 options:
- List all available names in the question **description** so the user can see the full list
- Use the 4 option slots for the most relevant choices based on context
- The user can always select "Other" to type any name not in the options

**Ask in two rounds** to filter CRS by task type:

**Round 1** — Task and benchmarks (skip if already known):
1. **Task**: bugfinding or bugfixing?
2. **Benchmarks**: Show all discovered suite paths in the description. Pick 3-4 most common as selectable options (e.g., `smoke/sanity`, `smoke/all`, `afc/final`, `crsbench/all`).

**Round 2** — CRS selection (after task is known):

The CRS registry uses a clear naming convention you can rely on without reading every file:

- `crs-bug-finding-<agent>` → **bug-finding** (LLM-based bug discovery): `crs-bug-finding-claude-code`, `crs-bug-finding-codex`, `crs-bug-finding-copilot-cli`, `crs-bug-finding-gemini-cli`, `crs-bug-finding-opencode`, plus model/CLI variants (`*-old-cli`, `*-old-model`, `*-glm-5-1`, `*-gpt-oss-*`)
- `crs-<agent>` (no `bug-finding` infix) → **bug-fixing** (LLM-based patching): `crs-claude-code`, `crs-codex`, `crs-copilot-cli`, `crs-gemini-cli`, `crs-opencode`, `crs-prism`, plus variants
- Pure fuzzers → **bug-finding** (no LLM): `crs-libfuzzer`, `crs-jazzer`, `atlantis-c-deepgen`, `atlantis-java-main`, `atlantis-multilang-given_fuzzer`, `atlantis-multilang-wo-concolic`
- Other / multi-purpose: `crs-multi-retrieval`, `crs-patch-ensemble`, `crs-shellphish`, `crs-vincent`, `fuzzing-brain`, `buttercup-seed-gen`, `lacrosse-seed-gen`, `builder-sidecar-*`, `roboduck` — read their registry `type:` field

When uncertain, read `oss-crs/registry/<name>.yaml` and check the `type:` list (`bug-finding` and/or `bug-fixing`).

Filter the options surfaced to the user by the selected task. List all matching CRS names in the question description; pick 3-4 most common as selectable options.

**LLM config is determined per-CRS, not per-task** (set automatically, don't ask the user):
- **Pure fuzzer** CRS (the list above) → `skip_litellm: True`
- **All other** CRS (LLM-based bug-finding *and* bug-fixing) → `litellm_mode: "external"`

Use sensible defaults for anything the user doesn't specify (e.g., 8 cores, delta mode, 1 trial, no memory limit). The goal is to minimize questions — two rounds of AskUserQuestion at most, then fill defaults for the rest.

### 3. Build the answers dict and generate

Build a Python dict and call the generator API:

```python
from pathlib import Path
from crsbench.genconfig.generator import generate_config

answers = {
    "name": "my-experiment",
    "task": "bugfinding",
    "mode": "delta",
    "benchmark_suite": "smoke/sanity",
    "crs_services": {"crs-libfuzzer": {"num_cores": 8}},
    # ... other fields as needed (see schema below)
}

# Default output: experiment-configs/<slugified-name>.yaml (flat).
# Real configs in this repo live in subdirs by deployment kind
# (local/, gcp/, agentic-cli/, smoke-testing/) — pass output_path
# explicitly to follow that convention.
output_path = Path("experiment-configs") / f"{answers['name']}.yaml"
success = generate_config(
    output_path=output_path,
    answers=answers,
    validate=True,
)
```

Always set `validate=True` — this catches schema errors before writing the file.

### 4. Show the result

After generating, read and display the YAML file so the user can review it. If they want changes, update the answers dict and regenerate.

## Answers Dict Schema

Only `name` and `crs_services` are truly required by the generator — everything else has defaults. The generator emits the grouped contract format; `crs_compose.oss_crs_infra` defaults to `{shared: true}` at the schema level and does not need to appear in the answers dict.

```python
{
    # === Required ===
    "name": str,                              # Unique experiment identifier

    # === Experiment identity ===
    "description": str | None,                # Human-readable note (default: None)
    "task": "bugfinding" | "bugfixing",       # default: "bugfinding"
    "mode": "delta" | "full" | "all" | "auto",  # default: "delta"
    #   delta = code diffs between two commits (most common)
    #   full  = single vulnerable commit snapshot
    #   all   = run both delta and full
    #   auto  = pick per benchmark (delta preferred)

    # === Benchmark selection (pick one) ===
    "benchmark_suite": str | None,            # Suite path, e.g. "smoke/sanity", "afc/final"
    "benchmarks": list[str] | None,           # Individual names (default: None)

    # === Sanitizers ===
    "sanitizers": list[str],                  # default: ["address"]
    # Options: "address", "memory", "undefined" — each creates separate trials

    # === CRS configuration (required, at least one) ===
    "crs_services": {
        "crs-name": {
            "num_cores": int,                 # CPU cores (required per CRS)
            "mem_limit": str | None,          # e.g. "8G", "16G" (optional)
            # Per-CRS extras not emitted by the generator but supported by the
            # schema — add by hand after generation if you need them:
            #   additional_env: {KEY: VALUE, ...}
            #   budget_policy: "continue" | "terminate"  (default: "continue";
            #     "terminate" requires runtime.snapshot_period > 0)
        },
    },

    # === Runtime ===
    "trials": int,                            # default: 1
    "max_total_time": int,                    # seconds, default: 28800 (8h)
    "build_timeout": int,                     # seconds, default: 3600 (1h)
    "run_timeout": int,                       # seconds, default: 14400 (4h)
    "verify_timeout": int,                    # seconds, default: 7200 (2h)
    "pov_early_stop": bool,                   # default: False

    # === LiteLLM (important for LLM-based CRS) ===
    "litellm_mode": "external" | None,        # default: None
    "skip_litellm": bool,                     # True for pure fuzzers (default: False)
    "llm_tracking_enabled": bool,             # default: True
    "litellm_cost_budget": float | None,      # USD per trial (default: None)

    # === Storage ===
    "experiment_filestore": str,              # default: "./experiment-data"
    "report_filestore": str,                  # default: "./report-data"

    # === Distributed (optional) ===
    "redis_host": str | None,                 # e.g. "localhost:6379" (default: None)
    "worker": {"jobs": int, "cores_per_job": int} | None,
    "evaluator": {"jobs": int, "cores_per_job": int} | None,
}
```

Fields the generator does not emit (but you can hand-edit the output to add):
- `resources:` (`cores_per_trial`, `memory_per_trial`, `cpu_tag`) — recommended for real runs; see examples below
- `cloud:` — **required for GCP runs**; copy the block from `experiment-configs/gcp/bug-finding.yaml`
- `runtime.source_mode: pkgs` — typical for cloud runs
- `runtime.per_pov_verify_timeout`, `runtime.snapshot_period`
- `runtime.inputs` (pov / sarif / seed / diff) — used by full-pipeline fixing configs to chain off finding output via `inputs.pov.from_experiment_by_crs` (see "Full-Pipeline Chaining" below)
- `storage.cleanup_after_trial`, `storage.keep_only_results`, `storage.copy_results_after_trial`, `storage.results_filestore`

## Natural Language → Answers Dict Examples

**User says:** "bugfinding with crs-libfuzzer on the sanity suite"
```python
{
    "name": "sanity-libfuzzer",
    "task": "bugfinding",
    "benchmark_suite": "smoke/sanity",
    "crs_services": {"crs-libfuzzer": {"num_cores": 8}},
    "skip_litellm": True,  # crs-libfuzzer is a pure fuzzer
}
```

**User says:** "bugfinding with claude-code on smoke/all"
```python
{
    "name": "smoke-claude-finding",
    "task": "bugfinding",
    "benchmark_suite": "smoke/all",
    # LLM-based bug-finding CRS: note the "crs-bug-finding-" prefix
    "crs_services": {"crs-bug-finding-claude-code": {"num_cores": 8}},
    "litellm_mode": "external",
}
```

**User says:** "bugfixing with crs-codex on afc/final, 3 trials, 16G memory, $10 budget"
```python
{
    "name": "afc-final-codex-bugfix",
    "task": "bugfixing",
    "benchmark_suite": "afc/final",
    # Bug-fixing variant: no "bug-finding" infix
    "crs_services": {"crs-codex": {"num_cores": 8, "mem_limit": "16G"}},
    "trials": 3,
    "litellm_mode": "external",
    "litellm_cost_budget": 10.0,
}
```

**User says:** "distributed run with crs-codex and crs-libfuzzer on smoke/all, 4 worker jobs"
```python
{
    "name": "smoke-multi-crs",
    "benchmark_suite": "smoke/all",
    "crs_services": {
        "crs-codex": {"num_cores": 8, "mem_limit": "16G"},
        "crs-libfuzzer": {"num_cores": 8},
    },
    "redis_host": "localhost:6379",
    "worker": {"jobs": 4, "cores_per_job": 8},
    "evaluator": {"jobs": 4, "cores_per_job": 4},
    "litellm_mode": "external",
    # Note: skip_litellm cannot be set per-CRS, so when mixing pure-fuzzer
    # and LLM-based CRS keep litellm_mode external — crs-libfuzzer simply
    # ignores it.
}
```

## CRS Type Hints

Check `oss-crs/registry/<crs-name>.yaml` to see each CRS's `type` field:
- `bug-finding` CRS with `crs-bug-finding-` prefix → LLM-based discovery → `litellm_mode: "external"`
- `bug-finding` CRS with `crs-libfuzzer` / `crs-jazzer` / `atlantis-*` names → pure fuzzers → `skip_litellm: True`
- `bug-fixing` CRS (`crs-claude-code`, `crs-codex`, `crs-prism`, etc.) → LLM-based patching → `litellm_mode: "external"`
- Some CRS list multiple types — pick the right one for the chosen task

## Config Output Format

The generated YAML uses the **grouped contract format** consumed by `validate_experiment_config_from_string`:

```yaml
# Top-level
description: ...           # optional

# Sections
experiment:                 # identity + benchmark selectors
  name: ...
  task: ...
  mode: ...
  benchmark_suite: ...      # e.g. smoke/sanity, afc/final   (OR benchmarks: [...])
  sanitizers: [...]

crs_compose:                # per-CRS resource allocation (flat keys)
  # oss_crs_infra: {shared: true}   # optional; defaults to shared=true
  crs-name:
    num_cores: 8
    mem_limit: "16G"        # optional
    # additional_env: {...} # optional
    # budget_policy: terminate  # optional ("continue" default)

runtime:                    # trial execution settings
  trials: 1
  max_total_time: 28800
  build_timeout: 3600
  run_timeout: 14400
  verify_timeout: 7200
  # redis_host: ...         # for distributed mode
  # litellm: {mode: external, cost_budget: 10.0}   # for LLM-based CRS
  # skip_litellm: true      # for pure fuzzers
  # source_mode: pkgs       # typical for cloud runs

storage:                    # output directories
  experiment_filestore: ./experiment-data
  report_filestore: ./report-data

worker:                     # optional, for distributed mode
  jobs: 4
  cores_per_job: 8

evaluator:                  # optional, for distributed mode
  jobs: 4
  cores_per_job: 4

# resources:                # optional but recommended for real runs
#   cores_per_trial: 16
#   memory_per_trial: "64G"

# cloud:                    # required for `crsbench cloud …` GCE runs
#   ...                     # copy from experiment-configs/gcp/*.yaml
```

## Reference Configs in the Repo

Curated, working examples live under `experiment-configs/` and are the best starting points to mirror:

- `experiment-configs/smoke-testing/smoke.yaml` — tiny end-to-end pipeline smoke
- `experiment-configs/local/{bug-finding,bug-fixing,full-pipeline-fixing}.yaml` — single-machine, single CRS
- `experiment-configs/gcp/{bug-finding,bug-fixing,full-pipeline-fixing}.yaml` — GCE-hosted with a populated `cloud:` block
- `experiment-configs/agentic-cli/{bug-finding,bug-fixing,full-pipeline-fixing}.yaml` — multi-CRS comparison across agentic CLIs

Note: there is no `full-pipeline-finding.yaml` — the finding side of a full-pipeline run reuses the same `bug-finding.yaml`. See the "Full-Pipeline Chaining" section below.

See `experiment-configs/README.md` for the layout convention, required fields recap, and the launch/monitor/collect/teardown command sequence for GCP runs.

## Full-Pipeline Chaining (Finding → Fixing)

A **full-pipeline experiment** chains a bug-finding run into a bug-fixing run so the fixing CRS patches POVs that another CRS actually discovered, instead of the benchmark's ground-truth POVs. It measures end-to-end discover-and-patch behavior.

It is **not a separate mode** in the generator. It's two independent `crsbench run` invocations:

1. **Phase 1** — run the existing `bug-finding.yaml` unchanged.
2. **Phase 2** — run `full-pipeline-fixing.yaml`, which is identical to `bug-fixing.yaml` except that `runtime.inputs.pov.from_experiment_by_crs` points at the phase-1 output instead of falling back to ground-truth POVs.

### Wiring

`from_experiment_by_crs` is a per-CRS map. Each key is a **fixing-side** CRS in this config's `crs_compose`; each value is a path to that CRS's bug-finding subtree from phase 1:

```yaml
runtime:
  inputs:
    pov:
      max_variants_per_cpv: 1
      from_experiment_by_crs:
        crs-claude-code: .run/local/experiment-data/local-bug-finding/local-bug-finding/local-bug-finding/crs-bug-finding-claude-code
```

The on-disk path layout produced by phase 1 is:

```
<storage.experiment_filestore>/<experiment.name>/<experiment.name>/<finding-crs-name>
```

The duplicated `<experiment.name>` segment reflects how the orchestrator nests per-experiment subtrees. If phase-1 `experiment.name` or `storage.experiment_filestore` changes, update phase-2 paths to match.

Multi-CRS pairing (e.g. `agentic-cli/full-pipeline-fixing.yaml`) pairs each fixing CRS with its same-CLI finding counterpart by convention — `crs-bug-finding-<agent>` (finding side) → `crs-<agent>` (fixing side). A fixing CRS without an entry in the map produces no trials.

### How to generate one

The generator does **not** emit `runtime.inputs`. Two practical options:

1. **Hand-edit after generation** — call `generate_config(...)` with `task: "bugfixing"`, then read the result and append the `inputs.pov.from_experiment_by_crs` block by hand.
2. **Copy a reference** — start from `experiment-configs/{local,gcp,agentic-cli}/full-pipeline-fixing.yaml` and adjust `experiment.name`, the finding-side path, and `crs_compose`.

When the user describes a full-pipeline run, ask which phase they want generated:
- If phase 1: produce a normal `bug-finding` config and remind them that the fixing-side path will reference its `experiment.name` and `experiment_filestore`.
- If phase 2: produce a `bugfixing` config and then hand-edit in the `runtime.inputs.pov.from_experiment_by_crs` map using paths derived from the phase-1 config the user describes.

### Cloud caveat

For GCE runs, phase 1 must be `cloud collect`-ed (and typically `cloud teardown`-ed) **before** phase 2 launches, because phase 2 reads POVs from the orchestrator's `experiment_filestore` on disk.

Reference: `docs/experiments/full-pipeline.md` (canonical spec, failure semantics, multi-CRS pairing rules).

## Schema Source & Full Reference

- **Pydantic schema**: `crsbench/validation/schemas.py` — `ExperimentConfig` and sub-models (`CrsComposeConfig`, `CrsComposeServiceConfig`, `CrsComposeInfraConfig`, `WorkerConfig`, `EvaluatorConfig`, `ResourceConfig`, `ExperimentInputsConfig`, `CloudConfig`)
- **Full annotated example**: `docs/experiment-config-distributed-example.yaml`
- **Generator API**: `crsbench/genconfig/generator.py` — `generate_config()`, `build_config_from_answers()`, `discover_benchmark_suites()`, `discover_crs_registry()`, `discover_crs_names()`
- **Validation**: `crsbench/validation/format_validator.py` — `validate_experiment_config_from_string()`
- **User-facing config layout**: `experiment-configs/README.md`
- **Suite layout and selectors**: `benchmark-suites/README.md`

When in doubt about a field's exact semantics or valid values, read the Pydantic schema source — it has docstrings and validators for every field.
