# Example Configs

Use these example files as reference inputs:

- [Distributed experiment config](../../docs/experiment-config-distributed-example.yaml)
- [Discovery-only OSS-Fuzz guide](../guides/experiments/discovery-only.md)
- [Benchmark suite example](../../docs/benchmark-suite-example.yaml)
- [Meta example](../../docs/meta-example.yaml)
- Repository config sets: [../../experiment-configs/README.md](../../experiment-configs/README.md)

Discovery-only example configs live under
[`experiment-configs/discovery-testing/`](../../experiment-configs/discovery-testing/).
Discovery smoke configs live under
[`experiment-configs/discovery-smoke-testing/`](../../experiment-configs/discovery-smoke-testing/).

Concrete discovery smoke examples:
- [`opencode-go-yaml-bugfinding.yaml`](../../experiment-configs/discovery-smoke-testing/opencode-go-yaml-bugfinding.yaml)
- [`gce-opencode-go-yaml-bugfinding.yaml`](../../experiment-configs/discovery-smoke-testing/gce-opencode-go-yaml-bugfinding.yaml)

The grouped distributed experiment config is the primary contract reference for
experiment YAML structure.
