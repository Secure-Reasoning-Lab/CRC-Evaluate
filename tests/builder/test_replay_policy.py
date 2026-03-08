import pytest
from crsbench.builder.replay_policy import (
    REPLAY_MODE_ENFORCE,
    replay_install_script,
    resolve_replay_policy,
)


def test_resolve_replay_policy_only_for_inc_build() -> None:
    disabled = resolve_replay_policy(inc_build=False)
    assert disabled.enabled is False
    assert disabled.mode != REPLAY_MODE_ENFORCE

    enabled = resolve_replay_policy(inc_build=True)
    assert enabled.enabled is True
    assert enabled.mode == REPLAY_MODE_ENFORCE
    assert enabled.tool_hooks == ("maven", "gradle")


def test_replay_install_script_installs_real_wrappers() -> None:
    policy = resolve_replay_policy(inc_build=True)
    script = replay_install_script(policy)

    assert "for tool in mvn mvnw gradle gradlew; do" in script
    assert 'install_hook "$tool" "$bin"' in script
    assert ".crsbench.real" in script


def test_resolve_replay_policy_ignores_off_for_inc_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CRSBENCH_REPLAY_POLICY", "off")
    enabled = resolve_replay_policy(inc_build=True)
    assert enabled.enabled is True
    assert enabled.mode == REPLAY_MODE_ENFORCE
