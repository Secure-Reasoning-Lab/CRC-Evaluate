# Install CRSBench

Use this page for the first-time bootstrap path. For environment variables and
deployment scenarios, see [configuration.md](./configuration.md).

## Prerequisites

- Python 3.12+
- `uv`
- Docker
- Git

## Bootstrap

```bash
git clone <repo>
cd CRSBench
uv sync
uv run crsbench prepare
```

`crsbench prepare` initializes the managed `third_party/oss-fuzz` checkout and
pulls the base images CRSBench relies on.

## Next Steps

1. Configure environment and LiteLLM: [configuration.md](./configuration.md)
2. Run a first experiment: [first-experiment.md](./first-experiment.md)
3. Author or inspect config files: [../guides/experiments/config-reference.md](../guides/experiments/config-reference.md)
