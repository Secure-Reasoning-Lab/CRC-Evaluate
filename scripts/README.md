# CRSBench Utility Scripts

This directory keeps operational and low-overhead utility scripts that are
relevant to CRSBench workflows.

## Core Scripts

- `valkey-helper.py` - manage Valkey for distributed runs (`clean <experiment>` is experiment-scoped and queue-model-aware)
- `litellm-helper.py` - manage LiteLLM service
- `test_litellm.py` - sanity checks for LiteLLM integration
- `cpv_report.py` - CPV report generation from experiment data
- `merge_experiment_results.py` - merge distributed result shards
- `orchestrate-workers.sh` - remote worker orchestration
- `setup-remote-worker.sh` - remote worker bootstrap helper
- `setup-third-party.sh` - fetch managed `third_party/oss-fuzz` and the pinned Atlantis `third_party/atlantis-multilang-given_fuzzer` checkout; reruns normalize the managed `oss-fuzz` checkout back to the configured repo and pinned commit, clean helper-source drift under `infra/`, preserve `build/*` artifacts, and then reapply CRSBench helper patches
- `sync-upstream-models.py` - sync upstream LiteLLM model catalog
- `check_patch_overlap.py` - patch overlap/debug utility
- `cleanup-failed-trials.sh` - cleanup helper for failed trial artifacts
- `cpv_assignment.py` - CPV assignment/debug utility
- `snapshot-utils.py` - snapshot inspect/validate/extract helper

## See Also

- [Distributed Experiments](../docs/guides/experiments/distributed.md)
- [Configuration](../docs/getting-started/configuration.md)
- [Valkey Service](../services/valkey/README.md)
