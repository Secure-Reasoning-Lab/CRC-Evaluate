---
name: crsbench-bug-finding-crs-debug
description: Debug and analyze bug-finding CRS experiment results. Use when the user wants to check experiment outcomes, verify CPV counts, diagnose trial failures, or investigate why agents didn't find bugs in crsbench bug-finding experiments. Trigger this whenever the user mentions experiment results, trial logs, CPV verification, POV analysis, or asks questions like "how did the experiment go", "what were the results", "why did this trial fail", or "analyze the logs".
allowed-tools: Read, Grep, Glob, Bash, Agent
---

# CRSBench Bug-Finding Experiment Debugger

This skill analyzes completed bug-finding CRS experiments to produce accurate CPV results and diagnose infrastructure issues.

## Phase 0: Get Experiment Config from User

Before starting analysis, ask the user for the experiment config YAML path:

```
To analyze the experiment, I need the experiment config YAML path.
Example: experiment-configs/sanity-bugfinding/agent-bug-finding-full-test.yaml

Please provide the path to the experiment config.
```

If the user provides an experiment data directory instead of a config, look for the config YAML that points to that directory, or extract the needed info directly from the data directory structure.

## Phase 1: Locate Experiment Data

Read the experiment config and extract key paths:

```bash
cat <experiment-config.yaml>

# Key fields to extract:
# - storage.experiment_filestore → experiment data directory
# - storage.report_filestore → report directory
# - experiment.benchmarks → list of benchmark/harness/CPV combos
# - crs_compose → list of CRS names (keys starting with "crs-")
```

Count completion status:

```bash
# Count completed trials (have .success marker)
find <experiment_data_dir> -name ".success" | wc -l

# Count total expected from trial_matrix.json
cat <experiment_data_dir>/trial_matrix.json | python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d))"
```

## Phase 2: CPV Results

Read CPV results from `pov_store.json` for each trial:

```python
import json
with open("povs/pov_store.json") as f:
    d = json.load(f)
store_cpvs = set(d.get("cpv_to_first_pov", {}).keys())
```

You can also verify against:

- **worker.log**: `grep "CPV .* found" worker.log`
- **povs/cpvs/ directory**: each `cpv_X` subdirectory = verified CPV

### Comprehensive results script

Use this pattern to get accurate results across all CRSes:

```python
import json, glob, os, re

base = "<experiment_data_dir>"
for crs_dir in sorted(os.listdir(base)):
    if not crs_dir.startswith("crs-"):
        continue
    crs_base = os.path.join(base, crs_dir)
    for md_file in glob.glob(crs_base + "/**/trial-1/metadata.json", recursive=True):
        trial_dir = os.path.dirname(md_file)
        # ... read pov_store.json for CPV results ...
```

## Phase 3: Per-Trial Health Check

For each trial, check these files for issues:

### worker.log
- Errors/exceptions: `grep -i "error\|exception\|traceback" worker.log`
- Timeouts: `grep "Timeout reached\|SIGTERM" worker.log`
- Agent status: `grep "Agent did not report success\|Agent completed" worker.log`
- Oversized POVs: `grep "exceeds.*byte limit" worker.log` — POVs over 10MB are silently dropped
- Snapshot thread hang: `grep "Snapshot thread did not stop" worker.log`
- LiteLLM spend log failures: `grep "get_spend_logs.*failed\|Read timed out" worker.log`

### llm-summary.json
- `total_requests: 0` is suspicious for any CRS except copilot-cli (copilot uses GitHub token, not LiteLLM)
- `failure_count > 0` indicates LLM call failures
- Check `failure_categories` for `BudgetExceededError` — agent was killed by cost cap

### .snapshot-*/llm-usage.json
- Compare `key_info.spend` vs `total_cost_usd_from_logs` in llm-summary
- If key/info shows spend > $0 but llm-summary shows 0 requests → LiteLLM spend log API timed out, but agent DID run and spend money

### finder stdout log
- `output/logs/services/*_finder.stdout.log` truncated at "Agent inputs saved" means SIGTERM killed the container before log copy — this does NOT mean the agent didn't run
- Check if `log_dir/logs/` directory is empty — empty means logs were never copied (SIGTERM issue)

### POV status breakdown
From `pov_store.json`, count POVs by status:
- `cpv`: verified match to target vulnerability
- `error`: verification failed or pending
- `not_vulnerable`: POV ran but didn't crash the target
- `unintended_crash`: POV crashes a different bug than the target CPV

## Phase 4: Summarize Findings

### Per-CRS summary table

| CRS | CPVs Found | PoVs Submitted | LLM Cost |
|-----|:----------:|:--------------:|:--------:|
| ... | X/Y        | N              | $Z.ZZ    |

### Benchmark comparison table

Show which CRS found which CPV, using these markers:
- `✓` = CPV found
- `✗(N)` = N PoVs submitted but no CPV match
- `—` = no PoVs submitted
- `⚠oversz` = PoV dropped due to size limit

### Infrastructure issues

List issues found with severity:
- **Medium**: Budget exceeded errors, oversized POV drops, snapshot thread hangs
- **Low**: LiteLLM spend mismatches, cgroup warnings

### Unfound CPVs

List CPVs that no CRS found, and for each note:
- Did any CRS submit PoVs? (tried but failed)
- Did agents actually run? (check LLM spend)
- Was there a timeout or infrastructure issue?

## Phase 5: Known Bugs Checklist

Always verify these known issues:

1. **10MB POV size limit** — POVs exceeding 10MB are silently dropped during Redis transport. Check worker.log for "exceeds.*byte limit" warnings. The agent gets no feedback that its POV was rejected.

2. **LiteLLM spend log API timeout** — When key/info shows spend > $0 but llm-summary shows 0 requests, it means the agent ran and made LLM calls but the spend log API timed out during post-run accounting. The agent DID run.

3. **Snapshot thread hang** — When snapshot thread doesn't stop within timeout, `llm-summary.json` is never written. Check `.snapshot-*/llm-usage.json` for actual spend data.

4. **Format string bug** — Error message `Manager cleanup completed with issues for harness '%s': %s` appears with literal `%s` — the actual harness name and exception are not logged.

5. **SIGTERM log truncation** — When the 1200s timeout hits, the finder container is killed by SIGTERM before log copy. `finder.stdout.log` will be truncated at "Agent inputs saved" and `log_dir/logs/` will be empty. This does NOT mean the agent didn't run — check LLM spend and POV output.

6. **copilot-cli 0 LLM requests** — copilot-cli uses its own GitHub token, not the LiteLLM proxy. `total_requests: 0` and `total_cost: $0.00` is expected and normal.

7. **No native fuzzer** — The bug-finding CRS contains only a finder (agent) and inc-builder-asan. There is no native fuzzer component. If POVs were produced, the agent produced them.

## Experiment Data Directory Structure

```
<experiment_filestore>/<experiment_name>/
├── trial_matrix.json               # All jobs and their configuration
└── <crs_name>/
    └── <benchmark>/<harness>/<mode>/address/trial-1/
        ├── worker.log                    # Authoritative source for CPV results
        ├── metadata.json                 # Trial metadata
        ├── llm-summary.json              # LLM usage (may be missing)
        ├── llm-logs.json                 # Detailed LLM call logs
        ├── crs-compose.yaml              # CRS compose config
        ├── .success                      # Trial completion marker
        ├── .snapshot-NNNN/
        │   └── llm-usage.json            # Key/info spend data
        ├── povs/
        │   ├── pov_store.json            # CPV results
        │   ├── snapshot_history.json     # Verification timeline
        │   ├── snapshots/                # Per-cycle snapshots
        │   ├── cpvs/                     # Verified CPV artifacts
        │   │   └── cpv_0/
        │   │       ├── blobs/
        │   │       └── crash_logs/
        │   ├── error/
        │   ├── not_vulnerable/
        │   └── unintended/
        └── output/logs/
            ├── docker-compose.{stdout,stderr}.log
            ├── services/
            │   ├── <crs>_finder.{stdout,stderr}.log
            │   ├── <crs>_inc-builder-asan.{stdout,stderr}.log
            │   └── oss-crs-exchange.{stdout,stderr}.log
            └── crs/<crs_name>/
                ├── <crs>_finder.{stdout,stderr}.log
                └── log_dir/logs/         # Agent debug logs (empty if SIGTERM)
```
