# Benchmark Statistics

This page summarizes the headline statistics of the CRSBench benchmark suite
(`crsbench-all`) as released with the paper. The numbers below match the
companion paper and can be regenerated locally with the CRSBench CLI.

> The figures here exclude the small `sanity` suite, which is intended for
> smoke testing rather than for evaluation.

## Suite Overview

| Metric | Value |
| --- | --- |
| Benchmarks | **124** |
| Upstream projects | **82** |
| Vulnerabilities (CPVs) | **315** |
| Distinct CWEs | **91** |
| 2025 CWE Top 25 covered | **21 / 25 (84%)** |
| Vulnerabilities per harness | **1.65 average, 12 max** |
| PoV variants per vulnerability | **3.89 average** |

## Language Split

| Language | Benchmarks | Vulnerabilities |
| --- | --- | --- |
| C / C++ | 63 | 123 |
| JVM (Java) | 61 | 192 |
| **Total** | **124** | **315** |

## Mode Split

CRSBench supports two evaluation modes derived from AIxCC:

- **Delta** — bug-inducing commit (BIC) provided; the CRS targets a known regression.
- **Full** — no BIC; the CRS evaluates the full project source.

| Mode | Benchmarks | Vulnerabilities |
| --- | --- | --- |
| Delta | 87 | 214 |
| Full | 37 | 101 |
| **Total** | **124** | **315** |

## Vulnerability Origins

Each CPV is either a **1-day** vulnerability ported from a real disclosure or
a **synthetic** vulnerability injected by benchmark authors.

| Language | 1-day | Synthetic |
| --- | --- | --- |
| C / C++ | 19 (6.0%) | 104 (33.0%) |
| JVM (Java) | 101 (32.1%) | 91 (28.9%) |
| **Total** | **120 (38.1%)** | **195 (61.9%)** |

## Top CWEs

The 10 most frequent CWEs across the 315 CPVs:

| CWE | Name | CPVs | Share |
| --- | --- | ---: | ---: |
| CWE-502 | Deserialization of Untrusted Data | 64 | 9.24% |
| CWE-94 | Code Injection | 56 | 8.08% |
| CWE-470 | Unsafe Reflection | 53 | 7.65% |
| CWE-122 | Heap-based Buffer Overflow | 37 | 5.34% |
| CWE-787 | Out-of-bounds Write | 33 | 4.76% |
| CWE-400 | Uncontrolled Resource Consumption | 27 | 3.90% |
| CWE-78 | OS Command Injection | 24 | 3.46% |
| CWE-125 | Out-of-bounds Read | 23 | 3.32% |
| CWE-20 | Improper Input Validation | 22 | 3.17% |
| CWE-918 | Server-Side Request Forgery (SSRF) | 22 | 3.17% |

The full distribution spans 91 distinct CWEs and includes resource-exhaustion
classes such as timeouts and ReDoS, in addition to the memory-safety and
injection classes shown above.

## Reproducing These Numbers

The CRSBench CLI ships a `benchmark stats` subcommand that emits CSVs with
the same per-benchmark fields used to produce the tables above:

```bash
# Per-benchmark CSV + suite-level summary CSV for the published suite
uv run crsbench benchmark stats \
  --benchmark-suite crsbench-all \
  --output benchmark_stats.csv

# Merged vuln.yaml index (CPV-level metadata across all benchmarks)
uv run crsbench benchmark stats \
  --benchmark-suite crsbench-all \
  --vuln-index-output vuln-index.yaml
```

`benchmark_stats.csv` contains one row per benchmark (language, upstream
project, LoC, CPV count); `benchmark_stats_summary.csv` aggregates totals;
and `vuln-index.yaml` holds CPV-level metadata (CWEs, locations, descriptions,
release dates) keyed by `<benchmark>/<harness>/<cpv>`.

To narrow the run to a subset:

```bash
uv run crsbench benchmark stats --benchmarks afc-curl-delta-01 atlanta-curl-delta-01
uv run crsbench benchmark stats --filter 'afc-*'
```

See [docs/reference/vuln-yaml.md](./vuln-yaml.md) for the CPV metadata schema.
