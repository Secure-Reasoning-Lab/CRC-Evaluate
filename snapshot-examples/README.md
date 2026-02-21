# Snapshot Examples

This directory contains sample snapshots and tools for generating and validating snapshot data.

## Overview

Snapshots are periodic captures of CRS trial progress, including:
- POVs discovered (incremental)
- Patches generated (incremental)
- Corpus files (incremental)
- LLM usage metrics (full cumulative)
- CRS logs (full)
- Configuration and metadata

See `docs/design/evaluation/snapshots.md` for the complete snapshot design specification.

## Contents

- `generate_snapshot.py` - Script to generate and validate sample snapshots
- `trial-example/` - Sample trial with 3 snapshots showing incremental POV discovery

## Usage

### Generate Sample Snapshots

```bash
python generate_snapshot.py [output_dir]
```

This creates a realistic trial directory with:
- Snapshot 1 (15 min): 2 POVs, 1 patch
- Snapshot 2 (30 min): 1 new POV, 2 new patches
- Snapshot 3 (45 min): 2 new POVs, 1 new patch

### List Snapshots

```bash
# List all snapshots in a directory with summaries
python generate_snapshot.py --list [snapshot_dir]

# List detailed contents of a specific snapshot
python generate_snapshot.py --list-snapshot <snapshot.tar.gz>
```

**Directory listing** shows:
- Snapshot cycle and elapsed time
- File counts by category (POVs, patches, corpus)
- Summary of contents

**Single snapshot listing** shows:
- Metadata (cycle, timestamp, elapsed time)
- Complete file tree with sizes
- Proper directory structure

### Validate Snapshots

```bash
python generate_snapshot.py --validate [snapshot_dir]
```

This validates:
- Archive integrity (tar.gz can be opened)
- Required files exist (metadata.json, config.yaml, etc.)
- Metadata structure is correct
- LLM usage structure is valid
- Completion markers exist
- Patch directory structure (patches organized by POV ID)

### Inspect Snapshots (Manual)

```bash
# List archive contents
tar -tzf trial-example/snapshot-0001.tar.gz

# Extract a snapshot
tar -xzf trial-example/snapshot-0001.tar.gz

# View metadata
cat metadata.json

# View LLM usage
cat llm-usage.json
```

## Snapshot Structure

Each snapshot archive (`snapshot-NNNN.tar.gz`) contains:

```
snapshot-NNNN/
├── metadata.json          # Snapshot metadata (cycle, timestamp, elapsed time)
├── config.yaml            # Experiment configuration (full)
├── execution.json         # Execution metadata (full)
├── llm-usage.json        # Cumulative LLM metrics (full)
├── crs-output.log        # Complete CRS log (full)
├── povs/                 # New POVs only (incremental)
│   └── pov_NNN           # Binary POV blob
├── patches/              # New patches only (incremental, organized by POV ID)
│   └── pov_N/
│       └── patch.diff
└── seeds/                # New/modified seeds (incremental)
    └── input-NNN
```

## Incremental vs Full Capture

**Incremental (new data only):**
- POVs - tracked by filename
- Patches - tracked by filename, organized in `pov_N/` subdirectories
- Corpus - tracked by modification time

**Full (complete state):**
- LLM usage logs - cumulative metrics
- CRS logs - complete output from start
- Config - static experiment configuration
- Execution metadata - static execution details

## Validation Criteria

A valid snapshot must:
1. Have a corresponding `.complete` marker file
2. Be a valid tar.gz archive
3. Contain required files: `metadata.json`, `config.yaml`, `execution.json`, `llm-usage.json`, `crs-output.log`
4. Have valid JSON structure in metadata and LLM usage
5. Have metadata cycle number matching filename
6. Patches must be organized in `pov_N/` subdirectories

## Testing

Use these samples for:
- Testing snapshot parsing code
- Validating report generation
- Demonstrating snapshot format
- Integration testing of snapshot manager
