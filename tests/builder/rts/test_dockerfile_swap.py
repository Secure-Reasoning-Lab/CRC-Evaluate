import pytest
from crsbench.builder.rts.dockerfile_swap import swap_dockerfile_from


def test_swap_jvm_from():
    original = (
        "FROM gcr.io/oss-fuzz/base-builder-jvm\n"
        "RUN apt-get update\n"
        "COPY build.sh $SRC\n"
    )
    result = swap_dockerfile_from(original, language="jvm")
    assert result.startswith("FROM ghcr.io/team-atlanta/crsbench-rts-jvm:latest\n")
    assert "RUN apt-get update" in result
    assert "COPY build.sh" in result


def test_swap_c_from():
    original = "FROM gcr.io/oss-fuzz/base-builder\nRUN apt-get update\n"
    result = swap_dockerfile_from(original, language="c")
    assert result.startswith("FROM ghcr.io/team-atlanta/crsbench-rts-c:latest\n")


def test_swap_cpp_uses_c_image():
    original = "FROM gcr.io/oss-fuzz/base-builder\nRUN echo hello\n"
    result = swap_dockerfile_from(original, language="c++")
    assert "crsbench-rts-c:latest" in result


def test_swap_preserves_rest_of_dockerfile():
    original = (
        "FROM gcr.io/oss-fuzz/base-builder-jvm\n"
        "ARG DEBIAN_FRONTEND=noninteractive\n"
        "RUN apt-get update && apt-get install -y git\n"
        "COPY build.sh $SRC/build.sh\n"
        "WORKDIR $SRC\n"
    )
    result = swap_dockerfile_from(original, language="jvm")
    lines = result.splitlines()
    assert lines[0].startswith("FROM ghcr.io/team-atlanta/crsbench-rts-jvm")
    assert lines[1] == "ARG DEBIAN_FRONTEND=noninteractive"
    assert len(lines) == 5


def test_swap_unsupported_language_raises():
    with pytest.raises(ValueError, match="Unsupported RTS language"):
        swap_dockerfile_from("FROM ubuntu\n", language="rust")


def test_swap_no_from_raises():
    with pytest.raises(ValueError, match="No FROM instruction"):
        swap_dockerfile_from("RUN echo hello\n", language="jvm")


def test_swap_nonstandard_base_still_works():
    """Any FROM gets replaced, not just known OSS-Fuzz bases."""
    original = "FROM ubuntu:22.04\nRUN echo hello\n"
    result = swap_dockerfile_from(original, language="jvm")
    assert result.startswith("FROM ghcr.io/team-atlanta/crsbench-rts-jvm:latest\n")
