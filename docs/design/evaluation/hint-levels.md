# Design: Hint Levels
- Audience: maintainers working on hint staging and experiment input contracts
- Scope: SARIF hint-level semantics, aggregation rules, and runtime delivery contracts
- Related: [Trial Directory Preparation](./trial-directory-preparation.md), [Snapshots](./snapshots.md)

## Goals and Non-goals

### Goals
- define the meaning of supported hint levels
- define how hint artifacts are selected and staged for trials
- define non-leaky aggregation expectations for delivered hint artifacts

### Non-goals
- command-line walkthroughs for hint generation
- implementation snapshots of hint staging helpers
- future placeholder configuration that is not part of the current runtime contract

## Core Contract

Hint delivery is controlled by experiment-config inputs. When SARIF hints are
enabled, CRSBench selects the configured level and stages only the matching hint
artifacts for the active benchmark/trial context.

## SARIF Hint-Level Semantics

Hint levels progress from coarse vulnerability-class information toward more
detailed location/context information. Higher levels may include more precise
location metadata, but they must remain bounded by the benchmark's intended hint
contract.

Representative benchmark artifact layout:

```text
benchmarks/<project>/.aixcc/<harness>/cpv_N/hints/
├── level_1.sarif
├── level_2.sarif
├── level_3.sarif
├── level_4.sarif
└── level_5.sarif
```

Typical progression:
- level 1: general vulnerability class
- level 2: specific CWE-oriented vulnerability type
- level 3: level 2 plus function-level location
- level 4: level 2 plus file/line range location
- level 5: level 4 plus vulnerability name and description

Level 3 commonly relies on SARIF `logicalLocations` plus a file path, while
level 4 adds precise `physicalLocation.region` information.

## Staging Invariants

- only the configured SARIF level is delivered for a given staged hint artifact
- staged hint filenames and layout must not leak benchmark-internal CPV identity
  unless the benchmark contract explicitly allows it
- hint staging must aggregate across the applicable trial context without
  silently including disabled hint sources

## Failure Semantics

- missing configured hint artifacts are explicit staging failures or warnings,
  depending on caller policy
- malformed hint artifacts are input errors
- disabled hint inputs must result in no staged hint output

## Future Compatibility

Additional hint-source types may be introduced later, but this document covers
only the active staged-hint contract. Future extensions must preserve the same
config-driven, non-leaky delivery principles.

## Validation

This contract should be covered by:
- experiment-config schema tests for hint input selection
- hint staging tests for level selection and non-leaky naming
- trial preparation integration tests
- direct hint-generation tests for SARIF production across levels

## Example Output Shapes

Examples are illustrative only; exact messages are benchmark-defined.

Level 1:

```json
{
  "results": [{
    "ruleId": "CWE-122",
    "message": {
      "text": "Memory Safety Issue: Heap Based Buffer Overflow"
    }
  }]
}
```

Level 3:

```json
{
  "results": [{
    "ruleId": "CWE-122",
    "message": {
      "text": "CWE-122 - Heap-based buffer overflow in function(s): UTF32ToUTF8"
    },
    "locations": [{
      "physicalLocation": {
        "artifactLocation": {"uri": "xmlIO.c"}
      },
      "logicalLocations": [{
        "name": "UTF32ToUTF8",
        "kind": "function"
      }]
    }]
  }]
}
```

Level 4:

```json
{
  "results": [{
    "ruleId": "CWE-122",
    "message": {
      "text": "CWE-122 - Heap-based buffer overflow at: xmlIO.c:2168-2177"
    },
    "locations": [{
      "physicalLocation": {
        "artifactLocation": {"uri": "xmlIO.c"},
        "region": {
          "startLine": 2168,
          "endLine": 2177
        }
      }
    }]
  }]
}
```

## Implementation Pointers

- hint preparation logic under `crsbench/evaluation/`
- hint-related validation and trial-preparation tests under `tests/`
