# Snapshot Guide

Snapshots provide periodic trial-state capture for monitoring and analysis.

## Quick Use

Set `snapshot_period` in experiment config:

```yaml
snapshot_period: 900  # seconds; 0 disables snapshots
# Optional: wait after CRS run before final LLM accounting capture
llm_accounting_settle_seconds: 60  # default; 0 disables settle wait
```

Run experiment normally:

```bash
crsbench run --experiment-config experiment-config.yaml
```

Snapshots are written per trial under the experiment output directory as
`snapshot-XXXX.tar.gz` plus completion markers.

## What To Read

- Runtime workflow context:
  - [docs/deployment/distributed.md](../deployment/distributed.md)
