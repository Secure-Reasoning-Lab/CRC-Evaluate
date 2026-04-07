"""Tests for the notification test script."""

from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent / "scripts" / "test_notification.py"
)


def _load_script_module():
    spec = importlib.util.spec_from_file_location(
        "test_notification_script", SCRIPT_PATH
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_main_returns_error_when_notification_config_missing(monkeypatch, capsys):
    module = _load_script_module()
    monkeypatch.delenv("CRSBENCH_NOTIFY_APPRISE_URLS", raising=False)
    monkeypatch.delenv("CRSBENCH_NOTIFY_APPRISE_TITLE", raising=False)
    monkeypatch.delenv("CRSBENCH_NOTIFY_APPRISE_TAG", raising=False)

    exit_code = module.main(["--no-dotenv"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "CRSBENCH_NOTIFY_APPRISE_URLS" in captured.out


def test_main_loads_dotenv_with_override_enabled(monkeypatch):
    module = _load_script_module()
    dotenv_calls: list[tuple[Path, bool]] = []

    monkeypatch.setattr(
        module,
        "load_dotenv",
        lambda path, override=False: dotenv_calls.append((path, override)),
    )
    monkeypatch.setattr(module, "load_apprise_notification_config", lambda: None)

    exit_code = module.main([])

    assert exit_code == 1
    assert dotenv_calls == [(module.PROJECT_ROOT / ".env", True)]


def test_main_dry_run_prints_resolved_config_without_sending(monkeypatch, capsys):
    module = _load_script_module()
    send_calls: list[tuple[object, str, str | None]] = []

    monkeypatch.setattr(
        module,
        "load_apprise_notification_config",
        lambda: module.AppriseNotificationConfig(
            urls=("discord://token/channel",),
            title="Bench Alerts",
            tag="ops",
        ),
    )
    monkeypatch.setattr(module, "_load_project_env", lambda: None)
    monkeypatch.setattr(
        module,
        "send_apprise_message",
        lambda config, *, body, title_suffix=None: send_calls.append(
            (config, body, title_suffix)
        ),
    )

    exit_code = module.main(["--dry-run", "--no-dotenv"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert send_calls == []
    assert "Dry run only" in captured.out
    assert "Bench Alerts" in captured.out
    assert "targets=1" in captured.out


def test_main_defaults_to_preview_without_sending(monkeypatch, capsys):
    module = _load_script_module()
    send_calls: list[tuple[object, str, str | None]] = []

    monkeypatch.setattr(
        module,
        "load_apprise_notification_config",
        lambda: module.AppriseNotificationConfig(
            urls=("discord://token/channel",),
            title="Bench Alerts",
            tag="ops",
        ),
    )
    monkeypatch.setattr(module, "_load_project_env", lambda: None)
    monkeypatch.setattr(
        module,
        "send_apprise_message",
        lambda config, *, body, title_suffix=None: send_calls.append(
            (config, body, title_suffix)
        ),
    )

    exit_code = module.main(["--no-dotenv"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert send_calls == []
    assert "Dry run only" in captured.out


def test_main_rejects_conflicting_send_and_dry_run_flags(monkeypatch, capsys):
    module = _load_script_module()
    monkeypatch.setattr(module, "_load_project_env", lambda: None)

    exit_code = module.main(["--no-dotenv", "--dry-run", "--send"])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "Choose either --dry-run or --send" in captured.out


def test_main_returns_error_when_notification_delivery_fails(monkeypatch, capsys):
    module = _load_script_module()

    monkeypatch.setattr(
        module,
        "load_apprise_notification_config",
        lambda: module.AppriseNotificationConfig(
            urls=("discord://token/channel",),
            title="Bench Alerts",
            tag=None,
        ),
    )
    monkeypatch.setattr(module, "_load_project_env", lambda: None)
    monkeypatch.setattr(module, "send_apprise_message", lambda *_args, **_kwargs: False)

    exit_code = module.main(["--no-dotenv", "--send"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Notification delivery failed" in captured.out


def test_main_sends_notification_and_returns_success(monkeypatch, capsys):
    module = _load_script_module()
    send_calls: list[tuple[object, str, str | None]] = []

    monkeypatch.setattr(
        module,
        "load_apprise_notification_config",
        lambda: module.AppriseNotificationConfig(
            urls=("discord://token/channel", "slack://token/channel"),
            title="Bench Alerts",
            tag=None,
        ),
    )
    monkeypatch.setattr(module, "_load_project_env", lambda: None)

    def _send(config, *, body, title_suffix=None):
        send_calls.append((config, body, title_suffix))
        return True

    monkeypatch.setattr(module, "send_apprise_message", _send)

    exit_code = module.main(["--no-dotenv", "--send", "--title-suffix", "smoke"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert len(send_calls) == 1
    sent_config, sent_body, sent_suffix = send_calls[0]
    assert sent_config.urls == ("discord://token/channel", "slack://token/channel")
    assert sent_body == "CRSBench notification test"
    assert sent_suffix == "smoke"
    assert "Sending test notification" in captured.out
    assert "Notification delivered successfully" in captured.out
