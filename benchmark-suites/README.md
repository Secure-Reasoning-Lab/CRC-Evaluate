# Benchmark Suites

YAML files defining named subsets of benchmarks for CRSBench evaluation.
Reference a suite from an experiment config with `experiment.benchmark_suite:
<path>` (e.g. `afc/final`); the loader resolves `<path>` relative to this
directory and appends `.yaml`.

## Layout

```
benchmark-suites/
├── afc/                     # DARPA AIxCC Final Competition (AFC) benchmarks
│   ├── all.yaml             # union of all rounds (61)
│   ├── r2.yaml              # round 2 (13)
│   ├── r3.yaml              # round 3 (14)
│   ├── final.yaml           # final round (36)
│   ├── c.yaml               # C/C++ subset (38)
│   └── jvm.yaml             # JVM subset (23)
├── asc/
│   └── all.yaml             # ASC benchmarks (1)
├── atlanta/
│   └── all.yaml             # Atlanta team benchmarks (62)
├── crsbench/                # combined CRSBench (AFC + Atlanta + ASC)
│   ├── all.yaml             # everything except sanity (123)
│   ├── c.yaml               # C/C++ across all sources (62)
│   ├── jvm.yaml             # JVM across all sources (61)
│   └── except-afc-final.yaml  # crsbench minus afc/final (81)
└── smoke/
    ├── sanity.yaml          # 2 tiny mocks — fastest possible run
    ├── bug-finding.yaml     # 7-benchmark smoke suite for bug-finding
    ├── bug-fixing.yaml      # 6-benchmark smoke suite for bug-fixing
    └── hf-download.yaml     # 3-benchmark cloud download rehearsal
```

## Suite file format

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
- A benchmark ID may appear only once in `benchmark_list`.

`Name` is documentation-only; the suite identifier used by configs is the path
under `benchmark-suites/` without the `.yaml` extension (e.g.
`afc/final` for `afc/final.yaml`).

## Adding a new suite

1. Drop a YAML file in the appropriate subdirectory (or create one).
2. Reference it from an experiment config: `benchmark_suite: <subdir>/<name>`.
3. Suite content is validated by `BenchmarkSuiteConfig` in
   `crsbench/validation/schemas.py`.

## AFC benchmarks not yet in CRSBench

The following 15 AFC competition projects do not have POV blobs or `vulns/`
directories under `.aixcc/` and so cannot be used as ground-truth benchmarks:

| Round | Project |
|-------|---------|
| R2 | dropbear-full-01 |
| R3 | libpostal-full-01 |
| Final | curl-delta-06, dav1d-full-01, dcm4che-full-01, dicoogle-full-01, freerdp-delta-04, healthcare-data-harmonization-full-01, hertzbeat-full-01, jsoup-full-01, libavif-delta-03, lcms-delta-01, mongoose-delta-03, ndpi-full-01, openssl |

Round 1 was an example/exercise round (`*-ex1`) and is intentionally excluded.
