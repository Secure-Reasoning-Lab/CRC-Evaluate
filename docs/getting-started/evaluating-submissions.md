# Submission Validation and Registration

CRC-Evaluate validates CRC-Template-compatible submissions and registers the selected Finder and Patcher under the names used by experiment configurations. Run all commands from the CRC-Evaluate repository root.

## Submission Directory

Clone the team submission into the public sanity directory:

```bash
git clone --recurse-submodules https://github.com/your-team/your-crc-template.git .run/sanity/team-01/submission
```

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

Each path is relative to the submission root and must identify a distinct OSS-CRS-compatible source directory containing `oss-crs/crs.yaml`. The Finder must declare `type: [bug-finding]`, the Patcher must declare `type: [bug-fixing]`, and each component must list its model aliases in `required_llms`.

`submission.name` is display metadata. The `--team-id` passed during registration determines the registry namespace, and repository-local launch configurations are not imported.

## Validate the Submission

```bash
uv run crsbench submission validate .run/sanity/team-01/submission
```

Validation checks the manifest schema, confines selected paths to the submission root, reads each selected `oss-crs/crs.yaml`, and verifies the CRS roles and model declarations.

## Register the Finder and Patcher

The public sanity configurations use the `team-01` namespace:

```bash
uv run crsbench submission register .run/sanity/team-01/submission \
  --team-id team-01 \
  --registry-dir .run/sanity/registry
```

Registration creates `.run/sanity/registry/team-01-finder.yaml` and `.run/sanity/registry/team-01-patcher.yaml` with registry IDs `team-01-finder` and `team-01-patcher`. Existing entries are preserved by default.

Use `--force` when intentionally replacing both entries for the same team ID:

```bash
uv run crsbench submission register .run/sanity/team-01/submission \
  --team-id team-01 \
  --registry-dir .run/sanity/registry \
  --force
```

Registry entries contain absolute paths to the selected CRS directories. Do not move the submission checkout after registration; register it again if either selected path changes.

## Run the Public Sanity Workflow

Configure the LLM endpoint and aliases as described in the [participant guide](../../README.md), then run:

```bash
./.run/sanity/team-01/run-sanity.sh
```

The launcher runs the Finder first and starts the Patcher only after the Finder completes successfully. The tracked Patcher configuration consumes the verified Finder outputs.
