# Dataset Module

Upload and download CRSBench benchmarks from HuggingFace.

## Overview

The dataset module manages distribution of benchmark data through a
registry-based backend system. Currently supports HuggingFace with
extensible support for S3 and Azure.

- **Single HF repo**: `sslab-gatech/crsbench-dataset` (134 benchmarks, ~12GB)
- **Gated access**: Data Use Agreement with canary strings for LLM contamination detection
- **Reliable uploads**: Uses `upload_large_folder` for the HuggingFace backend
- **Card auto-upload**: HF card files are uploaded from canonical repo-root files
  (`README_HF.md`, `LICENSE`, `LICENSE-THIRD-PARTY.md`)
- **Incremental sync**: Shared remote/local manifests skip unchanged benchmarks
- **Complete download gating**: benchmark downloads may reuse partial staging
  across retries, but extraction only proceeds once every requested benchmark
  bundle is present locally
- **Opt-in prune**: `crsbench benchmark upload --prune` deletes remote
  benchmark folders and manifest entries that are absent from the local
  benchmarks directory. Incompatible with `--benchmarks` because a subset
  upload does not represent the intended full state.

## File Structure

| File | Purpose |
|------|---------|
| `registry.py` | `DatasetConfig` dataclass and `DATASET_REGISTRY` |
| `backends.py` | Backend dispatch (HuggingFace, S3, Azure) for download/upload/delete |
| `download.py` | `download_dataset`, `download_all`, `download_suite` |
| `upload.py` | `upload_dataset` with dry-run, card upload, and `--prune` support |
| `manifest.py` | Shared fingerprint/index helpers for incremental upload/download |
| `cli.py` | `crsbench download` CLI subparser (top-level command) |

Upload CLI is wired through the benchmark packaging command layer in
`crsbench/benchmark/packaging/cli/benchmark_command.py`.

## Operational Boundaries

- user-facing dataset download workflows are documented in `docs/guides/`
- maintainer-facing upload workflows are documented in contributor/runtime docs
- this page only defines the module surface and backend layout

## Dependency

`huggingface_hub` is a core dependency, installed automatically with `crsbench`.

## Design Doc

See `../design/dataset/dataset.md` for architecture details.
