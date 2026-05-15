# Vulnerability Metadata RFC

Status: Draft
Canonical Path: `docs/reference/vuln-yaml.md`
Last Updated: 2026-05-15

`vuln.yaml` records structured metadata for one CRSBench vulnerability. It is
ground truth used by validation, statistics, manual review, and hint generation.

Each CPV directory must contain one `vuln.yaml`:

```text
benchmarks/<benchmark>/.aixcc/<harness>/<cpv-id>/vuln.yaml
```

## Goals

`vuln.yaml` should answer four questions:

- what vulnerability this CPV represents
- where the relevant project code is
- which vulnerability classes apply
- where the metadata came from and who checked it

It must not contain CRS-generated findings, trial runtime state, scoring data,
or experiment-specific hint selection.

## Example

```yaml
id: cpv_0
name: Heap buffer overflow in UTF-32 conversion
author: benchmark-author
validator: reviewer-name
origin: 1-day
release_date: 02/01/2026
references:
  - https://example.com/advisory/CVE-YYYY-NNNN
  - https://example.com/project/commit/abc123
cwes:
  - CWE-122
description: |
  The UTF-32 conversion path can write past the end of the destination buffer
  when the input length is inconsistent with the decoded output length.
locations:
  - type: root_cause
    path_from_root: xmlIO.c
    function_name: UTF32ToUTF8
    startLine: 2168
    startColumn: 17
    endLine: 2177
    endColumn: 4
  - type: crash_site
    path_from_root: parser.c
    function_name: parseDocument
    startLine: 912
    startColumn: 5
    endLine: 912
    endColumn: 30
```

## Top-Level Fields

Required fields:

- `id`: CPV identifier. Must match the containing CPV directory, for example
  `cpv_0`.
- `name`: short human-readable vulnerability name.
- `description`: concise explanation of the vulnerability and root cause.

Recommended fields:

- `cwes`: list of CWE identifiers such as `CWE-122`.
- `locations`: list of project-code locations related to the vulnerability.
- `origin`: `1-day` for a ported real vulnerability or `synthetic` for a
  benchmark-created vulnerability.
- `release_date`: disclosure or benchmark release date in `MM/DD/YYYY` format.
- `references`: authoritative URLs such as CVE, OSV, advisory, bug tracker, or
  upstream patch links.
- `author`: person or process that authored the metadata.
- `validator`: person who manually validated the metadata.

Unknown fields are allowed for forward compatibility, but consumers must not
require them unless this RFC is updated.

## Locations

Each `locations` entry describes repository-internal project code. Locations
must not point at harness code, libFuzzer internals, sanitizer runtimes, or
standard-library frames.

```yaml
locations:
  - type: root_cause
    path_from_root: src/parser.c
    function_name: parse_header
    startLine: 120
    startColumn: 3
    endLine: 126
    endColumn: 20
```

Fields:

- `type`: optional. Allowed values are `root_cause` and `crash_site`.
- `path_from_root`: source path relative to the unpacked repository root.
- `function_name`: optional function containing the location.
- `startLine`, `startColumn`, `endLine`, `endColumn`: one-based source region.

Use `root_cause` when the defect location is known. Use `crash_site` for the
project-code frame where the vulnerability manifests. If the true root cause is
uncertain, omit it instead of guessing.

## CWE Guidance

`cwes` should include all relevant CWE IDs. Prefer specific CWEs when they are
known, and include broader parent CWEs only when useful for analysis.

The hint-generation module may map CWEs to coarser categories for SARIF hints,
but that derived hint behavior is outside this metadata contract.

## Origin and References

For `origin: 1-day`, include at least one authoritative reference when possible:

- CVE record
- OSV entry
- upstream advisory
- upstream patch or fixing commit
- public bug tracker entry

For `origin: synthetic`, use the CRSBench release date and cite internal or
repository-local context when available.

## Validation Rules

Validation should check that:

- `id`, `name`, and `description` are present and non-empty
- `id` matches the containing CPV directory
- `origin`, when present, is `1-day` or `synthetic`
- `references` is a list of strings
- `cwes` is a list of strings
- each location has `path_from_root` and a complete source region
- each location `type`, when present, is `root_cause` or `crash_site`
- location paths do not target harness files

Manual review is still required. `vuln.yaml` is allowed to be incomplete when a
fact is genuinely unknown, but it must not invent unsupported details.

## Consumers

Known consumers include:

- benchmark validation and CI
- statistics export
- manual validation workflows
- SARIF hint generation

Consumers should treat this file as benchmark-owned ground truth, not mutable
runtime state.
