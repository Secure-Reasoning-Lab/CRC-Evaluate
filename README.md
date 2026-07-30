# CRC-Evaluate

`CRC-Evaluate` is the evaluation harness for the Cyber Reasoning Competition at CSAW. It is based on [CRSBench](https://github.com/sslab-gatech/CRSBench) and evaluates OSS-CRS-compatible Finder and Patcher submissions created from [CRC-Template](https://github.com/Secure-Reasoning-Lab/CRC-Template).

During qualification, teams can use this repository to validate, register, and test their CRS. Organizers will use the same evaluation harness to reproduce selected results.

## Related Repositories

| Repository | Purpose |
| --- | --- |
| [CRC-CSAW](https://github.com/Secure-Reasoning-Lab/CRC-CSAW) | Competition rules, registration, schedule, and submission policy |
| [CRC-Template](https://github.com/Secure-Reasoning-Lab/CRC-Template) | Starter repository for building and locally testing a Finder/Patcher CRS |
| [CRC-Evaluate](https://github.com/Secure-Reasoning-Lab/CRC-Evaluate) | Submission validation, CRS registration, benchmark execution, verification, and reporting |

The `oss-crs/` submodule is pinned to revision `8eefe84d67b2339de920cef5794933d6befeefdb`, published on the [`CRC@CSAW` branch](https://github.com/Secure-Reasoning-Lab/oss-crs/tree/CRC@CSAW) of the Secure Reasoning Lab [OSS-CRS fork](https://github.com/Secure-Reasoning-Lab/oss-crs). The fork is based on upstream revision `39712ee41f9b198e13f756ba55c56c1bde163e85`.

## Requirements

- Linux
- Git with submodule support
- [uv](https://docs.astral.sh/uv/)
- Docker Engine with Docker Compose v2
- 16 logical CPUs and 64 GB of memory for the public sanity profile
- An OpenAI-compatible upstream endpoint and API key for the models used by the submitted CRS

All commands below must be run from the CRC-Evaluate repository root because relative paths in experiment configurations are resolved from the current working directory.

## 1. Clone and Prepare CRC-Evaluate

```bash
git clone --recurse-submodules https://github.com/Secure-Reasoning-Lab/CRC-Evaluate.git
cd CRC-Evaluate
uv sync
uv run crsbench prepare
```

`crsbench prepare` initializes the managed OSS-Fuzz checkout and prepares the Docker images required by the sanity Finder and Patcher runs.

## 2. Configure the LLM Endpoint

Create the local environment file:

```bash
cp .env.example .env
```

Set the upstream endpoint and credential in `.env`:

```dotenv
LITELLM_UPSTREAM_BASE_URL=https://api.example.com/v1
LITELLM_UPSTREAM_API_KEY=...
```

The tracked [`.run/sanity/litellm-config.yaml`](.run/sanity/litellm-config.yaml) starts an internal LiteLLM proxy for each trial. It maps the Claude model aliases used by the bundled CRC-Template implementations to GPT models and records the token prices used for cost accounting. Update its model routes, aliases, and prices when the upstream endpoint exposes different models. Every alias declared in a submitted CRS's `required_llms` must appear in this file.

Do not commit `.env`. The sanity commands below use local execution and do not require Redis or Valkey.

## 3. Place the Team Submission

Clone the team's customized CRC-Template repository into the fixed sanity submission directory:

```bash
TEAM_SUBMISSION_GIT_URL=https://github.com/your-team/your-crc-template.git
git clone --recurse-submodules "$TEAM_SUBMISSION_GIT_URL" .run/sanity/team-01/submission
```

The submission root must contain `submission.yaml`. Its Finder and Patcher paths are POSIX-style paths relative to the submission root:

```yaml
schema_version: 1

submission:
  name: my-crs

crs:
  finder:
    path: crs/my-finder
  patcher:
    path: crs/my-patcher
```

The selected directories must be different and must each contain `oss-crs/crs.yaml`. The Finder must declare `type: [bug-finding]`, the Patcher must declare `type: [bug-fixing]`, and each component must list its model aliases in `required_llms`.

## 4. Validate and Register the Submission

Validate the manifest and selected CRS directories:

```bash
uv run crsbench submission validate .run/sanity/team-01/submission
```

Register the selected Finder and Patcher under the namespace expected by the public sanity configurations:

```bash
uv run crsbench submission register .run/sanity/team-01/submission \
  --team-id team-01 \
  --registry-dir .run/sanity/registry
```

Registration creates `.run/sanity/registry/team-01-finder.yaml` and `.run/sanity/registry/team-01-patcher.yaml`. The generated registry IDs are `team-01-finder` and `team-01-patcher`.

Registry entries contain absolute paths to the selected CRS directories. Do not move the submission checkout after registration. If the selected paths change, regenerate both entries intentionally:

```bash
uv run crsbench submission register .run/sanity/team-01/submission \
  --team-id team-01 \
  --registry-dir .run/sanity/registry \
  --force
```

## 5. Run the Finder and Patcher

After registration, run both stages with the tracked sanity launcher:

```bash
./.run/sanity/team-01/run-sanity.sh
```

The launcher runs the Finder first and starts the Patcher only after the Finder completes successfully. Both stages use local execution. The Patcher consumes the verified Finder outputs and verifies the submitted patch.

To validate Finder trial generation without starting the CRS:

```bash
uv run crsbench run \
  --local-only \
  --dry-run \
  --experiment-config .run/sanity/team-01/finder-runtime.yaml
```

To run either stage separately:

```bash
# Finder
uv run crsbench run \
  --local-only \
  --experiment-config .run/sanity/team-01/finder-runtime.yaml

# Patcher; run after the Finder produces a verified PoV
uv run crsbench run \
  --local-only \
  --experiment-config .run/sanity/team-01/patcher-runtime.yaml
```

The Finder runs against `sanity-mock-c-delta-01`, targeting `fuzz_parse_buffer_section` and `cpv_1`. Verified outputs are written below `.run/sanity/team-01/results/experiment-data/team-01-finder-sanity/team-01-finder`, and reports are written below `.run/sanity/team-01/results/report-data`.

## Public Sanity Profile

The public sanity configurations use the qualification compute profile for one benchmark and one trial.

| Limit | Finder | Patcher |
| --- | ---: | ---: |
| Trial CPU | 16 cores | 16 cores |
| Trial memory | 64G | 64G |
| LLM cost budget | $50 | $50 |
| Build timeout | 21600 seconds | 21600 seconds |
| CRS run timeout | 28800 seconds | 7200 seconds |
| Verification timeout | 14400 seconds | 10800 seconds |
| Maximum total time | 76800 seconds | 43200 seconds |
| Per-PoV verification | 300 seconds | 300 seconds |

These limits apply per benchmark and trial. The sanity benchmark is a compatibility test for the submission, registry, build, runtime, artifact, and verification interfaces; it is not a scoring benchmark or a score predictor.

## Additional Documentation

### CRC-Evaluate Participant Documentation

- [Submission validation and registration](docs/getting-started/evaluating-submissions.md)

### CRSBench Framework Reference

- [Experiment configuration reference](docs/reference/experiment-config.md)
- [Advanced deployment](docs/getting-started/deployment.md)
- [Framework documentation index](docs/README.md)

## License and Attribution

CRC-Evaluate is based on CRSBench and is distributed under the [MIT License](LICENSE). OSS-CRS, OSS-Fuzz, benchmark sources, and other bundled dependencies retain their original licenses and attribution; see [LICENSE-THIRD-PARTY.md](LICENSE-THIRD-PARTY.md).
