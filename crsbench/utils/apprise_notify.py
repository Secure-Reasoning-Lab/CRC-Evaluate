"""Best-effort Apprise notifications for CRSBench."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

import apprise

from crsbench.utils.logger import get_logger

logger = get_logger(__name__)

_URL_SPLIT_RE = re.compile(r"[\n,]")

if TYPE_CHECKING:
    from collections.abc import Mapping


@dataclass(frozen=True)
class AppriseNotificationConfig:
    """Resolved Apprise notification inputs."""

    urls: tuple[str, ...]
    title: str = "CRSBench"
    tag: str | None = None


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None


def _split_urls(raw: str) -> tuple[str, ...]:
    return tuple(
        part
        for part in (_clean(piece) for piece in _URL_SPLIT_RE.split(raw))
        if part is not None
    )


def load_apprise_notification_config(
    env: Mapping[str, str] | None = None,
) -> AppriseNotificationConfig | None:
    """Load notification settings from environment-like values."""
    source = os.environ if env is None else env
    urls_raw = _clean(source.get("CRSBENCH_NOTIFY_APPRISE_URLS"))
    if urls_raw is None:
        return None

    urls = _split_urls(urls_raw)
    if not urls:
        return None

    title = _clean(source.get("CRSBENCH_NOTIFY_APPRISE_TITLE")) or "CRSBench"
    tag = _clean(source.get("CRSBENCH_NOTIFY_APPRISE_TAG"))
    return AppriseNotificationConfig(urls=urls, title=title, tag=tag)


def format_completion_message(title: str) -> str:
    """Format a concise completion message."""
    return f"{title} run completed"


def format_failure_message(title: str, reason: str) -> str:
    """Format a concise failure message."""
    return f"{title} run failed: {reason}"


def _build_title(config: AppriseNotificationConfig, title_suffix: str | None) -> str:
    if title_suffix:
        return f"{config.title}: {title_suffix}"
    return config.title


def send_apprise_message(
    config: AppriseNotificationConfig,
    *,
    body: str,
    title_suffix: str | None = None,
) -> bool:
    """Send a best-effort Apprise notification."""
    notifier = apprise.Apprise()
    for url in config.urls:
        if config.tag is None:
            notifier.add(url)
        else:
            notifier.add(url, tag=config.tag)

    payload = {
        "body": body,
        "title": _build_title(config, title_suffix),
    }
    if config.tag is not None:
        payload["tag"] = config.tag

    try:
        delivered = bool(notifier.notify(**payload))
        if not delivered:
            logger.warning(
                f"Apprise notification failed: notify() returned falsey for {payload['title']}"
            )
        return delivered
    except Exception as exc:  # best-effort notification helper
        logger.warning(f"Apprise notification failed: {exc}")
        return False
