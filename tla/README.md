# TLA+ Model Notes

## Files

- `DistributedTimeoutRecovery.tla`: minimal dual-state model of queue state plus shadow lifecycle state
- `DistributedTimeoutRecovery.cfg`: tiny TLC model configuration that enables the buggy timeout-requeue path

## Purpose

This first model is meant to catch a specific bug class:

- a job times out
- recovery updates lifecycle state back to `queued`
- but the concrete executable queue entry is not restored

That produces a stalled job even though the lifecycle layer says it is runnable again.

## Run TLC

TLC is provided by `tla2tools.jar` and requires Java.

This repo can use `.envrc` to expose the jar on `CLASSPATH`:

```bash
source .envrc
java tlc2.TLC -config tla/DistributedTimeoutRecovery.cfg tla/DistributedTimeoutRecovery.tla
```

If you prefer not to rely on `CLASSPATH`:

```bash
java -cp /path/to/tla2tools.jar tlc2.TLC \
  -config tla/DistributedTimeoutRecovery.cfg \
  tla/DistributedTimeoutRecovery.tla
```

## Expected First Result

With `BuggyRequeueEnabled = TRUE`, the initial model is expected to fail `QueuedMeansExecutable` and produce a short counterexample. That is intentional: the config is set up to demonstrate the timeout-recovery divergence bug class.

Observed counterexample shape:

- `ClaimJob`
- `StartJob`
- `StaleHeartbeat`
- `CrashWorker`
- `TimeoutScanGrace`
- `TimeoutRecoverToQueuedBuggy`

Final bad state:

- `lcState[j] = "queued"`
- `rqState[j] = "rq_absent"`

To explore the intended recovery behavior instead, set:

```tla
BuggyRequeueEnabled = FALSE
```

in `tla/DistributedTimeoutRecovery.cfg` and re-run TLC.
