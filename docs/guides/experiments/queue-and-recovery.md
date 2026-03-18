# Queue and Recovery

Use this page for queue cleanup, continue-vs-fresh behavior, and retry flows.

## Snapshot Versus Live Attach

- `crsbench cloud status <experiment> --config <config.yaml>` prints a one-shot
  fleet, job, and recovery snapshot.
- `crsbench cloud monitor <experiment> --config <config.yaml>` attaches to a
  launched remote orchestrator and keeps refreshing the live trial-queue view.

## Cleaning an Experiment Queue

```bash
crsbench queue clean --experiment <experiment-name> --yes
```

Optional scoped cleanup:

```bash
crsbench queue clean --experiment <experiment-name> --queues trial,verify --yes
```

## `crsbench run` Existing-Queue Behavior

- TTY: prompts for `fresh`, `continue`, or `quit`
- non-TTY: defaults to `continue`
- `continue`: skips completed work and handles orphaned started jobs
- failed jobs requeue only with explicit opt-in

```bash
crsbench run --experiment-config config.yaml --queue-mode continue --retry-failed
```

## Related

- Full experiment workflow: [distributed.md](./distributed.md)
- Benchmark CI queue topology: [../benchmark-ci/distributed.md](../benchmark-ci/distributed.md)
