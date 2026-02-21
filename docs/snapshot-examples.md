# Snapshot Guide

Snapshots provide periodic trial-state capture for monitoring and analysis.

## Quick Use

Set `snapshot_period` in experiment config:

```yaml
snapshot_period: 900  # seconds; 0 disables snapshots
```

Run experiment normally:

```bash
crsbench run --experiment-config experiment-config.yaml
```

Snapshots are written per trial under the experiment output directory as
`snapshot-XXXX.tar.gz` plus completion markers.

## What To Read

- User-facing examples and generator tooling:
  - [snapshot-examples/README.md](../snapshot-examples/README.md)
- Full design and archive format:
  - [docs/design/evaluation/snapshots.md](./design/evaluation/snapshots.md)
- Runtime workflow context:
  - [docs/experiment-workflow.md](./experiment-workflow.md)

## Common Commands

```bash
python snapshot-examples/generate_snapshot.py --list <trial_dir>
python snapshot-examples/generate_snapshot.py --list-snapshot <snapshot.tar.gz>
python snapshot-examples/generate_snapshot.py --validate <trial_dir>
```
