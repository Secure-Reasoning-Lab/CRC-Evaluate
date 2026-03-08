from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from crsbench.builder.infrastructure import OSSFuzzInfrastructure


def _make_mock_oss_fuzz(tmp_path: Path) -> Path:
    oss_fuzz = tmp_path / "oss-fuzz"
    (oss_fuzz / "infra").mkdir(parents=True)
    (oss_fuzz / "projects").mkdir(parents=True)
    (oss_fuzz / "build" / "out").mkdir(parents=True)
    (oss_fuzz / "build" / "work").mkdir(parents=True)
    (oss_fuzz / "infra" / "helper.py").write_text("# mock helper")
    return oss_fuzz


def test_run_tests_syncs_patched_src_into_resolved_workdir(tmp_path: Path):
    """run_tests should sync patched source into runtime WORKDIR, not hardcoded /src."""
    oss_fuzz = _make_mock_oss_fuzz(tmp_path)
    infra = OSSFuzzInfrastructure(oss_fuzz)

    project = oss_fuzz / "projects" / "proj"
    project.mkdir(parents=True)
    (project / "test.sh").write_text("#!/bin/bash\necho ok\n")
    src_path = tmp_path / "patched-src"
    src_path.mkdir(parents=True)
    (src_path / "file.txt").write_text("x")

    with (
        patch("crsbench.builder.infrastructure.run_with_timeout") as mock_run,
        patch("crsbench.builder.infrastructure.fix_docker_ownership"),
    ):
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="/src/wireshark\n", stderr=""),
            MagicMock(returncode=0, stdout="ok", stderr=""),
        ]
        passed, _, _ = infra.run_tests("proj", src_path, docker_tag="latest")

    assert passed is True
    inspect_cmd = mock_run.call_args_list[0][0][0]
    cmd = mock_run.call_args_list[1][0][0]
    cmd_str = " ".join(cmd)

    assert inspect_cmd[:4] == ["docker", "image", "inspect", "--format"]
    assert "--user" in cmd
    assert "0" in cmd
    assert f"{src_path.resolve()}:/CRSBENCH_PATCHED_SRC:ro" in cmd_str
    assert f"{project.resolve()}:/CRSBENCH_PROJ_PATH:ro" in cmd_str
    assert f"{src_path.resolve()}:/src" not in cmd_str
    assert "CRSBENCH_PATCHED_SRC=/CRSBENCH_PATCHED_SRC" in cmd_str
    assert "CRSBENCH_PROJ_PATH=/CRSBENCH_PROJ_PATH" in cmd_str
    assert "CRSBENCH_EFFECTIVE_WORKDIR=/src/wireshark" in cmd_str
    assert "project_path=/CRSBENCH_PROJ_PATH;" in cmd_str
    assert "patched_src=/CRSBENCH_PATCHED_SRC;" in cmd_str
    assert "workdir=/src/wireshark;" in cmd_str
    assert (
        'if [ -L "$workdir" ]; then echo "Unsafe symlink workdir: $workdir"; exit 2; fi;'
        in cmd_str
    )
    assert 'resolved_workdir="$workdir";' in cmd_str
    assert 'echo "realpath unavailable; refusing unsafe sync"; exit 2; ' in cmd_str
    assert 'case "$resolved_workdir" in /src|/src/*) ;; ' in cmd_str
    assert 'rsync -a --delete "$patched_src/" "$workdir/";' in cmd_str
    assert 'cd "$workdir" && /bin/bash "$project_path/test.sh";' in cmd_str


def test_resolve_test_workdir_falls_back_to_dockerfile(tmp_path: Path):
    """If image inspect fails, WORKDIR should be parsed from benchmark Dockerfile."""
    oss_fuzz = _make_mock_oss_fuzz(tmp_path)
    infra = OSSFuzzInfrastructure(oss_fuzz)

    project = oss_fuzz / "projects" / "proj"
    project.mkdir(parents=True)
    (project / "Dockerfile").write_text("FROM base-builder\nWORKDIR $SRC/wireshark\n")

    with patch("crsbench.builder.infrastructure.run_with_timeout") as mock_run:
        mock_run.side_effect = RuntimeError("inspect failed")
        workdir = infra._resolve_test_workdir("gcr.io/oss-fuzz/proj:latest", project)

    assert workdir == "/src/wireshark"


def test_resolve_test_workdir_falls_back_on_nonzero_inspect(tmp_path: Path):
    """Non-zero inspect return code should also trigger Dockerfile fallback."""
    oss_fuzz = _make_mock_oss_fuzz(tmp_path)
    infra = OSSFuzzInfrastructure(oss_fuzz)

    project = oss_fuzz / "projects" / "proj"
    project.mkdir(parents=True)
    (project / "Dockerfile").write_text("FROM base-builder\nWORKDIR /src/libxml2\n")

    with patch("crsbench.builder.infrastructure.run_with_timeout") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="no image")
        workdir = infra._resolve_test_workdir("gcr.io/oss-fuzz/proj:latest", project)

    assert workdir == "/src/libxml2"


def test_resolve_test_workdir_uses_cumulative_dockerfile_workdir(tmp_path: Path):
    """Fallback parser should follow Docker's cumulative relative WORKDIR behavior."""
    oss_fuzz = _make_mock_oss_fuzz(tmp_path)
    infra = OSSFuzzInfrastructure(oss_fuzz)

    project = oss_fuzz / "projects" / "proj"
    project.mkdir(parents=True)
    (project / "Dockerfile").write_text(
        "FROM base-builder\nWORKDIR /src\nWORKDIR wireshark\nWORKDIR fuzz\n"
    )

    with patch("crsbench.builder.infrastructure.run_with_timeout") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="no image")
        workdir = infra._resolve_test_workdir("gcr.io/oss-fuzz/proj:latest", project)

    assert workdir == "/src/wireshark/fuzz"


def test_run_tests_uses_run_tests_sh_when_test_sh_missing(tmp_path: Path):
    """When only run_tests.sh exists, run_tests.sh branch should be used."""
    oss_fuzz = _make_mock_oss_fuzz(tmp_path)
    infra = OSSFuzzInfrastructure(oss_fuzz)

    project = oss_fuzz / "projects" / "proj"
    project.mkdir(parents=True)
    (project / "run_tests.sh").write_text("#!/bin/bash\necho run-tests\n")
    src_path = tmp_path / "patched-src"
    src_path.mkdir(parents=True)

    with (
        patch("crsbench.builder.infrastructure.run_with_timeout") as mock_run,
        patch("crsbench.builder.infrastructure.fix_docker_ownership"),
    ):
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="/src/wireshark\n", stderr=""),
            MagicMock(returncode=0, stdout="ok", stderr=""),
        ]
        infra.run_tests("proj", src_path, docker_tag="latest")

    cmd = mock_run.call_args_list[1][0][0]
    cmd_str = " ".join(cmd)
    assert 'elif [ -f "$project_path/run_tests.sh" ]; then ' in cmd_str
    assert f"{(project / 'run_tests.sh').resolve()}:/src/run_tests.sh:ro" in cmd_str


def test_run_tests_exports_rts_mode(tmp_path: Path):
    """RTS mode should prepend export for container test command."""
    oss_fuzz = _make_mock_oss_fuzz(tmp_path)
    infra = OSSFuzzInfrastructure(oss_fuzz)

    project = oss_fuzz / "projects" / "proj"
    project.mkdir(parents=True)
    (project / "test.sh").write_text("#!/bin/bash\necho ok\n")
    src_path = tmp_path / "patched-src"
    src_path.mkdir(parents=True)

    with (
        patch("crsbench.builder.infrastructure.run_with_timeout") as mock_run,
        patch("crsbench.builder.infrastructure.fix_docker_ownership"),
    ):
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="/src/wireshark\n", stderr=""),
            MagicMock(returncode=0, stdout="ok", stderr=""),
        ]
        infra.run_tests("proj", src_path, docker_tag="latest", rts_mode=True)

    cmd = mock_run.call_args_list[1][0][0]
    cmd_str = " ".join(cmd)
    assert "export RTS_MODE=1;" in cmd_str


def test_normalize_container_workdir_guards_unsafe_paths() -> None:
    """Unsafe or escaping workdirs should fallback safely to /src."""
    assert OSSFuzzInfrastructure._normalize_container_workdir("/") == "/src"
    assert OSSFuzzInfrastructure._normalize_container_workdir("../tmp") == "/src"
    assert OSSFuzzInfrastructure._normalize_container_workdir("/src/../tmp") == "/src"
    assert (
        OSSFuzzInfrastructure._normalize_container_workdir("/workspace/project")
        == "/src"
    )
    assert (
        OSSFuzzInfrastructure._normalize_container_workdir("/src/wireshark")
        == "/src/wireshark"
    )


def test_validate_variant_name_rejects_path_separators() -> None:
    with pytest.raises(ValueError):
        OSSFuzzInfrastructure._validate_variant_name("../bad")
    with pytest.raises(ValueError):
        OSSFuzzInfrastructure._validate_variant_name("bad/name")


def test_validate_variant_name_accepts_standard_name() -> None:
    assert OSSFuzzInfrastructure._validate_variant_name("afc-libxml2-delta-03") == (
        "afc-libxml2-delta-03"
    )


def test_run_tests_inc_mode_injects_replay_shim_env_and_mount(tmp_path: Path):
    oss_fuzz = _make_mock_oss_fuzz(tmp_path)
    infra = OSSFuzzInfrastructure(oss_fuzz)

    project = oss_fuzz / "projects" / "proj"
    project.mkdir(parents=True)
    (project / "test.sh").write_text("#!/bin/bash\necho ok\n")
    src_path = tmp_path / "patched-src"
    src_path.mkdir(parents=True)

    with (
        patch("crsbench.builder.infrastructure.run_with_timeout") as mock_run,
        patch("crsbench.builder.infrastructure.fix_docker_ownership"),
        patch.object(infra, "_ensure_replay_hooks_in_image", return_value=True),
    ):
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="/src/proj\n", stderr=""),
            MagicMock(returncode=0, stdout="ok", stderr=""),
        ]
        passed, _, _ = infra.run_tests("proj", src_path, docker_tag="inc")

    assert passed is True
    cmd = mock_run.call_args_list[1][0][0]
    cmd_str = " ".join(cmd)
    assert "CRSBENCH_REPLAY_POLICY_SOURCE=crsbench-shim" in cmd_str
    assert "CRSBENCH_REPLAY_POLICY_TOOLS=maven,gradle" in cmd_str
    assert "/CRSBENCH_REPLAY_BIN:ro" not in cmd_str


def test_run_tests_latest_mode_does_not_inject_replay_env(tmp_path: Path):
    oss_fuzz = _make_mock_oss_fuzz(tmp_path)
    infra = OSSFuzzInfrastructure(oss_fuzz)

    project = oss_fuzz / "projects" / "proj"
    project.mkdir(parents=True)
    (project / "test.sh").write_text("#!/bin/bash\necho ok\n")
    src_path = tmp_path / "patched-src"
    src_path.mkdir(parents=True)

    with (
        patch("crsbench.builder.infrastructure.run_with_timeout") as mock_run,
        patch("crsbench.builder.infrastructure.fix_docker_ownership"),
    ):
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="/src/proj\n", stderr=""),
            MagicMock(returncode=0, stdout="ok", stderr=""),
        ]
        passed, _, _ = infra.run_tests("proj", src_path, docker_tag="latest")

    assert passed is True
    cmd = mock_run.call_args_list[1][0][0]
    cmd_str = " ".join(cmd)
    assert "CRSBENCH_REPLAY_POLICY_SOURCE=crsbench-shim" not in cmd_str
    assert "CRSBENCH_REPLAY_BIN=/CRSBENCH_REPLAY_BIN" not in cmd_str
