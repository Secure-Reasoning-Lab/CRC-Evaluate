# Benchmark Suites

This directory contains YAML files defining benchmark suites for CRSBench evaluation.

## AFC (DARPA AIxCC) Benchmark Suites

The AFC benchmarks are organized by competition round:

| Suite | Description | Count |
|-------|-------------|-------|
| `afc-r2.yaml` | Round 2 benchmarks | 13 |
| `afc-r3.yaml` | Round 3 benchmarks | 14 |
| `afc-final.yaml` | Final round benchmarks | 36 |
| `afc-final-variants.yaml` | Final round benchmarks (only CPVs with POV variants, scoped by harness) | 17 |
| `afc-all.yaml` | All unique AFC benchmarks | 61 |

**Note:** Round suites may have overlapping projects (e.g., some R3 projects also appear in Final).

### Why no R1 suite?

Round 1 projects (`afc-oss-r1-projects/`) only contained "ex1" (example/exercise) variants:
- `afc-libxml2-lx-ex1-delta-01`
- `afc-zookeeper-zk-ex1-delta-01`

These were preliminary example challenges used for initial testing and are not included in CRSBench benchmarks. The actual competition benchmarks started from Round 2.

### AFC benchmarks NOT yet in CRSBench

The following 15 AFC competition projects have not been migrated to CRSBench:

| Round | Project |
|-------|---------|
| R2 | dropbear-full-01 |
| R3 | libpostal-full-01 |
| Final | curl-delta-06, dav1d-full-01, dcm4che-full-01, dicoogle-full-01, freerdp-delta-04, healthcare-data-harmonization-full-01, hertzbeat-full-01, jsoup-full-01, libavif-delta-03, lcms-delta-01, mongoose-delta-03, ndpi-full-01, openssl |

**Why are these missing?** These projects don't have POV blobs or vulns directories in their `.aixcc/` folders - they lack the ground truth data needed for the benchmark.

### Language-specific suites

| Suite | Description |
|-------|-------------|
| `crsbench-afc-c.yaml` | C/C++ AFC benchmarks |
| `crsbench-afc-jvm.yaml` | JVM (Java) AFC benchmarks |

## Suite Format

```yaml
Name: suite-name
Description: A description of the benchmark suite.
Release date: MM.DD.YYYY
benchmark_list:
  - benchmark-name-1
  - benchmark-name-2
  - benchmark-name-3:
      - harness-a
      - harness-b
  - benchmark-name-4:
      harness-c:
        - cpv_0
        - cpv_1
      harness-d:
        - cpv_2
```

Selector forms:
- `benchmark-name`: include all harnesses
- `benchmark-name: [harness-a, harness-b]`: include specific harnesses
- `benchmark-name: {harness-a: [cpv_0, cpv_1]}`: include specific CPVs per harness
- A benchmark ID can appear only once in `benchmark_list`.

Variant-focused suite notes:
- `afc-final-variants.yaml` uses the CPV-scoped form (`benchmark -> harness -> cpv list`).
- Inline comments like `# variants: N` indicate the number of POV blobs under `.aixcc/{harness}/{cpv}/blobs`.
