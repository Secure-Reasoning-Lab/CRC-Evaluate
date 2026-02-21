"""Dataset registry for CRSBench benchmarks.

Each dataset entry specifies its backend and location. Currently
supports HuggingFace; S3 and Azure backends can be added in backends.py.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class DatasetConfig:
    """Configuration for a single dataset."""

    # Storage backend: "huggingface" (S3/Azure planned)
    backend: str
    # Backend-specific location (e.g., HF repo ID)
    location: str
    # Benchmark directory prefixes that belong to this dataset
    prefixes: list[str] = field(default_factory=list)
    # HF repo type (only for huggingface backend)
    repo_type: str = "dataset"


# Central registry: dataset short name -> config
DATASET_REGISTRY: dict[str, DatasetConfig] = {
    "crsbench": DatasetConfig(
        backend="huggingface",
        location="sslab-gatech/crsbench-dataset",
        prefixes=["atlanta-", "sanity-", "afc-", "asc-"],
        repo_type="dataset",
    ),
}


def get_dataset_names() -> list[str]:
    """Return sorted list of available dataset names."""
    return sorted(DATASET_REGISTRY.keys())


def get_dataset(name: str) -> DatasetConfig:
    """Look up a dataset by name.

    Raises:
        ValueError: If dataset name is unknown
    """
    if name not in DATASET_REGISTRY:
        available = ", ".join(get_dataset_names())
        raise ValueError(f"Unknown dataset: {name!r}. Available: {available}")
    return DATASET_REGISTRY[name]


def resolve_prefix(benchmark_name: str) -> Optional[str]:
    """Resolve a benchmark name to its dataset name via prefix matching.

    Returns:
        Dataset name, or None if no prefix matches
    """
    for name, config in DATASET_REGISTRY.items():
        for prefix in config.prefixes:
            if benchmark_name.startswith(prefix):
                return name
    return None
