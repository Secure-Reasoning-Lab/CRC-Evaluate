---
name: agent-smoke-test-runner
description: Run CRSBench smoke tests end-to-end. Use when the user wants to run smoke tests, verify experiment configs work, test the CRS pipeline, or check if CRS agents are functioning correctly. Handles launching run/worker/evaluator in parallel, monitoring progress, and summarizing results (POVs found, patches generated, LLM costs, errors).
---

# CRSBench Smoke Test Runner

Run smoke test experiment configs through the full CRSBench pipeline and report results.

## Prerequisites

Before running, verify:

```bash
# 1. Valkey/Redis is running
uv run python scripts/valkey-helper.py status

# 2. .env has required secrets (checked automatically by crsbench via load_dotenv)
cat .env   # COPILOT_GITHUB_TOKEN, etc.

# 3. Configs exist
ls experiment-configs/smoke-testing/*.yaml
```

## Workflow

### Step 1: Dry-run validation

Before launching real runs, validate all configs:

```bash
for cfg in experiment-configs/smoke-testing/*.yaml; do
  echo "=== $(basename $cfg) ==="
  uv run crsbench run --experiment-config "$cfg" --dry-run 2>&1 | tail -3
  echo
done
```

Check: each config should show a trial count table and "Total trials: N". If any fail, fix before proceeding.

### Step 2: Launch the pipeline

Three processes must run simultaneously. Launch all in background:

```bash
# A) Enqueue all experiments (run in background)
for cfg in experiment-configs/smoke-testing/*.yaml; do
  uv run crsbench run --experiment-config "$cfg" --distributed --queue-mode fresh 2>&1 &
done

# B) Worker — processes trial jobs from Redis (background)
uv run crsbench worker --jobs 10 --cores-per-job 8 --no-continuous &

# C) Evaluator — builds variants and verifies POVs/patches (background)
uv run crsbench evaluator --jobs 2 --cores-per-job 4 --idle-timeout 1800 &
```

**Important notes:**
- Worker with `--no-continuous` drains the queue and exits. Use `--continuous` if you want it to keep polling.
- Evaluator `--idle-timeout 1800` exits after 30min of no work. Restart if needed after worker finishes more trials.
- Omitting `--experiment-config` from worker/evaluator makes them discover ALL experiments from Redis registry automatically.

### Step 3: Monitor progress (real-time)

While the pipeline runs, **proactively and periodically** check and report to the user:

1. **Job states** — queued/running/completed/failed counts per experiment
2. **CLI version & model verification** — as trials complete, check finder/patcher logs to confirm the expected CLI version and model were actually used (not cached from a different variant)
3. **Agent logs** — check for errors, warnings, auth failures, litellm routing issues
4. **POV/patch counts** — report discoveries as they happen

Do NOT wait for the user to ask. Report status updates as trials complete, flag any issues immediately.

Check job states across all experiments:

```python
import redis, json
conn = redis.Redis(host="localhost", port=6379, decode_responses=True)
exps = [k.split(":")[-1] for k in conn.keys("crsbench:jobs:*")]
for exp in sorted(exps):
    jobs = conn.hgetall(f"crsbench:jobs:{exp}")
    c = {}
    for v in jobs.values():
        s = json.loads(v).get("state", "?")
        c[s] = c.get(s, 0) + 1
    print(f"{exp}: Q={c.get('queued',0)} R={c.get('running',0)} D={c.get('completed',0)} F={c.get('failed',0)}")
```

Also check:
- `docker ps --format '{{.Names}}' | grep crs_compose | wc -l` — active containers
- `ps aux | grep "crsbench" | grep -v grep | wc -l` — running processes

### Step 4: Check results

#### POVs discovered (bug-finding):

```bash
find .run/smoke-testing/experiment-data/ -path "*/trial-1/output/povs" -type d | while read d; do
  count=$(find "$d" -type f | wc -l)
  crs=$(echo "$d" | cut -d/ -f6)
  bench=$(echo "$d" | cut -d/ -f7)
  [ "$count" -gt 0 ] && echo "$count POVs: $crs / $bench"
done | sort -t: -k1 -rn
```

#### Patches generated (bug-fixing):

```bash
find .run/smoke-testing/experiment-data/ -path "*/trial-1/output/patches" -type d | while read d; do
  count=$(find "$d" -name "*.diff" -type f | wc -l)
  crs=$(echo "$d" | cut -d/ -f6)
  bench=$(echo "$d" | cut -d/ -f7)
  [ "$count" -gt 0 ] && echo "$count patches: $crs / $bench"
done | sort -t: -k1 -rn
```

#### CRS agent logs — CLI version & model verification (CRITICAL):

For every completed trial, verify the agent log shows the **expected** CLI version and model. This catches image caching bugs where a different variant's image gets reused.

```bash
find .run/smoke-testing/results/ -name "*finder.stdout.log" -o -name "*patcher.stdout.log" | while read f; do
  exp=$(echo "$f" | cut -d/ -f4)
  crs=$(echo "$f" | cut -d/ -f6)
  bench=$(echo "$f" | cut -d/ -f7)
  cli=$(grep "CLI version" "$f" | head -1 | sed 's/.*CLI version: //' | sed 's/ .*//')
  model=$(grep -E "(Model:|ANTHROPIC_MODEL:)" "$f" | head -1 | sed 's/.*Model: //' | sed 's/.*ANTHROPIC_MODEL: //')
  exit_code=$(grep "exit code" "$f" | head -1 | grep -oP 'exit code: \K\d+')
  warn=$(grep -c "WARNING" "$f")
  err=$(grep -c "ERROR" "$f")
  printf "%-35s %-35s cli=%-10s model=%-25s exit=%-3s warn=%s err=%s\n" \
    "$exp" "$crs" "${cli:--}" "${model:--}" "${exit_code:--}" "$warn" "$err"
done | sort
```

**What to check:**
- `old-harness` trials should show OLD CLI versions (e.g., `2.0.17` not `2.1.92`)
- `old-model` trials should show OLD model names (e.g., `claude-opus-4-5-20251101` not `claude-opus-4-6`)
- `open-model` trials should show the open model names (`zai-org/GLM-5.1`, `vertex_ai/gpt-oss-*`)
- `exit=1` + `warn > 0` may indicate auth failure (copilot token) or litellm routing issue
- Any `ERROR` should be investigated via full finder log

#### Detailed agent debug (for failed/suspicious trials):

```bash
# Check claude_stdout.log for actual agent output
find .run/smoke-testing/results/ -path "*/log_dir/agent/claude_stdout.log" -size +0c | while read f; do
  crs=$(echo "$f" | grep -oP 'crs-[^/]+' | head -1)
  echo "=== $crs ($(wc -c < "$f") bytes) ==="
  head -5 "$f"
  echo
done

# Check for litellm/API errors
find .run/smoke-testing/results/ -path "*/log_dir/agent/claude_stderr.log" -size +0c | while read f; do
  crs=$(echo "$f" | grep -oP 'crs-[^/]+' | head -1)
  echo "=== ERROR: $crs ==="
  cat "$f"
  echo
done
```

#### LLM usage:

```bash
find .run/smoke-testing/results/ -name "llm-usage.json" | head -10 | while read f; do
  crs=$(echo "$f" | grep -oP 'crs-[^/]+' | head -1)
  cost=$(python3 -c "import json; print(json.load(open('$f')).get('total_cost_usd', 0))")
  echo "$crs: \$$cost"
done
```

### Step 5: Cleanup / stop

To stop a running smoke test:

```bash
# Kill processes
pkill -f "crsbench run"
pkill -f "crsbench worker"
pkill -f "crsbench evaluator"
sleep 2
pkill -9 -f "uv run crsbench"  # force kill stragglers

# Stop docker containers
docker ps -q --filter "name=crs_compose" | xargs docker stop

# Verify
ps aux | grep crsbench | grep -v grep | wc -l
docker ps --filter "name=crs_compose" -q | wc -l
```

## Smoke Test Config Structure

Configs live in `experiment-configs/smoke-testing/`. Each config has multiple CRS services in `crs_compose`.

Each variant uses **separate CRS registry names** so that docker images are built independently (env vars like CLI version are applied at build time, not just runtime). The naming convention:

- original: base name (e.g., `crs-bug-finding-claude-code`)
- old-model: `-old-model` suffix (e.g., `crs-bug-finding-claude-code-old-model`)
- old-harness: `-old-cli` suffix (e.g., `crs-bug-finding-claude-code-old-cli`)
- open-model: model-specific suffix (e.g., `-glm-5-1`, `-gpt-oss-120b`, `-gpt-oss-20b`)

All registry entries point to the same `source.url` and `ref: main` — the different CRS names ensure separate docker image builds so `additional_env` (CLI version etc.) takes effect.

### Current 8 configs (4 variants x 2 tasks):

| Config | Variant | CRS services |
|---|---|---|
| smoke-bug-finding-original | latest model + latest harness | 4 CRS (base names) |
| smoke-bug-finding-old-model | old model + latest harness | 4 CRS (`-old-model` suffix) |
| smoke-bug-finding-old-harness | latest model + old harness | 4 CRS (`-old-cli` suffix) |
| smoke-bug-finding-open-model | open source models | 3 CRS (`-glm-5-1`, `-gpt-oss-120b`, `-gpt-oss-20b`) |
| smoke-bug-fixing-* | same 4 variants for bug-fixing | corresponding bug-fixing CRS names |

### CRS registry entries (30 total in `oss-crs/registry/`):

**Bug-finding (15)**:
- `crs-bug-finding-claude-code`, `-old-model`, `-old-cli`, `-glm-5-1`, `-gpt-oss-120b`, `-gpt-oss-20b`
- `crs-bug-finding-codex`, `-old-model`, `-old-cli`
- `crs-bug-finding-copilot-cli`, `-old-model`, `-old-cli`
- `crs-bug-finding-gemini-cli`, `-old-model`, `-old-cli`

**Bug-fixing (15)**:
- `crs-claude-code`, `-old-model`, `-old-cli`, `-glm-5-1`, `-gpt-oss-120b`, `-gpt-oss-20b`
- `crs-codex`, `-old-model`, `-old-cli`
- `crs-copilot-cli`, `-old-model`, `-old-cli`
- `crs-gemini-cli`, `-old-model`, `-old-cli`

### Variant matrix:

#### Original (latest model + latest harness)

| Agent | Model | CLI Version |
|---|---|---|
| Claude Code | `claude-opus-4-6` | `2.1.92` |
| Codex | `gpt-5.4` | `0.121.0` |
| Copilot CLI | `gpt-5.4` | `1.0.28` |
| Gemini CLI | `gemini-3-pro-preview` | `0.38.1` |

#### Old Model (old model + latest harness)

| Agent | Model | CLI Version |
|---|---|---|
| Claude Code | `claude-opus-4-5-20251101` | `2.1.92` |
| Codex | `gpt-5-codex` | `0.121.0` |
| Copilot CLI | `gpt-5-codex` | `1.0.28` |
| Gemini CLI | `gemini-2.5-pro` | `0.38.1` |

#### Old Harness (latest model + old harness)

| Agent | Model | CLI Version |
|---|---|---|
| Claude Code | `claude-opus-4-6` | `2.0.17` |
| Codex | `gpt-5.4` | `0.47.0` |
| Copilot CLI | `gpt-5.4` | `0.0.341` |
| Gemini CLI | `gemini-3-pro-preview` | `0.9.0` |

#### Open Model (open source models, Claude Code harness 2.1.92)

| Agent | Model | CLI Version |
|---|---|---|
| Claude Code | `zai-org/GLM-5.1` | `2.1.92` |
| Claude Code | `vertex_ai/gpt-oss-120b` | `2.1.92` |
| Claude Code | `vertex_ai/gpt-oss-20b` | `2.1.92` |

### Key env vars per CRS family:

| CRS | Model env | CLI version env | Extra |
|---|---|---|---|
| claude-code | `ANTHROPIC_MODEL` | `CLAUDE_CODE_CLI_VERSION` | |
| codex | `CODEX_MODEL` | `CODEX_CLI_VERSION` | |
| copilot-cli | `COPILOT_MODEL` | `COPILOT_CLI_VERSION` | `COPILOT_GITHUB_TOKEN` required in `.env` |
| gemini-cli | `GEMINI_MODEL` | `GEMINI_CLI_VERSION` | |

### Runtime settings:

- `run_timeout: 1200` (20 min)
- `pov_early_stop: true` (bug-finding only — exits when POV found)
- `rts_enabled: true`, `inc_build_enabled: true`

## Known Issues from Previous Runs

1. **copilot-cli requires `COPILOT_GITHUB_TOKEN`** in `.env` — without it, agent exits immediately with rc=1 and 0 LLM calls
2. **Open model `zai-org/GLM-5.1`** fails with litellm 500 error (`'NoneType' object has no attribute 'choices'`) — litellm proxy needs model routing config for this model
3. **`gpt-oss-120b`** partially works — litellm tries Anthropic endpoint first (fails), then falls back to `vertex_ai/openai/gpt-oss-120b-maas`. Many requests fail before fallback kicks in
4. **`gpt-oss-20b`** works correctly — routed to `vertex_ai/openai/gpt-oss-20b-maas`
5. **build-target** can take 10-20 min per benchmark (C project compilation). This is unavoidable overhead per trial
6. **litellm spend log collection** can timeout on busy proxy — `llm-logs.json` may be empty even though `llm-usage.json` shows real spend from `key_info`
7. **CRS image caching**: same CRS registry name = cached docker image reused. Different variant envs (e.g., CLI version) require **separate CRS names** so `oss-crs prepare` builds a fresh image. This is why each variant has its own `-old-cli`/`-old-model` registry entry.

## Timing Expectations

| Phase | Duration |
|---|---|
| CRS prepare (docker bake) | 1-3 min (cached after first) |
| build-target (compile benchmark) | 0.5-20 min (inc build cached, first build slower) |
| oss-crs run (agent execution) | Until agent exits, POV found (early stop), or run_timeout (1200s) |
| Post-run (snapshots, LLM logs, cleanup) | 1-5 min |
| **Total per trial** | **~5 min (early stop) to ~25 min (full timeout)** |

With 10 worker jobs and run_timeout=1200s, a 32-trial experiment takes ~1-2 hours.
