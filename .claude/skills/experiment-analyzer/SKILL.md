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

**If no argument provided**: Ask the user for the experiment directory path before proceeding.

## Analysis Strategy

### Step 1: Load Trial Matrix
Read `{experiment_dir}/trial_matrix.json` to get the full list of trials:
- Total trial count
- CRS/benchmark/harness combinations

### Step 2: Scan All Trial Directories
For each trial directory under `{experiment_dir}` (`<crs>/<benchmark>/<harness>/<mode>/<sanitizer>/trial-<N>/`):
1. Check for `.success` file (existence = **normal termination**, NOT successful patch)
2. Read `execution.json` for execution details (returncode, duration)
3. Read `patch_verification_results.json` for **actual success** (valid patches generated)
4. Collect `llm-usage.json` data

**IMPORTANT**: `.success` marker only means the CRS process completed without crashing. It does NOT mean:
- Patches were generated
- Patches are valid
- The vulnerability was fixed

**Actual success** is determined by `patch_verification_results.json`:
- `summary.valid > 0` = at least one valid patch exists

### Step 3: Categorize Results
Group trials by outcome (based on `patch_verification_results.json`):

**Success**:
- **Valid Patches**: `summary.valid > 0` - At least one patch fixes the vulnerability

**Failures** (no valid patches):
- **CRS Crash**: No `.success` marker, CRS process crashed
- **Build Failure**: CRS build failed (check `worker.log` for "Build command failed")
- **CRS Error**: Non-zero returncode with internal error (check `crs-logs/crs_run_*.log`)
- **No Patches**: Completed but 0 patches generated (`summary.patches_generated == 0`)
- **Empty Patches**: Patch file exists but is empty (check `worker.log` for "Empty patch for cpv_X")
  - This counts as patch generation failure, NOT as a generated patch
  - Often results in `build_failed` during verification (patch cannot be applied)
- **Build Failed Patches**: Patches generated but failed to apply/build (`summary.build_failed > 0`)
- **POV Still Triggers**: Patches applied but vulnerability still exists (`summary.pov_still_triggers > 0`)

**Note on Empty Patches**: When CRS logs show `Empty patch for cpv_X`, the patch file may exist but contains no actual diff content. This is a **patch generation failure** - the CRS failed to produce a meaningful fix. These are often misreported as `patches_generated=1` but will fail verification with "Failed to apply patch".

### Step 4: Analyze Error Patterns (Use Subagents)
**IMPORTANT**: Log files can be very large. Use Task tool with subagents to analyze logs in parallel.

For failed trials, launch subagents to analyze:
- `crs-logs/crs_run_*.log`: Look for ERROR, FAIL, AssertionError, Exception
- `worker.log`: Build failures, timeout messages, "Empty patch for cpv_X"

**Detecting Empty Patches**:
- Check `worker.log` for: `WARNING  | grapple-1 | [evaluation] | Empty patch for cpv_X`
- Check patch file size: `output/patches/cpv_X/patch.diff` with 0 or minimal bytes
- Verification result shows: "Failed to apply patch" with `build_failed=1`

**Strategy**:
- Launch up to 3 Explore subagents in parallel for different failed benchmarks
- Each subagent analyzes one trial's logs and returns a summary
- Aggregate subagent results into the final report

Example:
```
Use Task tool with subagent_type="Explore" to analyze:
- Trial 1: atlanta-pac4j-full-01 failure
- Trial 2: atlanta-curl-delta-01 failure
- Trial 3: atlanta-file-delta-01 failure
```

### Step 5: Aggregate LLM Costs
Sum up from all `llm-usage.json`:
- Total cost
- Total API calls
- By-model breakdown

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

1. `trial_matrix.json` - Trial list
2. `*/trial-*/execution.json` - Execution results (returncode, duration)
3. `*/trial-*/patch_verification_results.json` - **Actual success** (valid patches count)
4. `*/trial-*/llm-usage.json` - LLM costs
5. `*/trial-*/.success` - Normal termination marker (NOT patch success!)
6. `*/trial-*/crs-logs/crs_run_*.log` - CRS errors (**use subagent for large files**)
7. `*/trial-*/worker.log` - Worker errors (**use subagent for large files**)

## Log Analysis with Subagents

**CRITICAL**: When analyzing failed trials, ALWAYS use Task tool with Explore subagent for log analysis.

```
For each failed benchmark that needs detailed log analysis:
1. Launch Task tool with subagent_type="Explore"
2. Prompt: "Analyze the CRS logs for [benchmark] trial at [path].
   Read crs-logs/crs_run_*.log and worker.log.
   Extract: error type, error message, stack trace.
   Classify root cause as one of:
   - crsbench-bug: CRSBench framework bug
   - oss-crs-bug: oss-bugfix-crs wrapper bug
   - crs-bug: CRS implementation bug (Atlantis/Grapple)
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
oss-bugfix-crs or oss-bugfind-crs wrapper issues
- Docker build/run failures from wrapper
- Command argument parsing errors
- Container mounting issues

### `crs-bug`
CRS implementation bugs (Atlantis, Grapple, etc.)
- Traceback in `/app/packages/` or CRS-specific paths
- AssertionError in CRS code
- Agent crashes
- **Empty patch generation**: CRS produced empty patch file (no actual diff content)
  - Log pattern: `Empty patch for cpv_X` in worker.log

### `crs-limitation`
CRS worked but couldn't solve the problem
- Zero patches generated (returncode=0)
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

```
experiment-data/<experiment-name>/
├── trial_matrix.json                    # Trial metadata
├── worker-logs/                         # Global worker logs
│   └── <worker-name>.log
└── <crs-name>/<benchmark>/<harness>/<mode>/<sanitizer>/trial-<N>/
    ├── metadata.json                    # Trial metadata
    ├── execution.json                   # Execution result
    ├── worker.log                       # Per-trial worker log
    ├── llm-usage.json                   # LLM cost/tokens
    ├── llm-logs.json                    # LLM API call logs
    ├── .success                         # Normal termination marker (NOT patch success!)
    ├── crs-logs/crs_run_*.log          # CRS internal logs (CRITICAL)
    ├── output/patches/                  # Generated patches
    ├── patches/snapshots/               # Snapshot patch states
    └── patch_verification_results.json  # Patch verification results
```
