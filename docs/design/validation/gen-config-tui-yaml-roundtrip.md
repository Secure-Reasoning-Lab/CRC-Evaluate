# Gen-Config TUI YAML Round-Trip Editing
- Audience: contributors working on `crsbench gen-config-tui` and config serialization
- Scope: contract for preserving loaded YAML structure, comments, and ordering when the TUI edits and writes experiment configs
- Related: [Validation Contract](../validation.md), [Design Documentation](../README.md)

## Goals and Non-goals

Goals:
- Preserve the loaded YAML document as the structural base for TUI edits.
- Keep semantic config validation driven by the normalized grouped-config model.
- Minimize edit distance when a user loads a file, edits it, and either updates it in place or saves it to a new path.
- Preserve meaningful YAML scaffolding such as comments, key order, empty inheritance placeholders, and formatting where possible.

Non-goals:
- Replacing schema validation with YAML-document-level validation.
- Treating an arbitrary save destination as the preservation base.
- Guaranteeing byte-for-byte output identity after semantic edits.
- Turning the TUI into a generic YAML editor for unsupported schema shapes.

## Constraints

- The TUI already uses a normalized grouped-config representation for previews, field editing, and schema validation.
- The write path must remain correct for provider-neutral cloud configs and other configs that rely on empty placeholder mappings such as `{}`.
- A user may load one file and save to a different path; the loaded file remains the authoritative preservation base.
- New configs created without loading a file first do not have an existing YAML document to preserve.

## Context and Boundaries

The TUI currently performs three distinct operations:
- Load a YAML file into a grouped-config model and flatten it into form state.
- Validate the grouped config through the CRSBench schema layer.
- Write a config by regenerating normalized YAML from grouped config.

The normalization path is correct for semantics but lossy for presentation. Regeneration can reorder fields, drop comments, collapse stylistic choices, and remove semantically meaningful placeholder mappings unless the TUI explicitly preserves them. The round-trip editing contract applies only to the write path. The grouped-config model remains the semantic source of truth for validation and preview rendering.

## Contract

### Preservation Base

- When a config file is loaded, the TUI must retain a round-trip YAML document for that loaded file in memory.
- That loaded document is the preservation base for all subsequent writes in the same editing session.
- `Update Loaded File` must write an edited version of the loaded document back to the loaded path.
- `Save As` must write an edited version of the same loaded document to the requested destination path, even when the destination path does not yet exist.
- If no file was loaded, writes may fall back to normalized YAML generation because no preservation base exists.

### Semantic Authority

- The grouped-config model remains authoritative for semantic values.
- Before any write succeeds, the grouped config derived from current form state must pass the existing CRSBench validation flow.
- The round-trip YAML writer must update the preservation-base document so that its effective semantic content matches the validated grouped config.

### Merge Semantics

- Untouched YAML structure from the loaded document should remain intact where it does not conflict with the edited grouped config.
- Edited fields must be reflected in the written YAML even if this changes nearby formatting or comments tied directly to the edited node.
- Unknown blocks and preserved extras that are outside the TUI’s editable surface must remain in the output unless they are semantically removed by the grouped-config model.
- Meaningful empty mappings used for inheritance or defaults, such as empty cloud placement entries and empty instance profile mappings, must survive round-trip writes.

### Destination Semantics

- The preservation base is the loaded document, not the destination file.
- If a user loads `A.yaml`, edits it, and saves to `B.yaml`, the output should be structurally derived from `A.yaml`.
- If `B.yaml` already exists, it is overwritten by the edited representation of `A.yaml`; the TUI does not merge against `B.yaml`.

## Runtime Behavior

Happy path:
- Load YAML into both a normalized grouped-config model and a round-trip YAML document.
- Edit form fields and rebuild the grouped-config model in memory.
- Validate the grouped config with the existing schema bridge.
- Apply the grouped-config changes onto a mutable round-trip document derived from the loaded YAML base.
- Write the round-trip document to either the loaded path or the save-as path.

Failure behavior:
- If the loaded YAML cannot be parsed into the round-trip representation, load fails and the TUI must not claim round-trip preservation.
- If grouped-config validation fails, the write fails before mutating the output path.
- If applying grouped-config changes onto the round-trip document fails, the write fails rather than silently falling back to lossy regeneration for a loaded-file workflow.
- If no file was loaded and normalized generation is used, the output remains valid but is not required to preserve formatting/comments because no base exists.

Retry behavior:
- Users may continue editing after a failed write.
- Repeated save attempts in the same loaded-file session continue to use the same loaded-document base unless the user reloads a different file.

## Deployment and Distributed Behavior

This contract is local to the config authoring TUI and does not alter distributed runtime behavior, worker execution, or cloud launch semantics. Its distributed impact is indirect: preserving semantically meaningful YAML scaffolding reduces the risk of unnecessary config churn in cloud and remote workflows where operators rely on comments, inherited placeholders, and stable diffs during review.

## Decisions and Tradeoffs

- Use a round-trip YAML library for write preservation instead of extending the current plain `PyYAML` regeneration path.
- Keep grouped-config generation for previews and validation instead of making the TUI edit arbitrary YAML directly.
- Prefer the loaded document as the single preservation base because it matches user intent for “load, edit, save” workflows and avoids ambiguous destination-file merge rules.
- Accept that some edited nodes may still change local formatting when the underlying round-trip YAML structure must be rewritten.

## Risks and Validation

Primary risks:
- Drift between grouped-config semantics and the round-trip document mutation logic.
- Accidental deletion of semantically meaningful placeholder nodes.
- Save-as behavior becoming ambiguous if destination-file content is incorrectly treated as a second source of truth.

Validation required:
- Regression tests for load-edit-update preserving comments and ordering.
- Regression tests for load-edit-save-as-to-new-file preserving the loaded file’s scaffolding.
- Regression tests for new-from-scratch saves continuing to emit valid normalized YAML.
- Regression tests for inherited empty cloud placeholders surviving round-trip writes.

## Implementation Pointers

- `crsbench/genconfig_tui/core.py`
- `crsbench/genconfig_tui/app.py`
- `tests/test_gen_config_tui_core.py`
- `tests/test_gen_config_tui_app.py`
