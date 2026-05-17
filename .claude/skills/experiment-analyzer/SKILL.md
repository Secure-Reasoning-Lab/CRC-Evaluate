---
name: experiment-analyzer
description: Analyze CRSBench experiment-data directories to diagnose issues. Use when reviewing crsbench run results, identifying failures, and summarizing LLM costs.
arg: experiment_dir
arg_description: Path to experiment-data directory (e.g., /path/to/experiment-data/experiment-name)
---

# Experiment Data Analyzer

Analyze a CRSBench experiment-data directory to provide a comprehensive summary of trial results, failures, and issues.

## Input

The skill receives `experiment_dir` as an argument:
- Usage: `/experiment-analyzer /path/to/experiment-data/experiment-name`
- The argument should be the full path to an experiment directory containing `trial_matrix.json`
- Typical locations (per `experiment-configs/*/`):
  - Local runs: `./experiment-data/<experiment-name>` or `.run/<kind>/experiment-data/<experiment-name>`
  - GCP runs: `/data/crsbench/experiment-data/<experiment-name>`

**If no argument provided**: Ask the user for the experiment directory path before proceeding.

## Analysis Strategy

### Step 1: Load Trial Matrix
Read `{experiment_dir}/trial_matrix.json` to get the full list of trials:
- Total trial count
- CRS / benchmark / harness / mode / sanitizer combinations
- For bug-fixing trials, `target_cpv_id` identifies which CPV the trial targets

### Step 2: Scan All Trial Directories
The trial directory layout depends on trial mode:
- **Bug-finding** (CRS name starts with `crs-bug-finding-`, e.g. `crs-bug-finding-claude-code`):
  `<crs>/<benchmark>/<harness>/<mode>/<sanitizer>/trial-<N>/`
- **Bug-fixing / patch-generation** (e.g. `crs-claude-code`, `crs-codex`, `crs-opencode`):
  `<crs>/<benchmark>/<harness>/<cpv>/<mode>/<sanitizer>/trial-<N>/`
- **Pure fuzzers** (`crs-libfuzzer`, `crs-jazzer`, `atlantis-*`) follow the bug-finding layout.

For each trial directory:
1. Check for `.success` / `.fail` markers (existence = **normal termination**, NOT successful patch).
2. Read `metadata.json` for trial metadata (mode, sanitizer, timestamps, `build_time`, `run_time`, `worker_machine`). Schema: `TrialMetadata` in `crsbench/validation/schemas.py`.
3. Read `patch_verification_results.json` for **actual patch success** (only present for bug-fixing trials).
4. Collect `llm-usage.json` data (`LLMUsage` / `ModelUsage` schema).
5. Optionally read `llm-summary.json` for per-trial cost summary (includes `total_cost_usd_from_key`).

**IMPORTANT**: `.success` marker only means the CRS process completed normally. It does NOT mean:
- Patches were generated
- Patches are valid
- The vulnerability was fixed

**Actual patch success** (bug-fixing): `patch_verification_results.json` shows `summary.valid > 0`.

**Actual bug-finding success**: At least one POV file in `output/povs/` reproduces a CPV (`povs/pov_store.json` records the reproduction state).

### Step 3: Categorize Results

**Success**:
- **Bug-finding**: At least one valid POV recorded in `povs/pov_store.json`.
- **Bug-fixing**: `patch_verification_results.json` shows `summary.valid > 0`.

**Failures** (bug-fixing, no valid patches):
- **CRS Crash**: `.fail` marker present (or neither marker present after run completed).
- **Build Failure**: CRS build failed (check `worker.log` and `output/logs/services/oss-crs-builder-sidecar.*.log`).
- **CRS Error**: Run finished but with internal error (check `output/logs/crs/<crs-name>/*.log`).
- **No Patches**: Completed but 0 patches generated (`summary.patches_generated == 0`).
- **Empty Patches**: Patch file exists but is empty (`worker.log` line `Empty patch for <pov_id>`, emitted by `crsbench/evaluation/verification/patch/engine.py`).
  - Counts as patch generation failure, NOT as a generated patch.
  - Verification logs `Failed to apply patch: ...` and the row is reported under `build_failed`.
- **Build Failed Patches**: Patches generated but failed to apply/build (`summary.build_failed > 0`).
- **POV Still Triggers**: Patches applied but vulnerability still exists (`summary.pov_still_triggers > 0`).
- **Test Failed**: Patch broke regression tests (`summary.test_failed > 0`).

**Note on Empty Patches**: When CRS logs show `Empty patch for <pov_id>`, the patch file may exist but contains no diff content. This is a **patch generation failure** — the CRS failed to produce a meaningful fix. The verification engine logs `Failed to apply patch` and the result is counted under `build_failed`.

### Step 4: Analyze Error Patterns (Use Subagents)
**IMPORTANT**: Log files can be very large. Use Task tool with subagents to analyze logs in parallel.

For failed trials, launch subagents to analyze:
- `worker.log` — orchestrator + verification messages (`Empty patch for ...`, `Failed to apply patch`, timeout/cancellation messages).
- `output/logs/crs/<crs-name>/*.log` and `output/logs/crs/<crs-name>/log_dir/` — CRS-internal logs (ERROR, FAIL, AssertionError, Exception, agent traces).
- `output/logs/services/*.stderr.log` — sidecar / exchange container logs (build errors, exchange errors).
- `output/docker-compose.stdout.log` / `output/docker-compose.stderr.log` — compose-level failures.

**Detecting Empty Patches**:
- Check `worker.log` for: `Empty patch for <pov_id>` (logged by `engine.py::_apply_patch`).
- Check patch file size: files under `output/patches/*.diff` with 0 or minimal bytes.
- Verification result shows `Failed to apply patch` and the row is recorded with `status: "build_failed"`.

**Strategy**:
- Launch up to 3 Explore subagents in parallel for different failed benchmarks.
- Each subagent analyzes one trial's logs and returns a summary.
- Aggregate subagent results into the final report.

### Step 5: Aggregate LLM Costs
Sum up from all `llm-usage.json` (`LLMUsage` schema):
- `total_cost_usd`
- `total_input_tokens`, `total_output_tokens`, `total_cached_tokens`
- `by_model` breakdown (`ModelUsage` per model name)

Cross-check with `llm-summary.json` (`total_cost_usd_from_key` reflects LiteLLM key-level spend, which is authoritative when per-call logging is suppressed).

## Output Format

```markdown
# Experiment Analysis: {experiment_name}

## Summary
- **Total Trials**: X
- **Completed (normal termination)**: Y (Z%)
- **Crashed**: W
- **Valid Patches Generated**: V (actual success rate: V/X = P%)

## Failure Breakdown
| Failure Type | Count | Root Cause | Affected Benchmarks |
|--------------|-------|------------|---------------------|
| CRS Crash | N | crs-bug | ... |
| Build Failure | N | crs-bug | ... |
| CRS Error | N | crs-bug | ... |
| No Patches | N | crs-limitation | ... |
| Empty Patches | N | crs-bug | ... |
| Build Failed Patches | N | crs-limitation | ... |
| POV Still Triggers | N | crs-limitation | ... |
| Test Failed | N | crs-limitation | ... |

## Common Error Patterns
### Pattern 1: [Error Type]
**Affected Trials**: N
**Error Message**:
```
<error snippet>
```
**Likely Cause**: <analysis>

## LLM Usage Summary
- **Total Cost**: $X.XX
- **Total API Calls**: N
- **Breakdown**:
  - claude-sonnet-4-5: N calls, $X.XX
  - claude-haiku-4-5: N calls, $X.XX

## Detailed Failure List
| Benchmark | Harness | Trial | Status | Error Type | Root Cause |
|-----------|---------|-------|--------|------------|------------|
| ... | ... | ... | ... | ... | crs-bug/oss-crs-bug/crsbench-bug/crs-limitation/benchmark-issue/infra-issue |

## Recommendations
1. <recommendation based on patterns>
2. ...
```

## Key Files to Read

Per-trial (resolved by `crsbench/evaluation/trial_paths.py::TrialDir`):

1. `trial_matrix.json` — trial list (at experiment root).
2. `<trial>/metadata.json` — trial metadata (`TrialMetadata` schema).
3. `<trial>/patch_verification_results.json` — **actual patch success** (bug-fixing only).
4. `<trial>/llm-usage.json` — LLM costs and tokens (`LLMUsage` schema).
5. `<trial>/llm-summary.json` — per-trial cost summary (key-level spend).
6. `<trial>/.success` / `<trial>/.fail` — normal termination marker (NOT patch success).
7. `<trial>/worker.log` — orchestrator + verification log (**use subagent for large files**).
8. `<trial>/output/logs/crs/<crs-name>/` — CRS-internal logs (**use subagent for large files**).
9. `<trial>/output/logs/services/*.log` — sidecar / exchange logs.
10. `<trial>/output/patches/*.diff` — generated patch diffs (flat layout).
11. `<trial>/output/povs/` and `<trial>/povs/pov_store.json` — discovered POVs.
12. `<trial>/snapshot-*.tar.gz` — periodic snapshots (see `docs/reference/snapshots.md`).

## Log Analysis with Subagents

**CRITICAL**: When analyzing failed trials, ALWAYS use Task tool with Explore subagent for log analysis.

```
For each failed benchmark that needs detailed log analysis:
1. Launch Task tool with subagent_type="Explore"
2. Prompt: "Analyze the CRS logs for [benchmark] trial at [path].
   Read worker.log and the files under output/logs/crs/<crs-name>/ and output/logs/services/.
   Extract: error type, error message, stack trace.
   Classify root cause as one of:
   - crsbench-bug: CRSBench framework bug
   - oss-crs-bug: oss-crs wrapper bug (builder-sidecar / runner-sidecar / exchange)
   - crs-bug: CRS implementation bug (claude-code, codex, opencode, atlantis-*, etc.)
   - crs-limitation: CRS couldn't handle this case
   - benchmark-issue: Benchmark itself has problems
   - infra-issue: Infrastructure problem (disk/memory/network)
   Return a concise summary with the root cause label."
3. Aggregate results from all subagents
```

This prevents context overflow when dealing with large log files (some can be 100KB+).

## Root Cause Classification Reference

When classifying failures, use these labels:

### `crsbench-bug`
CRSBench framework itself has a bug
- Traceback in `crsbench/` modules
- Trial directory creation failures
- Snapshot capture failures

### `oss-crs-bug`
oss-crs wrapper / sidecar issues (`oss-crs-builder-sidecar`, `oss-crs-runner-sidecar`, `oss-crs-exchange`)
- Docker build/run failures from wrapper
- Command argument parsing errors
- Container mounting issues

### `crs-bug`
CRS implementation bugs. Real CRS names live under `oss-crs/registry/` and follow:
- `crs-bug-finding-<agent>` — LLM-driven bug-finding (`crs-bug-finding-claude-code`, `crs-bug-finding-codex`, ...)
- `crs-<agent>` — LLM-driven bug-fixing (`crs-claude-code`, `crs-codex`, `crs-opencode`, ...)
- `crs-libfuzzer`, `crs-jazzer`, `atlantis-*` — pure fuzzers
Indicators:
- Traceback / AssertionError inside `output/logs/crs/<crs-name>/`
- Agent crashes
- **Empty patch generation**: CRS produced empty patch file (no actual diff content)
  - Log pattern: `Empty patch for <pov_id>` in `worker.log`

### `crs-limitation`
CRS worked but couldn't solve the problem
- Zero patches generated (normal termination, no crash)
- All patches invalid
- No exploit found

### `benchmark-issue`
Benchmark configuration or source problems
- Source code doesn't compile
- Test harness errors
- POV doesn't trigger vulnerability

### `infra-issue`
Infrastructure/resource problems
- OOM (Out of Memory)
- Disk full
- Network timeout
- Docker daemon issues

## Directory Structure Reference

Bug-finding experiment:
```
experiment-data/<experiment-name>/
├── trial_matrix.json                                  # Trial list
└── <crs>/<benchmark>/<harness>/<mode>/<sanitizer>/trial-<N>/
    ├── metadata.json                                  # TrialMetadata
    ├── worker.log                                     # Orchestrator + verification log
    ├── llm-usage.json                                 # LLM cost/tokens (LLMUsage)
    ├── llm-summary.json                               # Per-trial cost summary
    ├── llm-logs.json                                  # LLM API call logs
    ├── .success | .fail                               # Normal termination marker (NOT patch success!)
    ├── crs-compose.yaml                               # Generated docker-compose for the trial
    ├── crs-input/                                     # Inputs handed to the CRS (povs, sarif, seeds, diff)
    ├── output/
    │   ├── povs/                                      # Discovered POVs (CRITICAL for bug-finding)
    │   ├── patches/*.diff                             # Generated patches (flat layout)
    │   ├── logs/crs/<crs-name>/                       # CRS internal logs (CRITICAL)
    │   ├── logs/services/*.{stdout,stderr}.log        # Sidecar / exchange logs
    │   ├── docker-compose.stdout.log
    │   └── docker-compose.stderr.log
    ├── povs/                                          # POV verification results (pov_store.json, snapshot_history.json)
    ├── patches/                                       # Patch verification work dir (incl. logs/)
    ├── patch_verification_results.json                # Patch verification results (bug-fixing trials)
    └── snapshot-*.tar.gz                              # Periodic snapshots (see docs/reference/snapshots.md)
```

Bug-fixing experiment adds a CPV directory: `<crs>/<benchmark>/<harness>/<cpv>/<mode>/<sanitizer>/trial-<N>/`.

## See Also

- `docs/experiments/full-pipeline.md` — full-pipeline finding + fixing runs
- `docs/experiments/discovery-only.md` — bug-finding only
- `docs/experiments/replay-povs.md` — replaying historical POVs
- `docs/reference/experiment-config.md` — experiment config schema
- `docs/reference/snapshots.md` — snapshot semantics and layout
- `crsbench/evaluation/trial_paths.py` — canonical trial artifact paths
- `crsbench/validation/schemas.py` — `TrialMetadata`, `LLMUsage`, `ModelUsage`, `SnapshotMetadata`
- `oss-crs/registry/` — authoritative list of CRS names
