from crsbench.evaluation.replay.mapping import (
    MappingResolution,
    load_benchmark_project_mapping,
    resolve_mapped_project,
)


def test_load_benchmark_project_mapping_reads_packaged_resource() -> None:
    mapping = load_benchmark_project_mapping()

    assert mapping["afc-curl-delta-01"] == "curl"
    assert mapping["afc-shadowsocks-full-01"] is None


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
