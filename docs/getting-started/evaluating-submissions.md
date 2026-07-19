# Evaluating Submissions

CRSBench can validate a CRC-Template-compatible submission and register its selected Finder and Patcher under evaluator-owned names.
The evaluator then uses those registry names in standard experiment configurations.

## Submission Contract

The submission root must contain `submission.yaml`:

```yaml
schema_version: 1

submission:
  name: example-crs

crs:
  finder:
    path: crs/example-finder
  patcher:
    path: crs/example-patcher
```

Each path is relative to the submission root and must identify a distinct OSS-CRS-compatible source directory containing `oss-crs/crs.yaml`.
The Finder must declare `type: [bug-finding]`, and the Patcher must declare `type: [bug-fixing]`.
Each CRS declares its model dependencies through `required_llms` in its own `oss-crs/crs.yaml`.

The manifest selects submitted components.
`submission.name` is display metadata; the evaluator's `--team-id` determines the registry namespace.
Repository-local launch configurations are not imported when a submission is registered.

## Validate a Submission

Run validation before preparing images or scheduling trials:

```bash
uv run crsbench submission validate /data/submissions/team-001
```

Validation checks the manifest schema, confines selected paths to the submission root, reads each selected `oss-crs/crs.yaml`, and verifies the CRS roles and model declarations.

## Register the Selected CRSes

Generate namespaced entries in an evaluator-managed registry directory:

```bash
uv run crsbench submission register /data/submissions/team-001 \
  --team-id team-001 \
  --registry-dir /data/crs-registry
```

This creates `team-001-finder.yaml` and `team-001-patcher.yaml` with registry IDs `team-001-finder` and `team-001-patcher`.
Existing entries are preserved by default.
Use `--force` only when intentionally replacing both entries for the same team ID.

The generated entries use absolute local source paths.
On a distributed deployment, the submission and generated registry directory must be mounted at the same absolute paths on every worker, or the registry entries must be deployed with equivalent source locations before workers start.

## Run Evaluator-Owned Experiments

Set `registry_dir` to the generated registry and use the generated ID as the flat service key under `crs_compose`.
A bug-finding configuration contains:

```yaml
registry_dir: /data/crs-registry

crs_compose:
  oss_crs_infra:
    shared: true
  team-001-finder:
    num_cores: 8
    mem_limit: 64G
    budget_policy: terminate
```

Use `team-001-patcher` in a `task: bugfixing` configuration.
All remaining experiment fields—including suites, modes, time limits, LLM policy, storage, and worker topology—come from the evaluator's standard configuration.
Start workers and submit the experiment with that same configuration:

```bash
uv run crsbench worker --experiment-config evaluator-finding.yaml
uv run crsbench run --experiment-config evaluator-finding.yaml
```

For a two-stage evaluation, run the Finder first and pass its verified POV outputs to the Patcher by following the [full-pipeline workflow](../experiments/full-pipeline.md).
