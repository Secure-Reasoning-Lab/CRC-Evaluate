# Design Document: Dataset Module

**Status**: Implemented
**Created**: 2026-02-14
**Related Issues**: #5 (protect ground truth), #61 (HuggingFace distribution)
**Related Design Doc**: `docs/design/benchmark-protection-and-contamination.md`

## 1. Problem

CRSBench benchmarks (134 directories, ~12GB) are too large for GitHub
(pack exceeds 2GB limit). Ground truth data must be protected from LLM
training scraping. We need a distribution mechanism that provides:

- Gated access with Data Use Agreement (DUA)
- Canary strings for contamination detection
- Programmatic download integrated with existing CLI
- Extensible storage backend (HuggingFace now, S3/Azure later)

## 2. Architecture

```
crsbench/dataset/
├── __init__.py
├── registry.py      # DatasetConfig + DATASET_REGISTRY
├── backends.py      # Backend dispatch (HuggingFace, S3, Azure)
├── bundle.py        # Bundle/unbundle benchmarks into tarballs
├── download.py      # download_dataset, download_all, download_suite
├── upload.py        # upload_dataset with dry-run + card file upload
└── cli.py           # CLI: crsbench download (top-level command)
```

### 2.1 Bundle Format

Each benchmark is stored as two tarballs on HuggingFace:

- **`benchmark.tar.gz`** — Everything except `.aixcc/` (Dockerfile, build scripts,
  `pkgs/` source tarballs, harnesses, etc.)
- **`ground-truth.tar.gz`** — `.aixcc/` directory (vulnerability metadata, patches,
  POVs)

This separation allows downloading benchmarks without ground truth answers
(`--no-ground-truth`) for blind CRS evaluation.

### 2.2 Data Flow

```
Upload (maintainer, via `crsbench benchmark upload`):
  benchmarks/ ──► bundle.py ──► upload.py ──► backends.py ──► HuggingFace API
                     │               │              │
                     │               │              └── upload_large_folder
                     │               ├── registry.py (resolve dataset → backend)
                     ▼               ├── _upload_card_files() → README_HF.md + licenses to repo root
                                     └── write index/benchmarks.jsonl (manifest)
                 staging/
                 ├── bench-1/
                 │   ├── benchmark.tar.gz
                 │   └── ground-truth.tar.gz
                 └── bench-2/
                     ├── benchmark.tar.gz
                     └── ground-truth.tar.gz

Download (user):
  CLI args ──► cli.py ──► download.py ──► backends.py ──► HuggingFace API
                              │                                 │
                              ├── registry.py (resolve)         │
                              ├── read index/benchmarks.jsonl   │
                              ├── compare with .crsbench-manifest.json
                              ├── _load_suite() (optional)      ▼
                              │                           staging/ (tarballs)
                              └── unbundle_all()                │
                                                                ▼
                                                          benchmarks/
```

### 2.3 Registry Design

Each dataset is a `DatasetConfig` dataclass:

```python
@dataclass(frozen=True)
class DatasetConfig:
    backend: str           # "huggingface", "s3", "azure"
    location: str          # Backend-specific (repo ID, S3 URI, etc.)
    prefixes: list[str]    # Benchmark name prefixes
    repo_type: str | None  # HF-specific
```

Central registry maps short names to configs:

```python
DATASET_REGISTRY = {
    "crsbench": DatasetConfig(
        backend="huggingface",
        location="sslab-gatech/crsbench-dataset",
        prefixes=["atlanta-", "sanity-", "afc-", "asc-"],
        repo_type="dataset",
    ),
}
```

Prefix-to-dataset resolution is used by `download_suite()` to route
benchmarks from a suite file to the correct backend automatically.

### 2.4 Backend Dispatch

Backends are plain functions registered in dispatch tables:

```python
DOWNLOAD_BACKENDS: dict[str, DownloadFn] = {
    "huggingface": _download_huggingface,
    "s3": _download_s3,            # NotImplementedError
    "azure": _download_azure,      # NotImplementedError
}
```

Adding a backend requires:
1. Implement `_download_<name>()` and `_upload_<name>()` in `backends.py`
2. Register in `DOWNLOAD_BACKENDS` / `UPLOAD_BACKENDS`
3. Add dataset entry in `DATASET_REGISTRY` with `backend="<name>"`

No changes needed to CLI, download.py, or upload.py.

### 2.6 Incremental Manifest

- Remote index: `index/benchmarks.jsonl` in the HF dataset repo
  (mirrored to root as `benchmarks-metadata.jsonl` for easier discovery).
- Local install state: `benchmarks/.crsbench-manifest.json`.
- Both upload and download use the same source fingerprints:
  - `benchmark_source_sha256` (project files excluding `.aixcc/`)
  - `ground_truth_source_sha256` (`.aixcc/` tree)

Behavior:

- `crsbench benchmark upload` skips unchanged benchmarks and uploads only changed bundles.
- `crsbench download` skips unchanged local benchmarks and downloads only changed/missing bundles.

### 2.5 Benchmark Suite Integration

Download supports the same benchmark suite YAML files used by
`crsbench run` (configured via `benchmark_suite` in the experiment config YAML):

```bash
crsbench download --benchmark-suite afc-all
```

This loads `benchmark-suites/afc-all.yaml`, groups benchmarks by prefix
to resolve the correct dataset, and downloads only those benchmarks.

```
benchmark-suites/afc-all.yaml
    │
    ▼ _load_suite()
[afc-curl-delta-01, afc-libxml2-delta-01, ...]
    │
    ▼ _group_by_dataset()
{"crsbench": [afc-curl-delta-01, ...]}
    │
    ▼ download_dataset("crsbench", benchmarks=[...])
    │
    ▼ backends.download() → snapshot_download(allow_patterns=[...])
```

## 3. CLI Interface

### Download

Download is a user-facing top-level command:

```bash
# Download everything
crsbench download --all

# Download without ground truth (blind CRS evaluation)
crsbench download --all --no-ground-truth

# Download specific dataset
crsbench download --dataset crsbench

# Download specific benchmarks from a dataset
crsbench download --dataset crsbench --benchmarks afc-curl-delta-01

# Download specific suite
crsbench download --benchmark-suite sanity

# Custom output directory
crsbench download --all --output-dir /data/benchmarks
```

`--dataset`, `--benchmark-suite`, and `--all` are mutually exclusive.

`--no-ground-truth` skips downloading `ground-truth.tar.gz` (the `.aixcc/`
directory with vulnerability metadata and patches). Useful for blind CRS
evaluation where the system should discover vulnerabilities without answers.

### Upload

Upload is a maintainer-facing command under `crsbench benchmark`:

```bash
# Upload dataset (benchmarks + card files)
crsbench benchmark upload --dataset crsbench

# Dry run (list what would be uploaded)
crsbench benchmark upload --dataset crsbench --dry-run

# Custom benchmarks directory
crsbench benchmark upload --dataset crsbench --benchmarks-dir ./benchmarks
```

Card files from canonical repo-root sources are automatically uploaded to the
HuggingFace repo root alongside benchmarks:

- `README_HF.md` -> `README.md`
- `LICENSE` -> `LICENSE`
- `LICENSE-THIRD-PARTY.md` -> `LICENSE-THIRD-PARTY.md`

## 4. HuggingFace Repo

| Repo | Prefixes | Gating |
|------|----------|--------|
| `sslab-gatech/crsbench-dataset` | `atlanta-*`, `sanity-*`, `afc-*`, `asc-*` | Auto-approve with DUA |

Uploads use `upload_large_folder` for reliable handling of the ~12GB dataset.

### Dataset Card

Card files are auto-uploaded to the HF repo root when running
`crsbench benchmark upload`:

- `README_HF.md` -- HuggingFace dataset card with YAML frontmatter
  (license, tags, description, citation BibTeX, DUA terms)
- `LICENSE` -- MIT license
- `LICENSE-THIRD-PARTY.md` -- upstream project licenses

Canary IDs are managed in `canary-registry.json` at repo root and referenced
from the dataset card to deter LLM training use.

## 5. Dependency

`huggingface_hub` is a core dependency in CRSBench and is installed by
`uv sync`.

## 6. Third-Party Licensing

`LICENSE-THIRD-PARTY.md` at repo root lists all bundled upstream projects
with license type and source URL. This covers only projects whose source
code is bundled under `benchmarks/*/pkgs/`, not Python pip dependencies.

The bundled source code has been modified from original upstream versions
to inject test vulnerabilities. Original license files are preserved.

## 7. .gitignore

Downloaded benchmarks are excluded from git:

```gitignore
benchmarks/afc-*
benchmarks/atlanta-*
benchmarks/asc-*
benchmarks/cp-*
```

`sanity-*` benchmarks remain in git as small CI test fixtures.

## 8. Future Work

- Implement S3 and Azure Blob Storage backends
- Auto-detect dataset from benchmark name in `crsbench run`
  (download on demand if benchmark not found locally)
