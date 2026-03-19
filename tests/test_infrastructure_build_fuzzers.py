import json
import multiprocessing
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, call, patch

from crsbench.builder.infrastructure import OSSFuzzInfrastructure
from crsbench.builder.types import BenchmarkMode, BuildConfig, VariantType


def _make_config(tmp_path: Path) -> BuildConfig:
    return BuildConfig(
        benchmark_name="afc-test-delta-01",
        variant_type=VariantType.DELTA_REF,
        commit="a" * 40,
        main_repo="https://example.com/repo.git",
        benchmark_path=tmp_path / "bench",
        mode=BenchmarkMode.DELTA,
        sanitizer="address",
        timeout=30,
    )


def _make_infra(tmp_path: Path) -> OSSFuzzInfrastructure:
    oss_fuzz = tmp_path / "oss-fuzz"
    (oss_fuzz / "infra").mkdir(parents=True)
    (oss_fuzz / "projects").mkdir(parents=True)
    (oss_fuzz / "build" / "out").mkdir(parents=True)
    (oss_fuzz / "infra" / "helper.py").write_text("#!/usr/bin/env python3\n")
    return OSSFuzzInfrastructure(oss_fuzz)


def test_build_fuzzers_fixes_build_output_ownership_on_success(tmp_path: Path) -> None:
    infra = _make_infra(tmp_path)
    config = _make_config(tmp_path)
    src_path = tmp_path / "src"
    src_path.mkdir()

    with (
        patch("crsbench.builder.infrastructure.run_with_timeout") as mock_run,
        patch("crsbench.builder.infrastructure.fix_docker_ownership") as mock_fix,
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        result = infra.build_fuzzers(config, src_path=src_path)

    assert result.success is True
    assert mock_fix.call_args_list == [
        call(infra.get_build_output_path(config.variant_name))
    ]


def test_build_fuzzers_does_not_fix_source_ownership_on_failure(tmp_path: Path) -> None:
    infra = _make_infra(tmp_path)
    config = _make_config(tmp_path)
    src_path = tmp_path / "src"
    src_path.mkdir()

    with (
        patch("crsbench.builder.infrastructure.run_with_timeout") as mock_run,
        patch("crsbench.builder.infrastructure.fix_docker_ownership") as mock_fix,
    ):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="fail")
        result = infra.build_fuzzers(config, src_path=src_path)

    assert result.success is False
    assert mock_fix.call_args_list == []


def test_prepare_inc_image_for_variant_uses_plain_variant_inc_tag(
    tmp_path: Path,
) -> None:
    infra = _make_infra(tmp_path)
    base_project = "afc-libavif-delta-02"
    variant_name = "afc-libavif-delta-02-asan-delta-patched-cpv_0-patch_0-unittest"

    with (
        patch.object(infra, "_docker_image_exists") as mock_exists,
        patch.object(infra, "_retag_for_ossfuzz", return_value=True) as mock_retag,
    ):

        def _exists(name: str) -> bool:
            # Source image exists; destination images do not.
            return name == "gcr.io/oss-fuzz/afc-libavif-delta-02-asan:inc"

        mock_exists.side_effect = _exists

        ok = infra.prepare_inc_image_for_variant(
            base_project,
            variant_name,
            sanitizer="address",
        )

    assert ok is True
    assert mock_retag.call_args_list == [
        call(
            "gcr.io/oss-fuzz/afc-libavif-delta-02-asan:inc",
            "gcr.io/oss-fuzz/afc-libavif-delta-02-asan-delta-patched-cpv_0-patch_0-unittest:inc",
        ),
        call(
            "gcr.io/oss-fuzz/afc-libavif-delta-02-asan:inc",
            "gcr.io/oss-fuzz/afc-libavif-delta-02-asan-delta-patched-cpv_0-patch_0-unittest:latest",
        ),
    ]


def test_ensure_inc_image_failure_uses_retry_cooldown(tmp_path: Path) -> None:
    infra = _make_infra(tmp_path)
    infra.inc_image_retry_interval_sec = 300

    with (
        patch.object(infra, "is_inc_image_available", return_value=False) as mock_avail,
        patch.object(infra, "pull_inc_build_image", return_value=False) as mock_pull,
        patch.object(infra, "build_inc_build_image", return_value=False) as mock_build,
    ):
        first = infra.ensure_inc_image("afc-libavif-delta-02", "address")
        second = infra.ensure_inc_image("afc-libavif-delta-02", "address")

    assert first is False
    assert second is False
    # First call attempts pull/build. Second call returns from cooldown gate.
    assert mock_avail.call_count == 1
    assert mock_pull.call_count == 1
    assert mock_build.call_count == 1


def test_ensure_inc_image_retries_after_cooldown(tmp_path: Path) -> None:
    infra = _make_infra(tmp_path)
    infra.inc_image_retry_interval_sec = 300

    cache_key = infra._inc_image_cache_key(
        "afc-libavif-delta-02",
        "address",
        infra.inc_image_registry,
        infra.local_image_prefix,
    )
    with (
        patch.object(infra, "is_inc_image_available", return_value=False) as mock_avail,
        patch.object(infra, "pull_inc_build_image", return_value=False) as mock_pull,
        patch.object(infra, "build_inc_build_image", return_value=False) as mock_build,
    ):
        assert infra.ensure_inc_image("afc-libavif-delta-02", "address") is False
        # Simulate cooldown expiry without sleeping.
        infra._inc_image_last_failure[cache_key] = time.monotonic() - 301
        assert infra.ensure_inc_image("afc-libavif-delta-02", "address") is False

    assert mock_avail.call_count == 2
    assert mock_pull.call_count == 2
    assert mock_build.call_count == 2


def test_ensure_inc_image_force_rebuild_clears_cache_and_removes_local_images(
    tmp_path: Path,
) -> None:
    infra = _make_infra(tmp_path)
    cache_key = infra._inc_image_cache_key(
        "afc-libavif-delta-02",
        "address",
        infra.inc_image_registry,
        infra.local_image_prefix,
    )
    infra._inc_image_cache[cache_key] = True
    infra._inc_image_last_failure[cache_key] = 1.0
    infra._replay_hooked_images["gcr.io/oss-fuzz/afc-libavif-delta-02-asan:inc"] = (
        "sha256:old"
    )
    infra._replay_hooked_images["gcr.io/oss-fuzz/afc-libavif-delta-02:latest"] = (
        "sha256:old"
    )

    with (
        patch.object(infra, "_docker_image_exists", return_value=False),
        patch.object(infra, "is_inc_image_available", return_value=True),
    ):
        assert (
            infra.ensure_inc_image(
                "afc-libavif-delta-02", "address", force_rebuild=True
            )
            is True
        )

    assert cache_key not in infra._inc_image_last_failure
    assert infra._inc_image_cache.get(cache_key) is True
    assert (
        "gcr.io/oss-fuzz/afc-libavif-delta-02-asan:inc"
        not in infra._replay_hooked_images
    )
    assert (
        "gcr.io/oss-fuzz/afc-libavif-delta-02:latest" not in infra._replay_hooked_images
    )


def test_ensure_inc_image_acquires_and_releases_distributed_lock(
    tmp_path: Path,
) -> None:
    infra = _make_infra(tmp_path)
    infra.inc_image_dist_lock_mode = "true"
    infra.inc_image_dist_lock_renew_interval_sec = 3600

    mock_conn = MagicMock()
    mock_conn.set.return_value = True
    mock_conn.eval.return_value = 1

    with (
        patch.object(
            infra, "_get_inc_image_dist_lock_connection", return_value=mock_conn
        ),
        patch.object(infra, "is_inc_image_available", return_value=False),
        patch.object(infra, "pull_inc_build_image", return_value=False),
        patch.object(infra, "build_inc_build_image", return_value=True),
    ):
        assert infra.ensure_inc_image("afc-libavif-delta-02", "address") is True

    assert mock_conn.set.call_count == 1
    # release path should invoke eval compare-and-del
    assert mock_conn.eval.call_count >= 1


def test_ensure_inc_image_lock_wait_returns_false_when_still_unavailable(
    tmp_path: Path,
) -> None:
    infra = _make_infra(tmp_path)
    infra.inc_image_dist_lock_mode = "true"
    infra.inc_image_dist_lock_wait_sec = 0

    mock_conn = MagicMock()
    mock_conn.set.return_value = False

    with (
        patch.object(
            infra, "_get_inc_image_dist_lock_connection", return_value=mock_conn
        ),
        patch.object(infra, "is_inc_image_available", return_value=False),
        patch.object(infra, "pull_inc_build_image") as mock_pull,
        patch.object(infra, "build_inc_build_image") as mock_build,
    ):
        assert infra.ensure_inc_image("afc-libavif-delta-02", "address") is False

    assert mock_pull.call_count == 0
    assert mock_build.call_count == 0


def test_distributed_inc_lock_auto_enabled_when_rq_job_present(tmp_path: Path) -> None:
    infra = _make_infra(tmp_path)
    infra.inc_image_dist_lock_mode = "auto"
    with patch.dict(os.environ, {"CRSBENCH_REDIS_HOST": ""}, clear=False):
        with (
            patch("rq.get_current_job", return_value=MagicMock()),
            patch.object(
                infra, "_get_inc_image_dist_lock_connection", return_value=MagicMock()
            ),
            patch.object(infra, "is_inc_image_available", return_value=True),
        ):
            assert infra.ensure_inc_image("afc-libavif-delta-02", "address") is True


def test_distributed_inc_lock_auto_enabled_when_redis_available(
    tmp_path: Path,
) -> None:
    infra = _make_infra(tmp_path)
    infra.inc_image_dist_lock_mode = "auto"
    with (
        patch.object(
            infra,
            "_get_inc_image_dist_lock_connection",
            return_value=MagicMock(),
        ),
        patch.object(infra, "is_inc_image_available", return_value=True),
    ):
        assert infra.ensure_inc_image("afc-libavif-delta-02", "address") is True


def test_ensure_inc_image_aborts_after_lease_loss_during_build(tmp_path: Path) -> None:
    infra = _make_infra(tmp_path)
    infra.inc_image_dist_lock_mode = "true"
    infra.inc_image_dist_lock_renew_interval_sec = 1

    mock_conn = MagicMock()
    mock_conn.set.return_value = True
    # First eval call (renew) fails -> lease lost, second eval call (release) succeeds.
    mock_conn.eval.side_effect = [0, 1]

    def _slow_build(*_args, **_kwargs):
        time.sleep(1.2)
        return False

    with (
        patch.object(
            infra, "_get_inc_image_dist_lock_connection", return_value=mock_conn
        ),
        patch.object(infra, "is_inc_image_available", return_value=False),
        patch.object(infra, "pull_inc_build_image", return_value=False),
        patch.object(infra, "build_inc_build_image", side_effect=_slow_build),
    ):
        assert infra.ensure_inc_image("afc-libavif-delta-02", "address") is False


def test_build_inc_build_image_bakes_snapshot_when_benchmark_path_provided(
    tmp_path: Path,
) -> None:
    infra = _make_infra(tmp_path)
    benchmark = tmp_path / "bench"
    benchmark.mkdir()
    (benchmark / "Dockerfile").write_text("FROM scratch\nWORKDIR /src/proj\n")

    with patch.dict(
        os.environ,
        {
            "OSS_FUZZ_CPUSET_CPUS": "80-95",
            "OSS_FUZZ_CGROUP_PARENT": "/user.slice/user-1003.slice/crsbench/build-1",
            "OSS_FUZZ_DOCKER_NETWORK": "none",
        },
        clear=False,
    ):
        with (
            patch("crsbench.builder.infrastructure.run_with_timeout") as mock_run,
            patch.object(infra, "_resolve_test_workdir", return_value="/src/proj"),
            patch.object(infra, "_retag_for_ossfuzz", return_value=True) as mock_retag,
        ):
            # helper build_image, docker run, docker commit, docker rm
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="", stderr=""),
                MagicMock(returncode=0, stdout="", stderr=""),
                MagicMock(returncode=0, stdout="", stderr=""),
                MagicMock(returncode=0, stdout="", stderr=""),
            ]
            ok = infra.build_inc_build_image(
                "afc-libavif-delta-02",
                "address",
                benchmark_path=benchmark,
            )

    assert ok is True
    assert mock_retag.call_count == 2
    helper_cmd = mock_run.call_args_list[0].args[0]
    assert helper_cmd[:3] == ["python3", str(infra._helper_script), "build_image"]
    assert "--no-pull" in helper_cmd
    assert "--pull" not in helper_cmd
    # Validate docker run bake phase is executed.
    run_cmd = mock_run.call_args_list[1].args[0]
    assert run_cmd[:3] == ["docker", "run", "--name"]
    assert "/CRSBENCH_PROJ_PATH" in " ".join(run_cmd)
    bake_script = run_cmd[-1]
    # Snapshot baking is compile-only. test.sh/run_tests.sh execute in later
    # verification stages, not during image snapshot creation.
    assert "/bin/bash /src/test.sh" not in bake_script
    assert "/bin/bash /src/run_tests.sh" not in bake_script
    assert "--cpuset-cpus" in run_cmd
    assert "80-95" in run_cmd
    assert "--cgroup-parent" in run_cmd
    assert "/user.slice/user-1003.slice/crsbench/build-1" in run_cmd
    assert "--network" in run_cmd
    assert "none" in run_cmd
    # Snapshot docker commit uses extended timeout by default.
    assert mock_run.call_args_list[2].kwargs["timeout"] == 1800


def test_build_inc_build_image_without_benchmark_path_skips_snapshot_bake(
    tmp_path: Path,
) -> None:
    infra = _make_infra(tmp_path)

    with (
        patch("crsbench.builder.infrastructure.run_with_timeout") as mock_run,
        patch.object(infra, "_retag_for_ossfuzz", return_value=True),
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        ok = infra.build_inc_build_image("afc-libavif-delta-02", "address")

    assert ok is True
    # Only helper.py build_image should run when no benchmark path is provided.
    assert mock_run.call_count == 1
    helper_cmd = mock_run.call_args_list[0].args[0]
    assert helper_cmd[:3] == ["python3", str(infra._helper_script), "build_image"]
    assert "--no-pull" in helper_cmd
    assert "--pull" not in helper_cmd


def test_build_fuzzers_inc_build_enables_replay_policy_env(tmp_path: Path) -> None:
    infra = _make_infra(tmp_path)
    config = _make_config(tmp_path)
    src_path = tmp_path / "src"
    src_path.mkdir()

    with (
        patch("crsbench.builder.infrastructure.run_with_timeout") as mock_run,
        patch.object(infra, "_ensure_replay_hooks_in_image", return_value=True),
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        result = infra.build_fuzzers(config, src_path=src_path, use_inc_image=True)

    assert result.success is True
    cmd = mock_run.call_args[0][0]
    cmd_str = " ".join(cmd)
    assert "--no-build-image" in cmd_str
    assert "REPLAY_BUILD=1" in cmd_str
    assert "CRSBENCH_REPLAY_POLICY=enforce" in cmd_str
    assert "CRSBENCH_REPLAY_POLICY_SOURCE=crsbench-shim" in cmd_str
    assert "CRSBENCH_REPLAY_POLICY_TOOLS=maven,gradle" in cmd_str


def test_build_fuzzers_snapshot_fallback_disables_replay_policy_env(
    tmp_path: Path,
) -> None:
    infra = _make_infra(tmp_path)
    config = _make_config(tmp_path)
    src_path = tmp_path / "src"
    src_path.mkdir()

    with patch("crsbench.builder.infrastructure.run_with_timeout") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        result = infra.build_fuzzers(
            config,
            src_path=src_path,
            use_inc_image=True,
            snapshot_fallback=True,
        )

    assert result.success is True
    cmd = mock_run.call_args[0][0]
    cmd_str = " ".join(cmd)
    assert "--no-build-image" in cmd_str
    assert "REPLAY_BUILD=0" in cmd_str
    assert "CRSBENCH_REPLAY_POLICY=enforce" not in cmd_str


def test_build_fuzzers_non_inc_build_does_not_set_no_build_image(
    tmp_path: Path,
) -> None:
    infra = _make_infra(tmp_path)
    config = _make_config(tmp_path)
    src_path = tmp_path / "src"
    src_path.mkdir()

    with patch("crsbench.builder.infrastructure.run_with_timeout") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        result = infra.build_fuzzers(config, src_path=src_path, use_inc_image=False)

    assert result.success is True
    cmd = mock_run.call_args[0][0]
    cmd_str = " ".join(cmd)
    assert "--no-build-image" not in cmd_str
    assert "LANG=C.UTF-8" in cmd_str
    assert "LC_ALL=C.UTF-8" in cmd_str


def test_build_fuzzers_inc_build_without_src_path_fails_fast(tmp_path: Path) -> None:
    infra = _make_infra(tmp_path)
    config = _make_config(tmp_path)

    with patch("crsbench.builder.infrastructure.run_with_timeout") as mock_run:
        result = infra.build_fuzzers(config, src_path=None, use_inc_image=True)

    assert result.success is False
    assert "requires src_path" in result.stderr
    mock_run.assert_not_called()


def test_replay_hook_commit_preserves_original_cmd_and_entrypoint(
    tmp_path: Path,
) -> None:
    infra = _make_infra(tmp_path)
    policy = MagicMock(enabled=True)

    inspect_payload = [
        {
            "Config": {
                "Entrypoint": ["/entrypoint.sh"],
                "Cmd": ["/bin/bash", "-lc", "compile"],
            }
        }
    ]

    with patch("crsbench.builder.infrastructure.run_with_timeout") as mock_run:
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=json.dumps(inspect_payload), stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),  # run hook install
            MagicMock(returncode=0, stdout="", stderr=""),  # commit
            MagicMock(returncode=0, stdout="", stderr=""),  # rm
        ]
        ok = infra._ensure_replay_hooks_in_image(
            "gcr.io/oss-fuzz/afc-test-delta-01-asan-delta-cpv0", policy
        )

    assert ok is True
    commit_cmd = mock_run.call_args_list[2].args[0]
    commit_cmd_str = " ".join(commit_cmd)
    assert "--change" in commit_cmd_str
    assert 'ENTRYPOINT ["/entrypoint.sh"]' in commit_cmd_str
    assert 'CMD ["/bin/bash", "-lc", "compile"]' in commit_cmd_str


def test_replay_hook_cache_short_circuits_only_on_same_image_id(tmp_path: Path) -> None:
    infra = _make_infra(tmp_path)
    policy = MagicMock(enabled=True)
    image = "gcr.io/oss-fuzz/afc-test-delta-01-asan-delta-cpv0"
    infra._replay_hooked_images[image] = "sha256:same"

    with patch.object(infra, "_get_local_image_id", return_value="sha256:same"):
        assert infra._ensure_replay_hooks_in_image(image, policy) is True


def test_replay_hook_cache_miss_when_image_id_changes(tmp_path: Path) -> None:
    infra = _make_infra(tmp_path)
    policy = MagicMock(enabled=True)
    image = "gcr.io/oss-fuzz/afc-test-delta-01-asan-delta-cpv0"
    infra._replay_hooked_images[image] = "sha256:old"
    inspect_payload = [
        {"Config": {"Entrypoint": None, "Cmd": ["/bin/bash", "-lc", "compile"]}}
    ]

    with (
        patch.object(
            infra,
            "_get_local_image_id",
            side_effect=["sha256:new", "sha256:new"],
        ),
        patch("crsbench.builder.infrastructure.run_with_timeout") as mock_run,
    ):
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=json.dumps(inspect_payload), stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
        ]
        assert infra._ensure_replay_hooks_in_image(image, policy) is True

    assert infra._replay_hooked_images[image] == "sha256:new"


def test_retag_invalidates_replay_hook_cache_for_destination_tag(
    tmp_path: Path,
) -> None:
    infra = _make_infra(tmp_path)
    dst = "gcr.io/oss-fuzz/afc-test-delta-01-asan:inc"
    infra._replay_hooked_images[dst] = "sha256:old"

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        assert infra._retag_for_ossfuzz("src:image", dst) is True

    assert dst not in infra._replay_hooked_images


def test_run_tests_sets_utf8_locale_env(tmp_path: Path) -> None:
    infra = _make_infra(tmp_path)
    project_name = "afc-test-delta-01-asan-delta-patched-cpv_0-patch_0-unittest"
    project_dir = infra.projects_base / project_name
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "test.sh").write_text("#!/bin/bash\nexit 0\n")
    src_path = tmp_path / "src"
    src_path.mkdir()

    with (
        patch.object(infra, "_resolve_test_workdir", return_value="/src"),
        patch("crsbench.builder.infrastructure.run_with_timeout") as mock_run,
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        passed, _, _ = infra.run_tests(
            project_name=project_name,
            src_path=src_path,
            sanitizer="address",
            timeout=10,
            docker_tag="latest",
        )

    assert passed is True
    cmd = mock_run.call_args[0][0]
    cmd_str = " ".join(cmd)
    assert "LANG=C.UTF-8" in cmd_str
    assert "LC_ALL=C.UTF-8" in cmd_str


# ---------------------------------------------------------------------------
# Concurrent build lock tests
# ---------------------------------------------------------------------------


def _child_build(
    oss_fuzz_path: str,
    config_kwargs: dict,
    barrier_path: str,
    result_queue: multiprocessing.Queue,
) -> None:
    """Entry point for child process that calls build_fuzzers.

    Uses a barrier file to synchronize two processes so they enter the
    lock region at approximately the same time.  Mocks are set up
    inside the child (fork copies parent module state but patches must
    be active when the code runs).
    """
    from unittest.mock import MagicMock  # noqa: I001
    from unittest.mock import patch as mock_patch

    infra = OSSFuzzInfrastructure(Path(oss_fuzz_path))
    config = BuildConfig(
        benchmark_name=config_kwargs["benchmark_name"],
        variant_type=VariantType(config_kwargs["variant_type"]),
        commit=config_kwargs["commit"],
        main_repo=config_kwargs["main_repo"],
        benchmark_path=Path(config_kwargs["benchmark_path"]),
        mode=BenchmarkMode(config_kwargs["mode"]),
        sanitizer=config_kwargs["sanitizer"],
        timeout=config_kwargs["timeout"],
    )

    barrier = Path(barrier_path)
    barrier.mkdir(parents=True, exist_ok=True)
    (barrier / str(os.getpid())).touch()
    # Wait for peer (max 5s)
    deadline = time.monotonic() + 5
    while len(list(barrier.iterdir())) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)

    variant_name = config.variant_name

    def _fake_build(*_args, **_kwargs):
        """Simulate a build that takes time and writes metadata."""
        time.sleep(0.5)
        # Write build metadata so is_variant_built() returns True
        out_dir = Path(oss_fuzz_path) / "build" / "out" / variant_name
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "fuzzer_binary").write_bytes(b"\x00")
        (out_dir / ".build-meta.json").write_text(
            json.dumps(
                {
                    "sanitizer": config.sanitizer,
                    "inc_build": False,
                    "fallback_used": False,
                }
            )
        )
        # Project dir must also exist
        (Path(oss_fuzz_path) / "projects" / variant_name).mkdir(
            parents=True, exist_ok=True
        )
        return MagicMock(returncode=0, stdout="ok", stderr="")

    with (
        mock_patch(
            "crsbench.builder.infrastructure.run_with_timeout",
            side_effect=_fake_build,
        ) as mock_run,
        mock_patch("crsbench.builder.infrastructure.fix_docker_ownership"),
    ):
        build_result = infra.build_fuzzers(config, src_path=Path(oss_fuzz_path) / "src")
        result_queue.put(
            {
                "pid": os.getpid(),
                "success": build_result.success,
                "run_called": mock_run.called,
            }
        )


def test_concurrent_build_fuzzers_serialized_by_lock(tmp_path: Path) -> None:
    """Two processes building the same variant must not race.

    One process should build; the other should find the completed build
    via the double-check after acquiring the lock, and skip the build.
    """
    oss_fuzz = tmp_path / "oss-fuzz"
    (oss_fuzz / "infra").mkdir(parents=True)
    (oss_fuzz / "projects").mkdir(parents=True)
    (oss_fuzz / "build" / "out").mkdir(parents=True)
    (oss_fuzz / "infra" / "helper.py").write_text("#!/usr/bin/env python3\n")
    (oss_fuzz / "src").mkdir()

    config_kwargs = {
        "benchmark_name": "afc-test-race-01",
        "variant_type": VariantType.DELTA_REF.value,
        "commit": "a" * 40,
        "main_repo": "https://example.com/repo.git",
        "benchmark_path": str(tmp_path / "bench"),
        "mode": BenchmarkMode.DELTA.value,
        "sanitizer": "address",
        "timeout": 30,
    }
    (tmp_path / "bench").mkdir()

    barrier_path = str(tmp_path / "barrier")
    result_queue = multiprocessing.Queue()

    ctx = multiprocessing.get_context("fork")
    p1 = ctx.Process(
        target=_child_build,
        args=(str(oss_fuzz), config_kwargs, barrier_path, result_queue),
    )
    p2 = ctx.Process(
        target=_child_build,
        args=(str(oss_fuzz), config_kwargs, barrier_path, result_queue),
    )

    p1.start()
    p2.start()
    p1.join(timeout=20)
    p2.join(timeout=20)

    assert p1.exitcode == 0, f"Process 1 crashed with exit code {p1.exitcode}"
    assert p2.exitcode == 0, f"Process 2 crashed with exit code {p2.exitcode}"

    results = []
    while not result_queue.empty():
        results.append(result_queue.get_nowait())

    assert len(results) == 2, f"Expected 2 results, got {len(results)}"
    assert all(r["success"] for r in results), f"Not all builds succeeded: {results}"

    # One process should have called run_with_timeout (the builder),
    # the other should have skipped via the double-check (no run call).
    run_calls = [r["run_called"] for r in results]
    assert True in run_calls, "At least one process must run the build"
    assert False in run_calls, (
        "Second process should skip build via double-check after lock, "
        f"but both called run_with_timeout: {results}"
    )
