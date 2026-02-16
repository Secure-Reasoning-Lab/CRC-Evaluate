# CRSBench - Cyber Reasoning System Benchmark Suite

A benchmark suite for evaluating AI-powered Cyber Reasoning Systems (CRS) across vulnerability discovery, program repair, and evaluation.

Unlike traditional fuzzing benchmarks (e.g., FuzzBench) that only report coverage/crashes, CRSBench stores complete ground truth to track whether vulnerabilities are actually found and correctly patched.

## Quick Start

```bash
# Install
git clone https://github.com/sslab-gatech/CRSBench.git && cd CRSBench
uv sync

# Download benchmarks from HuggingFace (gated — requires access)
#   1. Create a token at https://huggingface.co/settings/tokens
#   2. Accept the Data Use Agreement at https://huggingface.co/datasets/sslab-gatech/crsbench-dataset
#   3. Log in:
uv sync --extra dataset
huggingface-cli login
crsbench download --all                     # all 134 benchmarks (~12GB)
crsbench download --benchmark-suite sanity  # small test suite

# Run an experiment (requires Valkey/Redis for job queue)
python scripts/valkey-helper.py --password start  # auto-generates password, saved to .env
crsbench run       --experiment-config experiment-configs/experiment-config-sanity.yaml
crsbench worker    --experiment-config experiment-configs/experiment-config-sanity.yaml
crsbench evaluator --experiment-config experiment-configs/experiment-config-sanity.yaml
```

CRS experiments use a distributed job queue (Redis/RQ). The orchestrator (`run`) enqueues jobs,
workers (`worker`) execute them. Add `evaluator` for real-time POV verification.

See [Experiment Workflow](docs/experiment-workflow.md) for multi-machine setup, core pinning, and
production deployment. See [Environment Setup](docs/environment-setup.md) for `.env` configuration.

### Verification (Standalone)

```bash
crsbench verify       benchmarks/project --pov-dir ./povs/
crsbench patch-verify benchmarks/project --patch-dir ./patches --pov-dir ./povs
crsbench coverage     benchmarks/project --corpus-dir ./corpus/
```

### Results

```bash
crsbench report    --experiment my-exp
crsbench dashboard --base-dir ./experiments
crsbench stats     --output stats.csv
```

### Dataset

```bash
crsbench download --all                                        # all benchmarks
crsbench download --dataset crsbench --benchmarks afc-curl-delta-01  # specific
crsbench download --benchmark-suite sanity                     # suite
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for developer commands (`crsbench benchmark`, `crsbench ci`).

## Architecture

```
CRSBench/
├── benchmarks/              # Benchmark projects (RFC format)
├── crsbench/                # Main Python package
│   ├── run_experiment.py    #   CLI entry point
│   ├── builder/             #   OSS-Fuzz variant building
│   ├── evaluation/          #   CRS execution & verification
│   ├── benchmark_ci/        #   Benchmark CI pipeline
│   ├── distributed/         #   Multi-machine execution (Redis/RQ)
│   ├── benchmark/           #   Packaging, canary, seed tools
│   ├── dataset/             #   HuggingFace upload/download
│   ├── validation/          #   Format validation & schemas
│   ├── migration/           #   Format migration tools
│   ├── hint_generation/     #   Progressive hint generation
│   ├── reporting/           #   Reports & dashboard
│   ├── statistics/          #   Benchmark statistics
│   └── utils/               #   Shared utilities (logger, YAML, etc.)
├── crses/                   # CRS configurations for evaluation
├── oss-crs-registry/        # CRS registry (submodule)
├── oss-fuzz/                # OSS-Fuzz (submodule)
├── docs/                    # User-facing documentation
└── design-docs/             # Internal architecture docs
```

## Documentation

| Topic | Link |
|-------|------|
| Benchmark RFC Specification | [docs/benchmark-spec.md](docs/benchmark-spec.md) |
| Experiment Workflow | [docs/experiment-workflow.md](docs/experiment-workflow.md) |
| Environment Setup | [docs/environment-setup.md](docs/environment-setup.md) |
| CRS Interface | [docs/ossfuzz-crs-interface.md](docs/ossfuzz-crs-interface.md) |
| Distributed Execution | [design-docs/distributed/](design-docs/distributed/) |
| Dataset Module | [design-docs/dataset/dataset.md](design-docs/dataset/dataset.md) |
| Architecture Overview | [design-docs/architecture.md](design-docs/architecture.md) |
| Logging | [docs/logger-usage-guide.md](docs/logger-usage-guide.md) |

Each module has its own README under `crsbench/<module>/README.md`.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and coding standards.

## License

CRSBench is licensed under [MIT](LICENSE). Bundled upstream source code retains
its original license — see [LICENSE-THIRD-PARTY.md](LICENSE-THIRD-PARTY.md).

## Citation

If you use CRSBench, please cite it. See [CITATION.cff](CITATION.cff) for
machine-readable metadata, or use:

```bibtex
@software{crsbench2026,
  title     = {CRSBench: A Benchmark for Cyber Reasoning Systems},
  author    = {Kim, Youngjoon and Fu, Yu-Fu and Kim, Dongkwan and
               Kim, Jiho and Chin, Andrew and Park, Younggi and
               Zhang, Cen and Lee, Junekyu and Fleischer, Fabian and
               Jeong, Dae R. and Xu, Qingxiao and Sheng, Ze and
               Silliman, Jonathan and Mcdaniel, Mikel and
               Casavant, Jefferson and Goldthwaite, Isaac and
               Vidovich, Nicholas and Lehman, Matthew and
               Kim, Taesoo},
  year      = {2026},
  url       = {https://github.com/sslab-gatech/CRSBench},
  license   = {MIT},
  note      = {Youngjoon Kim, Yu-Fu Fu, and Dongkwan Kim contributed equally},
}
```

## Related Projects

- [FuzzBench](https://github.com/google/fuzzbench) - Fuzzer evaluation platform
- [OSS-Fuzz](https://github.com/google/oss-fuzz) - Continuous fuzzing for open source
- [AIxCC](https://aicyberchallenge.com/) - AI Cyber Challenge
