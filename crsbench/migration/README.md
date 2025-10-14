# CRSBench Migration Module

## Script-Based Atlanta to RFC Migration

In addition to the LangGraph-based migration system described above, this module includes a direct script-based implementation for migrating Team-Atlanta format to CRSBench RFC format.

### Implementation

The script-based migrator consists of:

#### **atlanta_to_rfc.py**
Main CLI script for orchestrating migrations.

#### **config_converter.py**
Converts `config.yaml` to `meta.yaml` format.

#### **vuln_metadata_generator.py**
Generates vulnerability YAML files with mock data.

#### **file_migrator.py**
Handles file operations with dry-run support.

### Quick Start

```bash
# Dry run to validate
.venv/bin/python -m crsbench.migration.atlanta_to_rfc \
  --source-dir /path/to/oss-fuzz/projects \
  --target-dir /path/to/CRSBench/benchmarks \
  --dry-run

# Actual migration
.venv/bin/python -m crsbench.migration.atlanta_to_rfc \
  --source-dir /path/to/oss-fuzz/projects \
  --target-dir /path/to/CRSBench/benchmarks \
  --projects curl-delta-04
```

### Features

- ✅ Complete directory structure transformation
- ✅ Config.yaml to meta.yaml conversion
- ✅ Vulnerability metadata generation with mock data
- ✅ Multiple POV variant support
- ✅ Dry-run mode for validation
- ✅ Comprehensive logging
- ✅ CSV migration reports

### Design Documentation

See [migration design document](../../design-docs/migration-atlanta-to-rfc.md) for implementation details.