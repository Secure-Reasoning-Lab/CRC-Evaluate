# CRSBench - Cyber Reasoning System Benchmark Suite

CRSBench is the benchmark suite for [OSS-CRS](https://github.com/ossf/oss-crs),
the open-source orchestration framework for LLM-based autonomous bug-finding
and bug-fixing systems (Cyber Reasoning Systems). It provides curated
benchmarks and an evaluation harness for measuring any OSS-CRS-compatible CRS
on vulnerability discovery and program repair.

Unlike traditional fuzzing benchmarks (e.g., FuzzBench) that only report
coverage/crashes, CRSBench stores complete ground truth to track whether
vulnerabilities are actually found and correctly patched.

## Benchmark Statistics

| Metric | Value |
| --- | --- |
| Benchmarks | **124** (87 Delta + 37 Full) |
| Upstream projects | **82** |
| Vulnerabilities (CPVs) | **315** |
| C / C++ | 63 benchmarks, 123 vulnerabilities |
| JVM (Java) | 61 benchmarks, 192 vulnerabilities |
| Distinct CWEs | **91** (covers 21 of the 2025 CWE Top 25) |
| Vulnerabilities per harness | 1.65 average, 12 max |
| PoV variants per vulnerability | 3.89 average |

Full breakdown and regeneration steps:
[docs/reference/benchmark-statistics.md](docs/reference/benchmark-statistics.md).

## Quick Start

CRSBench is Linux-only and requires Docker. The smallest first run is a
queue-backed single-host experiment against the `sanity` suite.

```bash
git clone https://github.com/sslab-gatech/CRSBench.git && cd CRSBench
git submodule update --init --recursive
uv sync
./scripts/setup-third-party.sh
uv run crsbench prepare
uv run crsbench prepare --coverage   # for the bundled starter CRS
```

Configure environment variables. CRSBench auto-loads `.env` from the repo root;
edit it for distributed Redis, LiteLLM credentials, etc. See
[docs/getting-started/configuration.md](docs/getting-started/configuration.md)
for the full reference.

```bash
cp .env.example .env
```

Request access to the HuggingFace dataset (gated). Open
<https://huggingface.co/datasets/sslab-gatech/crsbench-dataset> and accept the
Data Use Agreement - access is granted after manual approval. Once approved,
authenticate (either set `HF_TOKEN=hf_...` in `.env`, or run `hf auth login`)
and download the sanity suite:

```bash
uv run hf auth login   # or set HF_TOKEN in .env
uv run crsbench download --benchmark-suite smoke/sanity
```

Run the bundled quick-start config
[experiment-configs/smoke-testing/first-run.yaml](experiment-configs/smoke-testing/first-run.yaml).
It targets the `smoke/sanity` suite (2 benchmarks, 3 harnesses) with the
bundled `atlantis-multilang-given_fuzzer` CRS, runs 3 trial jobs in parallel,
and does not need external LLM credentials (`runtime.litellm.skip: true`):

```bash
uv run python scripts/valkey-helper.py start
uv run crsbench worker --experiment-config experiment-configs/smoke-testing/first-run.yaml   # terminal 1
uv run crsbench run    --experiment-config experiment-configs/smoke-testing/first-run.yaml   # terminal 2
```

## Documentation

Start with **[Getting Started](docs/getting-started/README.md)**:

1. [Install](docs/getting-started/install.md)
2. [Configuration](docs/getting-started/configuration.md)
3. [First Experiment](docs/getting-started/first-experiment.md)
4. [Experiments](docs/getting-started/experiments.md) - bug-finding, bug-fixing, discovery, replay, merge
5. [Deployment](docs/getting-started/deployment.md) - single-machine, multi-machine, GCE cloud

Other entry points:

- Benchmark format contract: [docs/RFC.md](docs/RFC.md)
- Full docs hub: [docs/README.md](docs/README.md)
- Contributing: [CONTRIBUTING.md](CONTRIBUTING.md)

## Architecture

```
CRSBench/
├── benchmarks/              # Benchmark projects (RFC format)
├── crsbench/                # Main Python package
│   ├── builder/             #   OSS-Fuzz variant building
│   ├── evaluation/          #   CRS execution & verification
│   ├── distributed/         #   Multi-machine execution (Redis/RQ)
│   ├── benchmark/           #   Packaging, canary, seed tools
│   ├── dataset/             #   HuggingFace upload/download
│   ├── validation/          #   Format validation & schemas
│   ├── reporting/           #   Reports & dashboard
│   └── statistics/          #   Benchmark statistics
├── oss-crs/                 # OSS-CRS runtime and registry (submodule)
├── third_party/oss-fuzz/    # Managed OSS-Fuzz checkout (sparse)
└── docs/                    # Documentation hub
```

## License

CRSBench is licensed under [MIT](LICENSE). Bundled upstream source code retains
its original license - see [LICENSE-THIRD-PARTY.md](LICENSE-THIRD-PARTY.md).

## Related Projects

- [FuzzBench](https://github.com/google/fuzzbench) - fuzzer evaluation platform
- [OSS-Fuzz](https://github.com/google/oss-fuzz) - continuous fuzzing for open source
- [AIxCC](https://aicyberchallenge.com/) - AI Cyber Challenge
