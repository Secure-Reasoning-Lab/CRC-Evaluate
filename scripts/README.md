# CRSBench Utility Scripts

This directory contains helper scripts for common CRSBench development and testing workflows.

## Available Scripts

### merge_experiment_results.py

Merges CRS evaluation results from multiple worker machines into a unified experiment-data directory.

**Purpose**: When running distributed experiments, each worker creates its own timestamped result directory. This script combines them into a single unified directory for analysis.

**Usage:**
```bash
# Option 1: Explicit input directories
python scripts/merge_experiment_results.py \
    --input-dirs /path/to/exp1 /path/to/exp2 ... \
    --output-dir /path/to/merged/experiment-data

# Option 2: Glob pattern
python scripts/merge_experiment_results.py \
    --input-pattern "/path/to/exp_*_*/experiment-data" \
    --output-dir /path/to/merged/experiment-data
```

**Features:**
- Detects and reports conflicts (multiple successful trials with same identity)
- Filters out failed trials (marked with `.fail`)
- Preserves directory structure
- Validates trial identities based on metadata

**Conflict Handling:**

A trial's identity is defined as: `(crs, benchmark, harness, mode, sanitizer, trial_num)`

- Multiple `.success` for same identity → **CONFLICT** (script exits with error)
- One `.success` + any `.fail` for same identity → **OK** (keeps `.success`)
- All `.fail` for same identity → **OK** (all skipped)

When conflicts are detected, the script prints a detailed report showing the conflicting trial paths and exits. You must manually remove one of the conflicting sources before re-running.

**Example:**
```bash
# Merge results from three workers
python scripts/merge_experiment_results.py \
    --input-pattern "/data/results/exp1_*_*/experiment-data" \
    --output-dir /data/merged/experiment-data

# Verify merged results
python -m crsbench.reporting.discover_trials /data/merged/experiment-data
```

**Requirements:**
- Python 3.11+
- CRSBench package installed (for schemas)

**See Also:**
- [Experiment Workflow](../docs/experiment-workflow.md)
- [Reporting Module](../crsbench/reporting/)

### valkey-helper.py

Valkey service management helper for distributed execution testing.

**Purpose**: Simplifies common Valkey operations without needing to remember docker-compose and valkey-cli commands.

**Usage:**
```bash
# Service management
python scripts/valkey-helper.py start                    # Docker network only (secure)
python scripts/valkey-helper.py --bind-host start        # Bind to localhost for host access
python scripts/valkey-helper.py --password start         # Password auth for remote workers
python scripts/valkey-helper.py stop
python scripts/valkey-helper.py restart                  # Auto-detects password from .env
python scripts/valkey-helper.py status                   # Shows port binding + auth status
python scripts/valkey-helper.py logs

# Queue management
python scripts/valkey-helper.py clean <experiment-name>
python scripts/valkey-helper.py clean-all [--force]
python scripts/valkey-helper.py list-queues
python scripts/valkey-helper.py queue-info <experiment-name>

# Monitoring
python scripts/valkey-helper.py stats
```

**Quick Start:**

**For Docker network only (default):**
```bash
# Start Valkey (secure, no host access)
python scripts/valkey-helper.py start
python scripts/valkey-helper.py status
```

**For host-based workers (same machine):**
```bash
# Start with host access
python scripts/valkey-helper.py --bind-host start

# Run workers on host
export REDIS_HOST=localhost
python -m crsbench.distributed.worker
```

**For remote workers (multi-machine):**
```bash
# Start with password auth (auto-generates password, binds 0.0.0.0, saves to .env)
python scripts/valkey-helper.py --password start

# Copy .env to worker machines
scp .env user@worker:/path/to/CRSBench/.env
```

**Common Testing Workflows:**

**Workflow 1: Quick Test**
```bash
# Setup
python scripts/valkey-helper.py start

# Test
crsbench run --experiment-config config.yaml --experiment-name quick-test ...

# Cleanup
python scripts/valkey-helper.py clean quick-test
```

**Workflow 2: Multiple Test Runs (Clean Between)**
```bash
python scripts/valkey-helper.py start

# Run test 1
crsbench --experiment-name test-1 ...
python scripts/valkey-helper.py clean test-1

# Run test 2
crsbench --experiment-name test-2 ...
python scripts/valkey-helper.py clean test-2
```

**Workflow 3: Complete Reset**
```bash
# Full cleanup (removes all experiments)
python scripts/valkey-helper.py clean-all

# Verify clean state
python scripts/valkey-helper.py list-queues
python scripts/valkey-helper.py stats
```

**Workflow 4: Debug Queue Issues**
```bash
# Check what's in the queues
python scripts/valkey-helper.py list-queues

# Get detailed info
python scripts/valkey-helper.py queue-info my-experiment

# Check overall stats
python scripts/valkey-helper.py stats

# View live logs
python scripts/valkey-helper.py logs
```

**Requirements:**
- Docker and docker-compose installed
- Python 3.11+ (standard library only)
- Valkey docker-compose setup in `services/valkey/`

**See Also:**
- [Experiment Workflow](../docs/experiment-workflow.md) - Full distributed execution documentation
- [Testing Setup Guide](../docs/testing-setup.md) - Complete testing environment setup
- [Valkey Service README](../services/valkey/README.md) - Valkey service details

## Adding New Scripts

When adding new utility scripts to this directory:

1. **Naming**: Use kebab-case (e.g., `my-helper.py`)
2. **Language**: Prefer Python for cross-platform compatibility
3. **Documentation**: Include comprehensive docstring with usage examples
4. **CLI**: Use `argparse` for command-line interface
5. **Error Handling**: Provide clear error messages and appropriate exit codes
6. **Update README**: Document the new script in this file

**Example Script Structure:**
```python
#!/usr/bin/env python3
"""
Script description and purpose.

Usage:
    python scripts/my-helper.py command [options]

Examples:
    python scripts/my-helper.py start
    python scripts/my-helper.py status
"""

import argparse
import sys

def main():
    parser = argparse.ArgumentParser(description="...")
    # ... implement CLI

if __name__ == "__main__":
    main()
```
