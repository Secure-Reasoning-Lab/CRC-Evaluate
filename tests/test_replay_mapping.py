import tomllib
from pathlib import Path
from typing import Literal, get_type_hints

from crsbench.evaluation.replay.mapping import (
    MappingResolution,
    load_benchmark_project_mapping,
    resolve_mapped_project,
)


def test_load_benchmark_project_mapping_reads_packaged_resource() -> None:
    mapping = load_benchmark_project_mapping()

    assert mapping["afc-curl-delta-01"] == "curl"
    assert mapping["afc-shadowsocks-full-01"] is None


def test_mapping_resolution_reason_uses_literal_contract() -> None:
    assert (
        get_type_hints(MappingResolution, include_extras=True)["reason"]
        == Literal["mapped", "unsupported_mapping", "missing_mapping"]
    )


def test_replay_package_data_declared_in_pyproject() -> None:
    pyproject_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))

    package_data = pyproject["tool"]["setuptools"]["package-data"]
    assert package_data["crsbench.evaluation.replay"] == ["*.json"]


def test_resolve_mapped_project_reports_mapping_outcomes() -> None:
    mapping = {
        "afc-curl-delta-01": "curl",
        "afc-shadowsocks-full-01": None,
    }

    assert resolve_mapped_project("afc-curl-delta-01", mapping) == MappingResolution(
        benchmark="afc-curl-delta-01",
        mapped_project="curl",
        reason="mapped",
    )
    assert resolve_mapped_project(
        "afc-shadowsocks-full-01", mapping
    ) == MappingResolution(
        benchmark="afc-shadowsocks-full-01",
        mapped_project=None,
        reason="unsupported_mapping",
    )
    assert resolve_mapped_project("unknown-benchmark", mapping) == MappingResolution(
        benchmark="unknown-benchmark",
        mapped_project=None,
        reason="missing_mapping",
    )
