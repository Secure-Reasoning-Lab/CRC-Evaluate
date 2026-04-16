import pytest
from crsbench.builder.rts import dockerfile_swap as dockerfile_swap_mod
from crsbench.builder.rts.dockerfile_swap import swap_dockerfile_from


@pytest.fixture(autouse=True)
def _stub_ensure_rts_image(monkeypatch):
    monkeypatch.setattr(
        dockerfile_swap_mod,
        "_ensure_rts_image",
        lambda language: dockerfile_swap_mod.RTS_IMAGE_TAGS[language],
    )


def test_swap_jvm_from():
    original = (
        "FROM gcr.io/oss-fuzz/base-builder-jvm\n"
        "RUN apt-get update\n"
        "COPY build.sh $SRC\n"
    )
    result = swap_dockerfile_from(original, language="jvm")
    assert result.startswith("FROM crsbench-rts-jvm:latest\n")
    assert "RUN apt-get update" in result
    assert "COPY build.sh" in result


def test_swap_c_from():
    original = "FROM gcr.io/oss-fuzz/base-builder\nRUN apt-get update\n"
    result = swap_dockerfile_from(original, language="c")
    assert result.startswith("FROM crsbench-rts-c:latest\n")


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
    assert lines[0].startswith("FROM crsbench-rts-jvm")
    assert lines[1] == "ARG DEBIAN_FRONTEND=noninteractive"
    assert len(lines) == 5


def test_swap_unsupported_language_raises(monkeypatch):
    def _raise(language):
        raise ValueError(f"Unsupported RTS language: {language}")

    monkeypatch.setattr(dockerfile_swap_mod, "_ensure_rts_image", _raise)
    with pytest.raises(ValueError, match="Unsupported RTS language"):
        swap_dockerfile_from("FROM ubuntu\n", language="rust")


def test_swap_no_from_raises():
    with pytest.raises(ValueError, match="No FROM instruction"):
        swap_dockerfile_from("RUN echo hello\n", language="jvm")


def test_swap_nonstandard_base_still_works():
    """Any FROM gets replaced, not just known OSS-Fuzz bases."""
    original = "FROM ubuntu:22.04\nRUN echo hello\n"
    result = swap_dockerfile_from(original, language="jvm")
    assert result.startswith("FROM crsbench-rts-jvm:latest\n")


def test_ensure_all_rts_images_builds_each_unique_tag_once(monkeypatch):
    calls: list[str] = []

    def _fake_ensure(language: str, *, force_rebuild: bool = False) -> str:
        assert force_rebuild is False
        calls.append(language)
        return dockerfile_swap_mod.RTS_IMAGE_TAGS[language]

    monkeypatch.setattr(dockerfile_swap_mod, "_ensure_rts_image", _fake_ensure)

    result = dockerfile_swap_mod.ensure_all_rts_images()

    assert result == {
        "jvm": "crsbench-rts-jvm:latest",
        "c": "crsbench-rts-c:latest",
    }
    assert calls == ["jvm", "c"]


def test_ensure_all_rts_images_can_force_rebuild_alias_languages(monkeypatch):
    calls: list[tuple[str, bool]] = []

    def _fake_ensure(language: str, *, force_rebuild: bool = False) -> str:
        calls.append((language, force_rebuild))
        return dockerfile_swap_mod.RTS_IMAGE_TAGS[language]

    monkeypatch.setattr(dockerfile_swap_mod, "_ensure_rts_image", _fake_ensure)

    result = dockerfile_swap_mod.ensure_all_rts_images(force_rebuild=True)

    assert result == {
        "jvm": "crsbench-rts-jvm:latest",
        "c": "crsbench-rts-c:latest",
    }
    assert calls == [("jvm", True), ("c", True)]
