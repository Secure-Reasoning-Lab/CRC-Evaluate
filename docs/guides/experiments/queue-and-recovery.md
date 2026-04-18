# Queue and Recovery

Use this page for queue cleanup, continue-vs-fresh behavior, and retry flows.

## Snapshot Versus Live Attach

For launched cloud experiments, these commands follow the shared cloud reconnect
contract: `status`, `events`, and `monitor` talk to the experiment control
plane, while collection and teardown can fall back to persisted launch state.
Today that managed-cloud path is implemented for GCE launches.

- `crsbench cloud --config <config.yaml> status <experiment>` prints a one-shot
  fleet, job, and recovery snapshot. In remote-orchestrator mode it waits for
  the Redis tunnel during bootstrap and falls back to the live queue view if
  lifecycle rows have not been populated yet.
- `crsbench cloud --config <config.yaml> monitor <experiment>` attaches to a
  launched remote orchestrator and keeps refreshing the live trial-queue view.
  When the terminal is too short to show every running-job row, the Rich view
  keeps the queue summary visible, shows a page indicator in the running-job
  caption, and rotates the visible running-job page on each refresh instead of
  pinning the same top rows forever.
  Cloud worker names in the monitor use the same short aliases as other cloud
  commands, so prefixes like `crsbench-<experiment>-` are hidden and workers
  appear as `work-001`, `work-002`, and so on.

When Apprise URLs are configured in the operator environment, `cloud monitor`
sends one operator-side terminal notification after it first observes the queue
transition from non-empty to empty during that attached session. When failed
jobs remain at that drain point, the terminal message reports a failure instead
of a completion. Attaching while the queue is already idle does not emit a
terminal notification for that initial idle state, but a later active-to-idle
transition in the same session still can. If operator-side `cloud monitor`
Apprise and orchestrator-side Apprise are both enabled, terminal notifications
can duplicate.

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
