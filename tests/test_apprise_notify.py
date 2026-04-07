"""Tests for the Apprise notification helper."""

from unittest.mock import MagicMock, patch

import pytest
from crsbench.utils.apprise_notify import (
    AppriseNotificationConfig,
    format_completion_message,
    format_failure_message,
    load_apprise_notification_config,
    send_apprise_message,
)

pytestmark = pytest.mark.notification


def test_load_config_returns_none_when_urls_missing():
    assert load_apprise_notification_config({}) is None


def test_load_config_defaults_to_os_environ_when_env_is_none(monkeypatch):
    monkeypatch.setenv("CRSBENCH_NOTIFY_APPRISE_URLS", "discord://a/b")
    monkeypatch.setenv("CRSBENCH_NOTIFY_APPRISE_TITLE", "Bench Alerts")
    monkeypatch.setenv("CRSBENCH_NOTIFY_APPRISE_TAG", "ops")

    config = load_apprise_notification_config()

    assert config == AppriseNotificationConfig(
        urls=("discord://a/b",),
        title="Bench Alerts",
        tag="ops",
    )


def test_load_config_splits_newline_and_comma_separated_urls():
    config = load_apprise_notification_config(
        {
            "CRSBENCH_NOTIFY_APPRISE_URLS": "discord://a/b\nslack://x/y,ntfy://z",
            "CRSBENCH_NOTIFY_APPRISE_TITLE": "Bench Alerts",
            "CRSBENCH_NOTIFY_APPRISE_TAG": "ops",
        }
    )

    assert config == AppriseNotificationConfig(
        urls=("discord://a/b", "slack://x/y", "ntfy://z"),
        title="Bench Alerts",
        tag="ops",
    )


def test_load_config_ignores_blank_urls_and_uses_default_title():
    config = load_apprise_notification_config(
        {"CRSBENCH_NOTIFY_APPRISE_URLS": "  \n discord://a/b , , slack://x/y \n"}
    )

    assert config == AppriseNotificationConfig(
        urls=("discord://a/b", "slack://x/y"),
        title="CRSBench",
        tag=None,
    )


def test_format_completion_message_uses_default_summary():
    assert format_completion_message("CRSBench") == "CRSBench run completed"


def test_format_failure_message_includes_failure_reason():
    assert (
        format_failure_message("CRSBench", "worker crashed")
        == "CRSBench run failed: worker crashed"
    )


def test_send_apprise_message_logs_and_returns_false_on_notify_error():
    config = AppriseNotificationConfig(urls=("discord://a/b",))
    mock_apprise = MagicMock()
    mock_apprise.notify.side_effect = RuntimeError("boom")

    with (
        patch(
            "crsbench.utils.apprise_notify.apprise.Apprise",
            return_value=mock_apprise,
        ),
        patch("crsbench.utils.apprise_notify.logger.warning") as mock_warning,
    ):
        assert send_apprise_message(config, body="hello") is False

    mock_apprise.add.assert_called_once_with("discord://a/b")
    mock_apprise.notify.assert_called_once_with(body="hello", title="CRSBench")
    assert mock_warning.call_count == 1
    assert "Apprise notification failed" in str(mock_warning.call_args.args[0])


def test_send_apprise_message_logs_and_returns_false_on_falsey_notify_result():
    config = AppriseNotificationConfig(urls=("discord://a/b",))
    mock_apprise = MagicMock()
    mock_apprise.notify.return_value = False

    with (
        patch(
            "crsbench.utils.apprise_notify.apprise.Apprise",
            return_value=mock_apprise,
        ),
        patch("crsbench.utils.apprise_notify.logger.warning") as mock_warning,
    ):
        assert send_apprise_message(config, body="hello") is False

    mock_apprise.add.assert_called_once_with("discord://a/b")
    mock_apprise.notify.assert_called_once_with(body="hello", title="CRSBench")
    assert mock_warning.call_count == 1
    assert "notify() returned falsey" in str(mock_warning.call_args.args[0])


def test_send_apprise_message_includes_optional_title_suffix_and_tag():
    config = AppriseNotificationConfig(
        urls=("discord://a/b", "slack://x/y"),
        title="Bench Alerts",
        tag="ops",
    )
    mock_apprise = MagicMock()
    mock_apprise.notify.return_value = True

    with patch(
        "crsbench.utils.apprise_notify.apprise.Apprise",
        return_value=mock_apprise,
    ):
        assert (
            send_apprise_message(
                config,
                body="run finished",
                title_suffix="completed",
            )
            is True
        )

    assert mock_apprise.add.call_args_list == [
        (("discord://a/b",), {"tag": "ops"}),
        (("slack://x/y",), {"tag": "ops"}),
    ]
    mock_apprise.notify.assert_called_once_with(
        body="run finished",
        title="Bench Alerts: completed",
        tag="ops",
    )
