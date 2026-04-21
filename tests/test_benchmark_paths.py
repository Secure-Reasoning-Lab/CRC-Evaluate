"""Tests for benchmark ground-truth path helpers."""

from pathlib import Path

import pytest
from crsbench.validation.ground_truth_paths import GroundTruthPaths


def test_ground_truth_paths_layout() -> None:
    gt = GroundTruthPaths(Path("/benchmarks/afc-curl-delta-01"))

    assert gt.aixcc_dir == Path("/benchmarks/afc-curl-delta-01/.aixcc")
    assert gt.meta_yaml == Path("/benchmarks/afc-curl-delta-01/.aixcc/meta.yaml")
    assert gt.ref_diff == Path("/benchmarks/afc-curl-delta-01/.aixcc/ref.diff")
    assert gt.harness_dir("curl_fuzzer") == Path(
        "/benchmarks/afc-curl-delta-01/.aixcc/curl_fuzzer"
    )
    assert gt.cpv_dir("curl_fuzzer", "cpv_0") == Path(
        "/benchmarks/afc-curl-delta-01/.aixcc/curl_fuzzer/cpv_0"
    )
    assert gt.cpv_blobs_dir("curl_fuzzer", "cpv_0") == Path(
        "/benchmarks/afc-curl-delta-01/.aixcc/curl_fuzzer/cpv_0/blobs"
    )
    assert gt.cpv_hints_dir("curl_fuzzer", "cpv_0") == Path(
        "/benchmarks/afc-curl-delta-01/.aixcc/curl_fuzzer/cpv_0/hints"
    )
    assert gt.harness_corpus_dir("curl_fuzzer") == Path(
        "/benchmarks/afc-curl-delta-01/.aixcc/curl_fuzzer/corpus"
    )


def test_ground_truth_paths_keeps_legacy_module_identity() -> None:
    assert GroundTruthPaths.__module__ == "crsbench.benchmark.paths"


@pytest.mark.parametrize(
    "invalid", ["", "   ", "../escape", "nested/name", "/abs/path"]
)
def test_ground_truth_paths_rejects_invalid_harness_or_cpv_segments(
    invalid: str,
) -> None:
    gt = GroundTruthPaths(Path("/benchmarks/afc-curl-delta-01"))

    with pytest.raises((TypeError, ValueError)):
        gt.harness_dir(invalid)
    with pytest.raises((TypeError, ValueError)):
        gt.cpv_dir("curl_fuzzer", invalid)
