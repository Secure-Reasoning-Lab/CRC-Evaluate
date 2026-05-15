---
name: crsbench-gen-config
description: Generate CRSBench experiment config YAML files conversationally. Use this skill whenever the user mentions experiment configs, wants to set up a CRSBench run, asks to create or scaffold a YAML config, describes an experiment they want to run (mentioning CRS names, benchmark suites, bugfinding/bugfixing), or needs help writing configuration for crsbench. Even partial descriptions like "run crs-libfuzzer on sanity" should trigger this skill.
---

# CRSBench Gen-Config Skill

Generate experiment configuration YAML files from natural language descriptions. The user describes what they want to run, and you produce a validated config file using the existing Python API.

## Workflow

### 1. Discover available options first

Before asking the user anything, check what's available in this repo:

```bash
ls benchmark-suites/*.yaml    # available suites
ls oss-crs/registry/*.yaml    # available CRS
```

This tells you which names are valid and gives you options to present to the user.

### 2. Gather missing information with AskUserQuestion

Extract what you can from the user's message, then use **AskUserQuestion** to collect anything missing. Skip questions the user already answered.

**Important**: AskUserQuestion supports max 4 options per question, but there are many CRS and benchmark suites. For selections with more than 4 options:
- List all available names in the question **description** so the user can see the full list
- Use the 4 option slots for the most relevant choices based on context
- The user can always select "Other" to type any name not in the options

**Ask in two rounds** to filter CRS by task type:

**Round 1** — Task and benchmarks (skip if already known):
1. **Task**: bugfinding or bugfixing?
2. **Benchmarks**: Show all discovered suite names in the description. Pick 3-4 most common as selectable options (e.g., sanity, smoke/bug-finding, afc/final).

**Round 2** — CRS selection (after task is known):

Read each CRS registry file (`oss-crs/registry/<name>.yaml`) and check the `type` field to filter by task:
- If task is **bugfinding**: show only CRS with `type` containing `bug-finding` (e.g., crs-libfuzzer, crs-jazzer, atlantis-c-deepgen)
- If task is **bugfixing**: show only CRS with `type` containing `bug-fixing` (e.g., crs-codex, crs-prism, crs-claude-code, crs-copilot-cli)
- Some CRS support both — include those in either case

List all matching CRS in the description, pick 3-4 most common as selectable options.

**CRS type also determines LLM config** (set automatically, don't ask the user):
- **bug-finding** CRS (pure fuzzers): set `skip_litellm: True`
- **bug-fixing** CRS (LLM-based): set `litellm_mode: "external"`

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

# Always save under experiment-configs/ directory
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

Only `name` and `crs_services` are truly required — everything else has defaults.

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
    "benchmark_suite": str | None,            # Suite name (default: None)
    "benchmarks": list[str] | None,           # Individual names (default: None)

    # === Sanitizers ===
    "sanitizers": list[str],                  # default: ["address"]
    # Options: "address", "memory", "undefined" — each creates separate trials

    # === CRS configuration (required, at least one) ===
    "crs_services": {
        "crs-name": {
            "num_cores": int,                 # CPU cores (required per CRS)
            "mem_limit": str | None,          # e.g. "8G", "16G" (optional)
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

**User says:** "bugfixing with crs-codex on afc/final, 3 trials, 16G memory, $10 budget"
```python
{
    "name": "afc-final-codex-bugfix",
    "task": "bugfixing",
    "benchmark_suite": "afc/final",
    "crs_services": {"crs-codex": {"num_cores": 8, "mem_limit": "16G"}},
    "trials": 3,
    "litellm_mode": "external",  # crs-codex is LLM-based
    "litellm_cost_budget": 10.0,
}
```

**User says:** "distributed run with crs-codex and crs-libfuzzer on smoke/bug-finding, 4 worker jobs"
```python
{
    "name": "smoke-multi-crs",
    "benchmark_suite": "smoke/bug-finding",
    "crs_services": {
        "crs-codex": {"num_cores": 8, "mem_limit": "16G"},
        "crs-libfuzzer": {"num_cores": 8},
    },
    "redis_host": "localhost:6379",
    "worker": {"jobs": 4, "cores_per_job": 8},
    "evaluator": {"jobs": 4, "cores_per_job": 4},
    "litellm_mode": "external",
}
```

## CRS Type Hints

Check `oss-crs/registry/<crs-name>.yaml` to see each CRS's `type` field:
- **bug-finding** CRS (e.g., crs-libfuzzer, crs-jazzer): pure fuzzers → set `skip_litellm: True`
- **bug-fixing** CRS (e.g., crs-codex, crs-prism, crs-claude-code): LLM-based → set `litellm_mode: "external"`
- Some CRS support both types

## Config Output Format

The generated YAML uses the **grouped contract format**:

```yaml
# Top-level
description: ...           # optional

# Sections
experiment:                 # identity + benchmark selectors
  name: ...
  task: ...
  mode: ...
  benchmark_suite: ...      # OR benchmarks: [...]
  sanitizers: [...]

crs_compose:                # per-CRS resource allocation
  crs-name:
    num_cores: 8
    mem_limit: "16G"        # optional

runtime:                    # trial execution settings
  trials: 1
  max_total_time: 28800
  build_timeout: 3600
  run_timeout: 14400
  verify_timeout: 7200
  # redis_host: ...         # for distributed mode
  # litellm: ...            # for LLM-based CRS
  # skip_litellm: true      # for pure fuzzers

storage:                    # output directories
  experiment_filestore: ./experiment-data
  report_filestore: ./report-data

worker:                     # optional, for distributed mode
  jobs: 4
  cores_per_job: 8

evaluator:                  # optional, for distributed mode
  jobs: 4
  cores_per_job: 4
```

## Schema Source & Full Reference

- **Pydantic schema**: `crsbench/validation/schemas.py` — `ExperimentConfig` class and sub-models (`CrsComposeConfig`, `WorkerConfig`, `EvaluatorConfig`, `ResourceConfig`, `ExperimentInputsConfig`)
- **Full annotated example**: `docs/experiment-config-distributed-example.yaml`
- **Generator API**: `crsbench/genconfig/generator.py` — `generate_config()`, `build_config_from_answers()`, `discover_benchmark_suites()`, `discover_crs_names()`
- **Validation**: `crsbench/validation/format_validator.py` — `validate_experiment_config_from_string()`

When in doubt about a field's exact semantics or valid values, read the Pydantic schema source — it has docstrings and validators for every field.
