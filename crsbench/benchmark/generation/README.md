# Benchmark Generation

This module will provide tools to **create new benchmarks** from various vulnerability sources.

## Status

**Not yet implemented** - Placeholder for future development.

## Planned Architecture

```
generation/
├── __init__.py
├── generator.py           # Core generator orchestration
├── templates/             # Benchmark templates per language
│   ├── c_cpp.py
│   ├── java.py
│   └── ...
└── adapters/              # Convert external sources → benchmark format
    ├── base.py            # AbstractAdapter interface
    ├── ossfuzz_vuln.py    # oss-fuzz-vuln database
    ├── bug_bounty.py      # Bug bounty reports
    ├── cve.py             # CVE/NVD entries
    └── manual.py          # Interactive wizard
```

## Planned Adapters

| Adapter | Source | Description |
|---------|--------|-------------|
| `OSSFuzzVulnAdapter` | oss-fuzz-vuln | Google's OSS-Fuzz vulnerability database |
| `CVEAdapter` | NVD/CVE | National Vulnerability Database |
| `BugBountyAdapter` | HackerOne, etc. | Bug bounty platform reports |
| `ManualAdapter` | Interactive | Wizard for manual benchmark creation |

## Workflow

```
External Source ──► Adapter ──► VulnerabilityInfo ──► Generator ──► Benchmark Directory
                                (normalized)                        (ready for packaging)
```

## Future CLI

```bash
# Generate from oss-fuzz-vuln
crsbench benchmark generate --from ossfuzz-vuln --id OSV-2024-1234

# Generate from CVE
crsbench benchmark generate --from cve --id CVE-2024-12345

# Interactive wizard
crsbench benchmark generate --interactive
```

## Related Modules

- `../packaging/` - Package generated benchmarks for distribution
- `../runtime/` - Load packaged benchmarks for evaluation
