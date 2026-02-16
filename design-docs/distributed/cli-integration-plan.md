# CLI Integration Plan for Distributed Workflow

## Current Workflow (Manual)

```bash
# 1. Start Redis
scripts/orchestrate-workers.sh redis-setup

# 2. Start evaluator
crsbench evaluator --experiment-config config.yaml

# 3. Start local workers
crsbench worker --experiment-config config.yaml --cores 0-111

# 4. Enqueue jobs
crsbench run --experiment-config config.yaml --distributed

# 5. SSH to remote servers and run workers manually
ssh cerebros "cd ~/CRSBench && crsbench worker --experiment-config ..."
ssh ramjet "cd ~/CRSBench && crsbench worker --experiment-config ..."

# 6. Collect results
scripts/orchestrate-workers.sh collect

# 7. Generate report
python scripts/cpv_report.py ~/experiment-data/... --csv
```

## Proposed CLI Structure

### New Commands

| Command | Description | Replaces |
|---------|-------------|----------|
| `crsbench redis start\|stop\|status` | Redis container management | `orchestrate-workers.sh redis-setup` |
| `crsbench status` | Unified experiment status | Manual Redis/worker checking |
| `crsbench collect` | Rsync results from remote workers | `orchestrate-workers.sh collect` |
| `crsbench report` | Generate CPV reports | `scripts/cpv_report.py` |

### Existing Commands (Keep As-Is)

| Command | Description |
|---------|-------------|
| `crsbench run --distributed` | Orchestrator - enqueue jobs to Redis |
| `crsbench worker` | Worker - process jobs from Redis |
| `crsbench evaluator` | Evaluator - pre-builds + POV verification |

## Command Specifications

### 1. `crsbench redis`

```bash
crsbench redis start [--port 6379] [--password-file .env]
crsbench redis stop
crsbench redis status
```

**Implementation:**
- Move Redis Docker logic from `orchestrate-workers.sh` to `crsbench/distributed/cli/redis_command.py`
- Store password in `.env` file
- Check container status, show queue stats

### 2. `crsbench status`

```bash
crsbench status --experiment-config config.yaml [--watch]
```

**Output:**
```
Experiment: afc-rest7
Redis: localhost:6379 (connected)

Queue Status:
  Total:     364
  Completed: 120 (33%)
  Running:   5
  Queued:    239
  Failed:    0

Workers (by heartbeat):
  cyclonus:7   ACTIVE   45 completed   Last seen: 2s ago
  cerebros:8   ACTIVE   32 completed   Last seen: 5s ago
  ramjet:8     ACTIVE   43 completed   Last seen: 3s ago
```

**Implementation:**
- Query Redis for queue stats (RQ job counts)
- Track worker heartbeats via Redis keys
- `--watch` flag for live updates (refresh every 5s)

### 3. `crsbench collect`

```bash
crsbench collect --experiment-config config.yaml \
    [--from host1,host2] \
    [--to /local/path]
```

**Implementation:**
- Read remote hosts from config or `--from` flag
- Rsync experiment data with `--exclude crs-build/ .oss-bugfind/`
- Merge into local experiment directory

### 4. `crsbench report`

```bash
crsbench report --experiment-data /path/to/experiment-data \
    [--format csv|json|table] \
    [--output report.csv]
```

**Implementation:**
- Wrap existing `cpv_report.py` logic
- Support multiple output formats
- Add to `crsbench/cli/report_command.py`

## Implementation Priority

### Phase 1: Quick Wins
1. `crsbench redis` - Move shell script logic to Python
2. `crsbench report` - Wrap existing cpv_report.py

### Phase 2: Status & Monitoring
3. `crsbench status` - Query Redis for job stats
4. Add worker heartbeat reporting

### Phase 3: Collection
5. `crsbench collect` - Rsync wrapper with config integration

## File Structure

```
crsbench/
├── distributed/
│   ├── cli/
│   │   ├── redis_command.py      # NEW: crsbench redis
│   │   ├── status_command.py     # NEW: crsbench status
│   │   ├── collect_command.py    # NEW: crsbench collect
│   │   ├── worker_command.py     # Existing
│   │   └── evaluator_command.py  # Existing
│   └── ...
├── cli/
│   └── report_command.py         # NEW: crsbench report
└── run_experiment.py             # Existing: crsbench run
```

## Example Integrated Workflow

```bash
# 1. Start infrastructure
crsbench redis start

# 2. Start evaluator (background)
crsbench evaluator --experiment-config config.yaml &

# 3. Start local workers (background)
crsbench worker --experiment-config config.yaml &

# 4. Enqueue jobs
crsbench run --experiment-config config.yaml --distributed

# 5. (Manual) SSH to remote servers, run workers
# This step remains manual due to SSH auth complexity

# 6. Monitor progress
crsbench status --experiment-config config.yaml --watch

# 7. Collect results when done
crsbench collect --experiment-config config.yaml --from cerebros,ramjet

# 8. Generate report
crsbench report --experiment-data ~/experiment-data/afc-rest7 --csv
```

## Notes

- Remote worker startup remains manual (SSH key auth complexity)
- All new commands should load `.env` for REDIS_PASSWORD
- Config file provides defaults, CLI flags override
