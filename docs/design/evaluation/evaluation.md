# Design: Evaluation
- Audience: maintainers working on CRS execution, result aggregation, and verify semantics
- Scope: evaluation contracts for benchmark execution and result aggregation
- Related: [Snapshots](./snapshots.md), [Patch Verification](./patch-verification.md), [Distributed Evaluation](../distributed/distributed-evaluation.md)

## Goals and Non-goals

### Goals
- define the evaluation lifecycle for bug-finding and bug-fixing execution
- define result-shape contracts and aggregate semantics
- define evaluation behavior across local and distributed execution paths

### Non-goals
- implementation mirroring of runner/adapter classes
- CLI tutorials
- exhaustive schema duplication from runtime config docs

## Context and Boundaries

The evaluation subsystem bridges:
- benchmark metadata and harness definitions
- CRS adapter lifecycle (`prepare`, `build-target`, `run`)
- POV/patch verification and aggregate reporting

It consumes validated experiment/benchmark inputs and produces structured per-harness and aggregate results.

## Lifecycle Contract

For each benchmark harness, evaluation follows this lifecycle:
1. resolve benchmark mode and harness context
2. configure the CRS adapter
3. prepare/build target as required by the adapter lifecycle
4. execute the CRS run phase
5. interpret run outputs into per-POV or per-patch results
6. aggregate harness-level results into benchmark-level output

## Adapter Contract

The adapter boundary must provide:
- explicit mode (`bug-finding` or `bug-fixing`)
- a stable prepare/build-target/run lifecycle
- deterministic output interpretation for the selected mode
- enough metadata for downstream reporting and verification

The concrete adapter implementation may change, but callers depend on these lifecycle guarantees.

## Result Contracts

### POVStatus
Canonical per-POV states:
- `FOUND`
- `MISSED`
- `ERROR`

### POVResult
Must capture:
- POV identity
- harness/sanitizer context
- status
- execution time
- optional error/output details

### HarnessResult
Must capture:
- harness identity
- overall success/error state
- total harness execution time
- list of associated POV or verification results

### EvaluationReport
Must capture:
- benchmark identity and mode
- summary counts and success/error metrics
- per-harness results
- configuration provenance
- total execution time
- serializable output form for reporting

## Aggregate Semantics

- infrastructure/dependency failures must remain distinguishable from benchmark-result failures
- split POV/Patch checks are authoritative only when the full split set exists
- partial split data may fall back to combined legacy checks where supported
- aggregate status gives infrastructure `ERROR` precedence over ordinary `FAIL` when the failure is not benchmark-semantic

## Distributed / CI Semantics

- evaluator `--ci` behavior is a compatibility alias on top of the unified evaluator path
- CI-specific behavior is queue selection, not a separate evaluation model
- build context and verify context must remain consistent across remote nodes
- verify-side cache hydration must not silently violate build/verify ownership

## Failure Semantics

- invalid benchmark/config state aborts before CRS execution
- CRS execution errors propagate into structured result states rather than ambiguous missing output
- missing dependency/build context is an infrastructure failure
- stale queue metadata or missing queued jobs is an infrastructure failure in distributed paths

## Trial Budget Policy

Per-CRS `budget_policy` governs how a trial reacts when the LiteLLM trial
budget (`runtime.litellm.cost_budget`) is exceeded:

- `continue` (default): the trial keeps running after LiteLLM revokes the
  key; no further cost is incurred but in-progress work is preserved
- `terminate`: the trial stops early via the same stop-event path used by
  POV early-stop, and a `budget-exceeded.json` record is persisted in the
  trial directory

Detection piggybacks on the snapshot cadence, so termination is cooperative
and may lag by up to one snapshot cycle. The policy is inert when no trial
budget is configured. Budget enforcement must never fail a trial on
polling errors; tracker-side failures are logged and ignored.

## Chained Bug-finding → Bug-fixing Input

Bug-fixing trials can be seeded with POVs discovered by a prior bug-finding
experiment instead of the benchmark's ground-truth blobs. This enables a
first-pass "full pipeline" where bug-finding runs first, producing POVs under
its experiment store, and a subsequent bug-fixing run consumes those POVs.

Contract:

- Opt in via `inputs.pov.from_experiment: <path>` in the bug-fixing config,
  pointing at a prior bug-finding experiment directory
  (`<experiment_filestore>/<experiment_name>`). Only valid when
  `task: bugfixing`; invalid for bug-finding.
- For multi-CRS experiments, use `inputs.pov.from_experiment_by_crs` instead
  to route each fixing CRS to a specific finding-experiment subtree:
  ```yaml
  inputs:
    pov:
      from_experiment_by_crs:
        crs-claude-code: .run/cc-finding/crs-claude-code
        crs-codex:       .run/cc-finding/crs-codex
  ```
  Mutually exclusive with `from_experiment`. A fixing CRS absent from the
  map has no POVs and is skipped at trial-matrix generation — use this to
  drop a CRS from the fixing run without editing `crs_compose`.
- CPVs absent from the source experiment are skipped at trial-matrix
  generation time — no bug-fixing trial is scheduled for an undiscovered CPV.
- POVs are deduplicated by crash signature (same algorithm as
  `crsbench benchmark dedup-povs`): prefer the `crash_signature` recorded in
  `pov_store.json`, fall back to `parse_crash_signature` on the associated
  crash log, and conservatively keep POVs whose signature cannot be parsed.
  Earliest `discovery_ts` wins within a signature bucket.
- `inputs.pov.max_variants_per_cpv` continues to cap how many deduped variants
  are staged per CPV.
- Staging layout matches ground-truth bug-fixing (`trial/crs-input/povs/`,
  `trial/crs-input/cpvs/<cpv>/`, `trial/povs/`), so downstream adapters see
  the same shape regardless of POV provenance.

Deployment behaviour:

- **Local runs**: `from_experiment` is a local directory on the operator's
  machine. The orchestrator process reads it directly.
- **Cloud (GCE) runs**: `from_experiment` (or each entry of
  `from_experiment_by_crs`) remains a local directory on the operator's
  machine. At `crsbench cloud launch` time, the CLI walks each local
  bundle and uploads only the files that `ExternalPovSource` will read
  — `pov_store.json` for every trial, plus CPV blobs and (when a crash
  signature is missing) the matching crash log — preserving their
  relative layout under canonical absolute paths on every orchestrator
  and worker VM:

  - Single-path mode: `/var/lib/crsbench/from-experiment/<experiment_name>/`.
  - Per-CRS mode: `/var/lib/crsbench/from-experiment/<experiment_name>/by-crs/<crs>/`
    per map key.

  The transported YAML is rewritten in memory so each entry resolves to
  its remote path; the on-disk user config is untouched. Evaluators
  receive the rewritten YAML for config parity but are not push targets —
  they do not read `from_experiment*`.

The push reuses the same SSH infrastructure as `cloud collect`. If SSH
readiness or any per-VM rsync fails, the launch is aborted and the
just-created VMs are rolled back. Re-running `cloud launch` is idempotent:
rsync resumes any partial transfer via `--partial-dir`.

Implementation pointers:

- `crsbench/evaluation/external_pov_source.py` — reads and dedups POVs.
- `crsbench/evaluation/runner.py` — `_prepare_bugfix_inputs_from_experiment`.
- `crsbench/run_experiment.py` — `generate_trial_matrix` CPV filter.
- `crsbench/cloud/from_experiment_bundle.py` — manifest walker that enumerates
  only the files the reader will consume.
- `crsbench/cloud/collection.py` — `ArtifactPusher`, `wait_for_ssh_ready`.
- `crsbench/cloud/ssh_broker.py` — shared SSH/IAP transport helper.
- `crsbench/cloud/gce/metadata.py` — `_rewrite_from_experiment_path`.

## Decisions and Tradeoffs

- decision: keep evaluation centered on an adapter lifecycle
  - tradeoff: more abstraction, cleaner support for multiple CRS behaviors
- decision: preserve explicit `ERROR` vs `FAIL`
  - tradeoff: more result complexity, better operational diagnosis
- decision: use structured per-harness aggregation
  - tradeoff: richer data model, clearer reporting and verification behavior

## Validation

This contract should be covered by:
- adapter branching tests
- evaluation result-shape tests
- split-vs-combined result fallback tests
- distributed evaluator/CI result-semantics tests

## Implementation Pointers

- `crsbench/evaluation/runner.py`
- `crsbench/evaluation/adapter/`
- `crsbench/evaluation/results.py`
