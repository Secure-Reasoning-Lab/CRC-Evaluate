# Full-Pipeline Experiments (Finding → Fixing)

A full-pipeline experiment chains a bug-finding run into a bug-fixing run so
the fixing CRS operates on POVs that another CRS actually discovered, instead
of the benchmark's ground-truth POVs.

CRSBench does not introduce a dedicated chaining mode for this. The two phases
are independent `crsbench run` invocations: the fixing config simply points
its POV input source at the finding config's output directory via
`inputs.pov.from_experiment_by_crs`.

## When to use it

- Bug-fixing alone (`bug-fixing.yaml`) consumes the benchmark's ground-truth
  POVs from `.aixcc/`. It measures how well a CRS patches a *known* vulnerable
  input.
- Full-pipeline fixing (`full-pipeline-fixing.yaml`) consumes POVs from a real
  prior finding run. It measures end-to-end discover-and-patch behavior, and
  every fixing CRS is forced to deal with whatever the finding CRS actually
  produced (including misses, duplicates by crash signature, and unintended
  crashes).

Use the standalone `bug-fixing.yaml` when you only want to benchmark the
patching capability. Use the full-pipeline pair when you want to evaluate the
combined discover-and-patch pipeline.

## The two phases

Phase 1 is `bug-finding.yaml` from the chosen subdir (`local/`, `gcp/`, or
`agentic-cli/`). It writes discovered POVs to its experiment filestore.

Phase 2 is `full-pipeline-fixing.yaml` in the same subdir. It differs from
`bug-fixing.yaml` in exactly one respect: it sets
`inputs.pov.from_experiment_by_crs` to point at the phase-1 output instead of
falling back to ground-truth POVs.

## How the chain is wired

`inputs.pov.from_experiment_by_crs` is a per-CRS map. Each key is a
**fixing-side** CRS name from this config's `crs_compose`, and each value is a
path to that CRS's bug-finding subtree from phase 1:

```yaml
runtime:
  inputs:
    pov:
      max_variants_per_cpv: 1
      from_experiment_by_crs:
        crs-claude-code: .run/local/experiment-data/local-bug-finding/local-bug-finding/local-bug-finding/crs-bug-finding-claude-code
```

CRSBench enumerates trials and stages POVs from the referenced directory in
place of ground-truth POVs. CPVs that the finding run missed are simply absent
from the fixing trial matrix. POVs are deduplicated by crash signature, and
`max_variants_per_cpv` caps how many variants per CPV are forwarded.

### Path layout

The path follows the on-disk tree CRSBench writes during a finding run:

```
<storage.experiment_filestore>/<experiment.name>/<experiment.name>/<finding-crs-name>
```

The duplicated `<experiment.name>` segment reflects how the orchestrator nests
per-experiment subtrees inside the experiment filestore. If you change
`experiment.name` or `storage.experiment_filestore` in phase 1, update the
phase-2 path to match.

### Single-CRS vs multi-CRS pairing

When a finding run hosts multiple CRSes (see `agentic-cli/bug-finding.yaml`),
the chain pairs each finding CRS to its matching fixing CRS:

```yaml
from_experiment_by_crs:
  crs-claude-code: .../crs-bug-finding-claude-code
  crs-codex:       .../crs-bug-finding-codex
  crs-copilot-cli: .../crs-bug-finding-copilot-cli
  crs-gemini-cli:  .../crs-bug-finding-gemini-cli
  crs-opencode:    .../crs-bug-finding-opencode
```

A fixing CRS without an entry in the map gets no POVs and is skipped during
trial-matrix generation. This is how each fixing agent only sees the POVs
produced by its same-CLI counterpart on the finding side.

The naming convention is `crs-bug-finding-<agent>` on the finding side and
`crs-<agent>` on the fixing side, but the map is opaque — any (fixing-CRS →
finding-CRS-subtree) wiring works as long as the subtree exists on disk.

## Running the chain

Local and agentic-cli (single-machine):

```bash
uv run crsbench run --config experiment-configs/local/bug-finding.yaml
uv run crsbench run --config experiment-configs/local/full-pipeline-fixing.yaml
```

GCE (cloud-orchestrated): phase 1 must be collected to disk before phase 2 can
read it. With the default GCE config, both phases consume
`/data/crsbench/experiment-data/...` on the orchestrator, so `cloud collect`
must complete before launching phase 2:

```bash
CONFIG_FIND=experiment-configs/gcp/bug-finding.yaml
CONFIG_FIX=experiment-configs/gcp/full-pipeline-fixing.yaml

uv run crsbench cloud launch   --config "$CONFIG_FIND"
uv run crsbench cloud monitor  --config "$CONFIG_FIND"
uv run crsbench cloud collect  --config "$CONFIG_FIND" --force
uv run crsbench cloud teardown --config "$CONFIG_FIND" --force

uv run crsbench cloud launch   --config "$CONFIG_FIX"
```

## Failure semantics

- If a CPV has no POV in the finding output, the fixing trial for that CPV is
  not generated at all (rather than running with empty input).
- POV files that fail re-verification during input staging are dropped before
  the fixing CRS starts. This mirrors how ground-truth POVs are staged when
  the fixing run is standalone.
- A fixing CRS listed in `crs_compose` but absent from
  `from_experiment_by_crs` produces no trials, even though the CRS service is
  spun up. This is the intended way to short-circuit a fixing agent whose
  finding-side counterpart did not run.

## Related

- [Experiment Config Reference](../reference/experiment-config.md) —
  `inputs.pov` schema, validation rules, default behavior per task.
- [experiment-configs/README.md](../../experiment-configs/README.md) —
  templates and per-tier conventions.
